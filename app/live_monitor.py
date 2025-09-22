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

# Global set to track processed files (add this at the top of your script if not already there)
processed_files = set()
# processed_snapshot_positions = set()  # Track processed snapshot starts to avoid duplicates


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
    """Improved progress bar with better visual feedback."""
    if total == 0:
        return
    
    fraction = current / total
    filled_length = int(bar_length * fraction)
    
    # Different bar styles for different states
    if processing:
        # Show active processing with animated character
        spin_chars = ['|', '/', '-', '\\']
        spin_index = int(time.time() * 8) % len(spin_chars)  # Faster spin
        bar = "█" * filled_length + ">" + "░" * (bar_length - filled_length - 1)
        if filled_length < bar_length:
            bar = bar[:filled_length] + spin_chars[spin_index] + bar[filled_length + 1:]
    else:
        # Static bar for completed states
        bar = "█" * filled_length + ">" + "░" * (bar_length - filled_length - 1)
        if filled_length >= bar_length:
            bar = "█" * bar_length + ">"
    
    percent = int(fraction * 100)
    
    # Color coding based on progress
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

    # Flatten the players dictionary into a list
    all_players = list(players_dict.values())

    # Sort players by kills, then damage, then knockouts
    all_players.sort(key=lambda p: (
        p.get("stats", {}).get("kills", 0),
        p.get("stats", {}).get("damage", 0),
        p.get("stats", {}).get("knockouts", 0)
    ), reverse=True)

    # Get the top 5 players
    for player in all_players[:5]:
        player_stats = player.get("stats", {})
        team_id = player.get("teamId")
        team_name = teams_dict.get(team_id, {}).get("name", "Unknown Team")  # Fixed: use "name" not "teamName"

        top_players.append({
            "playerId": player["id"],
            "name": player["name"],
            "teamName": team_name,
            "totalKills": player_stats.get("kills", 0),
            "totalDamage": int(player_stats.get("damage", 0)),
            "totalKnockouts": player_stats.get("knockouts", 0),
            "totalMatches": 1 # This is per-match data, so matches = 1
        })
    return top_players

def _finalize_and_persist():
    """Finalizes match data and resets current live state."""
    if state["current_match"]["id"] and state["match_state"]["status"] == "live":
        logging.info(f"Finalizing and persisting match ID: {state['current_match']['id']}")
        
        # Deep copy the current match state
        final_match_data = copy.deepcopy(state["current_match"])
        
        # Recalculate standings for the final state
        end_match_and_update_phase(final_match_data)
        logging.info("Match standings have been updated.")
        
        # Always add to matches history in live mode (skip only during archive/catchup)
        if not in_archive_processing and not in_catchup_processing:
            if final_match_data["id"] not in state["processed_matches"]:
                state["matches"].append(final_match_data)
                state["processed_matches"].add(final_match_data["id"])
                logging.info(f"LIVE: Added match {final_match_data['id']} to history. Total matches: {len(state['matches'])}")
            else:
                logging.warning(f"LIVE: Skipped duplicate match {final_match_data['id']}")
        
        # Full reset of live match state
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
        state["match_state"]["status"] = "idle"
        state["match_state"]["last_updated"] = int(time.time())
        logging.info("Live match state fully reset to idle.")
    else:
        logging.warning("Finalization requested but no active match ID or in processing mode. Skipping.")
        
        # Ensure full reset if no ID
        if not state["current_match"]["id"]:
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
            state["match_state"]["status"] = "idle"
            state["match_state"]["last_updated"] = int(time.time())
            logging.info("Forced full reset due to no active match ID.")

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
    teams = {}
    for line in config_string.strip().splitlines():
        m = re.search(r'TeamLogoAndColor=\(TeamNo=(\d+),TeamName=([^,]+),TeamLogoPath=([^,]+)', line)
        if m:
            teams[str(int(m.group(1)))] = {"name": m.group(2), "logoPath": m.group(3)}
    return teams

def get_team_logos(ini_file_path):
    """Parse team logos and names from INI file."""
    try:
        with open(ini_file_path, "r", encoding="utf-8") as fh:
            ini_content = fh.read()
            ini_match = INI_BLOCK.search(ini_content)
            if ini_match:
                return _parse_ini(ini_match.group(1))
            else:
                logging.warning("Team logos INI block not found")
    except FileNotFoundError:
        logging.error(f"INI file not found at {ini_file_path}")
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
            if hasattr(directory, 'mkdir'):  # Path object
                directory.mkdir(parents=True, exist_ok=True)
            else:  # String path
                Path(directory).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.warning(f"Could not create directory {directory}: {e}")

    # Also ensure the parent directory of output files exists
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
        # Full mode - process entire text, ignore buffer
        snapshots = extract_snapshots(log_text)
        total_snapshots = len(snapshots)
        
        for idx, snap in enumerate(snapshots):
            process_snapshot(snap, parsed_logos)
            snapshots_processed += 1
            
            # Update progress after every snapshot
            if progress_callback and total_snapshots > 0:
                processed_bytes = (idx + 1) / total_snapshots * len(log_text)
                progress_callback(processed_bytes, len(log_text))
                
    else:
        # Chunk mode - append to buffer and process new snapshots
        buffer_start_len = len(buffer)
        if log_text:  # Only append if there's actual content
            buffer += log_text
        
        snapshots = extract_snapshots(buffer)
        new_snapshots = len(snapshots)
        
        for snap in snapshots:
            process_snapshot(snap, parsed_logos)
            snapshots_processed += 1
        
        # Aggressively clear ALL processed snapshots from buffer
        if snapshots:
            # Find the end of the last processed snapshot and truncate
            last_end = 0
            for snap in snapshots:
                snap_start = buffer.find(snap)
                if snap_start >= 0:
                    last_end = max(last_end, snap_start + len(snap))
            
            # Truncate buffer to remove all processed content
            if last_end > 0:
                buffer = buffer[last_end:]
                logging.debug(f"Processed {new_snapshots} snapshots, buffer truncated to {len(buffer)} bytes")
            else:
                # If no snapshots were found in buffer, clear it entirely
                buffer = ''
                logging.debug("No valid snapshots found; buffer cleared")
        else:
            # No snapshots processed, clear buffer to prevent accumulation
            buffer = ''
            logging.debug("No snapshots; buffer cleared")
        
        # Final progress update for chunk mode
        if progress_callback and log_text:
            progress_callback(len(log_text), len(log_text))
    
    logging.debug(f"parse_and_apply: processed {snapshots_processed} snapshots (buffer was {buffer_start_len}, now {len(buffer)})")

def process_snapshot(snap_text, parsed_logos):
    # Define mode early for consistent logging (e.g., in skips)
    finalization_mode = "CATCHUP" if in_catchup_processing else "ARCHIVE" if in_archive_processing else "LIVE"
    
    gid_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", snap_text)
    new_game_id = gid_match.group(1) if gid_match else None

    if new_game_id and new_game_id in state["processed_matches"]:
        logging.info(f"{finalization_mode}: Skipping snapshot for already processed match {new_game_id}")
        return  # Exit early, don't process this snapshot

    if new_game_id and state["current_match"]["id"] and new_game_id != state["current_match"]["id"]:
        # Finalize previous match if it exists and is active
        if state["current_match"]["status"] in ["live", "finished"]:
            logging.info(f"{finalization_mode}: Finalizing previous match {state['current_match']['id']} due to new ID {new_game_id}")
            final_match_data = copy.deepcopy(state["current_match"])
            
            # Check for duplicate before updating phase and adding
            if final_match_data["id"] not in state["processed_matches"]:
                end_match_and_update_phase(final_match_data)
                state["matches"].append(final_match_data)
                state["processed_matches"].add(final_match_data["id"])
                logging.info(f"{finalization_mode}: Added match {final_match_data['id']} to history")
            else:
                logging.warning(f"{finalization_mode}: Skipped duplicate match {final_match_data['id']}")
            
            # Only persist in live mode
            if not in_archive_processing and not in_catchup_processing:
                _finalize_and_persist()
        else:
            logging.debug(f"{finalization_mode}: Skipping non-active match {state['current_match']['id']}")
        
        # Reset for new match
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

    # Check if match has ended (only for live processing)
    if state["current_match"]["status"] == "finished" and not in_archive_processing and not in_catchup_processing:
        end_match_and_update_phase()

    # Only update phase from live matches
    if not in_archive_processing and not in_catchup_processing:
        _update_phase_from_live_match()

    state["match_state"]["status"] = "live" if state["current_match"]["id"] else "idle"
    state["match_state"]["last_updated"] = int(time.time())

def _reset_match_but_keep_id(new_id=None):
    """Reset match state but keep the ID if provided."""
    if new_id:
        # Reset everything but keep the new ID
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
        # Full clean reset when no ID provided
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
    tid = str(t.get("teamId") or "")
    if not tid or tid == "None":
        return
        
    team = state["current_match"]["teams"].setdefault(tid, {
        "id": tid,
        "name": t.get("teamName") or "Unknown Team",
        "logo": DEFAULT_TEAM_LOGO,
        "liveMembers": 0,
        "kills": 0,
        "placementPointsLive": 0,
        "players": []
    })
    team.setdefault("placementPointsLive", 0)

    team_name = t.get("teamName") or team["name"]
    team["name"] = team_name
    _register_team_mapping(tid, team_name)
    
    if parsed_logos and tid in parsed_logos:
        logo_info = parsed_logos[tid]
        team["logo"] = get_asset_url(logo_info["logoPath"], DEFAULT_TEAM_LOGO)

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
    if final_match_data is None:
        final_match_data = copy.deepcopy(state["current_match"])

    if not final_match_data["id"]:
        logging.warning("end_match_and_update_phase called but no match ID is set.")
        return

    print_colored(f"\nFinalizing match {final_match_data['id']}", Fore.CYAN, Style.BRIGHT)

    # Make working copies of the data for processing
    current_match_teams = final_match_data["teams"].copy()
    current_match_players = final_match_data["players"].copy()
    
    # Mark this match as processed
    state["processed_matches"].add(final_match_data["id"])
    
    # Find the winner team (team with live members > 0)
    winner_team_id = None
    for tid, team in current_match_teams.items():
        if team.get("liveMembers", 0) > 0:
            winner_team_id = tid
            break
    
    # Get elimination order and all team names
    elimination_order = final_match_data["eliminationOrder"]
    all_team_names = [team["name"] for team in current_match_teams.values()]
    
    logging.info(f"Winner: {current_match_teams[winner_team_id]['name'] if winner_team_id else 'None'}")
    logging.info(f"Elimination order: {elimination_order}")
    
    # Create placement ranking based on elimination order (reverse: last eliminated = best placement)
    # Winner gets 1st place, then teams are ranked by reverse elimination order
    placement_ranking = []
    
    # Add winner as 1st place
    if winner_team_id:
        winner_team = current_match_teams[winner_team_id]
        placement_ranking.append({
            "team_id": winner_team_id,
            "team_name": winner_team["name"],
            "elimination_position": 0,  # Winner not eliminated
            "rank": 1
        })
        logging.info(f"Rank 1: {winner_team['name']} (Winner)")
    
    # Add eliminated teams in reverse elimination order (last eliminated = highest rank)
    for elim_pos, team_name in enumerate(reversed(elimination_order), 1):
        # Find the team ID for this team name
        team_id = None
        for tid, team in current_match_teams.items():
            if team["name"] == team_name:
                team_id = tid
                break
        
        if team_id:
            rank = len(placement_ranking) + 1  # Current rank position
            placement_ranking.append({
                "team_id": team_id,
                "team_name": team_name,
                "elimination_position": len(elimination_order) - elim_pos + 1,  # Reverse position
                "rank": rank
            })
            logging.info(f"Rank {rank}: {team_name} (eliminated {elim_pos}th from end)")
    
    # Assign placement points based on final ranking
    final_placement = {}
    for ranking in placement_ranking:
        rank = ranking["rank"]
        placement_pts = PLACEMENT_POINTS.get(rank, 0)
        final_placement[ranking["team_id"]] = placement_pts
        logging.info(f"Team {ranking['team_name']} ranked {rank} with {placement_pts} placement points")
    
    # Update the final match data with placement points
    for tid, placement_pts in final_placement.items():
        if tid in final_match_data["teams"]:
            final_match_data["teams"][tid]["placementPointsLive"] = placement_pts
            logging.info(f"Set {final_match_data['teams'][tid]['name']} placementPointsLive = {placement_pts}")
    
    # Add placement ranking to match data for debugging
    final_match_data["placementRanking"] = placement_ranking
    
    # *** FIX 1: ENSURE ALL TEAMS EXIST IN PHASE BEFORE AWARDING WWCD ***
    # First, initialize all teams in phase state (this happens BEFORE WWCD awarding)
    for team_id, team_data in current_match_teams.items():
        team_name = team_data["name"]
        
        # Ensure team exists in phase BEFORE any WWCD logic
        if team_name not in state["phase"]["teams"]:
            state["phase"]["teams"][team_name] = {
                "id": team_id,
                "name": team_name,
                "logo": team_data.get("logo", DEFAULT_TEAM_LOGO),
                "totals": {"kills": 0, "placementPoints": 0, "points": 0, "wwcd": 0}
            }
            logging.info(f"Initialized phase team: {team_name}")

    # *** FIX 2: NOW award WWCD to winner (all teams now exist in phase) ***
    if winner_team_id:
        winner_name = current_match_teams[winner_team_id]["name"]
        if winner_name in state["phase"]["teams"]:
            phase_winner_team = state["phase"]["teams"][winner_name]
            phase_winner_team["totals"]["wwcd"] += 1
            logging.info(f"Awarded WWCD to {winner_name} (total WWCD: {phase_winner_team['totals']['wwcd']})")
        else:
            logging.warning(f"WWCD winner {winner_name} not found in phase teams!")

    # Update phase totals for teams - ONLY for this specific match
    for team_id, team_data in current_match_teams.items():
        team_name = team_data["name"]
        phase_team = state["phase"]["teams"][team_name]  # Guaranteed to exist now
        
        # Add this match's kills
        match_kills = team_data.get("kills", 0)
        phase_team["totals"]["kills"] += match_kills
        
        # Add this match's placement points
        placement_pts = final_placement.get(team_id, 0)
        phase_team["totals"]["placementPoints"] += placement_pts
        
        # Recalculate total points
        phase_team["totals"]["points"] = phase_team["totals"]["kills"] + phase_team["totals"]["placementPoints"]
        
        logging.info(f"Updated {team_name}: +{match_kills} kills, +{placement_pts} placement (total: {phase_team['totals']['points']} pts)")

    # Update phase totals for players - ONLY for this specific match
    for player_id, player_data in current_match_players.items():
        team_name = _get_team_name_by_id(player_data["teamId"]) or "Unknown Team"
        
        # Ensure player exists in phase
        if player_id not in state["phase"]["players"]:
            state["phase"]["players"][player_id] = {
                "id": player_id,
                "name": player_data["name"],
                "photo": player_data.get("photo", DEFAULT_PLAYER_PHOTO),
                "teamName": team_name,
                "live": {"isAlive": False, "health": 0, "healthMax": 100},
                "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
            }
        
        phase_player = state["phase"]["players"][player_id]
        
        # Add this match's stats
        phase_player["totals"]["kills"] += player_data["stats"]["kills"]
        phase_player["totals"]["damage"] += player_data["stats"]["damage"]
        phase_player["totals"]["knockouts"] += player_data["stats"]["knockouts"]
        phase_player["totals"]["matches"] += 1

    # *** FIX 3: FULL RESET OF CURRENT MATCH STATE ***
    # Reset current match state for next match
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
    state["match_state"]["status"] = "idle"
    state["match_state"]["last_updated"] = int(time.time())
    _cleanup_old_team_mappings()
    
    logging.info(f"Match {final_match_data['id']} finalization complete. {len(current_match_teams)} teams processed.")
    
def _print_terminal_snapshot(test_mode=False):
    """Enhanced terminal output with colors and simulation progress."""
    m = state["current_match"]
    
    # Clear screen for better visual experience
    os.system('cls' if os.name == 'nt' else 'clear')
    
    # Header
    mode_text = "TEST MODE" if test_mode else "LIVE MODE"
    mode_color = Fore.YELLOW if test_mode else Fore.GREEN
    
    print_colored("╔" + "═" * 58 + "╗", Fore.BLUE)
    print_colored(f"║{' ' * 20}PUBG LIVE SCOREBOARD{' ' * 19}║", Fore.CYAN, Style.BRIGHT)
    print_colored(f"║{' ' * 15}{mode_text} - {datetime.datetime.now().strftime('%H:%M:%S')}{' ' * (42 - len(mode_text))}║", mode_color)
    print_colored("╠" + "═" * 58 + "╣", Fore.BLUE)
    
    # Match info
    match_id = m['id'] or 'waiting...'
    status = m['status']
    status_color = Fore.GREEN if status == "live" else Fore.YELLOW if status == "finished" else Fore.WHITE
    
    print_colored(f"║ Match: {match_id:<20} Status: ", Fore.WHITE, end="")
    print_colored(f"{status:<15} ║", status_color)
    
    # Simulation progress (if in test mode)
    if test_mode and simulation_manager:
        progress_str = simulation_manager.get_progress_string()
        print_colored(f"║ Simulation: {progress_str:<38} ║", Fore.CYAN)
    
    print_colored("╠" + "═" * 58 + "╣", Fore.BLUE)
    
    # Teams table
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
    
    # Fill remaining rows
    for _ in range(max(0, 8 - len(rows))):
        print_colored("║" + " " * 58 + "║", Fore.WHITE)
    
    print_colored("╠" + "═" * 58 + "╣", Fore.BLUE)
    
    # Kill feed
    print_colored("║ RECENT KILLS" + " " * 46 + "║", Fore.RED, Style.BRIGHT)
    kill_feed = m["killFeed"][-4:]
    for kill in kill_feed:
        kill_display = kill[:56] if len(kill) <= 56 else kill[:53] + "..."
        print_colored(f"║ {kill_display:<56} ║", Fore.YELLOW)
    
    for _ in range(max(0, 4 - len(kill_feed))):
        print_colored("║" + " " * 58 + "║", Fore.WHITE)
    
    print_colored("╚" + "═" * 58 + "╝", Fore.BLUE)
    
    # Phase standings
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
                "leaderboards": {
                    "currentMatchTopPlayers": _current_match_top_players()
                },
                # Convenience views for frontend
                "activePlayers": _get_active_players(),
                "teamKills": _get_team_kills()
            },
            "matches": state["matches"]  # Complete match history
        }
        with open(OUTPUT_JSON, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error during JSON export: {e}")

# ---------- File Processing Functions ----------
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
    """Load all-time player data from JSON file."""
    if ALL_TIME_PLAYERS_JSON.exists():
        try:
            with open(ALL_TIME_PLAYERS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                players_data = data.get("players", {})
                
                clean_players = {}
                for pid, pdata in players_data.items():
                    if isinstance(pdata, dict) and "totals" in pdata and isinstance(pdata["totals"], dict):
                        clean_players[pid] = pdata
                    else:
                        logging.warning(f"Removing malformed player data for {pid}")
                
                state["all_time"]["players"] = clean_players
                logging.info(f"Loaded {len(clean_players)} all-time players")
                return True
        except Exception as e:
            logging.error(f"Error loading all-time player data: {e}")
    return False

def save_all_time_players():
    """Save all-time player data to JSON file."""
    try:
        with open(ALL_TIME_PLAYERS_JSON, "w", encoding="utf-8") as f:
            json.dump({"players": state["all_time"]["players"]}, f, indent=2, ensure_ascii=False)
        logging.info(f"Saved all-time player data")
    except Exception as e:
        logging.error(f"Error saving all-time player data: {e}")

def apply_archived_file_to_all_time(log_text, parsed_logos):
    """Apply archived log data to all-time statistics."""
    global in_archive_processing
    temp_before = state["current_match"].copy()
    temp_mapping_before = state["teamNameMapping"].copy()
    
    in_archive_processing = True
    try:
        parse_and_apply(log_text, parsed_logos=parsed_logos, mode="full")
    finally:
        in_archive_processing = False
    
    # Only update all-time players from the final state, don't touch phase
    for pid, pl in state["current_match"]["players"].items():
        team_name = _get_team_name_by_id(pl.get("teamId")) or "Unknown Team"
        
        at = state["all_time"]["players"].setdefault(pid, {
            "id": pid,
            "name": pl["name"],
            "teamName": team_name,
            "photo": pl.get("photo") or DEFAULT_PLAYER_PHOTO,
            "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
        })
        
        at["name"] = pl["name"]
        at["photo"] = pl.get("photo") or at["photo"]
        at["teamName"] = team_name
        at["totals"]["kills"] += int(pl["stats"]["kills"])
        at["totals"]["damage"] += int(pl["stats"]["damage"])
        at["totals"]["knockouts"] += int(pl["stats"]["knockouts"])
        at["totals"]["matches"] += 1

    # Restore state - phase updates during processing are ignored due to flag
    state["current_match"].update(temp_before)
    state["teamNameMapping"] = temp_mapping_before

def process_archives_for_all_time(parsed_logos, force_repopulate=False):
    """Process archived logs for all-time statistics."""
    if force_repopulate:
        print_colored("Force reprocessing all-time player data...", Fore.MAGENTA)
    elif load_all_time_players():
        print_colored("Using cached all-time player data.", Fore.GREEN)
        return

    print_colored("Processing archived logs for all-time statistics...", Fore.CYAN)
    archived_logs = get_all_log_files(ARCHIVE_LOG_DIR, exclude_live_log=False)
    
    if not archived_logs:
        print_colored("No archived logs found.", Fore.YELLOW)
        return

    print_colored(f"Found {len(archived_logs)} archived logs to process.", Fore.WHITE)
    
    # Calculate total size for progress bar
    total_file_size = sum(f.stat().st_size for f in archived_logs)
    processed_size = 0
    
    for i, f in enumerate(archived_logs):
        file_size = f.stat().st_size
        print_colored(f"\nProcessing archive file {i+1}/{len(archived_logs)}: {f.name}", Fore.YELLOW)
        
        try:
            with open(f, "r", encoding="utf-8") as fh:
                log_text = fh.read()
                
                # Show progress bar for this file
                print_progress_bar(processed_size, total_file_size, prefix=f"Archive {i+1}/{len(archived_logs)}", suffix=f"{f.name}")
                
                apply_archived_file_to_all_time(log_text, parsed_logos)
                
                # Update processed size
                processed_size += file_size
                
                # Update progress bar after processing
                print_progress_bar(processed_size, total_file_size, prefix=f"Archive {i+1}/{len(archived_logs)}", suffix=f"{f.name} complete")
                
        except Exception as e:
            logging.warning(f"Archive error for {f}: {e}")
            processed_size += file_size  # Still count as processed

    # Final progress bar
    print_progress_bar(total_file_size, total_file_size, prefix="Archive", suffix="PROCESSING COMPLETE")
    save_all_time_players()
    print_colored("All-time processing complete.", Fore.GREEN)

def process_current_phase_files(log_files_to_process, parsed_logos):
    """Process current phase log files for catch-up with real-time progress tracking."""
    global in_catchup_processing, processed_files
    
    if not log_files_to_process:
        print_colored("No current phase logs to process.", Fore.WHITE)
        return
        
    # Filter out already processed files
    log_files_to_process = [f for f in log_files_to_process if f.name not in processed_files]
    if not log_files_to_process:
        print_colored("All files already processed.", Fore.WHITE)
        return
        
    print_colored("Processing current-phase logs for catch-up...", Fore.CYAN)
    print_colored(f"Files to process: {len(log_files_to_process)}", Fore.WHITE)

    # Sort files by modification time (oldest first)
    log_files_to_process.sort(key=lambda f: f.stat().st_mtime)
    
    # Calculate total size for progress tracking
    total_file_size = sum(f.stat().st_size for f in log_files_to_process)
    processed_size = 0
    
    for i, log_file in enumerate(log_files_to_process):
        is_last_file = (i == len(log_files_to_process) - 1)
        file_size = log_file.stat().st_size
        
        if not is_last_file:
            print_colored(f"\nProcessing completed match file {i+1}/{len(log_files_to_process)}: {log_file.name}", Fore.YELLOW)
        else:
            print_colored(f"\nProcessing live file {i+1}/{len(log_files_to_process)} for catch-up: {log_file.name}", Fore.CYAN)
        
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                log_text = f.read()
                
                # Log file start
                logging.info(f"Starting processing of file: {log_file.name}")
                
                # Process in chunks
                chunk_size = max(1024, file_size // 50)
                bytes_processed_in_file = 0
                
                in_catchup_processing = True
                try:
                    # Process the file in chunks for better progress tracking
                    for chunk_start in range(0, len(log_text), chunk_size):
                        chunk_end = min(chunk_start + chunk_size, len(log_text))
                        chunk = log_text[chunk_start:chunk_end]
                        
                        # Process chunk with progress callback
                        parse_and_apply(chunk, parsed_logos=parsed_logos, mode="chunk", progress_callback=lambda processed, total: print_progress_bar(
                            processed_size + bytes_processed_in_file + processed,
                            total_file_size,
                            prefix=f"File {i+1}/{len(log_files_to_process)}",
                            suffix=f"{log_file.name}",
                            processing=True
                        ))
                        
                        # Update progress
                        bytes_processed_in_file = chunk_end
                        print_progress_bar(
                            processed_size + bytes_processed_in_file,
                            total_file_size,
                            prefix=f"File {i+1}/{len(log_files_to_process)}",
                            suffix=f"{log_file.name}",
                            processing=True
                        )
                        time.sleep(0.01)
                    
                    # Flush any remaining buffer without re-processing the whole file
                    logging.info(f"Flushing remaining buffer for {log_file.name}")
                    parse_and_apply('', parsed_logos=parsed_logos, mode="chunk", progress_callback=lambda processed, total: print_progress_bar(
                        processed_size + bytes_processed_in_file + processed,
                        total_file_size,
                        prefix=f"File {i+1}/{len(log_files_to_process)}",
                        suffix=f"{log_file.name}",
                        processing=True
                    ))
                    
                    # Finalize any completed matches
                    if state["current_match"]["id"] and state["current_match"]["status"] in ["live", "finished"]:
                        logging.info(f"CATCHUP: Finalizing match at file end: {state['current_match']['id']}")
                        final_match_data = copy.deepcopy(state["current_match"])
                        
                        # Check for duplicate before updating phase and adding
                        if final_match_data["id"] not in state["processed_matches"]:
                            end_match_and_update_phase(final_match_data)
                            state["matches"].append(final_match_data)
                            state["processed_matches"].add(final_match_data["id"])
                            logging.info(f"CATCHUP: Added match {final_match_data['id']} to history")
                        else:
                            logging.warning(f"CATCHUP: Skipped duplicate match {final_match_data['id']}")
                        
                        # Reset only for non-last file
                        if not is_last_file:
                            state["current_match"] = {
                                "id": None, "status": "idle", "winnerTeamId": None,
                                "winnerTeamName": None, "eliminationOrder": [], "killFeed": [],
                                "teams": {}, "players": {}
                            }
                        
                finally:
                    in_catchup_processing = False
                    
                # Update processed size and mark file as processed
                processed_size += file_size
                processed_files.add(log_file.name)
                
                # Show completed progress
                print_progress_bar(
                    processed_size,
                    total_file_size,
                    prefix=f"File {i+1}/{len(log_files_to_process)}",
                    suffix=f"{log_file.name} complete"
                )
                        
        except Exception as e:
            logging.error(f"Error processing catch-up file {log_file.name}: {e}")
            processed_size += file_size
            processed_files.add(log_file.name)
            print_progress_bar(
                processed_size,
                total_file_size,
                prefix=f"File {i+1}/{len(log_files_to_process)}",
                suffix=f"{log_file.name} ERROR"
            )

    # Final progress
    print_progress_bar(total_file_size, total_file_size, prefix="Catch-up", suffix="ALL FILES COMPLETE")
    print_colored("✓ Current phase catch-up complete!", Fore.GREEN)

def confirm_file_setup():
    """Display detected files and ask user for confirmation."""
    print_colored("\n" + "="*60, Fore.CYAN)
    print_colored("FILE SETUP CONFIRMATION", Fore.CYAN, Style.BRIGHT)
    print_colored("="*60, Fore.CYAN)

    # Check all_time_players.json
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

    # Check archived logs
    archived_logs = get_all_log_files(ARCHIVE_LOG_DIR, exclude_live_log=False)
    if archived_logs:
        print_colored(f"✓ Archived logs: {len(archived_logs)} files in {ARCHIVE_LOG_DIR}", Fore.GREEN)
    else:
        print_colored(f"⚪ No archived logs found in {ARCHIVE_LOG_DIR}", Fore.WHITE)

    # Check current phase logs
    current_phase_logs = get_all_log_files(CURRENT_LOG_DIR, exclude_live_log=True)
    if current_phase_logs:
        print_colored(f"✓ Current phase logs: {len(current_phase_logs)} files", Fore.GREEN)
    else:
        print_colored("⚪ No current phase logs found", Fore.WHITE)

    # Check test logs if in test mode
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

# ---------- File Monitoring Functions ----------
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

finalization_requested = False
complete_shutdown_requested = False
finalization_lock = threading.Lock()

def force_end_listener():
    """Listen for force end commands in a background thread."""
    while True:
        try:
            # Check for force end file
            force_end_file = Path("force_end.flag")
            if force_end_file.exists():
                print_colored("Force end flag detected. Requesting finalization...", Fore.RED)
                request_finalization()
                force_end_file.unlink()  # Remove the flag file
                break
            
            # Check for force shutdown file (complete exit)
            force_shutdown_file = Path("force_shutdown.flag")
            if force_shutdown_file.exists():
                print_colored("Force shutdown flag detected. Requesting complete shutdown...", Fore.RED)
                request_finalization()
                force_shutdown_file.unlink()  # Remove the flag file
                # Set a global flag for complete shutdown
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
    
    # Continue exporting JSON periodically to keep overlays fresh
    last_json_export = 0
    
    try:
        while True:
            # Check for shutdown request
            if should_shutdown():
                print_colored("Complete shutdown requested", Fore.RED)
                break
            
            # Check for force shutdown file
            force_shutdown_file = Path("force_shutdown.flag")
            if force_shutdown_file.exists():
                print_colored("Force shutdown flag detected", Fore.RED)
                force_shutdown_file.unlink()
                break
            
            now = time.time()
            
            # Continue exporting JSON every 10 seconds to keep overlays updated
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
        final_match_data = copy.deepcopy(state["current_match"])
        
        # Append to matches for live mode (like catch-up)
        if final_match_data["id"] not in state["processed_matches"]:
            end_match_and_update_phase(final_match_data)
            state["matches"].append(final_match_data)
            state["processed_matches"].add(final_match_data["id"])
            logging.info(f"LIVE: Added match {final_match_data['id']} to history")
        else:
            logging.warning(f"LIVE: Skipped duplicate match {final_match_data['id']}")
        
        _finalize_and_persist()
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
        _finalize_and_persist()
    global finalization_requested
    
    print_colored("\nPerforming finalization...", Fore.CYAN, Style.BRIGHT)
    
    # Export final state
    try:
        _export_json()
        print_colored("Final state exported to JSON", Fore.GREEN)
    except Exception as e:
        logging.error(f"Error during final JSON export: {e}")
    
    # Save all-time players
    try:
        save_all_time_players()
        print_colored("All-time player data saved", Fore.GREEN)
    except Exception as e:
        logging.error(f"Error saving all-time players: {e}")
    
    # Stop simulation if running
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
        # If we already received a signal, force exit
        print_colored(f"\nForce exit requested (signal {signum} received again)", Fore.RED)
        os._exit(1)
    
    signal_received = True
    print_colored(f"\nReceived signal {signum}. Requesting complete shutdown...", Fore.YELLOW)
    
    with finalization_lock:
        complete_shutdown_requested = True
        finalization_requested = True
    
    # Set the shutdown event to wake up sleeping threads
    shutdown_event.set()

def setup_signal_handlers():
    """Setup enhanced signal handlers for graceful shutdown."""
    # Handle common termination signals
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Termination request
    
    # Handle additional signals if available
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal_handler)  # Hang up
    if hasattr(signal, 'SIGQUIT'):
        signal.signal(signal.SIGQUIT, signal_handler)  # Quit signal

def check_shutdown_conditions():
    """Check all possible shutdown conditions."""
    # Check shutdown event
    if shutdown_event.is_set():
        return True
    
    # Check finalization request
    if should_finalize():
        return True
    
    # Check for force end file
    force_end_file = Path("force_end.flag")
    if force_end_file.exists():
        print_colored("Force end flag detected. Requesting finalization...", Fore.RED)
        request_finalization()
        try:
            force_end_file.unlink()  # Remove the flag file
        except:
            pass
        return True
    
    # Check for force shutdown file
    force_shutdown_file = Path("force_shutdown.flag")
    if force_shutdown_file.exists():
        print_colored("Force shutdown flag detected. Requesting complete shutdown...", Fore.RED)
        request_finalization()
        global complete_shutdown_requested
        with finalization_lock:
            complete_shutdown_requested = True
        try:
            force_shutdown_file.unlink()  # Remove the flag file
        except:
            pass
        return True
    
    return False

def interruptible_sleep(duration, check_interval=0.1):
    """Sleep that can be interrupted by shutdown signals."""
    end_time = time.time() + duration
    
    while time.time() < end_time:
        if check_shutdown_conditions():
            return True  # Interrupted
        
        remaining = end_time - time.time()
        sleep_time = min(check_interval, remaining)
        
        if sleep_time > 0:
            time.sleep(sleep_time)
    
    return False  # Completed normally

def process_with_shutdown_check(log_files_to_process, parsed_logos):
    """Process files with regular shutdown checks and progress updates."""
    global in_catchup_processing, processed_files
    
    if not log_files_to_process:
        print_colored("No current phase logs to process.", Fore.WHITE)
        return
        
    # Filter out already processed files
    log_files_to_process = [f for f in log_files_to_process if f.name not in processed_files]
    if not log_files_to_process:
        print_colored("All files already processed.", Fore.WHITE)
        return
        
    print_colored("Processing current-phase logs for catch-up...", Fore.CYAN)
    print_colored(f"Files to process: {len(log_files_to_process)}", Fore.WHITE)
    print_colored("Press Ctrl+C to interrupt processing...", Fore.YELLOW)

    # Sort files by modification time (oldest first)
    log_files_to_process.sort(key=lambda f: f.stat().st_mtime)
    
    # Calculate total size for progress tracking
    total_file_size = sum(f.stat().st_size for f in log_files_to_process)
    processed_size = 0
    
    for i, log_file in enumerate(log_files_to_process):
        # Check for shutdown before processing each file
        if check_shutdown_conditions():
            print_colored(f"\nShutdown requested during file processing. Stopping at file {i+1}/{len(log_files_to_process)}", Fore.YELLOW)
            return
        
        is_last_file = (i == len(log_files_to_process) - 1)
        file_size = log_file.stat().st_size
        
        if not is_last_file:
            print_colored(f"\nProcessing completed match file {i+1}/{len(log_files_to_process)}: {log_file.name}", Fore.YELLOW)
        else:
            print_colored(f"\nProcessing live file {i+1}/{len(log_files_to_process)} for catch-up: {log_file.name}", Fore.CYAN)
        
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                log_text = f.read()
                
                # Log file start
                logging.info(f"Starting processing of file: {log_file.name}")
                
                # Process in smaller chunks with shutdown checks
                chunk_size = max(1024, file_size // 100)
                bytes_processed_in_file = 0
                
                in_catchup_processing = True
                try:
                    # Process the file in chunks
                    for chunk_start in range(0, len(log_text), chunk_size):
                        if check_shutdown_conditions():
                            print_colored(f"\nShutdown requested during chunk processing. Stopping...", Fore.YELLOW)
                            return
                        
                        chunk_end = min(chunk_start + chunk_size, len(log_text))
                        chunk = log_text[chunk_start:chunk_end]
                        
                        # Process chunk with progress callback
                        parse_and_apply(chunk, parsed_logos=parsed_logos, mode="chunk", progress_callback=lambda processed, total: print_progress_bar(
                            processed_size + bytes_processed_in_file + processed,
                            total_file_size,
                            prefix=f"File {i+1}/{len(log_files_to_process)}",
                            suffix=f"{log_file.name}",
                            processing=True
                        ))
                        
                        # Update progress
                        bytes_processed_in_file = chunk_end
                        print_progress_bar(
                            processed_size + bytes_processed_in_file,
                            total_file_size,
                            prefix=f"File {i+1}/{len(log_files_to_process)}",
                            suffix=f"{log_file.name}",
                            processing=True
                        )
                    
                    # Flush any remaining buffer without re-processing the whole file
                    if not check_shutdown_conditions():
                        logging.info(f"Flushing remaining buffer for {log_file.name}")
                        parse_and_apply('', parsed_logos=parsed_logos, mode="chunk", progress_callback=lambda processed, total: print_progress_bar(
                            processed_size + bytes_processed_in_file + processed,
                            total_file_size,
                            prefix=f"File {i+1}/{len(log_files_to_process)}",
                            suffix=f"{log_file.name}",
                            processing=True
                        ))
                        
                        # Finalize any completed matches
                        if state["current_match"]["id"] and state["current_match"]["status"] in ["live", "finished"]:
                            logging.info(f"CATCHUP: Finalizing match at file end: {state['current_match']['id']}")
                            final_match_data = copy.deepcopy(state["current_match"])
                            
                            # Check for duplicate before updating phase and adding
                            if final_match_data["id"] not in state["processed_matches"]:
                                end_match_and_update_phase(final_match_data)
                                state["matches"].append(final_match_data)
                                state["processed_matches"].add(final_match_data["id"])
                                logging.info(f"CATCHUP: Added match {final_match_data['id']} to history")
                            else:
                                logging.warning(f"CATCHUP: Skipped duplicate match {final_match_data['id']}")
                        
                        # ALWAYS reset in catch-up after finalizing (fix for lingering state)
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
                        state["match_state"]["status"] = "idle"
                        state["match_state"]["last_updated"] = int(time.time())
                        logging.info("CATCHUP: Reset current match state after finalization")
                        
                finally:
                    in_catchup_processing = False
                    
                # Update processed size and mark file as processed
                processed_size += file_size
                processed_files.add(log_file.name)
                
                # Show completed progress
                print_progress_bar(
                    processed_size,
                    total_file_size,
                    prefix=f"File {i+1}/{len(log_files_to_process)}",
                    suffix=f"{log_file.name} complete"
                )
                        
        except Exception as e:
            if check_shutdown_conditions():
                print_colored(f"\nShutdown requested during error handling", Fore.YELLOW)
                return
            logging.error(f"Error processing catch-up file {log_file.name}: {e}")
            processed_size += file_size
            processed_files.add(log_file.name)
            print_progress_bar(
                processed_size,
                total_file_size,
                prefix=f"File {i+1}/{len(log_files_to_process)}",
                suffix=f"{log_file.name} ERROR"
            )

    # Final progress
    if not check_shutdown_conditions():
        print_progress_bar(total_file_size, total_file_size, prefix="Catch-up", suffix="ALL FILES COMPLETE")
        print_colored("✓ Current phase catch-up complete!", Fore.GREEN)
        
def enhanced_main_loop(test_mode=False, team_logos=None):
    """Enhanced main loop with proper shutdown handling."""
    global buffer
    buffer = ''  # Ensure clean start
    
    current_log_path = None
    last_pos = 0
    last_json = 0
    last_term = 0
    MATCH_CHECK_INTERVAL = 0.1
    no_data_timeout = 5  # Reduce timeout for faster testing; set to 30 in production
    last_data_time = time.time()
    last_warning_time = 0
    WARNING_INTERVAL = 60
    
    current_phase_logs = get_all_log_files(CURRENT_LOG_DIR)
    if current_phase_logs:
        current_log_path = current_phase_logs[-1]
        last_pos = current_log_path.stat().st_size
        print_colored(f"✓ Live monitoring started on: {current_log_path.name}", Fore.GREEN)
    
    print_colored(f"\nStarting live monitoring... (Press Ctrl+C to stop)", Fore.CYAN, Style.BRIGHT)
    
    try:
        while not check_shutdown_conditions():
            now = time.time()
            
            # Check for match end condition and force finalization
            if state["current_match"]["status"] == "live" and state["current_match"]["id"]:
                alive_teams = [t for t in state["current_match"]["teams"].values() if t["liveMembers"] > 0]
                if len(alive_teams) <= 1:
                    logging.info(f"LIVE: Match end detected. Finalizing match ID: {state['current_match']['id']}")
                    end_match_and_update_phase()
                    _finalize_and_persist()
                    # Clear buffer after finalization
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
                    # No new data - check for EOF timeout
                    if now - last_data_time > no_data_timeout and state["current_match"]["status"] == "live":
                        alive_teams = [t for t in state["current_match"]["teams"].values() if t["liveMembers"] > 0]
                        if len(alive_teams) <= 1:
                            logging.info(f"LIVE: No new data for {no_data_timeout}s and match ended - forcing finalization of match {state['current_match']['id']}")
                            end_match_and_update_phase()
                            _finalize_and_persist()
                            buffer = ''
                            last_data_time = now  # Reset timer after finalization
                        else:
                            if now - last_warning_time > WARNING_INTERVAL:
                                logging.warning(f"LIVE: No new data for {now - last_data_time:.1f}s but {len(alive_teams)} teams still alive - waiting for more data to complete match {state['current_match']['id']}")
                                last_warning_time = now
            
            all_current_logs = get_all_log_files(CURRENT_LOG_DIR)
            if all_current_logs and all_current_logs[-1] != current_log_path:
                print_colored(f"\nNew live log detected: {all_current_logs[-1].name}", Fore.YELLOW)
                buffer = ''  # Clear on file switch
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
    global simulation_manager

    # Setup signal handlers for graceful shutdown
    setup_signal_handlers()

    # Ensure directories exist
    ensure_directories()

    # Print status
    mode = "Test" if test_mode else "Production"
    print_status_header(mode)

    # Load team logos
    team_logos = get_team_logos(TEAM_CONFIG_FILE)
    if not team_logos:
        print_colored("Warning: Could not load team logos. Continuing without them.", Fore.YELLOW)

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

    # CATCH-UP: Process ALL current phase files with enhanced progress tracking
    current_phase_logs = get_all_log_files(CURRENT_LOG_DIR, exclude_live_log=False)
    if current_phase_logs:
        print_colored(f"\nCatching up on {len(current_phase_logs)} current phase file(s)...", Fore.CYAN)
        print_colored("Starting catch-up processing with progress tracking...", Fore.YELLOW)
        
        # Use the enhanced processing function with shutdown checks and real progress
        process_with_shutdown_check(current_phase_logs, team_logos)
        
    else:
        print_colored("No current phase logs found for catch-up.", Fore.WHITE)

    # Start simulation if in test mode
    if test_mode:
        print_colored("Starting background simulation...", Fore.YELLOW)
        simulation_manager = SimulationManager(quiet=True)
        if not simulation_manager.start():
            print_colored("Failed to start simulation. Check test log files.", Fore.RED)
            return
        print_colored("✓ Simulation running in background", Fore.GREEN)
        time.sleep(0.5)

    # Start web server
    if WEBSERVER_AVAILABLE:
        try:
            webserver.start_server()
            print_colored(f"Web server started on http://{WEB_SERVER_HOST}:{WEB_SERVER_PORT}", Fore.GREEN)
        except Exception as e:
            print_colored(f"Failed to start web server: {e}", Fore.RED)

    # Set the initial live file (newest current phase file)
    if current_phase_logs:
        current_log_path = current_phase_logs[-1]  # Newest file
        print_colored(f"✓ Live monitoring started on: {current_log_path.name}", Fore.GREEN)
    else:
        print_colored("⚠ No live log file to monitor", Fore.YELLOW)
        current_log_path = None

    try:
        print_colored(f"\nStarting {mode.lower()} monitoring...", Fore.CYAN, Style.BRIGHT)
        if test_mode:
            print_colored(f"Monitoring simulated log: {SIMULATED_LOG_FILE}", Fore.YELLOW)
        else:
            print_colored(f"Monitoring live log: {current_log_path.name if current_log_path else 'None'}", Fore.GREEN)

        enhanced_main_loop(test_mode=test_mode, team_logos=team_logos)

    except KeyboardInterrupt:
        print_colored("\nStopped by user (Ctrl+C).", Fore.YELLOW)
        request_finalization()
        
    except Exception as e:
        logging.exception(f"Live monitor error: {e}")
        request_finalization()
    
    finally:
        # Force final cleanup on exit if there's lingering match data
        if (state["current_match"]["id"] or 
            state["current_match"]["teams"] or 
            state["current_match"]["killFeed"] or 
            state["current_match"]["eliminationOrder"]):
            logging.info("Forcing finalization on exit for lingering match data")
            end_match_and_update_phase()
            _finalize_and_persist()
        
        perform_finalization(keep_server_running=True)
        setup_force_end_thread()
        server_only_mode()
        
if __name__ == "__main__":
    main()