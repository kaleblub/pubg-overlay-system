import json
import re
import time
import os
import shutil
import logging
from pathlib import Path
import webserver  # your server

# ---------- Config ----------
ROOT_LOG_DIR = Path("./logs")
CURRENT_LOG_DIR = ROOT_LOG_DIR / "current"
ARCHIVE_LOG_DIR = ROOT_LOG_DIR / "All other logs"
OUTPUT_JSON = Path("./live_scoreboard.json")

LOGO_FOLDER_PATH = Path("./assets/LOGO")
ADJACENT_LOGO_FOLDER_PATH = "http://localhost:5000/assets/LOGO/"
DEFAULT_TEAM_LOGO = "http://localhost:5000/assets/default-team-logo.jpg"
DEFAULT_PLAYER_PHOTO = "http://localhost:5000/assets/PUBG.png"

UPDATE_INTERVAL = 0.5
FILE_CHECK_INTERVAL = 0.1

PLACEMENT_POINTS = {1: 10, 2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 1, 8: 1}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- State (normalized) ----------
state = {
    "phase": {  # current tournament phase only
        "teams": {},   # teamId -> { id, name, logo, totals:{kills, placementPoints, points, wwcd} }
        "players": {}  # playerId -> { id, name, photo, teamId, live:{isAlive, health, healthMax}, totals:{kills, damage, knockouts, matches} }
    },
    "all_time": {  # from archives only (for global top 5)
        "players": {}
    },
    "match": {  # current match only
        "id": None,
        "status": "idle",           # idle | live | finished
        "winnerTeamId": None,
        "eliminationOrder": [],
        "killFeed": [],
        "teams": {},   # teamId -> { id, name, logo, liveMembers, kills, players:[ids] }
        "players": {}  # playerId -> { id, teamId, name, photo, live:{alive/health}, stats:{kills, damage, knockouts} }
    }
}

# ---------- Asset Helpers ----------
def get_asset_url(full_path_from_log, default_url):
    if not full_path_from_log or not str(full_path_from_log).strip():
        return default_url
    try:
        p = Path(str(full_path_from_log).strip())
        if p.exists() and p.is_file():
            return f"{ADJACENT_LOGO_FOLDER_PATH}{p.name}"
    except Exception:
        pass
    # search by name in local folder
    try:
        name = Path(full_path_from_log).name
        if LOGO_FOLDER_PATH.exists():
            for file in os.listdir(LOGO_FOLDER_PATH):
                if file.lower() == name.lower():
                    return f"{ADJACENT_LOGO_FOLDER_PATH}{file}"
    except Exception:
        pass
    return default_url

# ---------- Health Tracking Helper ----------
def _add_or_update_player(player_data, is_alive, health=0, health_max=100):
    """
    Adds or updates a player's entry in the phase.players state.
    This is critical for the HTML scoreboard to get individual player health.
    """
    player_id = player_data["id"]
    team_id = player_data.get("teamId")
    
    # Initialize or update the player in the main phase state
    if player_id not in state["phase"]["players"]:
        state["phase"]["players"][player_id] = {
            "id": player_id,
            "name": player_data.get("name", "Unknown Player"),
            "photo": player_data.get("photo", DEFAULT_PLAYER_PHOTO),
            "teamId": team_id,
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
        # Update live status
        state["phase"]["players"][player_id]["live"]["isAlive"] = is_alive
        state["phase"]["players"][player_id]["live"]["health"] = health
        state["phase"]["players"][player_id]["live"]["healthMax"] = health_max

# ---------- Parsing (single path) ----------
INI_BLOCK = re.compile(r'\[/Script/ShadowTrackerExtra.FCustomTeamLogoAndColor](.*?)\n\n', re.DOTALL)
OBJ_BLOCKS = re.compile(r'(TotalPlayerList:|TeamInfoList:)')
OBJ_KV = re.compile(r'(\w+):\s*(?:"([^"]*)"|\'([^\']*)\'|([^{},\n]+))')

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
            if key == "teamId":   # keep teamId as string later
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

def _apply_ini_to_match(match, ini_map):
    for tid, info in ini_map.items():
        # Get the team entry, but don't create it here.
        t = match["teams"].get(tid)
        if t: # Only apply if the team already exists
            t["name"] = info["name"]
            t["logo"] = get_asset_url(info["logoPath"], DEFAULT_TEAM_LOGO)

def get_team_logos(ini_file_path):
    """
    Parses the team logos and names from a specified INI file.
    Returns a dictionary mapping team IDs to logo paths and names.
    """
    try:
        with open(ini_file_path, "r", encoding="utf-8") as fh:
            ini_content = fh.read()
            ini_match = INI_BLOCK.search(ini_content)
            if ini_match:
                # _parse_ini needs to be adjusted to return the logo and name
                return _parse_ini(ini_match.group(1))
            else:
                logging.warning("Team logos INI block not found in the specified file.")
    except FileNotFoundError:
        logging.error(f"INI file not found at {ini_file_path}")
    return None

def parse_and_apply(log_text, parsed_logos=None, mode="chunk"):
    gid = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", log_text)
    logging.info(f"gid: {gid}")
    if gid:
        game_id = gid.group(1)
        if state["match"]["id"] and game_id != state["match"]["id"]:
            # a new game detected → finish previous match if not already finished
            if state["match"]["status"] != "finished":
                end_match_and_update_phase(state)
            _reset_match(state["match"])
        if state["match"]["id"] != game_id:
            state["match"]["id"] = game_id
            state["match"]["status"] = "live"

    # Parse health-related events from the log
    lines = log_text.splitlines()
    for line in lines:
        # Match for player death events
        m = re.search(r'G_PlayerDied.*?PlayerName=(.*?),Health=(.*?),AttackInfo', line)
        if m:
            player_name = m.group(1)
            player_health = int(m.group(2))
            
            # Find the player ID using their name, and update their live status
            for p_id, p_data in state["phase"]["players"].items():
                if p_data["name"] == player_name:
                    _add_or_update_player(p_data, is_alive=False, health=player_health)
                    break

        # Match for player state updates (join and health updates)
        m = re.search(r'G_PlayerState_InMatch.*?:.*?TeamID=(.*?)/PlayerID=(.*?)/PlayerName=(.*?)/LiveState=(.*?)/Health=(.*?),MaxHealth=(.*?)', line)
        if m:
            team_id = m.group(1)
            player_id = m.group(2)
            player_name = m.group(3)
            is_alive = True if m.group(4) == '1' else False
            health = int(m.group(5))
            health_max = int(m.group(6))
            
            player_data = {
                "id": player_id,
                "name": player_name,
                "teamId": team_id
            }
            _add_or_update_player(player_data, is_alive, health, health_max)

    # walk blocks once
    parts = OBJ_BLOCKS.split(log_text)
    for i, marker in enumerate(parts):
        if marker == "TotalPlayerList:" and i + 1 < len(parts):
            for obj_txt in re.findall(r'\{[^{}]*\}', parts[i+1]):
                p = _parse_kv_object(obj_txt)
                _upsert_player_from_total(p)
        elif marker == "TeamInfoList:" and i + 1 < len(parts):
            for obj_txt in re.findall(r'\{[^{}]*\}', parts[i+1]):
                t = _parse_kv_object(obj_txt)
                _upsert_team_from_teaminfo(t, parsed_logos) # Pass logos here

    _update_live_eliminations()
    return state["match"]["id"]

def _reset_match(match):
    match.update({
        "id": None,
        "status": "idle",
        "winnerTeamId": None,
        "eliminationOrder": [],
        "killFeed": [],
        "teams": {},
        "players": {}
    })

def _upsert_team_from_teaminfo(t, parsed_logos):
    tid = str(t.get("teamId") or "")
    if not tid or tid == "None":
        return
    team = state["match"]["teams"].setdefault(tid, {
        "id": tid, "name": t.get("teamName") or "Unknown Team",
        "logo": DEFAULT_TEAM_LOGO, "liveMembers": 0,
        "kills": 0, "placementPointsLive": 0, "players": []
    })
    
    # Update team name from TeamInfo
    team["name"] = t.get("teamName") or team["name"]
    team["liveMembers"] = int(t.get("liveMemberNum") or 0)
    
    # Check if a logo exists in the pre-parsed data and update
    if parsed_logos and tid in parsed_logos:
        logo_info = parsed_logos[tid]
        team["logo"] = get_asset_url(logo_info["logoPath"], DEFAULT_TEAM_LOGO)

def _upsert_player_from_total(p):
    pid = str(p.get("uId") or "")
    tid = str(p.get("teamId") or "")
    if not pid or not tid or tid == "None":
        return

    # team presence
    team = state["match"]["teams"].setdefault(tid, {
        "id": tid, "name": p.get("teamName") or "Unknown Team",
        "logo": DEFAULT_TEAM_LOGO, "liveMembers": 0,
        "kills": 0, "placementPointsLive": 0, "players": []
    })
    if pid not in team["players"]:
        team["players"].append(pid)

    # player live & stats
    is_alive = (p.get("liveState") != 5)
    player = state["match"]["players"].setdefault(pid, {
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
    player["stats"]["kills"] = int(p.get("killNum") or 0)
    player["stats"]["damage"] = int(p.get("damage") or 0)
    player["stats"]["knockouts"] = int(p.get("knockouts") or 0)

    # Also update the phase player data with health info
    phase_player_data = {
        "id": pid,
        "name": player["name"],
        "teamId": tid
    }
    _add_or_update_player(phase_player_data, is_alive, 
                         player["live"]["health"], player["live"]["healthMax"])

    # team kills aggregate
    _recompute_team_kills(tid)

    # kill feed (simple heuristic: if kills increased)
    prev_kills = player["stats"].get("_prevKills", 0)
    if player["stats"]["kills"] > prev_kills:
        tn = state["match"]["teams"].get(tid, {}).get("name", "Unknown Team")
        pn = player["name"]
        state["match"]["killFeed"].append(f"🔥 {pn} ({tn}) got a new kill!")
        state["match"]["killFeed"] = state["match"]["killFeed"][-5:]
    player["stats"]["_prevKills"] = player["stats"]["kills"]

def _recompute_team_kills(tid):
    team = state["match"]["teams"].get(tid)
    if not team:
        return
    total = 0
    for pid in team["players"]:
        total += state["match"]["players"].get(pid, {}).get("stats", {}).get("kills", 0)
    team["kills"] = total

def _update_live_eliminations():
    for tid, t in state["match"]["teams"].items():
        if t["liveMembers"] == 0 and tid not in state["match"]["eliminationOrder"]:
            state["match"]["eliminationOrder"].append(tid)

    alive = [tid for tid, t in state["match"]["teams"].items() if t["liveMembers"] > 0]
    if len(alive) == 1 and state["match"]["status"] == "live":
        state["match"]["winnerTeamId"] = alive[0]
        state["match"]["status"] = "finished"

def _placement_points_live():
    # compute placement points based on elimination order + alive
    team_ids = list(state["match"]["teams"].keys())
    eliminated = state["match"]["eliminationOrder"]
    ranks_from_last = list(reversed(eliminated)) + [
        tid for tid in team_ids if tid not in eliminated
    ]
    # last place gets highest rank number
    points = {}
    n = len(team_ids)
    rank_pos = {tid: idx+1 for idx, tid in enumerate(ranks_from_last)}  # 1..n
    for tid, rank in rank_pos.items():
        # rank 1 is winner
        # map to your PLACEMENT_POINTS (1..8), default 0
        pts = PLACEMENT_POINTS.get(rank, 0)
        points[tid] = pts
    return points

def _all_time_top_players():
    players = []
    for pid, p in state["all_time"]["players"].items():
        t = p["totals"]
        players.append({
            "playerId": pid, "name": p["name"],
            "teamId": None,  # unknown from archives; overlay can map last-known if desired
            "totalKills": t["kills"], "totalDamage": t["damage"],
            "totalKnockouts": t["knockouts"], "totalMatches": t["matches"]
        })
    players.sort(key=lambda x: (x["totalKills"], x["totalDamage"], x["totalKnockouts"]), reverse=True)
    return players[:5]

# ---------- Aggregation (phase & all-time) ----------
def end_match_and_update_phase(_state):
    m = _state["match"]
    if not m["id"] or m["status"] == "finished":  # already done
        return
    # close match
    m["status"] = "finished"
    if not m["winnerTeamId"]:
        alive = [tid for tid, t in m["teams"].items() if t["liveMembers"] > 0]
        if len(alive) == 1:
            m["winnerTeamId"] = alive[0]

    placement = _placement_points_live()
    # apply to phase totals
    for tid, team in m["teams"].items():
        team_name = team["name"]
        phase_team = _state["phase"]["teams"].setdefault(tid, {
            "id": tid, "name": team_name, "logo": team.get("logo") or DEFAULT_TEAM_LOGO,
            "totals": {"kills": 0, "placementPoints": 0, "points": 0, "wwcd": 0}
        })
        phase_team["name"] = team_name
        phase_team["logo"] = team.get("logo") or phase_team["logo"]
        k = int(team.get("kills", 0))
        pp = int(placement.get(tid, 0))
        phase_team["totals"]["kills"] += k
        phase_team["totals"]["placementPoints"] += pp
        phase_team["totals"]["points"] = phase_team["totals"]["kills"] + phase_team["totals"]["placementPoints"]
        if pp == PLACEMENT_POINTS.get(1, 10):
            phase_team["totals"]["wwcd"] += 1

    # apply to phase players
    for pid, pl in m["players"].items():
        ph = _state["phase"]["players"].setdefault(pid, {
            "id": pid, "name": pl["name"], "photo": pl.get("photo") or DEFAULT_PLAYER_PHOTO,
            "teamId": pl["teamId"],
            "live": {"isAlive": False, "health": 0, "healthMax": 100},
            "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
        })
        ph["name"] = pl["name"]
        ph["photo"] = pl.get("photo") or ph["photo"]
        ph["teamId"] = pl["teamId"]
        ph["totals"]["kills"] += int(pl["stats"]["kills"])
        ph["totals"]["damage"] += int(pl["stats"]["damage"])
        ph["totals"]["knockouts"] += int(pl["stats"]["knockouts"])
        ph["totals"]["matches"] += 1

def apply_archived_file_to_all_time(log_text, parsed_logos):
    # parse into a temp match, then update all_time players only
    temp_before = state["match"].copy()
    parse_and_apply(log_text, parsed_logos=parsed_logos, mode="full")
    for pid, pl in state["match"]["players"].items():
        at = state["all_time"]["players"].setdefault(pid, {
            "id": pid, "name": pl["name"], "photo": pl.get("photo") or DEFAULT_PLAYER_PHOTO,
            "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
        })
        at["name"] = pl["name"]
        at["photo"] = pl.get("photo") or at["photo"]
        at["totals"]["kills"] += int(pl["stats"]["kills"])
        at["totals"]["damage"] += int(pl["stats"]["damage"])
        at["totals"]["knockouts"] += int(pl["stats"]["knockouts"])
        at["totals"]["matches"] += 1
    # restore match (archives should not affect live match)
    _reset_match(state["match"])
    state["match"].update({k: temp_before[k] for k in temp_before})

# ---------- IO ----------
def get_all_log_files(log_dir):
    if not log_dir.exists():
        return []
    out = []
    for root, _dirs, files in os.walk(log_dir):
        for f in files:
            if f.endswith(".txt"):
                out.append(Path(root) / f)
    return sorted(out)

def process_archives_for_all_time(parsed_logos):
    logging.info("Processing archived logs for ALL-TIME top players...")
    for f in get_all_log_files(ARCHIVE_LOG_DIR):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                apply_archived_file_to_all_time(fh.read(), parsed_logos)
        except Exception as e:
            logging.warning(f"Archive error for {f}: {e}")
    logging.info("Done (all-time).")

def process_current_phase_files(log_files_to_process, parsed_logos):
    logging.info("Processing current-phase logs for phase standings...")
    
    # The logos are now passed in, so we don't need to load them here.
    if not parsed_logos:
        logging.warning("Could not load team logos. Continuing without them.")
        
    state["phase"]["teams"] = {}
    state["phase"]["players"] = {}
    
    for f in log_files_to_process:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                log_text = fh.read()
                parse_and_apply(log_text, parsed_logos=parsed_logos, mode="full")
                end_match_and_update_phase(state)
        except Exception as e:
            logging.warning(f"Phase file error for {f}: {e}")
            
    logging.info("Done (phase).")

def _phase_standings():
    teams = []
    for tid, t in state["phase"]["teams"].items():
        tot = t["totals"]
        teams.append({
            "teamId": tid, "kills": tot["kills"], "placementPoints": tot["placementPoints"],
            "points": tot["points"], "wwcd": tot["wwcd"]
        })
    teams.sort(key=lambda x: (x["points"], x["kills"]), reverse=True)
    for i, row in enumerate(teams, 1):
        row["rank"] = i
    return teams

def _current_match_top_players():
    players = []
    for pid, p in state["match"]["players"].items():
        players.append({
            "playerId": pid, "teamId": p["teamId"], "name": p["name"],
            "kills": p["stats"]["kills"], "damage": p["stats"]["damage"], "knockouts": p["stats"]["knockouts"]
        })
    players.sort(key=lambda x: (x["kills"], x["damage"], x["knockouts"]), reverse=True)
    return players[:5]

def _get_active_players_with_health():
    """
    Returns active players from the current match with their live health data.
    This is specifically for JavaScript health monitoring.
    """
    active_players = []
    
    if state["match"]["status"] == "live":
        for player_id, player_data in state["match"]["players"].items():
            # Get team info
            team_data = state["match"]["teams"].get(player_data["teamId"], {})
            
            active_players.append({
                "playerId": player_id,
                "name": player_data["name"],
                "teamId": player_data["teamId"],
                "teamName": team_data.get("name", "Unknown Team"),
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
    
    # Sort by team, then by health status (alive first), then by kills
    active_players.sort(key=lambda p: (
        p["teamId"],
        not p["live"]["isAlive"],  # False (alive) comes before True (dead)
        -p["stats"]["kills"]  # Higher kills first
    ))
    
    return active_players

def _get_current_match_team_kills():
    """
    Returns current match team kills for live display.
    """
    team_kills = {}
    
    for team_id, team_data in state["match"]["teams"].items():
        team_kills[team_id] = {
            "teamId": team_id,
            "teamName": team_data["name"],
            "kills": team_data["kills"],
            "liveMembers": team_data["liveMembers"]
        }
    
    return team_kills

def _export_json():
    payload = {
        "meta": {"schemaVersion": 1, "generatedAt": int(time.time())},

        "phase": {
            "standings": _phase_standings(),
            "teams": state["phase"]["teams"],
            "players": state["phase"]["players"]  # Includes live health data for phase tracking
        },

        "match": state["match"],  # This already includes live match data with player health

        "leaderboards": {
            "currentMatchTopPlayers": _current_match_top_players(),
            "allTimeTopPlayers": _all_time_top_players()
        },
        
        # Add a dedicated section for live match health monitoring
        "live": {
            "activePlayers": _get_active_players_with_health(),
            "teamKills": _get_current_match_team_kills()
        }
    }
    try:
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Error writing JSON: {e}")

# ---------- Live loop ----------
def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:
        return 0

def _read_new(path: Path, pos: int):
    try:
        with open(path, "r", encoding="utf-8") as f:
            f.seek(pos)
            data = f.read()
            return data, f.tell()
    except Exception as e:
        logging.warning(f"Read error: {e}")
        return "", pos

def _print_terminal_snapshot():
    m = state["match"]
    print("\n🎮 LIVE SCOREBOARD")
    print(f"Match: {m['id'] or 'waiting...'}  Status: {m['status']}")
    print("Teams (current match):")
    rows = []
    for tid, t in m["teams"].items():
        rows.append((t["name"], t["kills"], t["liveMembers"]))
    rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
    for name, kills, live in rows[:12]:
        print(f"  - {name:22}  K:{kills:<3}  Live:{live}")
    print("Kill feed:")
    for k in m["killFeed"][-5:]:
        print("  ", k)

def _is_log_updating(log_path, min_size=0):
    try:
        initial_size = log_path.stat().st_size
        if initial_size < min_size:
            return False
        time.sleep(1)
        current_size = log_path.stat().st_size
        return current_size > initial_size
    except Exception:
        return False

def main():
    os.makedirs(CURRENT_LOG_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_LOG_DIR, exist_ok=True)
    os.makedirs(LOGO_FOLDER_PATH, exist_ok=True)

    # Load team logos ONCE at the very beginning
    ini_file_path = "./TeamLogoAndColor.ini"
    team_logos = get_team_logos(ini_file_path)
    if not team_logos:
        logging.warning("Could not load team logos. Continuing without them.")

    process_archives_for_all_time(team_logos)
    
    # Process completed logs
    completed_logs_for_phase = [f for f in get_all_log_files(CURRENT_LOG_DIR) if not _is_log_updating(f, min_size=500)]
    process_current_phase_files(completed_logs_for_phase, team_logos)

    # Initialize variables for the main loop
    current_log = None
    last_pos = 0
    last_update_time = time.time()

    webserver.start_server()
    last_json = 0
    last_term = 0
    
    try:
        while True:
            now = time.time()
            
            # State 1: No log is being monitored or the current one has timed out
            if not current_log or (now - last_update_time > 10 and last_pos > 0): # 10s timeout
                logging.info("Checking for a new live log file...")
                current_log = None
                
                # Check all logs in reverse order to find the newest
                all_logs = get_all_log_files(CURRENT_LOG_DIR)
                if all_logs:
                    most_recent_log = all_logs[-1]
                    if _is_log_updating(most_recent_log, min_size=500):
                        logging.info(f"✨ New live log file detected: {most_recent_log}")
                        current_log = most_recent_log
                        last_pos = 0 # Reset for the new file
                        last_update_time = now

                        # Initial pass to set up the match state
                        try:
                            with open(current_log, "r", encoding="utf-8") as f:
                                parse_and_apply(f.read(), parsed_logos=team_logos, mode="full")
                                last_pos = f.tell()
                                state["match"]["status"] = "live" if state["match"]["id"] else "idle"
                        except FileNotFoundError:
                            logging.error(f"Log not found: {current_log}")
                            current_log = None
                            
            # State 2: A log is being monitored
            if current_log:
                size = _file_size(current_log)
                if size > last_pos:
                    chunk, new_pos = _read_new(current_log, last_pos)
                    if chunk:
                        parse_and_apply(chunk, parsed_logos=team_logos, mode="chunk")
                        last_pos = new_pos
                        last_update_time = now
                
            # Periodic updates for JSON and terminal output
            if now - last_json >= UPDATE_INTERVAL:
                _export_json()
                last_json = now
            if now - last_term >= 1.0:
                _print_terminal_snapshot()
                last_term = now

            time.sleep(FILE_CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n⏹️ Stopped by user.")
        end_match_and_update_phase(state)
        _export_json()
    except Exception as e:
        logging.exception(f"Live error: {e}")
        try:
            _export_json()
        except Exception:
            pass

if __name__ == "__main__":
    main()