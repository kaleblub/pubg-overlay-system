import json
import re
import time
import datetime
import os
import logging
import threading
from pathlib import Path
from config import *
from log_simulator import SimulationManager
import copy
import signal

try:
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
    COLORAMA_AVAILABLE = True
except ImportError:
    COLORAMA_AVAILABLE = False
    class Fore:
        RED = YELLOW = GREEN = CYAN = MAGENTA = BLUE = WHITE = RESET = ""
    class Back:
        BLACK = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = RESET = ""
    class Style:
        BRIGHT = DIM = NORMAL = RESET_ALL = ""

try:
    import webserver
    WEBSERVER_AVAILABLE = True
except ImportError:
    WEBSERVER_AVAILABLE = False
    logging.warning("webserver module not found. Web interface will not be available.")

# Setup logging
logging.basicConfig(level=getattr(logging, LOG_LEVEL), format=LOG_FORMAT)

# Global simulation manager
simulation_manager = None

# Global buffer for chunk processing
buffer = ''
in_archive_processing = False
in_catchup_processing = False

# Global shutdown control variables
shutdown_event = threading.Event()
finalization_requested = False
complete_shutdown_requested = False
finalization_lock = threading.Lock()
signal_received = False

# Global set to track processed files
processed_files = set()

expected_teams = {}

# ---------- State (normalized) ----------
state = {
    "phase": {
        "teams": {},
        "players": {}
    },
    "all_time": {
        "players": {}
    },
    "current_match": {
        "id": None,
        "status": "idle",
        "winnerTeamId": None,
        "winnerTeamName": None,
        "eliminationOrder": [],
        "killFeed": [],
        "teams": {},
        "players": {}
    },
    "matches": [],  # List of completed matches
    "teamNameMapping": {},
    "processed_matches": set(),  # Track processed GameIDs
    "match_history": [],  # Deprecated, use "matches"
    "match_state": {
        "status": "idle",
        "last_updated": int(time.time())
    }
}

def print_colored(text, color=Fore.WHITE, style=Style.NORMAL, end="\n"):
    """Print colored text if colorama is available."""
    if COLORAMA_AVAILABLE:
        print(f"{style}{color}{text}{Style.RESET_ALL}", end=end)
    else:
        print(text, end=end)

def print_status_header(mode="Production"):
    """Print a status header with current mode."""
    status_color = Fore.GREEN if mode == "Production" else Fore.YELLOW
    print_colored(f"\n{'='*60}", Fore.BLUE)
    print_colored(f"PUBG LIVE MONITOR - {mode.upper()} MODE", status_color, Style.BRIGHT)
    print_colored(f"{'='*60}", Fore.BLUE)

def print_progress_bar(current, total, bar_length=50, prefix="Progress", suffix="Complete", processing=False):
    """Improved progress bar with safe clamping (never >100%)."""
    if total <= 0:
        return

    # Clamp so it never exceeds 100%
    fraction = min(current / total, 1.0)
    filled_length = int(bar_length * fraction)

    if processing:
        spin_chars = ['|', '/', '-', '\\']
        spin_index = int(time.time() * 8) % len(spin_chars)
        bar = "█" * filled_length + ">" + "░" * (bar_length - filled_length - 1)
        if filled_length < bar_length:
            bar = bar[:filled_length] + spin_chars[spin_index] + bar[filled_length + 1:]
    else:
        bar = "█" * filled_length + ">" + "░" * (bar_length - filled_length - 1)
        if filled_length >= bar_length:
            bar = "█" * bar_length + ">"

    percent = int(fraction * 100)

    if percent == 100:
        color = Fore.GREEN
    elif percent > 50:
        color = Fore.CYAN
    elif processing:
        color = Fore.YELLOW
    else:
        color = Fore.WHITE

    if processing and percent < 100:
        print_colored(f"\r{prefix}: |{bar}| {percent}% {suffix}...", color, end="")
    else:
        print_colored(f"\r{prefix}: |{bar}| {percent}% {suffix}", color, end="")
        if current >= total:
            print()

# ---------- Parsing Functions ----------
INI_BLOCK = re.compile(r'\[/Script/ShadowTrackerExtra.FCustomTeamLogoAndColor](.*?)\n\n', re.DOTALL)
OBJ_BLOCKS = re.compile(r'(TotalPlayerList:|TeamInfoList:)')
OBJ_KV = re.compile(r'(\w+):\s*(?:"([^"]*)"|\'([^\']*)\'|([^{},\n]+))')

def _calculate_top_players(players_dict, teams_dict):
    """Calculates and returns the top players for the match, sorted by kills."""
    top_players = []
    all_players = list(players_dict.values())
    all_players.sort(key=lambda p: (
        p.get("stats", {}).get("kills", 0),
        p.get("stats", {}).get("damage", 0),
        p.get("stats", {}).get("knockouts", 0)
    ), reverse=True)
    for player in all_players[:5]:
        player_stats = player.get("stats", {})
        team_id = player.get("teamId")
        team_name = teams_dict.get(team_id, {}).get("name", "Unknown Team")
        top_players.append({
            "playerId": player["id"],
            "name": player["name"],
            "teamName": team_name,
            "totalKills": player_stats.get("kills", 0),
            "totalDamage": int(player_stats.get("damage", 0)),
            "totalKnockouts": player_stats.get("knockouts", 0),
            "totalMatches": 1
        })
    return top_players

def _finalize_and_persist():
    global state
    if state["current_match"]["id"] and state["match_state"]["status"] == "live":
        logging.info(f"Finalizing and persisting match ID: {state['current_match']['id']}")
        final_match_data = copy.deepcopy(state["current_match"])
        end_match_and_update_phase(final_match_data)
        # The line above handles the full reset via _reset_current_match().
        # Removed the redundant and incomplete manual reset block here.
        logging.info("Live match state fully reset to idle.")
    else:
        logging.warning("Finalization requested but no active match ID or in processing mode. Skipping.")
        if not state["current_match"]["id"]:
            # Use the comprehensive reset function when no match is active.
            _reset_current_match() 
            
    # Write state to JSON with relative paths
    json_file_path = os.path.join(PROJECT_ROOT, 'live_scoreboard.json')
    state_copy = copy.deepcopy(state)
    # Strip any localhost prefixes
    for team in state_copy["current_match"]["teams"].values():
        if team.get("logo") and team["logo"].startswith("http://"):
            team["logo"] = team["logo"].replace("http://localhost:5000", "")
    for team in state_copy["phase"]["teams"].values():
        if team.get("logo") and team["logo"].startswith("http://"):
            team["logo"] = team["logo"].replace("http://localhost:5000", "")
    for match in state_copy.get("matches", []):
        for team in match.get("teams", {}).values():
            if team.get("logo") and team["logo"].startswith("http://"):
                team["logo"] = team["logo"].replace("http://localhost:5000", "")
    try:
        with open(json_file_path, 'w', encoding='utf-8') as f:
            json.dump(state_copy, f, indent=2)
        logging.info(f"Persisted state to {json_file_path}")
    except Exception as e:
        logging.error(f"Failed to write JSON: {e}")

def _parse_kv_object(text):
    obj = {}
    pairs = OBJ_KV.findall(text)
    for key, v1, v2, v3 in pairs:
        raw = (v1 or v2 or v3 or "").strip()
        if raw.lower() in ("null", "none", ""):
            val = None
        elif raw.lower() == "true":
            val = True
        elif raw.lower() == "false":
            val = False
        elif raw.replace('.', '', 1).replace('-', '', 1).isdigit():
            val = float(raw) if '.' in raw else int(raw)
            if key == "teamId":
                val = str(val)
        else:
            val = raw
        obj[key] = val
    return obj

def _parse_ini(config_string):
    import os
    import shutil
    from pathlib import Path

    teams = {}

    # Define source and target directories
    source_dir = Path(r"C:/LOGO")
    project_root = Path(__file__).resolve().parent
    target_dir = project_root / "assets" / "LOGO"
    os.makedirs(target_dir, exist_ok=True)

    for line in config_string.strip().splitlines():
        m = re.search(r'TeamLogoAndColor=\(TeamNo=(\d+),TeamName=([^,]+),TeamLogoPath=([^,]+)', line)
        if not m:
            continue

        team_no = str(int(m.group(1)))
        team_name = m.group(2).strip('"')
        logo_path = m.group(3).strip('"')

        # Extract filename
        filename = os.path.basename(logo_path)
        if filename:
            source_file = source_dir / filename
            target_file = target_dir / filename

            # Copy only if file exists and is new/updated
            try:
                if source_file.exists():
                    if not target_file.exists() or source_file.stat().st_mtime > target_file.stat().st_mtime:
                        shutil.copy2(source_file, target_file)
                        logging.info(f"Copied logo: {filename} → assets/LOGO/")
                else:
                    logging.warning(f"Logo not found in source: {source_file}")
            except Exception as e:
                logging.error(f"Error copying logo {filename}: {e}")

            # Create Flask-served relative URL
            relative_url = f"/assets/LOGO/{filename}"
        else:
            relative_url = ""

        teams[team_no] = {
            "name": team_name,
            "logoPath": relative_url
        }

    return teams

def _calculate_missing_teams():
    """Calculate missing teams for current match based on expected_teams."""
    if not expected_teams or not state["current_match"]["id"]:
        return []
    
    # Get current match team names (case-insensitive)
    match_team_names = {t.get("name", "").lower() for t in state["current_match"]["teams"].values()}
    
    missing = []
    for team_name, info in expected_teams.items():
        if team_name.lower() not in match_team_names:
            missing.append({
                "id": info["id"],
                "name": team_name,
                "logo": get_asset_url(info["logoPath"], DEFAULT_TEAM_LOGO),
                "kills": 0,
                "placementPoints": 0,
                "points": 0,
                "rank": "MISS",
                "liveMembers": 0,
                "players": [],
                "missing": True
            })
    
    return missing

def get_team_logos(ini_file_path):
    try:
        with open(ini_file_path, "r", encoding="utf-8") as fh:
            ini_content = fh.read()
            ini_match = INI_BLOCK.search(ini_content)
            if ini_match:
                all_teams = _parse_ini(ini_match.group(1))
                filtered_teams = {}
                for tid, info in all_teams.items():
                    team_name = info["name"]
                    if "empty_" not in info["logoPath"].lower() and not team_name.startswith("Team "):
                        # Convert local path to Flask-relative URL
                        filename = os.path.basename(info["logoPath"])
                        logo_url = f"/assets/LOGO/{filename}" if filename else "/assets/default-team-logo.jpg"
                        filtered_teams[team_name] = {
                            "id": tid,
                            "name": team_name,
                            "logoPath": logo_url
                        }
                logging.info(f"Loaded {len(filtered_teams)} teams from INI: {[(name, info['logoPath']) for name, info in filtered_teams.items()]}")
                return filtered_teams
            else:
                logging.warning("Team logos INI block not found")
    except FileNotFoundError:
        logging.error(f"INI file not found at {ini_file_path}")
    except Exception as e:
        logging.error(f"Error parsing INI file: {e}")
    return {}

def get_asset_url(full_path_from_log, default_url):
    if not full_path_from_log or not str(full_path_from_log).strip():
        return default_url
    try:
        p = Path(str(full_path_from_log).strip())
        if p.exists() and p.is_file():
            return f"{ADJACENT_LOGO_FOLDER_PATH}{p.name}"
    except Exception:
        pass
    try:
        name = Path(full_path_from_log).name
        if LOGO_FOLDER_PATH.exists():
            for file in os.listdir(LOGO_FOLDER_PATH):
                if file.lower() == name.lower():
                    return f"{ADJACENT_LOGO_FOLDER_PATH}{file}"
    except Exception:
        pass
    return default_url

def ensure_directories():
    """Ensure all required directories exist."""
    directories = [
        CURRENT_LOG_DIR,
        ARCHIVE_LOG_DIR,
        TEST_LOGS_DIR,
        LOGO_FOLDER_PATH
    ]
    for directory in directories:
        try:
            if hasattr(directory, 'mkdir'):
                directory.mkdir(parents=True, exist_ok=True)
            else:
                Path(directory).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.warning(f"Could not create directory {directory}: {e}")
    try:
        Path(OUTPUT_JSON).parent.mkdir(parents=True, exist_ok=True)
        Path(ALL_TIME_PLAYERS_JSON).parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.warning(f"Could not create output directories: {e}")

# ---------- Team Name Mapping Helpers ----------
def _get_team_name_by_id(team_id):
    """Get team name for current match by team ID"""
    current_match_id = state["current_match"]["id"]
    if current_match_id and current_match_id in state["teamNameMapping"]:
        return state["teamNameMapping"][current_match_id].get(team_id)
    return None

def _get_team_id_by_name(team_name):
    """Get team ID for current match by team name"""
    current_match_id = state["current_match"]["id"]
    if current_match_id and current_match_id in state["teamNameMapping"]:
        mapping = state["teamNameMapping"][current_match_id]
        for tid, tname in mapping.items():
            if tname == team_name:
                return tid
    return None

def _register_team_mapping(team_id, team_name):
    """Register team ID -> name mapping for current match"""
    current_match_id = state["current_match"]["id"]
    if current_match_id:
        if current_match_id not in state["teamNameMapping"]:
            state["teamNameMapping"][current_match_id] = {}
        state["teamNameMapping"][current_match_id][team_id] = team_name

def _cleanup_old_team_mappings():
    """Clean up team mappings for old matches"""
    current_match_id = state["current_match"]["id"]
    if current_match_id:
        state["teamNameMapping"] = {current_match_id: state["teamNameMapping"].get(current_match_id, {})}

# ---------- State Management Functions ----------
def _add_or_update_player(player_data, is_alive, health=0, health_max=100):
    """Add or update a player's entry in the phase.players state."""
    player_id = player_data["id"]
    team_id = player_data.get("teamId")
    team_name = _get_team_name_by_id(team_id) if team_id else None
    if not team_name:
        team_name = player_data.get("teamName", "Unknown Team")
    if player_id not in state["phase"]["players"]:
        state["phase"]["players"][player_id] = {
            "id": player_id,
            "name": player_data.get("name", "Unknown Player"),
            "photo": player_data.get("photo", DEFAULT_PLAYER_PHOTO),
            "teamName": team_name,
            "live": {
                "isAlive": is_alive,
                "health": health,
                "healthMax": health_max
            },
            "totals": {
                "kills": 0,
                "damage": 0,
                "knockouts": 0,
                "matches": 0
            }
        }
    else:
        state["phase"]["players"][player_id]["live"]["isAlive"] = is_alive
        state["phase"]["players"][player_id]["live"]["health"] = health
        state["phase"]["players"][player_id]["live"]["healthMax"] = health_max
        state["phase"]["players"][player_id]["teamName"] = team_name

def _add_or_update_team(team_data):
    """Add or update a team's entry in the phase.teams state."""
    team_name = team_data.get("teamName", "Unknown Team")
    team_id = team_data.get("teamId", "Unknown ID")
    if team_name not in state["phase"]["teams"]:
        state["phase"]["teams"][team_name] = {
            "id": team_id,
            "name": team_name,
            "logo": get_asset_url(team_data.get("logo", ""), DEFAULT_TEAM_LOGO),
            "totals": {
                "kills": 0,
                "placementPoints": 0,
                "points": 0,
                "wwcd": 0
            },
        }

def _update_phase_from_live_match():
    """Update phase state based on live match data."""
    match_id = state["current_match"]["id"]
    if not match_id:
        return
    for team_id, team_data in state["current_match"]["teams"].items():
        team_name = team_data.get("name", "Unknown Team")
        if team_name not in state["phase"]["teams"]:
            state["phase"]["teams"][team_name] = {
                "id": team_id,
                "name": team_name,
                "logo": team_data.get("logo", ""),
                "totals": {
                    "kills": 0,
                    "placementPoints": 0,
                    "points": 0,
                    "wwcd": 0
                }
            }
        for player_id in team_data.get("players", []):
            player_data = state["current_match"]["players"].get(player_id)
            if player_data:
                _add_or_update_player({
                    "id": player_id,
                    "name": player_data.get("name", "Unknown Player"),
                    "photo": player_data.get("photo", DEFAULT_PLAYER_PHOTO),
                    "teamName": team_name,
                    "teamId": team_id
                }, is_alive=player_data["live"]["isAlive"])

def extract_snapshots(log_text):
    """Extract snapshots from log text without duplicate position tracking."""
    snapshots = []
    pos = 0
    while True:
        start_match = re.search(r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] POST /totalmessage', log_text[pos:])
        if not start_match:
            break
        start = pos + start_match.start()
        next_start_match = re.search(r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] POST /totalmessage', log_text[start + len(start_match.group(0)):])
        if next_start_match:
            end = start + len(start_match.group(0)) + next_start_match.start()
        else:
            end = len(log_text)
        snap_text = log_text[start:end]
        snapshots.append(snap_text)
        pos = end
    return snapshots

def parse_and_apply(log_text, parsed_logos=None, mode="chunk", progress_callback=None):
    """Parse log text and apply to state."""
    global buffer
    snapshots_processed = 0
    if mode == "full":
        snapshots = extract_snapshots(log_text)
        total_snapshots = len(snapshots)
        for idx, snap in enumerate(snapshots):
            process_snapshot(snap, parsed_logos)
            snapshots_processed += 1
            if progress_callback and total_snapshots > 0:
                processed_bytes = (idx + 1) / total_snapshots * len(log_text)
                progress_callback(processed_bytes, len(log_text))
    else:
        buffer_start_len = len(buffer)
        if log_text:
            buffer += log_text
        snapshots = extract_snapshots(buffer)
        new_snapshots = len(snapshots)
        for snap in snapshots:
            process_snapshot(snap, parsed_logos)
            snapshots_processed += 1
        if snapshots:
            last_end = 0
            for snap in snapshots:
                snap_start = buffer.find(snap)
                if snap_start >= 0:
                    last_end = max(last_end, snap_start + len(snap))
            if last_end > 0:
                buffer = buffer[last_end:]
                logging.debug(f"Processed {new_snapshots} snapshots, buffer truncated to {len(buffer)} bytes")
            else:
                buffer = ''
                logging.debug("No valid snapshots found; buffer cleared")
        else:
            buffer = ''
            logging.debug("No snapshots; buffer cleared")
        if progress_callback and log_text:
            progress_callback(len(log_text), len(log_text))
    logging.debug(f"parse_and_apply: processed {snapshots_processed} snapshots (buffer was {buffer_start_len}, now {len(buffer)})")

def process_snapshot(snap_text, parsed_logos):
    finalization_mode = "CATCHUP" if in_catchup_processing else "ARCHIVE" if in_archive_processing else "LIVE"
    gid_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", snap_text)
    new_game_id = gid_match.group(1) if gid_match else None
    if new_game_id and new_game_id in state["processed_matches"]:
        logging.info(f"{finalization_mode}: Skipping snapshot for already processed match {new_game_id}")
        return
    if new_game_id and state["current_match"]["id"] and new_game_id != state["current_match"]["id"]:
        if state["current_match"]["status"] in ["live", "finished"]:
            logging.info(f"{finalization_mode}: Finalizing previous match {state['current_match']['id']} due to new ID {new_game_id}")
            end_match_and_update_phase()
        else:
            logging.debug(f"{finalization_mode}: Skipping non-active match {state['current_match']['id']}")
        _reset_match_but_keep_id(new_game_id)
    if not state["current_match"]["id"] and new_game_id:
        logging.info(f"{finalization_mode}: INITIALIZING MATCH: {new_game_id}")
        _reset_match_but_keep_id(new_game_id)
    _process_player_state_changes(snap_text)
    parts = OBJ_BLOCKS.split(snap_text)
    for i, marker in enumerate(parts):
        if marker == "TotalPlayerList:" and i + 1 < len(parts):
            for obj_txt in re.findall(r'\{[^{}]*\}', parts[i+1]):
                p = _parse_kv_object(obj_txt)
                _upsert_player_from_total(p)
        elif marker == "TeamInfoList:" and i + 1 < len(parts):
            for obj_txt in re.findall(r'\{[^{}]*\}', parts[i+1]):
                t = _parse_kv_object(obj_txt)
                _upsert_team_from_teaminfo(t, parsed_logos)
    _recalculate_live_members()
    _update_live_eliminations()
    if state["current_match"]["status"] == "finished" and not in_archive_processing and not in_catchup_processing:
        end_match_and_update_phase()
    if not in_archive_processing and not in_catchup_processing:
        _update_phase_from_live_match()
    if state["current_match"]["id"] and not in_archive_processing and not in_catchup_processing:
        state["current_match"]["missing_teams"] = _calculate_missing_teams()
    state["match_state"]["status"] = "live" if state["current_match"]["id"] else "idle"
    state["match_state"]["last_updated"] = int(time.time())

def _reset_match_but_keep_id(new_id=None):
    """Reset match state but keep the ID if provided."""
    if new_id:
        state["current_match"] = {
            "id": new_id,
            "status": "live",
            "winnerTeamId": None,
            "winnerTeamName": None,
            "eliminationOrder": [],
            "killFeed": [],
            "teams": {},
            "players": {}
        }
        logging.info(f"Reset match state with new ID: {new_id}")
    else:
        state["current_match"] = {
            "id": None,
            "status": "idle",
            "winnerTeamId": None,
            "winnerTeamName": None,
            "eliminationOrder": [],
            "killFeed": [],
            "teams": {},
            "players": {}
        }
        logging.info("Full clean reset of match state")
    state["match_state"]["status"] = "live" if new_id else "idle"
    state["match_state"]["last_updated"] = int(time.time())

def _upsert_team_from_teaminfo(t, parsed_logos):
    """Update team in current_match, using log-provided name with case-insensitive logo matching."""
    tid = str(t.get("teamId") or "")
    if not tid or tid == "None":
        return
    team = state["current_match"]["teams"].setdefault(tid, {
        "id": tid,
        "name": "Unknown Team",
        "logo": DEFAULT_TEAM_LOGO,
        "liveMembers": 0,
        "kills": 0,
        "placementPointsLive": 0,
        "players": []
    })
    team.setdefault("placementPointsLive", 0)
    # Use log name
    team_name = t.get("teamName") or "Unknown Team"
    team["name"] = team_name
    _register_team_mapping(tid, team_name)
    # Use INI logo if team name matches expected_teams (case-insensitive)
    if parsed_logos:
        for info in parsed_logos.values():
            if info["name"].lower() == team_name.lower():
                team["logo"] = get_asset_url(info["logoPath"], DEFAULT_TEAM_LOGO)
                logging.debug(f"Assigned INI logo {team['logo']} to team {team_name} (ID: {tid})")
                break
        else:
            team["logo"] = DEFAULT_TEAM_LOGO
            logging.warning(f"No INI logo found for team {team_name} (ID: {tid}); using default logo {DEFAULT_TEAM_LOGO}")

def _upsert_player_from_total(p):
    pid = str(p.get("uId") or "")
    tid = str(p.get("teamId") or "")
    if not pid or not tid or tid == "None":
        return
    team = state["current_match"]["teams"].setdefault(tid, {
        "id": tid,
        "name": "Unknown Team",
        "logo": DEFAULT_TEAM_LOGO,
        "liveMembers": 0,
        "kills": 0,
        "placementPointsLive": 0,
        "players": []
    })
    team.setdefault("placementPointsLive", 0)
    if pid not in team["players"]:
        team["players"].append(pid)
    is_alive = (p.get("liveState") != 5)
    player = state["current_match"]["players"].setdefault(pid, {
        "id": pid, "teamId": tid, "name": p.get("playerName") or "Unknown",
        "photo": p.get("picUrl") or DEFAULT_PLAYER_PHOTO,
        "live": {"isAlive": True, "health": 0, "healthMax": 100, "liveState": 0},
        "stats": {"kills": 0, "damage": 0, "knockouts": 0}
    })
    player["name"] = p.get("playerName") or player["name"]
    player["photo"] = p.get("picUrl") or player["photo"]
    player["teamId"] = tid
    player["live"] = {
        "isAlive": is_alive,
        "health": int(p.get("health") or 0),
        "healthMax": int(p.get("healthMax") or 100),
        "liveState": int(p.get("liveState") or 0)
    }
    new_kills = int(p.get("killNum") or 0)
    current_kills = player["stats"]["kills"]
    if new_kills > current_kills:
        kill_diff = new_kills - current_kills
        player["stats"]["kills"] = new_kills
        if kill_diff > 0:
            team_name = _get_team_name_by_id(tid) or "Unknown Team"
            pn = player["name"]
            for _ in range(kill_diff):
                state["current_match"]["killFeed"].append(f"Kill: {pn} ({team_name}) got a new kill!")
            state["current_match"]["killFeed"] = state["current_match"]["killFeed"][-5:]
    player["stats"]["damage"] = int(p.get("damage") or 0)
    player["stats"]["knockouts"] = int(p.get("knockouts") or 0)
    team_name = _get_team_name_by_id(tid)
    if team_name:
        phase_player_data = {
            "id": pid,
            "name": player["name"],
            "teamId": tid,
            "teamName": team_name
        }
        _add_or_update_player(phase_player_data, is_alive, 
                             player["live"]["health"], player["live"]["healthMax"])
    _recompute_team_kills(tid)

def _recompute_team_kills(tid):
    team = state["current_match"]["teams"].get(tid)
    if not team:
        return
    total = 0
    for pid in team["players"]:
        total += state["current_match"]["players"].get(pid, {}).get("stats", {}).get("kills", 0)
    team["kills"] = total

def _recalculate_live_members():
    """Recalculate live members for each team."""
    for team_id, team_data in state["current_match"]["teams"].items():
        live_count = 0
        for player_id in team_data.get("players", []):
            player = state["current_match"]["players"].get(player_id)
            if player:
                is_alive = (
                    player["live"]["isAlive"] and 
                    player["live"]["liveState"] != 5 and
                    player["live"]["health"] > 0
                )
                if is_alive:
                    live_count += 1
        old_count = team_data.get("liveMembers", 0)
        team_data["liveMembers"] = live_count
        if old_count != live_count and old_count > 0:
            logging.info(f"Team {team_data['name']} live members: {old_count} -> {live_count}")

def _process_player_state_changes(log_text):
    """Process player state changes, deaths, and knockouts."""
    lines = log_text.splitlines()
    for line in lines:
        death_patterns = [
            r'G_PlayerDied.*?PlayerName=([^,]+).*?Health=([^,]+)',
            r'PlayerDied.*?Name=([^,]+).*?Health=([^,]+)',
            r'Death.*?Player=([^,]+).*?Health=([^,]+)'
        ]
        for pattern in death_patterns:
            m = re.search(pattern, line)
            if m:
                player_name = m.group(1).strip('\'"')
                try:
                    player_health = int(float(m.group(2)))
                except (ValueError, TypeError):
                    player_health = 0
                _update_player_death_status(player_name, player_health)
                break

def _update_player_death_status(player_name, health):
    """Update a specific player's death status by name."""
    for p_id, p_data in state["current_match"]["players"].items():
        if p_data["name"] == player_name:
            p_data["live"]["isAlive"] = False
            p_data["live"]["health"] = health
            p_data["live"]["liveState"] = 5
            logging.info(f"Player {player_name} died (health: {health})")
            return
    for p_id, p_data in state["phase"]["players"].items():
        if p_data["name"] == player_name:
            _add_or_update_player(p_data, is_alive=False, health=health)
            return

def _update_live_eliminations():
    """Update live eliminations tracking by team names."""
    for tid, t in state["current_match"]["teams"].items():
        team_name = t["name"]
        if t["liveMembers"] == 0 and team_name not in state["current_match"]["eliminationOrder"]:
            state["current_match"]["eliminationOrder"].append(team_name)
            logging.info(f"Team {team_name} eliminated")
    alive = [tid for tid, t in state["current_match"]["teams"].items() if t["liveMembers"] > 0]
    if len(alive) == 1 and state["current_match"]["status"] == "live":
        state["current_match"]["winnerTeamId"] = alive[0]
        state["current_match"]["winnerTeamName"] = _get_team_name_by_id(alive[0])
        state["current_match"]["status"] = "finished"

def end_match_and_update_phase(final_match_data=None):
    """
    Finalize match data, update all-time (only once per match), append matches (no duplicates),
    and rebuild phase standings from the canonical state['matches'] list.
    """
    global state, expected_teams, in_archive_processing
    try:
        if not final_match_data:
            final_match_data = copy.deepcopy(state["current_match"])

        match_id = final_match_data.get("id")
        if not match_id:
            logging.warning("No match ID found, skipping finalization")
            _reset_current_match()
            _export_json()
            return

        logging.info(f"Finalizing match {match_id}")

        # Ensure processed_game_ids exists and is a set
        state["all_time"].setdefault("processed_game_ids", set())
        if not isinstance(state["all_time"]["processed_game_ids"], set):
            state["all_time"]["processed_game_ids"] = set(state["all_time"].get("processed_game_ids", []))

        already_processed = match_id in state["all_time"]["processed_game_ids"] or match_id in state.get("processed_matches", set())

        # Add missing teams (only if not processing archive)
        if not in_archive_processing and expected_teams:
            match_team_names = {t.get("name", "").lower() for t in final_match_data.get("teams", {}).values()}
            missing = []
            for team_name, info in expected_teams.items():
                if team_name.lower() not in match_team_names:
                    missing.append({
                        "id": info["id"],
                        "name": team_name,
                        "logo": get_asset_url(info["logoPath"], DEFAULT_TEAM_LOGO),
                        "kills": 0,
                        "placementPoints": 0,
                        "points": 0,
                        "rank": "MISS",
                        "liveMembers": 0,
                        "players": [],
                        "missing": True
                    })
            final_match_data["missing_teams"] = missing
            logging.debug(f"Match {match_id}: Added {len(missing)} missing teams")

        # --- Calculate and assign placementPointsLive per team ---
        elimination_order = final_match_data.get("eliminationOrder", [])
        total_teams = len(final_match_data.get("teams", {}))
        rank_map = {t: i + 2 for i, t in enumerate(reversed(elimination_order))}
        winner_name = final_match_data.get("winnerTeamName")
        if winner_name:
            rank_map[winner_name] = 1

        placement_points_map = {1: 10, 2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 1, 8: 1}

        for tid, team in final_match_data.get("teams", {}).items():
            team_name = team.get("name", "Unknown Team")
            rank = rank_map.get(team_name, total_teams)
            placement_points = placement_points_map.get(rank, 0)
            final_match_data["teams"][tid]["placementPointsLive"] = placement_points
            logging.debug(f"Set {team_name} placementPointsLive={placement_points} (rank={rank})")


        # --- Update all-time players only if this match was not already processed ---
        if not already_processed:
            player_count = 0
            for pid, player in final_match_data.get("players", {}).items():
                if not pid or pid == "None":
                    continue
                team_name = _get_team_name_by_id(player.get("teamId")) or player.get("teamName", "Unknown Team")
                # All-time player
                if pid not in state["all_time"]["players"]:
                    state["all_time"]["players"][pid] = {
                        "id": pid,
                        "name": player.get("name", "Unknown Player"),
                        "photo": player.get("photo", DEFAULT_PLAYER_PHOTO),
                        "teamName": team_name,
                        "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
                    }
                ap = state["all_time"]["players"][pid]
                ap["name"] = player.get("name", ap.get("name"))
                ap["photo"] = player.get("photo", ap.get("photo"))
                ap["teamName"] = team_name
                ap["totals"]["kills"] += int(player.get("stats", {}).get("kills", 0))
                ap["totals"]["damage"] += int(player.get("stats", {}).get("damage", 0))
                ap["totals"]["knockouts"] += int(player.get("stats", {}).get("knockouts", 0))
                ap["totals"]["matches"] += 1
                player_count += 1

            # record processed game id and persist all-time players
            state["all_time"]["processed_game_ids"].add(match_id)
            try:
                save_all_time_players()
                logging.info(f"Saved all-time players after match {match_id} (updated {player_count} players)")
            except Exception as e:
                logging.error(f"Failed saving all-time players for {match_id}: {e}")
        else:
            logging.info(f"Match {match_id} already in all_time processed list; skipping all-time update")

        # --- Append to state['matches'] only if not already present ---
        if not any(m.get("id") == match_id for m in state.get("matches", [])):
            state.setdefault("matches", []).append(final_match_data)
            logging.info(f"Appended match {match_id} to state['matches'] (now {len(state['matches'])} matches)")
        else:
            logging.debug(f"Match {match_id} already exists in state['matches'] — not appending")

        # mark processed_matches for in-memory check
        state.setdefault("processed_matches", set()).add(match_id)

        # --- Rebuild phase from canonical matches list so we never double-count ---
        rebuild_phase_from_matches()

        # Force export JSON
        _export_json()

        # Reset current match state (keeps matches list intact)
        _reset_current_match()
        state["match_state"]["status"] = "idle"
        state["match_state"]["last_updated"] = int(time.time())
        logging.info(f"Match {match_id} finalized and current match reset")

    except Exception as e:
        logging.error(f"Error finalizing match {match_id}: {e}")
        _reset_current_match()
        _export_json()

def rebuild_phase_from_matches():
    """
    Rebuild state['phase']['teams'] and state['phase']['players'] from state['matches'].
    - Deduplicates matches by id (preserves first occurrence).
    - Computes placementPoints using placementPointsLive if present, otherwise falls back to rank heuristic.
    - Recomputes phase standings and top players.
    """
    global state
    logging.info("Rebuilding phase data from state['matches']")
    # Deduplicate matches by id, preserving first occurrence
    seen = set()
    unique_matches = []
    for m in state.get("matches", []):
        mid = m.get("id")
        if not mid:
            continue
        if mid in seen:
            logging.debug(f"Skipping duplicate match id in rebuild: {mid}")
            continue
        seen.add(mid)
        unique_matches.append(m)
    state["matches"] = unique_matches

    # Reset phase
    state["phase"]["teams"] = {}
    state["phase"]["players"] = {}

    # Helper for placement points fallback
    placement_points_map = {1: 10, 2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 1, 8: 1}

    for match in state["matches"]:
        # compute elimination/ranks fallback only if needed
        elimination_order = match.get("eliminationOrder", [])
        total_teams = len(match.get("teams", {})) or 0
        rank_map = {t: i + 2 for i, t in enumerate(reversed(elimination_order))}
        winner_name = match.get("winnerTeamName")
        if winner_name:
            rank_map[winner_name] = 1

        # Per-team accumulation for this match
        for tid, team in match.get("teams", {}).items():
            team_name = team.get("name", "Unknown Team")
            kills = int(team.get("kills", 0))
            # prefer placementPointsLive if present (set at finalization), otherwise compute via rank_map / fallback map
            if "placementPointsLive" in team:
                placement_points = int(team.get("placementPointsLive", 0))
            else:
                rank = rank_map.get(team_name, total_teams or 0)
                placement_points = placement_points_map.get(rank, 0)
            points = kills + placement_points
            if team_name not in state["phase"]["teams"]:
                state["phase"]["teams"][team_name] = {
                    "id": team.get("id", tid),
                    "name": team_name,
                    "logo": team.get("logo", DEFAULT_TEAM_LOGO),
                    "totals": {"kills": 0, "placementPoints": 0, "points": 0, "wwcd": 0}
                }
            tt = state["phase"]["teams"][team_name]["totals"]
            tt["kills"] += kills
            tt["placementPoints"] += placement_points
            tt["points"] += points
            if team_name == winner_name:
                tt["wwcd"] += 1

        # Per-player accumulation for this match
        for pid, p in match.get("players", {}).items():
            # determine team name (prefer mapping helper)
            team_name = _get_team_name_by_id(p.get("teamId")) or p.get("teamName") or "Unknown Team"
            if pid not in state["phase"]["players"]:
                state["phase"]["players"][pid] = {
                    "id": pid,
                    "name": p.get("name", "Unknown Player"),
                    "photo": p.get("photo", DEFAULT_PLAYER_PHOTO),
                    "teamName": team_name,
                    "live": {"isAlive": False, "health": 0, "healthMax": 100},
                    "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
                }
            pp = state["phase"]["players"][pid]["totals"]
            pp["kills"] += int(p.get("stats", {}).get("kills", 0))
            pp["damage"] += int(p.get("stats", {}).get("damage", 0))
            pp["knockouts"] += int(p.get("stats", {}).get("knockouts", 0))
            pp["matches"] += 1

    # Recompute derived lists
    try:
        state["phase"]["standings"] = _phase_standings()
    except Exception:
        # fallback: make standings by manual sort if helper misbehaves
        teams = []
        for team_name, t in state["phase"]["teams"].items():
            tot = t["totals"]
            teams.append({
                "teamId": team_name,
                "teamName": t["name"],
                "kills": tot["kills"],
                "placementPoints": tot["placementPoints"],
                "points": tot["points"],
                "wwcd": tot.get("wwcd", 0),
                "rank": None
            })
        teams.sort(key=lambda x: (x["points"], x["kills"]), reverse=True)
        for i, row in enumerate(teams, 1):
            row["rank"] = i
        state["phase"]["standings"] = teams

    # Update all-time top players view for the phase
    try:
        state["phase"]["allTimeTopPlayers"] = _calculate_top_players(state["phase"]["players"], state["phase"]["teams"])
    except Exception:
        state["phase"]["allTimeTopPlayers"] = []
    logging.info("Rebuild finished: %d teams, %d players", len(state["phase"]["teams"]), len(state["phase"]["players"]))


def _reset_current_match():
    """Reset the current_match state to its comprehensive initial idle state."""
    global state
    state["current_match"] = {
        "id": None,
        "status": "idle",
        "winnerTeamId": None,
        "winnerTeamName": None,
        "eliminationOrder": [],
        "killFeed": [],
        "teams": {},
        "players": {},
        "missing_teams": [],
        "leaderboards": {"currentMatchTopPlayers": []},
        "activePlayers": 0,
        "teamKills": 0,
        "placementPointsMap": {}
    }
    state["match_state"]["status"] = "idle"
    state["match_state"]["last_updated"] = int(time.time())
    logging.info("Current match state comprehensively reset to idle.")

def _print_terminal_snapshot(test_mode=False):
    """Enhanced terminal output with colors and simulation progress."""
    m = state["current_match"]
    os.system('cls' if os.name == 'nt' else 'clear')
    mode_text = "TEST MODE" if test_mode else "LIVE MODE"
    mode_color = Fore.YELLOW if test_mode else Fore.GREEN
    print_colored("╔" + "═" * 58 + "╗", Fore.BLUE)
    print_colored(f"║{' ' * 20}PUBG LIVE SCOREBOARD{' ' * 19}║", Fore.CYAN, Style.BRIGHT)
    print_colored(f"║{' ' * 15}{mode_text} - {datetime.datetime.now().strftime('%H:%M:%S')}{' ' * (42 - len(mode_text))}║", mode_color)
    print_colored("╠" + "═" * 58 + "╣", Fore.BLUE)
    match_id = m['id'] or 'waiting...'
    status = m['status']
    status_color = Fore.GREEN if status == "live" else Fore.YELLOW if status == "finished" else Fore.WHITE
    print_colored(f"║ Match: {match_id:<20} Status: ", Fore.WHITE, end="")
    print_colored(f"{status:<15} ║", status_color)
    if test_mode and simulation_manager:
        progress_str = simulation_manager.get_progress_string()
        print_colored(f"║ Simulation: {progress_str:<38} ║", Fore.CYAN)
    print_colored("╠" + "═" * 58 + "╣", Fore.BLUE)
    print_colored("║ TEAMS" + " " * 53 + "║", Fore.CYAN, Style.BRIGHT)
    print_colored("║ Team Name              Kills  Live  Points         ║", Fore.WHITE, Style.DIM)
    print_colored("║" + "─" * 58 + "║", Fore.BLUE)
    rows = []
    for tid, t in m["teams"].items():
        live_points = t.get("placementPointsLive", 0)
        total_points = t["kills"] + live_points
        rows.append((t["name"], t["kills"], t["liveMembers"], total_points))
    rows.sort(key=lambda r: (r[3], r[1]), reverse=True)
    for i, (name, kills, live, points) in enumerate(rows[:8]):
        rank_color = Fore.YELLOW if i == 0 else Fore.GREEN if i < 3 else Fore.WHITE
        alive_color = Fore.GREEN if live > 0 else Fore.RED
        name_display = name[:22] if len(name) <= 22 else name[:19] + "..."
        print_colored(f"║ {name_display:<22} ", Fore.WHITE, end="")
        print_colored(f"{kills:>3}   ", rank_color, end="")
        print_colored(f"{live:>2}   ", alive_color, end="")
        print_colored(f"{points:>3}          ║", rank_color)
    for _ in range(max(0, 8 - len(rows))):
        print_colored("║" + " " * 58 + "║", Fore.WHITE)
    print_colored("╠" + "═" * 58 + "╣", Fore.BLUE)
    print_colored("║ RECENT KILLS" + " " * 46 + "║", Fore.RED, Style.BRIGHT)
    kill_feed = m["killFeed"][-4:]
    for kill in kill_feed:
        kill_display = kill[:56] if len(kill) <= 56 else kill[:53] + "..."
        print_colored(f"║ {kill_display:<56} ║", Fore.YELLOW)
    for _ in range(max(0, 4 - len(kill_feed))):
        print_colored("║" + " " * 58 + "║", Fore.WHITE)
    print_colored("╚" + "═" * 58 + "╝", Fore.BLUE)
    phase_teams = _phase_standings()[:3]
    if phase_teams:
        print_colored("\nPHASE STANDINGS (Top 3):", Fore.MAGENTA, Style.BRIGHT)
        for i, team in enumerate(phase_teams, 1):
            medal = "1st" if i == 1 else "2nd" if i == 2 else "3rd"
            print_colored(f"{medal} {team['teamName']}: {team['points']} pts ({team['kills']} K + {team['placementPoints']} P)", 
                         Fore.YELLOW if i == 1 else Fore.WHITE)

def _phase_standings():
    """Generate phase standings from cumulative phase data"""
    teams = []
    for team_name, t in state["phase"]["teams"].items():
        tot = t["totals"]
        teams.append({
            "teamId": team_name,
            "teamName": t["name"],
            "kills": tot["kills"],
            "placementPoints": tot["placementPoints"],
            "points": tot["points"],
            "wwcd": tot.get("wwcd", 0),
            "rank": None
        })
    teams.sort(key=lambda x: (x["points"], x["kills"]), reverse=True)
    for i, row in enumerate(teams, 1):
        row["rank"] = i
    return teams

def _current_match_top_players():
    players = []
    for pid, p in state["current_match"]["players"].items():
        team_name = _get_team_name_by_id(p["teamId"]) or "Unknown Team"
        players.append({
            "playerId": pid, "teamName": team_name, "name": p["name"],
            "kills": p["stats"]["kills"], "damage": p["stats"]["damage"], 
            "knockouts": p["stats"]["knockouts"]
        })
    players.sort(key=lambda x: (x["kills"], x["damage"], x["knockouts"]), reverse=True)
    return players[:5]

def _all_time_top_players():
    players = []
    for pid, p in state["all_time"]["players"].items():
        if not isinstance(p, dict):
            continue
        t = p.get("totals", {})
        if not isinstance(t, dict):
            continue
        players.append({
            "playerId": pid,
            "name": p.get("name", "Unknown"),
            "teamName": p.get("teamName", "Unknown Team"),
            "totalKills": t.get("kills", 0),
            "totalDamage": t.get("damage", 0),
            "totalKnockouts": t.get("knockouts", 0),
            "totalMatches": t.get("matches", 0)
        })
    players.sort(key=lambda x: (x["totalKills"], x["totalDamage"], x["totalKnockouts"]), reverse=True)
    return players[:5]

def _get_active_players():
    """Returns active players with health data."""
    if state["current_match"]["status"] != "live":
        return []
    active_players = []
    for player_id, player_data in state["current_match"]["players"].items():
        team_data = state["current_match"]["teams"].get(player_data["teamId"], {})
        team_name = team_data.get("name", "Unknown Team")
        active_players.append({
            "playerId": player_id,
            "name": player_data["name"],
            "teamId": player_data["teamId"],
            "teamName": team_name,
            "teamLogo": team_data.get("logo", DEFAULT_TEAM_LOGO),
            "live": {
                "isAlive": player_data["live"]["isAlive"],
                "health": player_data["live"]["health"],
                "healthMax": player_data["live"]["healthMax"],
                "liveState": player_data["live"]["liveState"]
            },
            "stats": {
                "kills": player_data["stats"]["kills"],
                "damage": player_data["stats"]["damage"],
                "knockouts": player_data["stats"]["knockouts"]
            }
        })
    return sorted(active_players, key=lambda p: (
        p["teamName"],
        not p["live"]["isAlive"],
        -p["stats"]["kills"]
    ))

def _get_team_kills():
    """Returns current match team kills."""
    if state["current_match"]["status"] != "live":
        return {}
    team_kills = {}
    for team_id, team_data in state["current_match"]["teams"].items():
        team_kills[team_id] = {
            "teamId": team_id,
            "teamName": team_data["name"],
            "kills": team_data["kills"],
            "liveMembers": team_data["liveMembers"]
        }
    return team_kills

def _export_json():
    """Exports the current state to a JSON file."""
    try:
        # Calculate missing teams for current match
        missing_teams = _calculate_missing_teams()
        
        data = {
            "match_state": state["match_state"],
            "phase": {
                "standings": _phase_standings(),
                "allTimeTopPlayers": _all_time_top_players()
            },
            "current_match": {
                "id": state["current_match"]["id"],
                "status": state["current_match"]["status"],
                "winnerTeamId": state["current_match"]["winnerTeamId"],
                "winnerTeamName": state["current_match"]["winnerTeamName"],
                "eliminationOrder": state["current_match"]["eliminationOrder"],
                "killFeed": state["current_match"]["killFeed"],
                "teams": state["current_match"]["teams"],
                "players": state["current_match"]["players"],
                "missing_teams": missing_teams,
                "leaderboards": {
                    "currentMatchTopPlayers": _current_match_top_players()
                },
                "activePlayers": _get_active_players(),
                "teamKills": _get_team_kills()
            },
            "matches": state["matches"]
        }
        with open(OUTPUT_JSON, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error during JSON export: {e}")

def get_all_log_files(log_dir, exclude_live_log=True):
    """Get all log files in log_dir."""
    if not log_dir.exists():
        return []
    out = []
    for item in log_dir.iterdir():
        if item.is_file() and item.suffix == ".txt":
            if exclude_live_log and item.name in ["simulated_live.txt"]:
                continue
            out.append(item)
    return sorted(out)

def load_all_time_players():
    """Load all-time player data and processed game IDs."""
    if not ALL_TIME_PLAYERS_JSON.exists():
        logging.info(f"No all-time players file found at {ALL_TIME_PLAYERS_JSON}")
        state["all_time"]["processed_game_ids"] = set()
        return False

    try:
        with open(ALL_TIME_PLAYERS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)

            if not isinstance(data, dict) or "players" not in data:
                logging.error(f"Invalid format in {ALL_TIME_PLAYERS_JSON}: missing 'players' key")
                state["all_time"]["processed_game_ids"] = set()
                return False

            # Load players
            state["all_time"]["players"] = data["players"]

            # Load processed game IDs (convert to set for fast lookup)
            processed_ids = data.get("processed_game_ids", [])
            if isinstance(processed_ids, list):
                state["all_time"]["processed_game_ids"] = set(processed_ids)
            else:
                state["all_time"]["processed_game_ids"] = set()

            logging.info(
                f"Loaded {len(state['all_time']['players'])} players and "
                f"{len(state['all_time']['processed_game_ids'])} processed game IDs from {ALL_TIME_PLAYERS_JSON}"
            )
            return True

    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from {ALL_TIME_PLAYERS_JSON}: {e}")
    except Exception as e:
        logging.error(f"Error loading all-time player data from {ALL_TIME_PLAYERS_JSON}: {e}")

    state["all_time"]["processed_game_ids"] = set()
    return False

def debug_log_content(log_text, file_name="unknown"):
    """Debug log file content to verify snapshot and player data presence."""
    logging.debug(f"Debugging log content for {file_name} (length: {len(log_text)} bytes)")
    snapshot_pattern = r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] POST /totalmessage'
    snapshots = re.findall(snapshot_pattern, log_text)
    logging.debug(f"Found {len(snapshots)} snapshots in {file_name}")
    player_pattern = r'TotalPlayerList:.*?\uId'
    player_matches = re.findall(player_pattern, log_text, re.DOTALL)
    logging.debug(f"Found {len(player_matches)} TotalPlayerList entries in {file_name}")
    if player_matches:
        logging.debug(f"Sample TotalPlayerList entry: {player_matches[0][:200]}...")
    if not snapshots or not player_matches:
        logging.warning(f"No valid snapshots or player data in {file_name}")

def save_all_time_players():
    """Save all-time player data to JSON file with enhanced error handling and debugging."""
    import shutil
    try:
        player_count = len(state["all_time"]["players"])
        
        # Ensure processed_game_ids is a set
        if not isinstance(state["all_time"].get("processed_game_ids"), set):
            logging.warning(f"processed_game_ids is not a set, it's a {type(state['all_time'].get('processed_game_ids'))}, converting...")
            state["all_time"]["processed_game_ids"] = set(state["all_time"].get("processed_game_ids", []))
        
        processed_games = len(state["all_time"]["processed_game_ids"])
        logging.info(f"Entering save_all_time_players: {player_count} players, {processed_games} games to save")
        
        # Debug: Show sample of game IDs
        if processed_games > 0:
            sample_ids = list(state["all_time"]["processed_game_ids"])[:5]
            logging.info(f"Sample game IDs being saved: {sample_ids}")
        else:
            logging.warning("No processed game IDs to save!")
        
        output_dir = Path(ALL_TIME_PLAYERS_JSON).parent
        logging.debug(f"Ensuring output directory exists: {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)

        # Check write permissions
        if not os.access(output_dir, os.W_OK):
            logging.error(f"No write permission for directory {output_dir}")
            return

        temp_file = ALL_TIME_PLAYERS_JSON.with_suffix('.json.tmp')
        logging.debug(f"Attempting to write to temporary file: {temp_file}")

        # Prepare data with processed game IDs (convert set to sorted list for readability)
        processed_ids_list = sorted(list(state["all_time"]["processed_game_ids"]))
        save_data = {
            "players": state["all_time"]["players"],
            "processed_game_ids": processed_ids_list
        }
        
        logging.debug(f"Prepared save_data with {len(processed_ids_list)} game IDs")

        # Write to temp file
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(save_data, f, indent=2, ensure_ascii=False)
            logging.debug(f"Successfully wrote {player_count} players and {len(processed_ids_list)} game IDs to temp file {temp_file}")
        except Exception as e:
            logging.error(f"Failed to write to temp file {temp_file}: {e}")
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            return

        # Verify temp file
        if not temp_file.exists():
            logging.error(f"Temporary file {temp_file} was not created")
            return
        if temp_file.stat().st_size == 0:
            logging.error(f"Temporary file {temp_file} is empty")
            temp_file.unlink(missing_ok=True)
            return

        # Close any existing file handles on Windows
        import gc
        gc.collect()

        # Remove target file if it exists (Windows compatibility)
        if ALL_TIME_PLAYERS_JSON.exists():
            try:
                ALL_TIME_PLAYERS_JSON.unlink()
                logging.debug(f"Removed existing {ALL_TIME_PLAYERS_JSON}")
            except Exception as e:
                logging.warning(f"Could not remove existing file: {e}")

        # Attempt to rename (now that target is gone)
        try:
            temp_file.rename(ALL_TIME_PLAYERS_JSON)
            logging.info(f"Successfully saved {ALL_TIME_PLAYERS_JSON} with {player_count} players and {len(processed_ids_list)} processed games")
            return
        except OSError as e:
            logging.error(f"Failed to rename {temp_file} to {ALL_TIME_PLAYERS_JSON}: {e}")

        # Fallback: Direct copy
        logging.warning(f"Attempting direct copy to {ALL_TIME_PLAYERS_JSON}")
        try:
            shutil.copy2(str(temp_file), str(ALL_TIME_PLAYERS_JSON))
            logging.info(f"Successfully copied to {ALL_TIME_PLAYERS_JSON}")
            temp_file.unlink(missing_ok=True)
        except Exception as e:
            logging.error(f"Failed to copy {temp_file} to {ALL_TIME_PLAYERS_JSON}: {e}")
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)

    except Exception as e:
        logging.error(f"Unexpected error in save_all_time_players: {e}")
        import traceback
        logging.error(traceback.format_exc())
        # Clean up temp file on error
        try:
            temp_file = ALL_TIME_PLAYERS_JSON.with_suffix('.json.tmp')
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
        except:
            pass

def apply_archived_file_to_all_time(log_text, parsed_logos, file_name="unknown"):
    """Apply archived log data to all-time player statistics."""
    global in_archive_processing
    temp_before = state["current_match"].copy()
    temp_mapping_before = state["teamNameMapping"].copy()
    in_archive_processing = True
    processed_players = 0
    start_time = time.time()
    try:
        # Validate and debug log content
        debug_log_content(log_text, file_name)
        if not validate_log_content(log_text):
            logging.error(f"Invalid or empty log content in {file_name}, skipping processing")
            return

        # Read log in chunks to reduce memory usage
        chunk_size = 1024 * 1024  # 1MB chunks
        snapshots = []
        buffer = ""
        pos = 0
        while pos < len(log_text):
            chunk = log_text[pos:pos + chunk_size]
            buffer += chunk
            new_snapshots = extract_snapshots(buffer)
            if new_snapshots:
                snapshots.extend(new_snapshots[:-1])  # All but the last (may be incomplete)
                buffer = new_snapshots[-1] if new_snapshots else ""
            pos += chunk_size
        if buffer:
            snapshots.extend(extract_snapshots(buffer))  # Process remaining buffer
        total_snapshots = len(snapshots)
        logging.info(f"Found {total_snapshots} snapshots in {file_name}")

        # Group last snapshot per game_id
        game_snapshots = {}
        for i, snap in enumerate(snapshots):
            gid_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", snap)
            game_id = gid_match.group(1) if gid_match else f"unknown_{i}"
            game_snapshots[game_id] = snap
            if (i + 1) % 100 == 0:  # Progress update every 100 snapshots
                print_progress_bar(i + 1, total_snapshots, prefix="Snapshots Scanned", suffix=f"{file_name}")

        total_matches = len(game_snapshots)
        logging.info(f"Detected {total_matches} unique matches in {file_name}")

        match_count = 0
        for game_id, last_snap in game_snapshots.items():
            # Skip if already processed in all_time
            if game_id in state["all_time"].get("processed_game_ids", set()):
                logging.info(f"Skipping archived match {game_id}, already processed in all_time.")
                continue

            if check_shutdown_conditions():
                logging.info(f"Shutdown requested during processing of {file_name}. Stopping.")
                break
            # Reset state for each match
            state["current_match"] = {
                "id": None,
                "status": "idle",
                "winnerTeamId": None,
                "winnerTeamName": None,
                "eliminationOrder": [],
                "killFeed": [],
                "teams": {},
                "players": {}
            }
            state["teamNameMapping"] = {}

            process_snapshot(last_snap, parsed_logos)
            logging.debug(f"Processed snapshot for game {game_id}, current_match players: {len(state['current_match']['players'])}")

            # Update all-time stats
            for pid, pl in state["current_match"]["players"].items():
                if not pid or pid == "None":
                    logging.warning(f"Skipping invalid player ID in match {game_id}")
                    continue
                team_name = _get_team_name_by_id(pl.get("teamId")) or "Unknown Team"
                at = state["all_time"]["players"].setdefault(pid, {
                    "id": pid,
                    "name": pl.get("name", "Unknown Player"),
                    "teamName": team_name,
                    "photo": pl.get("photo", DEFAULT_PLAYER_PHOTO),
                    "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
                })
                at["name"] = pl.get("name", at["name"])
                at["photo"] = pl.get("photo", at["photo"])
                at["teamName"] = team_name
                at["totals"]["kills"] += int(pl["stats"].get("kills", 0))
                at["totals"]["damage"] += int(pl["stats"].get("damage", 0))
                at["totals"]["knockouts"] += int(pl["stats"].get("knockouts", 0))
                at["totals"]["matches"] += 1
                processed_players += 1
                logging.debug(f"Updated all-time stats for player {pid} in match {game_id}: {at['totals']}")
            # Mark this game_id as processed
            state["all_time"].setdefault("processed_game_ids", set()).add(game_id)
            match_count += 1
            print_progress_bar(match_count, total_matches, prefix="Matches", suffix=f"{file_name}")

        if total_matches == 0:
            logging.warning(f"No matches found in {file_name}")
        elapsed = time.time() - start_time
        logging.info(f"Processed {file_name} in {elapsed:.2f}s: {total_matches} matches, {processed_players} player updates")

        # Save after processing all matches
        save_all_time_players()
        logging.info(f"Attempted to save all-time player data after processing {file_name}")
    except Exception as e:
        logging.error(f"Error processing archived log {file_name}: {e}")
    finally:
        in_archive_processing = False
        state["current_match"].update(temp_before)
        state["teamNameMapping"] = temp_mapping_before

def validate_log_content(log_text):
    """Validate that the log text contains valid snapshot data."""
    if not log_text or not isinstance(log_text, str):
        logging.error("Log text is empty or invalid")
        return False
    snapshot_pattern = r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] POST /totalmessage'
    if not re.search(snapshot_pattern, log_text):
        logging.error("No valid snapshots found in log text")
        return False
    player_pattern = r'TotalPlayerList:.*?\uId'
    if not re.search(player_pattern, log_text, re.DOTALL):
        logging.warning("No player data found in log text")
        return False
    return True

def process_archives_for_all_time(parsed_logos, force_repopulate=False):
    """Process archived logs for all-time player statistics."""
    if not force_repopulate and load_all_time_players():
        print_colored("Using cached all-time player data.", Fore.GREEN)
        return
    if force_repopulate:
        state["all_time"]["players"] = {}
        logging.info("Force repopulating all-time players")
    print_colored("Processing archived logs for all-time player statistics...", Fore.CYAN)
    archived_logs = get_all_log_files(ARCHIVE_LOG_DIR, exclude_live_log=False)
    if not archived_logs:
        print_colored("No archived logs found.", Fore.YELLOW)
        save_all_time_players()
        return
    print_colored(f"Found {len(archived_logs)} archived logs to process.", Fore.WHITE)
    total_file_size = sum(f.stat().st_size for f in archived_logs)
    processed_size = 0
    for i, f in enumerate(archived_logs):
        if check_shutdown_conditions():
            logging.info("Shutdown requested during archive processing. Stopping.")
            break
        file_size = f.stat().st_size
        print_colored(f"\nProcessing archive file {i+1}/{len(archived_logs)}: {f.name}", Fore.YELLOW)
        try:
            with open(f, "r", encoding="utf-8") as fh:
                log_text = fh.read()
                apply_archived_file_to_all_time(log_text, parsed_logos, file_name=f.name)
                processed_size += file_size
                print_progress_bar(processed_size, total_file_size, 
                                   prefix=f"Archive {i+1}/{len(archived_logs)}", 
                                   suffix=f"{f.name} complete")
        except Exception as e:
            logging.error(f"Error processing archive file {f.name}: {e}")
            processed_size += file_size
            print_progress_bar(processed_size, total_file_size, 
                               prefix=f"Archive {i+1}/{len(archived_logs)}", 
                               suffix=f"{f.name} ERROR")
    print_progress_bar(total_file_size, total_file_size, prefix="Archive", suffix="PROCESSING COMPLETE")
    save_all_time_players()
    print_colored(f"All-time processing complete. Saved {len(state['all_time']['players'])} players.", Fore.GREEN)

def confirm_file_setup():
    """Display detected files and ask user for confirmation."""
    print_colored("\n" + "="*60, Fore.CYAN)
    print_colored("FILE SETUP CONFIRMATION", Fore.CYAN, Style.BRIGHT)
    print_colored("="*60, Fore.CYAN)
    if ALL_TIME_PLAYERS_JSON.exists():
        try:
            with open(ALL_TIME_PLAYERS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                player_count = len(data.get("players", {}))
                print_colored(f"✓ Found all_time_players.json with {player_count} players", Fore.GREEN)
        except Exception as e:
            print_colored(f"⚠ Found all_time_players.json but couldn't read it: {e}", Fore.YELLOW)
    else:
        print_colored("⚪ No all_time_players.json found (will be created)", Fore.WHITE)
    archived_logs = get_all_log_files(ARCHIVE_LOG_DIR, exclude_live_log=False)
    if archived_logs:
        print_colored(f"✓ Archived logs: {len(archived_logs)} files in {ARCHIVE_LOG_DIR}", Fore.GREEN)
    else:
        print_colored(f"⚪ No archived logs found in {ARCHIVE_LOG_DIR}", Fore.WHITE)
    current_phase_logs = get_all_log_files(CURRENT_LOG_DIR, exclude_live_log=True)
    if current_phase_logs:
        print_colored(f"✓ Current phase logs: {len(current_phase_logs)} files", Fore.GREEN)
    else:
        print_colored("⚪ No current phase logs found", Fore.WHITE)
    test_logs = get_all_log_files(TEST_LOGS_DIR, exclude_live_log=False)
    if test_logs:
        print_colored(f"✓ Test logs: {len(test_logs)} files in {TEST_LOGS_DIR}", Fore.GREEN)
    else:
        print_colored(f"⚪ No test logs found in {TEST_LOGS_DIR}", Fore.WHITE)
    print_colored("\n" + "-"*60, Fore.BLUE)
    print_colored("1. Continue with current setup", Fore.GREEN)
    print_colored("3. Exit", Fore.RED)
    print_colored("-"*60, Fore.BLUE)
    while True:
        choice = input(f"{Fore.CYAN}Enter your choice (1-3): {Style.RESET_ALL}").strip()
        if choice == "1":
            return "continue"
        elif choice == "3":
            print_colored("Exiting...", Fore.YELLOW)
            exit(0)
        else:
            print_colored("Invalid choice. Please enter 1 or 3.", Fore.RED)

def _file_size(path):
    """Returns the file size in bytes."""
    return os.stat(path).st_size if os.path.exists(path) else 0

def _read_new(path: Path, pos: int):
    """Read new content from file starting at position."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            f.seek(pos)
            data = f.read()
            return data, f.tell()
    except Exception as e:
        logging.warning(f"Read error: {e}")
        return "", pos

def _is_log_updating(log_path, min_size=0):
    """Check if a log file is actively being updated."""
    try:
        initial_size = log_path.stat().st_size
        if initial_size < min_size:
            return False
        time.sleep(1)
        current_size = log_path.stat().st_size
        return current_size > initial_size
    except Exception:
        return False

def force_end_listener():
    """Listen for force end commands in a background thread."""
    while True:
        try:
            force_end_file = Path("force_end.flag")
            if force_end_file.exists():
                print_colored("Force end flag detected. Requesting finalization...", Fore.RED)
                request_finalization()
                force_end_file.unlink()
                break
            force_shutdown_file = Path("force_shutdown.flag")
            if force_shutdown_file.exists():
                print_colored("Force shutdown flag detected. Requesting complete shutdown...", Fore.RED)
                request_finalization()
                force_shutdown_file.unlink()
                global complete_shutdown_requested
                with finalization_lock:
                    complete_shutdown_requested = True
                break
            time.sleep(1)
        except Exception as e:
            logging.error(f"Error in force end listener: {e}")
            break

def setup_force_end_thread():
    """Setup a background thread to listen for force end commands."""
    thread = threading.Thread(target=force_end_listener, daemon=True)
    thread.start()
    return thread

def server_only_mode():
    """Run in server-only mode after finalization."""
    print_colored("\n" + "="*60, Fore.MAGENTA)
    print_colored("ENTERING SERVER-ONLY MODE", Fore.MAGENTA, Style.BRIGHT)
    print_colored("The web server is running and overlays remain accessible", Fore.GREEN)
    print_colored("Log monitoring has stopped", Fore.YELLOW)
    print_colored("="*60, Fore.MAGENTA)
    print_colored("\nOverlay data will show the final phase standings", Fore.CYAN)
    print_colored("Create 'force_shutdown.flag' file or press Ctrl+C to exit completely\n", Fore.WHITE)
    last_json_export = 0
    try:
        while True:
            if should_shutdown():
                print_colored("Complete shutdown requested", Fore.RED)
                break
            force_shutdown_file = Path("force_shutdown.flag")
            if force_shutdown_file.exists():
                print_colored("Force shutdown flag detected", Fore.RED)
                force_shutdown_file.unlink()
                break
            now = time.time()
            if now - last_json_export >= 10:
                try:
                    _export_json()
                    last_json_export = now
                except Exception as e:
                    logging.error(f"Error exporting JSON in server-only mode: {e}")
            time.sleep(1)
    except KeyboardInterrupt:
        print_colored("\nServer shutdown requested by user", Fore.YELLOW)
    print_colored("Shutting down web server...", Fore.YELLOW)

def request_finalization():
    """Thread-safe way to request finalization."""
    if state["current_match"]["status"] in ["live", "finished"]:
        end_match_and_update_phase()
    global finalization_requested
    with finalization_lock:
        finalization_requested = True

def should_finalize():
    """Check if finalization has been requested."""
    global finalization_requested
    with finalization_lock:
        return finalization_requested

def should_shutdown():
    """Check if complete shutdown has been requested."""
    global complete_shutdown_requested
    with finalization_lock:
        return complete_shutdown_requested

def perform_finalization(keep_server_running=True):
    """Perform the actual finalization logic."""
    if state["current_match"]["status"] in ["live", "finished"]:
        end_match_and_update_phase()
    print_colored("\nPerforming finalization...", Fore.CYAN, Style.BRIGHT)
    try:
        _export_json()
        print_colored("Final state exported to JSON", Fore.GREEN)
    except Exception as e:
        logging.error(f"Error during final JSON export: {e}")
    try:
        save_all_time_players()
        print_colored("All-time player data saved", Fore.GREEN)
    except Exception as e:
        logging.error(f"Error saving all-time players: {e}")
    if simulation_manager:
        simulation_manager.stop()
        print_colored("Simulation stopped", Fore.YELLOW)
    if keep_server_running:
        print_colored("Server remains active for overlay access", Fore.GREEN)
        print_colored("Create 'force_shutdown.flag' file or press Ctrl+C to fully exit", Fore.CYAN)
    print_colored("Finalization complete", Fore.GREEN, Style.BRIGHT)
    with finalization_lock:
        finalization_requested = False

def signal_handler(signum, frame):
    """Enhanced signal handler for graceful shutdown."""
    global signal_received, complete_shutdown_requested, finalization_requested
    if signal_received:
        print_colored(f"\nForce exit requested (signal {signum} received again)", Fore.RED)
        os._exit(1)
    signal_received = True
    print_colored(f"\nReceived signal {signum}. Requesting complete shutdown...", Fore.YELLOW)
    with finalization_lock:
        complete_shutdown_requested = True
        finalization_requested = True
    shutdown_event.set()

def setup_signal_handlers():
    """Setup enhanced signal handlers for graceful shutdown."""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal_handler)
    if hasattr(signal, 'SIGQUIT'):
        signal.signal(signal.SIGQUIT, signal_handler)

def check_shutdown_conditions():
    """Check all possible shutdown conditions."""
    if shutdown_event.is_set():
        return True
    if should_finalize():
        return True
    force_end_file = Path("force_end.flag")
    if force_end_file.exists():
        print_colored("Force end flag detected. Requesting finalization...", Fore.RED)
        request_finalization()
        try:
            force_end_file.unlink()
        except:
            pass
        return True
    force_shutdown_file = Path("force_shutdown.flag")
    if force_shutdown_file.exists():
        print_colored("Force shutdown flag detected. Requesting complete shutdown...", Fore.RED)
        request_finalization()
        global complete_shutdown_requested
        with finalization_lock:
            complete_shutdown_requested = True
        try:
            force_shutdown_file.unlink()
        except:
            pass
        return True
    return False

def interruptible_sleep(duration, check_interval=0.1):
    """Sleep that can be interrupted by shutdown signals."""
    end_time = time.time() + duration
    while time.time() < end_time:
        if check_shutdown_conditions():
            return True
        remaining = end_time - time.time()
        sleep_time = min(check_interval, remaining)
        if sleep_time > 0:
            time.sleep(sleep_time)
    return False

def process_with_shutdown_check(log_files_to_process, parsed_logos):
    """Process files with shutdown checks and seamless catch-up for live file."""
    global in_catchup_processing, processed_files, buffer
    if not log_files_to_process:
        print_colored("No current phase logs to process.", Fore.WHITE)
        return None, 0

    # ignore files we've already fully processed
    log_files_to_process = [f for f in log_files_to_process if f.name not in processed_files]
    if not log_files_to_process:
        print_colored("All files already processed.", Fore.WHITE)
        return None, 0

    print_colored("Processing current-phase logs for catch-up...", Fore.CYAN)
    print_colored(f"Files to process: {len(log_files_to_process)}", Fore.WHITE)
    print_colored("Press Ctrl+C to interrupt processing...", Fore.YELLOW)
    log_files_to_process.sort(key=lambda f: f.stat().st_mtime)

    # Calculate total size for overall progress
    total_file_size = sum(f.stat().st_size for f in log_files_to_process if f.stat().st_size > 0)
    processed_size = 0

    # Determine if the last file is actively updating
    live_file = None
    if log_files_to_process:
        last_file = log_files_to_process[-1]
        if _is_log_updating(last_file, min_size=1024):
            live_file = last_file
            print_colored(f"Detected live file: {live_file.name}", Fore.GREEN)
        else:
            print_colored(f"Last file {last_file.name} is not updating; treating as completed match file", Fore.YELLOW)

    # Process all files (including last file if not live)
    for i, log_file in enumerate(log_files_to_process):
        if check_shutdown_conditions():
            print_colored(f"\nShutdown requested during file processing. Stopping at file {i+1}/{len(log_files_to_process)}", Fore.YELLOW)
            return None, 0

        file_size = log_file.stat().st_size
        if file_size == 0:
            print_colored(f"\nSkipping empty file {i+1}/{len(log_files_to_process)}: {log_file.name}", Fore.YELLOW)
            processed_files.add(log_file.name)
            continue

        is_live_file = (log_file == live_file)
        file_type = "live" if is_live_file else "completed match"
        print_colored(f"\nProcessing {file_type} file {i+1}/{len(log_files_to_process)}: {log_file.name}", Fore.CYAN if is_live_file else Fore.YELLOW)

        try:
            with open(log_file, "r", encoding="utf-8") as f:
                log_text = f.read()
                logging.info(f"Starting processing of file: {log_file.name}")
                chunk_size = max(1024, file_size // 100)
                bytes_processed_in_file = 0
                in_catchup_processing = True
                try:
                    for chunk_start in range(0, len(log_text), chunk_size):
                        if check_shutdown_conditions():
                            print_colored(f"\nShutdown requested during chunk processing. Stopping...", Fore.YELLOW)
                            return None, 0

                        chunk_end = min(chunk_start + chunk_size, len(log_text))
                        chunk = log_text[chunk_start:chunk_end]

                        # progress callback uses overall total_file_size (clamped in print_progress_bar)
                        parse_and_apply(chunk, parsed_logos=parsed_logos, mode="chunk", progress_callback=lambda processed, total: print_progress_bar(
                            processed_size + bytes_processed_in_file + processed,
                            total_file_size,
                            prefix=f"File {i+1}/{len(log_files_to_process)}",
                            suffix=f"{log_file.name}",
                            processing=True
                        ))

                        bytes_processed_in_file = chunk_end
                        print_progress_bar(
                            processed_size + bytes_processed_in_file,
                            total_file_size,
                            prefix=f"File {i+1}/{len(log_files_to_process)}",
                            suffix=f"{log_file.name}",
                            processing=True
                        )
                        time.sleep(0.01)

                    # flush buffer for this file
                    if not check_shutdown_conditions():
                        logging.info(f"Flushing remaining buffer for {log_file.name}")
                        # We DO NOT call finalization here for live file — only finalize non-live files below.
                        parse_and_apply('', parsed_logos=parsed_logos, mode="chunk")
                        # Finalize only for non-live (completed) files:
                        if not is_live_file and state["current_match"]["id"] and state["current_match"]["status"] in ["live", "finished"]:
                            logging.info(f"CATCHUP: Finalizing completed match at file end: {state['current_match']['id']} (from file {log_file.name})")
                            end_match_and_update_phase()
                finally:
                    # always leave catchup mode after processing the file chunks
                    in_catchup_processing = False
                    # keep the progress bar visually complete
                    print_colored("\nFinished processing file chunk(s)", Fore.CYAN)

                # For completed files, mark file as processed. IMPORTANT: do NOT mark the live file as processed
                if not is_live_file:
                    processed_files.add(log_file.name)
                processed_size += file_size

                print_progress_bar(
                    processed_size,
                    total_file_size,
                    prefix=f"File {i+1}/{len(log_files_to_process)}",
                    suffix=f"{log_file.name} complete"
                )

        except Exception as e:
            if check_shutdown_conditions():
                print_colored(f"\nShutdown requested during error handling", Fore.YELLOW)
                return None, 0
            logging.error(f"Error processing catch-up file {log_file.name}: {e}")
            processed_size += file_size
            processed_files.add(log_file.name)
            print_progress_bar(
                processed_size,
                total_file_size,
                prefix=f"File {i+1}/{len(log_files_to_process)}",
                suffix=f"{log_file.name} ERROR"
            )

        # If this was the live file, hand off to caller for real tailing
        if is_live_file:
            # last_pos = position at end of file (start tailing from here)
            last_pos = file_size
            # Important: clear buffer so parse_and_apply won't try to reparse already-processed snapshot
            buffer = ''
            print_colored("\n✓ Current phase catch-up complete for live file. Transitioning to live monitoring.", Fore.GREEN)
            print_progress_bar(processed_size, total_file_size, prefix="Catch-up", suffix="ALL FILES COMPLETE")
            return log_file, last_pos

    # No live file detected / nothing left
    print_colored("✓ Current phase catch-up complete! No live file detected.", Fore.GREEN)
    print_progress_bar(total_file_size, total_file_size, prefix="Catch-up", suffix="ALL FILES COMPLETE")
    return None, 0

def enhanced_main_loop(test_mode=False, team_logos=None, live_log_path=None, start_pos=0):
    """Enhanced main loop with proper shutdown handling, integrating live tailing if provided."""
    global buffer
    buffer = ''
    current_log_path = live_log_path  # Use the live path from catch-up if available
    last_pos = start_pos if live_log_path else 0
    last_json = 0
    last_term = 0
    MATCH_CHECK_INTERVAL = 0.1
    no_data_timeout = 5 if test_mode else 30
    last_data_time = time.time()
    last_warning_time = 0
    WARNING_INTERVAL = 60
    current_phase_logs = get_all_log_files(CURRENT_LOG_DIR)
    if not current_log_path and current_phase_logs:
        current_log_path = current_phase_logs[-1]
        last_pos = current_log_path.stat().st_size
        print_colored(f"✓ Live monitoring started on: {current_log_path.name}", Fore.GREEN)
    print_colored(f"\nStarting live monitoring... (Press Ctrl+C to stop)", Fore.CYAN, Style.BRIGHT)
    try:
        while not check_shutdown_conditions():
            now = time.time()
            if state["current_match"]["status"] == "live" and state["current_match"]["id"]:
                alive_teams = [t for t in state["current_match"]["teams"].values() if t["liveMembers"] > 0]
                if len(alive_teams) <= 1:
                    logging.info(f"LIVE: Match end detected. Finalizing match ID: {state['current_match']['id']}")
                    end_match_and_update_phase()
                    buffer = ''
                    continue
            log_was_updated = False
            if current_log_path and current_log_path.exists():
                size = _file_size(current_log_path)
                if size > last_pos:
                    chunk, new_pos = _read_new(current_log_path, last_pos)
                    if chunk:
                        old_buffer_len = len(buffer)
                        parse_and_apply(chunk, parsed_logos=team_logos, mode="chunk")
                        last_pos = new_pos
                        log_was_updated = True
                        last_data_time = now
                        logging.debug(f"LIVE: Processed {len(chunk)} bytes (buffer: {old_buffer_len} -> {len(buffer)})")
                else:
                    if now - last_data_time > no_data_timeout and state["current_match"]["status"] == "live":
                        alive_teams = [t for t in state["current_match"]["teams"].values() if t["liveMembers"] > 0]
                        if len(alive_teams) <= 1:
                            logging.info(f"LIVE: No new data for {no_data_timeout}s and match ended - forcing finalization of match {state['current_match']['id']}")
                            end_match_and_update_phase()
                            buffer = ''
                            last_data_time = now
                        else:
                            if now - last_warning_time > WARNING_INTERVAL:
                                logging.warning(f"LIVE: No new data for {now - last_data_time:.1f}s but {len(alive_teams)} teams still alive - waiting for more data to complete match {state['current_match']['id']}")
                                last_warning_time = now
            all_current_logs = get_all_log_files(CURRENT_LOG_DIR)
            if all_current_logs and all_current_logs[-1] != current_log_path:
                print_colored(f"\nNew live log detected: {all_current_logs[-1].name}", Fore.YELLOW)
                buffer = ''
                current_log_path = all_current_logs[-1]
                last_pos = 0
                log_was_updated = True
                last_data_time = now
            
            if now - last_json >= UPDATE_INTERVAL:
                _export_json()
                last_json = now
            
            if now - last_term >= 1.0:
                _print_terminal_snapshot(test_mode)
                last_term = now
            
            if interruptible_sleep(MATCH_CHECK_INTERVAL):
                break
                
    except KeyboardInterrupt:
        print_colored("\nKeyboard interrupt received", Fore.YELLOW)
        if buffer:
            logging.info(f"LIVE: Flushing final {len(buffer)} bytes from buffer on exit")
            parse_and_apply('', parsed_logos=team_logos, mode="chunk")
        if state["current_match"]["status"] == "live" and state["current_match"]["id"]:
            alive_teams = [t for t in state["current_match"]["teams"].values() if t["liveMembers"] > 0]
            if len(alive_teams) <= 1:
                logging.info("Force finalizing on interrupt (match ended)")
                end_match_and_update_phase()
                _finalize_and_persist()
            else:
                logging.warning("Match incomplete on interrupt (multiple teams alive) - skipping finalization to avoid partial stats")
        buffer = ''
    except Exception as e:
        logging.exception(f"Live monitor error: {e}")
        if buffer:
            logging.info(f"LIVE: Flushing {len(buffer)} bytes from buffer due to error")
            parse_and_apply('', parsed_logos=team_logos, mode="chunk")
        if state["current_match"]["status"] == "live":
            logging.info("Force finalizing on error")
            end_match_and_update_phase()
            _finalize_and_persist()
            buffer = ''
    
    print_colored("Exiting main monitoring loop...", Fore.YELLOW)

def main(test_mode=False, reprocess=False):
    """Main function to run the live monitor with enhanced finalization."""
    global simulation_manager, expected_teams, buffer

    # Setup signal handlers for graceful shutdown
    setup_signal_handlers()

    # Ensure directories exist
    ensure_directories()

    # Print status
    mode = "Test" if test_mode else "Production"
    print_status_header(mode)

    # Start web server early
    if WEBSERVER_AVAILABLE:
        try:
            webserver.start_server()
            print_colored(f"Web server started on http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}", Fore.GREEN)
        except Exception as e:
            print_colored(f"Failed to start web server: {e}", Fore.RED)

    # Load team logos and expected_teams (filtered non-placeholders, keyed by name)
    # ini_path = r"C:\Users\Baghila\AppData\Local\ShadowTrackerExtra\Saved\TeamLogoAndColor.ini"
    ini_path = ".\TeamLogoAndColor.ini"
    if reprocess:
        team_logos = {}
        expected_teams = {}
        logging.info("Reprocess mode: Skipping expected_teams load from INI")
    else:
        if os.path.exists(ini_path):
            team_logos = get_team_logos(ini_path)
            expected_teams = team_logos  # Keyed by name
            logging.info(f"Loaded {len(expected_teams)} expected teams from INI: {[(name, info['logoPath']) for name, info in expected_teams.items()]}")
            if state["current_match"]["teams"]:
                current_team_names = {team["name"] for team in state["current_match"]["teams"].values()}
                missing_names = set(expected_teams.keys()) - current_team_names
                if missing_names:
                    logging.warning(f"Teams in INI but not in current match: {missing_names}")
        else:
            team_logos = {}
            expected_teams = {}
            logging.warning("INI file not found; no expected teams loaded")

    # Handle reprocess mode (archives only)
    if reprocess:
        print_colored("Reprocessing all archived data...", Fore.MAGENTA, Style.BRIGHT)
        process_archives_for_all_time(team_logos, force_repopulate=True)
        print_colored("Reprocessing complete!", Fore.GREEN)
        return

    # File setup confirmation
    action = confirm_file_setup()

    # Process all-time players (archives only, no phase impact)
    if action == "reprocess":
        process_archives_for_all_time(team_logos, force_repopulate=True)
    else:
        process_archives_for_all_time(team_logos, force_repopulate=False)

    # Start simulation if in test mode (before catch-up/live monitoring)
    if test_mode:
        print_colored("Starting background simulation...", Fore.YELLOW)
        simulation_manager = SimulationManager(quiet=True)
        if not simulation_manager.start():
            print_colored("Failed to start simulation. Check test log files.", Fore.RED)
            return
        print_colored("✓ Simulation running in background", Fore.GREEN)
        time.sleep(0.5)

    # CATCH-UP: Process ALL current phase files with enhanced progress tracking
    current_phase_logs = get_all_log_files(CURRENT_LOG_DIR, exclude_live_log=False)
    live_log_path = None
    start_pos = 0
    if current_phase_logs:
        print_colored(f"\nCatching up on {len(current_phase_logs)} current phase file(s)...", Fore.CYAN)
        print_colored("Starting catch-up processing with progress tracking...", Fore.YELLOW)
        
        live_log_path, start_pos = process_with_shutdown_check(current_phase_logs, team_logos)
        if live_log_path:
            print_colored(f"Transitioning to live monitoring on {live_log_path.name}...", Fore.GREEN)
        else:
            print_colored("No live log file detected after catch-up.", Fore.WHITE)

    # Now run the main live loop (which will use live_log_path if available)
    try:
        print_colored(f"\nStarting {mode.lower()} monitoring...", Fore.CYAN, Style.BRIGHT)
        if test_mode:
            print_colored(f"Monitoring simulated log: {SIMULATED_LOG_FILE}", Fore.YELLOW)
        else:
            print_colored(f"Monitoring live log: {live_log_path.name if live_log_path else 'None'}", Fore.GREEN)

        enhanced_main_loop(test_mode=test_mode, team_logos=team_logos, live_log_path=live_log_path, start_pos=start_pos)

    except KeyboardInterrupt:
        print_colored("\nStopped by user (Ctrl+C).", Fore.YELLOW)
        if buffer:
            logging.info(f"Flushing buffer before finalization: {len(buffer)} bytes")
            parse_and_apply('', parsed_logos=team_logos, mode="chunk")
            buffer = ''
        request_finalization()
        
    except Exception as e:
        logging.exception(f"Live monitor error: {e}")
        if buffer:
            logging.info(f"Flushing buffer before finalization: {len(buffer)} bytes")
            parse_and_apply('', parsed_logos=team_logos, mode="chunk")
            buffer = ''
        request_finalization()
    
    finally:
        # Force final cleanup on exit if there's lingering match data
        if (state["current_match"]["id"] or 
            state["current_match"]["teams"] or 
            state["current_match"]["killFeed"] or 
            state["current_match"]["eliminationOrder"] or 
            state["current_match"]["players"] or 
            state["current_match"]["leaderboards"]["currentMatchTopPlayers"] or 
            state["current_match"]["activePlayers"] or 
            state["current_match"]["teamKills"]):
            logging.info("Forcing finalization on exit for lingering match data")
            end_match_and_update_phase()
            _finalize_and_persist()
        
        perform_finalization(keep_server_running=True)
        setup_force_end_thread()
        server_only_mode()
        
if __name__ == "__main__":
    main()
