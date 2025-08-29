import json
import re
import time
import datetime
import os
import shutil
import logging
from pathlib import Path
import webserver  # your server

# ---------- Config ----------
ROOT_DIR = Path(__file__).parent
ROOT_LOG_DIR = Path("./logs")
CURRENT_LOG_DIR = ROOT_DIR
ARCHIVE_LOG_DIR = ROOT_LOG_DIR
OUTPUT_JSON = Path("./live_scoreboard.json")
ALL_TIME_PLAYERS_JSON = Path("./all_time_players.json")

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
        "teams": {},   # teamName -> { id=teamName, name, logo, totals:{kills, placementPoints, points, wwcd} }
        "players": {}  # playerId -> { id, name, photo, teamName, live:{isAlive, health, healthMax}, totals:{kills, damage, knockouts, matches} }
    },
    "all_time": {  # from archives only (for global top 5)
        "players": {}
    },
    "match": {  # current match only
        "id": None,
        "status": "idle",           # idle | live | finished
        "winnerTeamId": None,
        "winnerTeamName": None,     # Add team name tracking
        "eliminationOrder": [],     # Will now store team names instead of IDs
        "killFeed": [],
        "teams": {},   # teamId -> { id, name, logo, liveMembers, kills, players:[ids] }
        "players": {}  # playerId -> { id, teamId, name, photo, live:{alive/health}, stats:{kills, damage, knockouts} }
    },
    "teamNameMapping": {}  # matchId -> {teamId -> teamName} for current match only
}

# ---------- Team Name Mapping Helpers ----------
def _get_team_name_by_id(team_id):
    """Get team name for current match by team ID"""
    current_match_id = state["match"]["id"]
    if current_match_id and current_match_id in state["teamNameMapping"]:
        return state["teamNameMapping"][current_match_id].get(team_id)
    return None

def _get_team_id_by_name(team_name):
    """Get team ID for current match by team name"""
    current_match_id = state["match"]["id"]
    if current_match_id and current_match_id in state["teamNameMapping"]:
        mapping = state["teamNameMapping"][current_match_id]
        for tid, tname in mapping.items():
            if tname == team_name:
                return tid
    return None

def _register_team_mapping(team_id, team_name):
    """Register team ID -> name mapping for current match"""
    current_match_id = state["match"]["id"]
    if current_match_id:
        if current_match_id not in state["teamNameMapping"]:
            state["teamNameMapping"][current_match_id] = {}
        state["teamNameMapping"][current_match_id][team_id] = team_name

def _cleanup_old_team_mappings():
    """Clean up team mappings for old matches"""
    current_match_id = state["match"]["id"]
    if current_match_id:
        # Keep only the current match mapping
        state["teamNameMapping"] = {current_match_id: state["teamNameMapping"].get(current_match_id, {})}

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

# ---------- Debugging Functions ----------
def _file_size(path):
    """Returns the file size in bytes."""
    return os.stat(path).st_size if os.path.exists(path) else 0

def _read_new(path, last_pos):
    """Reads new lines from a file from a specified position."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(last_pos)
        chunk = f.read()
        return chunk, f.tell()

def _safe_increment(data, key, increment):
    """Safely increments a value in a dictionary."""
    data[key] = data.get(key, 0) + increment

def _debug_placement_calculation(when=""):
    """Debug placement points calculation"""
    print(f"\n=== PLACEMENT DEBUG ({when}) ===")
    print(f"Match ID: {state['match']['id']}")
    print(f"Match Status: {state['match']['status']}")
    print(f"Elimination Order: {state['match']['eliminationOrder']}")
    
    live_points = _calculate_live_placement_points()
    final_points = _calculate_final_placement_points()
    
    print("Live Placement Points:")
    for tid, points in live_points.items():
        team_name = _get_team_name_by_id(tid) or "Unknown"
        print(f"  {team_name}: {points}")
    
    print("Final Placement Points:")
    for tid, points in final_points.items():
        team_name = _get_team_name_by_id(tid) or "Unknown"
        print(f"  {team_name}: {points}")
    
    print("Phase Team Totals:")
    for team_name, data in state["phase"]["teams"].items():
        print(f"  {team_name}: {data['totals']['placementPoints']} placement points")
    print("=" * 40)

def _debug_kill_tracking():
    """Debug function to track kill accumulation"""
    print("\n=== KILL DEBUG ===")
    for tid, team in state["match"]["teams"].items():
        print(f"Team {team['name']} (ID: {tid}): {team['kills']} kills")
        for pid in team.get("players", []):
            player = state["match"]["players"].get(pid, {})
            p_kills = player.get("stats", {}).get("kills", 0)
            print(f"  - {player.get('name', 'Unknown')}: {p_kills} kills")
    print("==================\n")

def _debug_phase_accumulation(when=""):
    """Debug phase data accumulation"""
    print(f"\n=== PHASE DEBUG ({when}) ===")
    for team_name, team_data in state["phase"]["teams"].items():
        totals = team_data["totals"]
        print(f"Team {team_name}:")
        print(f"  Kills: {totals['kills']}")
        print(f"  Placement: {totals['placementPoints']}")
        print(f"  Total: {totals['points']}")
        print(f"  WWCD: {totals['wwcd']}")
    print("=" * 40)


# ---------- Health Tracking Helper ----------
def _add_or_update_player(player_data, is_alive, health=0, health_max=100):
    """
    Adds or updates a player's entry in the phase.players state.
    Now uses team names instead of team IDs for phase tracking.
    """
    player_id = player_data["id"]
    team_id = player_data.get("teamId")
    team_name = _get_team_name_by_id(team_id) if team_id else None
    
    if not team_name:
        team_name = player_data.get("teamName", "Unknown Team")
    
    # Initialize or update the player in the main phase state
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
        # Update live status and team name
        state["phase"]["players"][player_id]["live"]["isAlive"] = is_alive
        state["phase"]["players"][player_id]["live"]["health"] = health
        state["phase"]["players"][player_id]["live"]["healthMax"] = health_max
        state["phase"]["players"][player_id]["teamName"] = team_name

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
            # Register the mapping
            _register_team_mapping(tid, info["name"])

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

# FIXED: Function to update phase data during live match - using team names
def _add_or_update_team(team_data):
    """
    Adds or updates a team's entry in the phase.teams state.
    Now uses team names as the primary key.
    """
    team_name = team_data.get("teamName", "Unknown Team")
    team_id = team_data.get("teamId", "Unknown ID")

    # Initialize team entry if it doesn't exist
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

# FIXED: This function was incorrectly called without arguments
def _update_phase_from_live_match():
    """
    Updates the phase state based on live match data.
    ONLY tracks kills and damage in real-time. NO PLACEMENT POINTS during live match.
    """
    match_id = state["match"]["id"]
    if not match_id:
        return
    
    # Update phase data for all teams and players in current match
    for team_id, team_data in state["match"]["teams"].items():
        team_name = team_data.get("name", "Unknown Team")
        
        # Ensure team exists but DON'T touch placement points
        if team_name not in state["phase"]["teams"]:
            state["phase"]["teams"][team_name] = {
                "id": team_id,
                "name": team_name,
                "logo": team_data.get("logo", ""),
                "totals": {
                    "kills": 0,
                    "placementPoints": 0,  # Initialize to 0
                    "points": 0,
                    "wwcd": 0
                }
            }
        
        # Update players for this team
        for player_id in team_data.get("players", []):
            player_data = state["match"]["players"].get(player_id)
            if player_data:
                # Ensure player exists in phase state
                _add_or_update_player({
                    "id": player_id,
                    "name": player_data.get("name", "Unknown Player"),
                    "photo": player_data.get("photo", DEFAULT_PLAYER_PHOTO),
                    "teamName": team_name,
                    "teamId": team_id
                }, is_alive=player_data["live"]["isAlive"])

def _calculate_live_placement_points():
    """
    Calculate placement points during live match.
    Only eliminated teams get placement points, surviving teams get 0.
    """
    points = {}
    eliminated_names = state["match"]["eliminationOrder"]
    total_teams = len(state["match"]["teams"])
    
    # Convert team names back to IDs for current match and assign points
    for i, team_name in enumerate(reversed(eliminated_names)):
        team_id = _get_team_id_by_name(team_name)
        if team_id:
            rank = total_teams - i
            points[team_id] = PLACEMENT_POINTS.get(rank, 0)
        
    # Surviving teams get 0 placement points during live match
    for team_id in state["match"]["teams"]:
        if team_id not in points:
            points[team_id] = 0
    
    return points

# FIXED: Phase standings now uses team names consistently
def _phase_standings():
    """Generate phase standings from cumulative phase data"""
    teams = []
    
    for team_name, t in state["phase"]["teams"].items():
        tot = t["totals"]
        
        teams.append({
            "teamId": team_name,  # Use team name as ID for phase standings
            "teamName": t["name"],
            "kills": tot["kills"],
            "placementPoints": tot["placementPoints"],
            "points": tot["points"],
            "wwcd": tot.get("wwcd", 0),
            "rank": None
        })
    
    # Sort by total points (kills + placement), then by kills
    teams.sort(key=lambda x: (x["points"], x["kills"]), reverse=True)
    
    # Add rank
    for i, row in enumerate(teams, 1):
        row["rank"] = i
    
    return teams

# Modified parse_and_apply function with better match detection
def parse_and_apply(log_text, parsed_logos=None, mode="chunk"):
    gid = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", log_text)
    
    new_game_id = None
    if gid:
        new_game_id = gid.group(1)
        
    # Check if a new game has started
    if new_game_id and state["match"]["id"] and new_game_id != state["match"]["id"]:
        logging.info(f"NEW MATCH DETECTED: {new_game_id} (previous: {state['match']['id']})")
        # End the previous game before we process the new one
        end_match_and_update_phase(state)
        _reset_match_but_keep_id(state["match"], new_game_id)
        
    if not state["match"]["id"]:
        if new_game_id:
            logging.info(f"INITIALIZING MATCH: {new_game_id}")
            state["match"]["id"] = new_game_id
            state["match"]["status"] = "live"
            
            if mode == "full":
                logging.info("FULL MODE: Resetting match state for in-progress match")
                _reset_match_but_keep_id(state["match"], new_game_id)

    # Process player state changes and deaths
    _process_player_state_changes(log_text)

    # Process the structured data
    parts = OBJ_BLOCKS.split(log_text)
    for i, marker in enumerate(parts):
        if marker == "TotalPlayerList:" and i + 1 < len(parts):
            for obj_txt in re.findall(r'\{[^{}]*\}', parts[i+1]):
                p = _parse_kv_object(obj_txt)
                _upsert_player_from_total(p, mode=mode)
        elif marker == "TeamInfoList:" and i + 1 < len(parts):
            for obj_txt in re.findall(r'\{[^{}]*\}', parts[i+1]):
                t = _parse_kv_object(obj_txt)
                _upsert_team_from_teaminfo(t, parsed_logos)

    # Recalculate live members after all updates
    _recalculate_live_members()
    _update_live_eliminations()
    
    # FIXED: Update phase data without arguments
    _update_phase_from_live_match()
    
    return state["match"]["id"]

def end_match_and_update_phase(state_obj):
    """Finalizes the match and updates the phase standings with accumulation."""
    
    print(f"\n=== FINALIZING MATCH {state_obj['match']['id']} ===")
    
    if not state_obj["match"]["id"]:
        logging.warning("end_match_and_update_phase called but no match ID is set.")
        return

    logging.info("Finalizing scores and updating phase standings...")
    
    # CRITICAL: Store a copy of the current match state to prevent it from being modified mid-calculation
    current_match_teams = state_obj["match"]["teams"].copy()
    current_match_players = state_obj["match"]["players"].copy()
    elimination_order = list(state_obj["match"]["eliminationOrder"])

    # --- Step 1: Separate Teams into Survivors and Eliminated ---
    eliminated_teams_data = [t for t in current_match_teams.values() if t['name'] in elimination_order]
    survivor_teams_data = [t for t in current_match_teams.values() if t['name'] not in elimination_order]

    # --- Step 2: Sort the Survivors and Eliminated Teams ---
    # Sort survivors by kills descending
    survivor_teams_data.sort(key=lambda t: t['kills'], reverse=True)
    
    # Sort eliminated teams by their rank in the elimination order
    eliminated_teams_data.sort(key=lambda t: elimination_order.index(t['name']), reverse=True)

    # --- Step 3: Combine and Assign Final Ranks ---
    all_teams_data = survivor_teams_data + eliminated_teams_data
    
    final_placement = {}
    for i, team_data in enumerate(all_teams_data):
        rank = i + 1
        placement_pts = PLACEMENT_POINTS.get(rank, 0)
        final_placement[team_data["id"]] = placement_pts

    logging.info("DEBUG PLACEMENT CALCULATION:")
    for i, team_data in enumerate(all_teams_data):
        rank = i + 1
        placement_pts = final_placement.get(team_data["id"], 0)
        status = "alive" if team_data.get("liveMembers", 0) > 0 else "eliminated"
        logging.info(f"  {team_data['name']} ({status}, {team_data['kills']} kills) -> Rank {rank} -> {placement_pts} points")

    # --- Step 4: Apply Points and Kills (rest of your original function) ---
    if all_teams_data and all_teams_data[0]["liveMembers"] > 0:
        winner_name = all_teams_data[0]["name"]
        winner_entry = state_obj["phase"]["teams"].get(winner_name)
        if winner_entry:
            winner_entry["totals"]["wwcd"] += 1
            logging.info(f"WWCD awarded to {winner_name}.")

    for team_id, team_data in current_match_teams.items():
        team_name = team_data["name"]
        
        if team_name not in state_obj["phase"]["teams"]:
            state_obj["phase"]["teams"][team_name] = {
                "id": team_name,
                "name": team_name,
                "logo": team_data.get("logo", DEFAULT_TEAM_LOGO),
                "totals": {"kills": 0, "placementPoints": 0, "points": 0, "wwcd": 0}
            }
        
        phase_team = state_obj["phase"]["teams"][team_name]
        phase_team["totals"]["kills"] += team_data.get("kills", 0)
        placement_pts = final_placement.get(team_id, 0)
        phase_team["totals"]["placementPoints"] += placement_pts
        
        logging.info(f"Added {team_data.get('kills', 0)} kills and {placement_pts} placement points to {team_name}")
    
    for player_id, player_data in current_match_players.items():
        team_name = _get_team_name_by_id(player_data["teamId"]) or "Unknown Team"
        
        if player_id not in state_obj["phase"]["players"]:
            state_obj["phase"]["players"][player_id] = {
                "id": player_id,
                "name": player_data["name"],
                "photo": player_data.get("photo", DEFAULT_PLAYER_PHOTO),
                "teamName": team_name,
                "live": {"isAlive": False, "health": 0, "healthMax": 100},
                "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
            }
        
        phase_player = state_obj["phase"]["players"][player_id]
        phase_player["totals"]["kills"] += player_data["stats"]["kills"]
        phase_player["totals"]["damage"] += player_data["stats"]["damage"]
        phase_player["totals"]["knockouts"] += player_data["stats"]["knockouts"]
        phase_player["totals"]["matches"] += 1

    for team_data in state_obj["phase"]["teams"].values():
        total_kills = team_data["totals"]["kills"]
        total_placement = team_data["totals"]["placementPoints"]
        team_data["totals"]["points"] = total_kills + total_placement

    logging.info("Phase standings updated and match data finalized.")
    _debug_phase_accumulation(f"Match End - {time.time()}")


    state_obj["match"]["id"] = None
    state_obj["match"]["winnerTeamId"] = None
    state_obj["match"]["winnerTeamName"] = None
    state_obj["match"]["eliminationOrder"] = []
    state_obj["match"]["killFeed"] = []
    state_obj["match"]["teams"] = {}
    state_obj["match"]["players"] = {}
    _cleanup_old_team_mappings()

def _reset_match_but_keep_id(match, game_id):
    """Reset match state but keep the game ID"""
    match.update({
        "id": game_id,  # Keep the game ID
        "status": "live",
        "winnerTeamId": None,
        "winnerTeamName": None,
        "eliminationOrder": [],
        "killFeed": [],
        "teams": {},
        "players": {}
    })

# Add this function to properly calculate live members
def _recalculate_live_members():
    """
    Recalculate live members for each team based on actual player states.
    This should be called after processing player updates.
    """
    for team_id, team_data in state["match"]["teams"].items():
        live_count = 0
        
        # Count live players based on actual player data
        for player_id in team_data.get("players", []):
            player = state["match"]["players"].get(player_id)
            if player:
                # Check multiple conditions for being alive
                is_alive = (
                    player["live"]["isAlive"] and 
                    player["live"]["liveState"] != 5 and  # 5 typically means dead
                    player["live"]["health"] > 0
                )
                
                if is_alive:
                    live_count += 1
        
        # Update the team's live member count
        old_count = team_data.get("liveMembers", 0)
        team_data["liveMembers"] = live_count
        
        # Log significant changes for debugging
        if old_count != live_count and old_count > 0:
            logging.info(f"Team {team_data['name']} live members: {old_count} -> {live_count}")

# Enhanced player death detection
def _process_player_state_changes(log_text):
    """
    Enhanced processing of player state changes, deaths, and knockouts.
    """
    lines = log_text.splitlines()
    
    for line in lines:
        # Match for player death events - multiple patterns
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
                
                # Find and update the player
                _update_player_death_status(player_name, player_health)
                break

        # Match for knockout events
        knockout_patterns = [
            r'G_PlayerKnocked.*?PlayerName=([^,]+)',
            r'PlayerKnocked.*?Name=([^,]+)',
            r'Knockout.*?Player=([^,]+)'
        ]
        
        for pattern in knockout_patterns:
            m = re.search(pattern, line)
            if m:
                player_name = m.group(1).strip('\'"')
                _update_player_knockout_status(player_name)
                break

        # Enhanced player state updates
        state_patterns = [
            r'G_PlayerState_InMatch.*?TeamID=([^/]+)/PlayerID=([^/]+)/PlayerName=([^/]+)/LiveState=([^/]+)/Health=([^,]+),MaxHealth=([^,\)]+)',
            r'PlayerState.*?Team=([^,]+).*?ID=([^,]+).*?Name=([^,]+).*?State=([^,]+).*?Health=([^,]+)'
        ]
        
        for pattern in state_patterns:
            m = re.search(pattern, line)
            if m:
                try:
                    team_id = m.group(1).strip()
                    player_id = m.group(2).strip()
                    player_name = m.group(3).strip('\'"')
                    live_state = int(m.group(4))
                    health = int(float(m.group(5)))
                    health_max = int(float(m.group(6))) if len(m.groups()) >= 6 else 100
                    
                    is_alive = (live_state != 5 and health > 0)
                    
                    player_data = {
                        "id": player_id,
                        "name": player_name,
                        "teamId": team_id
                    }
                    _add_or_update_player(player_data, is_alive, health, health_max)
                    
                except (ValueError, TypeError, IndexError) as e:
                    logging.debug(f"Error parsing player state: {e}")
                break

def _update_player_death_status(player_name, health):
    """Update a specific player's death status by name."""
    for p_id, p_data in state["match"]["players"].items():
        if p_data["name"] == player_name:
            p_data["live"]["isAlive"] = False
            p_data["live"]["health"] = health
            p_data["live"]["liveState"] = 5  # Dead state
            logging.info(f"Player {player_name} died (health: {health})")
            return
    
    # Also check phase players
    for p_id, p_data in state["phase"]["players"].items():
        if p_data["name"] == player_name:
            _add_or_update_player(p_data, is_alive=False, health=health)
            return

def _update_player_knockout_status(player_name):
    """Update a specific player's knockout status by name."""
    for p_id, p_data in state["match"]["players"].items():
        if p_data["name"] == player_name:
            p_data["live"]["isAlive"] = False  # Knocked out players are not alive
            p_data["live"]["liveState"] = 4  # Knocked state
            p_data["stats"]["knockouts"] = p_data["stats"].get("knockouts", 0) + 1
            logging.info(f"Player {player_name} was knocked out")
            return

# Modified _upsert_team_from_teaminfo to register team name mapping
def _upsert_team_from_teaminfo(t, parsed_logos):
    if "next_id" in state["match"]:
        next_id = state["match"].pop("next_id")
        _reset_match_but_keep_id(state["match"], next_id)

    tid = str(t.get("teamId") or "")
    if not tid or tid == "None":
        return
        
    team = state["match"]["teams"].setdefault(tid, {
        "id": tid,
        "name": t.get("teamName") or "Unknown Team",
        "logo": DEFAULT_TEAM_LOGO,
        "liveMembers": 0,
        "kills": 0,
        "placementPointsLive": 0,
        "players": []
    })
    team.setdefault("placementPointsLive", 0)

    # Update team name from TeamInfo
    team_name = t.get("teamName") or team["name"]
    team["name"] = team_name
    
    # Register the team ID -> name mapping for current match
    _register_team_mapping(tid, team_name)
    
    # Check if a logo exists in the pre-parsed data and update
    if parsed_logos and tid in parsed_logos:
        logo_info = parsed_logos[tid]
        team["logo"] = get_asset_url(logo_info["logoPath"], DEFAULT_TEAM_LOGO)

def _upsert_player_from_total(p, mode="chunk"):
    if "next_id" in state["match"]:
        next_id = state["match"].pop("next_id")
        _reset_match_but_keep_id(state["match"], next_id)

    pid = str(p.get("uId") or "")
    tid = str(p.get("teamId") or "")
    if not pid or not tid or tid == "None":
        return

    # Team presence
    team = state["match"]["teams"].setdefault(tid, {
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

    # Player live & stats
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

    # CRITICAL FIX: Handle kill count based on mode
    new_kills = int(p.get("killNum") or 0)
    
    if mode == "chunk":
        # In chunk mode, only increment if kills actually increased
        current_kills = player["stats"]["kills"]
        if new_kills > current_kills:
            # Only add the difference, not the total
            kill_diff = new_kills - current_kills
            player["stats"]["kills"] = new_kills
            
            # Add kill feed entry only for the new kills
            if kill_diff > 0:
                team_name = _get_team_name_by_id(tid) or "Unknown Team"
                pn = player["name"]
                for _ in range(kill_diff):
                    state["match"]["killFeed"].append(f"Kill: {pn} ({team_name}) got a new kill!")
                state["match"]["killFeed"] = state["match"]["killFeed"][-5:]
    else:
        # In full mode, set the kills directly (this is cumulative from match start)
        player["stats"]["kills"] = new_kills

    player["stats"]["damage"] = int(p.get("damage") or 0)
    player["stats"]["knockouts"] = int(p.get("knockouts") or 0)

    # Also update the phase player data with health info
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

    # Team kills aggregate
    _recompute_team_kills(tid)

def _recompute_team_kills(tid):
    team = state["match"]["teams"].get(tid)
    if not team:
        return
    total = 0
    for pid in team["players"]:
        total += state["match"]["players"].get(pid, {}).get("stats", {}).get("kills", 0)
    team["kills"] = total

def _update_live_eliminations():
    """
    Update live eliminations - now tracking by team names instead of IDs
    """
    for tid, t in state["match"]["teams"].items():
        team_name = t["name"]
        if t["liveMembers"] == 0 and team_name not in state["match"]["eliminationOrder"]:
            state["match"]["eliminationOrder"].append(team_name)
            logging.info(f"Team {team_name} eliminated")

    alive = [tid for tid, t in state["match"]["teams"].items() if t["liveMembers"] > 0]
    if len(alive) == 1 and state["match"]["status"] == "live":
        state["match"]["winnerTeamId"] = alive[0]
        state["match"]["winnerTeamName"] = _get_team_name_by_id(alive[0])
        state["match"]["status"] = "finished"

def _placement_points_live():
    """
    Compute placement points based on elimination order.
    Only eliminated teams get placement points during live match.
    Surviving teams get 0 points until match ends.
    """
    points = {}
    eliminated_names = state["match"]["eliminationOrder"]
    
    # Only assign points to eliminated teams
    total_teams = len(state["match"]["teams"])
    
    for i, team_name in enumerate(reversed(eliminated_names)):
        # Last eliminated gets highest remaining rank
        rank = total_teams - i
        team_id = _get_team_id_by_name(team_name)
        if team_id:
            points[team_id] = PLACEMENT_POINTS.get(rank, 0)
    
    # Surviving teams get 0 points during live match
    for team_id in state["match"]["teams"]:
        if team_id not in points:
            points[team_id] = 0
    
    return points

def _calculate_final_placement_points():
    """Calculates placement points for a match based on elimination order or final kills if order is empty."""
    points = {}
    total_teams = len(state["match"]["teams"])

    # This is the primary method - it relies on eliminationOrder being correct
    eliminated_names = state["match"]["eliminationOrder"]
    
    # Check if the elimination list is incomplete
    if len(eliminated_names) < total_teams - 1:
        logging.warning("Incomplete elimination order. Using fallback placement calculation.")
        
        # Fallback method: Rank teams based on their live members and then kills.
        
        # 1. Identify all teams
        all_teams_data = list(state["match"]["teams"].values())
        
        # 2. Sort teams: non-eliminated first, then by decreasing kills
        all_teams_data.sort(key=lambda t: (t.get("liveMembers", 0) > 0, t.get("kills", 0)), reverse=True)
        
        # 3. Assign ranks and points based on this sorted list
        for i, team_data in enumerate(all_teams_data):
            rank = i + 1
            team_id = team_data["id"]
            
            # The placement points are based on rank, not on a full elimination order.
            points[team_id] = PLACEMENT_POINTS.get(rank, 0)

        # Log the fallback results for debugging
        logging.info("DEBUG PLACEMENT CALCULATION (FALLBACK):")
        for i, team_data in enumerate(all_teams_data):
            team_id = team_data["id"]
            kills = team_data.get("kills", 0)
            rank = i + 1
            placement_pts = points.get(team_id, 0)
            status = "alive" if team_data.get("liveMembers", 0) > 0 else "eliminated"
            logging.info(f"  {team_data['name']} ({status}, {kills} kills) -> Rank {rank} -> {placement_pts} points")

    else:
        # This is the correct method when the elimination order is fully populated.
        # This part of the code you already have.
        
        # Assign ranks and points to eliminated teams
        for i, team_name in enumerate(reversed(eliminated_names)):
            rank = total_teams - i
            team_data = state["match"]["teams"].get(team_name)
            if team_data:
                team_id = team_data["id"]
                points[team_id] = PLACEMENT_POINTS.get(rank, 0)
        
        # Assign points to the WWCD winner (if there is one)
        wwcd_winner = [t for t in state["match"]["teams"].values() if t.get("liveMembers", 0) > 0]
        if len(wwcd_winner) == 1:
            winner_team = wwcd_winner[0]
            points[winner_team["id"]] = PLACEMENT_POINTS.get(1, 0)
        
        # Log the primary method results for debugging
        logging.info("DEBUG PLACEMENT CALCULATION (PRIMARY):")
        for i, team_name in enumerate(reversed(eliminated_names)):
            rank = total_teams - i
            logging.info(f"  {team_name} -> Rank {rank} -> {PLACEMENT_POINTS.get(rank, 0)} points")
        if len(wwcd_winner) == 1:
            logging.info(f"  {winner_team['name']} -> Rank 1 -> {PLACEMENT_POINTS.get(1, 0)} points (WWCD)")

    return points

# FIXED: all_time_top_players to handle malformed data
def _all_time_top_players():
    players = []
    for pid, p in state["all_time"]["players"].items():
        # Guard against malformed data where p might be a string instead of dict
        if not isinstance(p, dict):
            logging.warning(f"Malformed player data for {pid}: {type(p)}")
            continue
            
        t = p.get("totals", {})
        if not isinstance(t, dict):
            logging.warning(f"Malformed totals data for player {pid}: {type(t)}")
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

# ---------- Aggregation (phase & all-time) ----------
def apply_archived_file_to_all_time(log_text, parsed_logos):
    # backup current match
    temp_before = state["match"].copy()
    temp_mapping_before = state["teamNameMapping"].copy()

    # parse the log into state["match"]
    parse_and_apply(log_text, parsed_logos=parsed_logos, mode="full")

    # update all-time players from parsed match
    for pid, pl in state["match"]["players"].items():
        team_name = _get_team_name_by_id(pl.get("teamId")) or "Unknown Team"
        
        at = state["all_time"]["players"].setdefault(pid, {
            "id": pid,
            "name": pl["name"],
            "teamName": team_name,
            "photo": pl.get("photo") or DEFAULT_PLAYER_PHOTO,
            "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
        })
        # always update values
        at["name"] = pl["name"]
        at["photo"] = pl.get("photo") or at["photo"]
        at["teamName"] = team_name
        at["totals"]["kills"] += int(pl["stats"]["kills"])
        at["totals"]["damage"] += int(pl["stats"]["damage"])
        at["totals"]["knockouts"] += int(pl["stats"]["knockouts"])
        at["totals"]["matches"] += 1

    # restore previous match and mapping (archives should not affect live match)
    state["match"].update(temp_before)
    state["teamNameMapping"] = temp_mapping_before

# ---------- IO ----------
def get_all_log_files(log_dir, exclude_live_log=True):
    """Get all log files in log_dir, excluding subdirectories and live log."""
    if not log_dir.exists():
        return []
    out = []
    # Only process files directly in the root directory, not subdirectories
    for item in log_dir.iterdir():
        if item.is_file() and item.suffix == ".txt" and not (exclude_live_log and item.name == "simulated_live.txt"):
            out.append(item)
    return sorted(out)

def process_archives_for_all_time(parsed_logos, force_repopulate=False):
    """Process archived logs for all-time top players, but only if needed."""
    if force_repopulate:
        logging.info("Force reprocessing all-time player data...")
    elif load_all_time_players():
        logging.info("Using cached all-time player data.")
        return

    logging.info("Processing archived logs for ALL-TIME top players...")
    archived_logs = get_all_log_files(ARCHIVE_LOG_DIR, exclude_live_log=False)
    if not archived_logs:
        logging.warning("No archived logs found.")
        return

    logging.info(f"Found {len(archived_logs)} archived logs to process.")

    for f in archived_logs:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                apply_archived_file_to_all_time(fh.read(), parsed_logos)
        except Exception as e:
            logging.warning(f"Archive error for {f}: {e}")

    save_all_time_players()
    logging.info("Done (all-time).")

def load_all_time_players():
    """Load all-time player data from JSON file if it exists."""
    if ALL_TIME_PLAYERS_JSON.exists():
        try:
            with open(ALL_TIME_PLAYERS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                players_data = data.get("players", {})
                
                # Validate and clean the data
                clean_players = {}
                for pid, pdata in players_data.items():
                    if isinstance(pdata, dict) and "totals" in pdata and isinstance(pdata["totals"], dict):
                        clean_players[pid] = pdata
                    else:
                        logging.warning(f"Removing malformed player data for {pid}")
                
                state["all_time"]["players"] = clean_players
                logging.info(f"Loaded {len(clean_players)} all-time players from {ALL_TIME_PLAYERS_JSON}")
                return True
        except Exception as e:
            logging.error(f"Error loading all-time player data: {e}")
    return False

def save_all_time_players():
    """Save all-time player data to JSON file."""
    try:
        with open(ALL_TIME_PLAYERS_JSON, "w", encoding="utf-8") as f:
            json.dump({"players": state["all_time"]["players"]}, f, indent=2, ensure_ascii=False)
        logging.info(f"Saved all-time player data to {ALL_TIME_PLAYERS_JSON}")
    except Exception as e:
        logging.error(f"Error saving all-time player data: {e}")

# FIXED: process_current_phase_files now properly accumulates phase data
def process_current_phase_files(log_files_to_process, parsed_logos):
    logging.info("Processing current-phase logs for phase standings...")
    logging.info(f"Files to process: {[f.name for f in log_files_to_process]}")

    if not parsed_logos:
        logging.warning("Could not load team logos. Continuing without them.")

    for f in log_files_to_process:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                log_text = fh.read()
                parse_and_apply(log_text, parsed_logos=parsed_logos, mode="full")
                _add_match_to_phase_totals()
        except Exception as e:
            logging.warning(f"Phase file error for {f}: {e}")

    logging.info("Done (phase).")

def _add_match_to_phase_totals():
    """
    Add the current match data to phase totals using team names.
    This should be called after processing each completed match file.
    """
    if not state["match"]["id"]:
        return
        
    # Calculate final placement points for this match
    final_placement = _calculate_final_placement_points()
    
    for tid, team in state["match"]["teams"].items():
        team_name = team["name"]
        
        # Use team name as the key for phase data
        phase_team = state["phase"]["teams"].setdefault(team_name, {
            "id": team_name,
            "name": team_name,
            "logo": team.get("logo", DEFAULT_TEAM_LOGO),
            "totals": {"kills": 0, "placementPoints": 0, "points": 0, "wwcd": 0}
        })

        # Update name/logo if changed
        phase_team["name"] = team_name
        phase_team["logo"] = team.get("logo", phase_team["logo"])

        # Accumulate stats
        phase_team["totals"]["kills"] += int(team.get("kills", 0))
        # phase_team["totals"]["placementPoints"] += final_placement.get(tid, 0)
        phase_team["totals"]["points"] += int(team.get("kills", 0)) + final_placement.get(tid, 0)
        phase_team["totals"]["wwcd"] += 1 if state["match"]["winnerTeamId"] == tid else 0

    # Add player data to phase totals (now using team names)
    for pid, player in state["match"]["players"].items():
        team_name = _get_team_name_by_id(player["teamId"]) or "Unknown Team"
        
        phase_player = state["phase"]["players"].setdefault(pid, {
            "id": pid,
            "name": player["name"],
            "photo": player.get("photo", DEFAULT_PLAYER_PHOTO),
            "teamName": team_name,
            "live": {"isAlive": False, "health": 0, "healthMax": 100},
            "totals": {"kills": 0, "damage": 0, "knockouts": 0, "matches": 0}
        })
        
        # Update basic info
        phase_player["name"] = player["name"]
        phase_player["photo"] = player.get("photo", phase_player["photo"])
        phase_player["teamName"] = team_name
        
        # ACCUMULATE stats instead of replacing
        phase_player["totals"]["kills"] += int(player["stats"]["kills"])
        phase_player["totals"]["damage"] += int(player["stats"]["damage"])
        phase_player["totals"]["knockouts"] += int(player["stats"]["knockouts"])
        phase_player["totals"]["matches"] += 1

def _current_match_top_players():
    players = []
    for pid, p in state["match"]["players"].items():
        team_name = _get_team_name_by_id(p["teamId"]) or "Unknown Team"
        players.append({
            "playerId": pid, "teamName": team_name, "name": p["name"],
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
    
    # Sort by team, then by health status (alive first), then by kills
    active_players.sort(key=lambda p: (
        p["teamName"],
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
            "players": state["phase"]["players"]
        },

        "match": state["match"],

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
    print("\nLIVE SCOREBOARD")
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

def confirm_file_setup():
    """Display detected files and ask user for confirmation."""
    print("\n" + "="*60)
    print("FILE SETUP CONFIRMATION")
    print("="*60)

    # Check all_time_players.json
    all_time_file = ALL_TIME_PLAYERS_JSON
    if all_time_file.exists():
        try:
            with open(all_time_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                player_count = len(data.get("players", {}))
                print(f"Found all_time_players.json with {player_count} players")
        except Exception as e:
            print(f"Found all_time_players.json but couldn't read it: {e}")
    else:
        print("No all_time_players.json found (will be created)")

    # Check archived logs
    archived_logs = get_all_log_files(ARCHIVE_LOG_DIR, exclude_live_log=False)
    print(f"\nArchived logs in {ARCHIVE_LOG_DIR}: {len(archived_logs)} files")
    if archived_logs:
        print("   Sample files:", ", ".join(f.name for f in archived_logs[:3]))
        if len(archived_logs) > 3:
            print(f"   ...and {len(archived_logs) - 3} more")

    # Check current phase logs
    current_phase_logs = get_all_log_files(CURRENT_LOG_DIR, exclude_live_log=True)
    print(f"\nCurrent phase logs in {CURRENT_LOG_DIR}: {len(current_phase_logs)} files")
    if current_phase_logs:
        print("   Sample files:", ", ".join(f.name for f in current_phase_logs[:3]))
        if len(current_phase_logs) > 3:
            print(f"   ...and {len(current_phase_logs) - 3} more")

    # Check for simulated_live.txt
    simulated_live_path = ROOT_DIR / "simulated_live.txt"
    if simulated_live_path.exists():
        print(f"\nFound simulated_live.txt in root directory")
    else:
        print(f"\nNo simulated_live.txt found in root directory")

    print("\n" + "-"*60)
    print("OPTIONS:")
    print("1. Continue with current setup")
    print("2. Reprocess all-time players (reprocess archived logs)")
    print("3. Exit script")
    print("-"*60)

    while True:
        choice = input("\nEnter your choice (1-3): ").strip()
        if choice == "1":
            return "continue"
        elif choice == "2":
            return "reprocess"
        elif choice == "3":
            print("Exiting script...")
            exit(0)
        else:
            print("Invalid choice. Please enter 1, 2, or 3.")


def main():
    os.makedirs(CURRENT_LOG_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_LOG_DIR, exist_ok=True)
    os.makedirs(LOGO_FOLDER_PATH, exist_ok=True)

    # Load team logos ONCE at the very beginning
    ini_file_path = "./TeamLogoAndColor.ini"
    team_logos = get_team_logos(ini_file_path)
    if not team_logos:
        logging.warning("Could not load team logos. Continuing without them.")

    # Ask user to confirm file setup
    action = confirm_file_setup()

    # Process all-time players based on user choice
    if action == "reprocess":
        process_archives_for_all_time(team_logos, force_repopulate=True)
    else:
        process_archives_for_all_time(team_logos, force_repopulate=False)

    # Process completed logs for cumulative phase standings
    current_phase_logs = get_all_log_files(CURRENT_LOG_DIR, exclude_live_log=True)
    process_current_phase_files(current_phase_logs, team_logos)

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
            
            # Check for a new live log file or if the current one has stopped updating
            is_stalled = current_log and (now - last_update_time > 10) and last_pos > 0
            if not current_log or is_stalled:
                logging.info("Checking for a new live log file...")
                
                # --- Key Fix 1: Finalize the previous match if the log stalled or a new one appeared ---
                if state["match"]["status"] == "live" or state["match"]["id"]:
                    logging.info(f"Finalizing previous match {state['match']['id']} before switching to new log.")
                    end_match_and_update_phase(state)
                    state["match"]["id"] = None
                    state["match"]["status"] = "idle"

                all_logs = get_all_log_files(CURRENT_LOG_DIR)
                if all_logs:
                    most_recent_log = all_logs[-1]
                    if _is_log_updating(most_recent_log, min_size=500):
                        logging.info(f"New live log file detected: {most_recent_log}")
                        
                        current_log = most_recent_log
                        last_pos = 0 # Reset for the new file
                        last_update_time = now

                        try:
                            with open(current_log, "r", encoding="utf-8") as f:
                                log_text = f.read()
                                parse_and_apply(log_text, parsed_logos=team_logos, mode="full")
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
                    
                        # --- Key Fix 2: Perform the end-of-match check immediately after processing a new chunk ---
                        alive_teams = [t for t in state["match"]["teams"].values() if t["liveMembers"] > 0]
                        if len(alive_teams) <= 1 and state["match"]["status"] == "live":
                            logging.info(f"Match end detected. Finalizing scores for match ID: {state['match']['id']}")
                            state["match"]["status"] = "finished"
                            end_match_and_update_phase(state)
                            state["match"]["id"] = None
                            state["match"]["status"] = "idle"
                
            # Periodic updates for JSON and terminal output
            if now - last_json >= UPDATE_INTERVAL:
                _export_json()
                last_json = now
            if now - last_term >= 1.0:
                _print_terminal_snapshot()
                last_term = now

            time.sleep(FILE_CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopped by user.")
        if state["match"]["status"] == "live" and state["match"]["id"]:
            teams_with_eliminations = 0
            for team_data in state["match"]["teams"].values():
                total_players = len(team_data.get("players", []))
                live_players = team_data.get("liveMembers", 0)
                
                if total_players > 0 and live_players < total_players:
                    teams_with_eliminations += 1
            
            if teams_with_eliminations > 0:
                print(f"Finalizing interrupted match - {teams_with_eliminations} teams had eliminations")
                end_match_and_update_phase(state)
            else:
                print("Skipping finalization - no eliminations detected (match may not have started)")
            
        _export_json()
    except Exception as e:
        logging.exception(f"Live error: {e}")
        try:
            _export_json()
        except Exception:
            pass

if __name__ == "__main__":
    main()