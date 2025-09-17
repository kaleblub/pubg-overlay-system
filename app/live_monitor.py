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
previous_match = {}

# ---------- State (normalized) ----------
state = {
    "phase": {
        "teams": {},
        "players": {}
    },
    "all_time": {
        "players": {}
    },
    "match": {
        "id": None,
        "status": "idle",
        "winnerTeamId": None,
        "winnerTeamName": None,
        "eliminationOrder": [],
        "killFeed": [],
        "teams": {},
        "players": {}
    },
    "teamNameMapping": {},
    "previous_match": {  # keep the single "last match" for convenience
        "status": "idle",
        "phase": {"standings": []},
        "players": {},
        "leaderboards": {"currentMatchTopPlayers": []},
        "live": {"activePlayers": [], "teamKills": {}},
        "teams": {}
    },
    "match_history": [],
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
        team_name = teams_dict.get(team_id, {}).get("teamName", "Unknown Team")

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
    """Finalizes match data, copies it to previous, and resets current live state."""
    if state["match_state"]["status"] == "live" and state["match"]["id"]:
        logging.info(f"Finalizing and persisting match ID: {state['match']['id']}")
        
        # Deep copy the current match state
        final_match_data = copy.deepcopy(state["match"])
        
        # Recalculate standings for the final state
        _end_match_and_update_phase(state, final_match_data)
        logging.info("Match standings have been updated.")
        
        # Populate the state["previous_match"]
        state["previous_match"] = {
            "id": final_match_data["id"],
            "status": "completed",
            "winnerTeamId": final_match_data.get("winnerTeamId"),
            "winnerTeamName": final_match_data.get("winnerTeamName"),
            "eliminationOrder": final_match_data["eliminationOrder"],
            "killFeed": final_match_data["killFeed"],
            "teams": final_match_data["teams"],
            "players": final_match_data["players"],
            "leaderboards": {
                "currentMatchTopPlayers": _calculate_top_players(final_match_data["players"], final_match_data["teams"])
            },
            "live": {
                "activePlayers": [],
                "teamKills": {}
            },
            "phase": {
                "standings": state["phase"]["standings"]
            }
        }
        logging.info("Previous match data populated. Data should now be available in JSON.")
        
        # Reset the live match state
        state["match"] = {
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
        logging.info("Live match state has been reset to idle.")
    else:
        logging.warning("Finalization requested but no active match ID. Skipping.")
        
        # Ensure match_state.status is idle if match.id is None
        if not state["match"]["id"]:
            state["match_state"]["status"] = "idle"
            state["match_state"]["last_updated"] = int(time.time())
            logging.info("Set match_state.status to idle due to no active match ID.")
        
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
    match_id = state["match"]["id"]
    if not match_id:
        return
    
    for team_id, team_data in state["match"]["teams"].items():
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
            player_data = state["match"]["players"].get(player_id)
            if player_data:
                _add_or_update_player({
                    "id": player_id,
                    "name": player_data.get("name", "Unknown Player"),
                    "photo": player_data.get("photo", DEFAULT_PLAYER_PHOTO),
                    "teamName": team_name,
                    "teamId": team_id
                }, is_alive=player_data["live"]["isAlive"])

def parse_and_apply(log_text, parsed_logos=None, mode="chunk"):
    """Parse log text and apply to state."""
    gid = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", log_text)
    
    new_game_id = None
    if gid:
        new_game_id = gid.group(1)
        
    if new_game_id and state["match"]["id"] and new_game_id != state["match"]["id"]:
        logging.info(f"NEW MATCH DETECTED: {new_game_id} (previous: {state['match']['id']})")
        end_match_and_update_phase(state)
        _reset_match_but_keep_id(state["match"], new_game_id)
        
    if not state["match"]["id"]:
        if new_game_id:
            logging.info(f"INITIALIZING MATCH: {new_game_id}")
            state["match"]["id"] = new_game_id
            state["match"]["status"] = "live"
            
            if mode == "full":
                logging.info("FULL MODE: Resetting match state")
                _reset_match_but_keep_id(state["match"], new_game_id)

    _process_player_state_changes(log_text)

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

    _recalculate_live_members()
    _update_live_eliminations()
    _update_phase_from_live_match()

    # Ensure match_state reflects the current match status after processing
    state["match_state"]["status"] = "live" if state["match"]["id"] else "idle"
    state["match_state"]["last_updated"] = int(time.time())
    
    return state["match"]["id"]

def _reset_match_but_keep_id(match, game_id):
    """Reset match state but keep the game ID"""
    match.update({
        "id": game_id,
        "status": "live",
        "winnerTeamId": None,
        "winnerTeamName": None,
        "eliminationOrder": [],
        "killFeed": [],
        "teams": {},
        "players": {}
    })

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

    team_name = t.get("teamName") or team["name"]
    team["name"] = team_name
    _register_team_mapping(tid, team_name)
    
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

    new_kills = int(p.get("killNum") or 0)
    
    if mode == "chunk":
        current_kills = player["stats"]["kills"]
        if new_kills > current_kills:
            kill_diff = new_kills - current_kills
            player["stats"]["kills"] = new_kills
            
            if kill_diff > 0:
                team_name = _get_team_name_by_id(tid) or "Unknown Team"
                pn = player["name"]
                for _ in range(kill_diff):
                    state["match"]["killFeed"].append(f"Kill: {pn} ({team_name}) got a new kill!")
                state["match"]["killFeed"] = state["match"]["killFeed"][-5:]
    else:
        player["stats"]["kills"] = new_kills

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
    team = state["match"]["teams"].get(tid)
    if not team:
        return
    total = 0
    for pid in team["players"]:
        total += state["match"]["players"].get(pid, {}).get("stats", {}).get("kills", 0)
    team["kills"] = total

def _recalculate_live_members():
    """Recalculate live members for each team."""
    for team_id, team_data in state["match"]["teams"].items():
        live_count = 0
        
        for player_id in team_data.get("players", []):
            player = state["match"]["players"].get(player_id)
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
    for p_id, p_data in state["match"]["players"].items():
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

def end_match_and_update_phase(state_obj):
    global previous_match

    print_colored(f"\nFinalizing match {state_obj['match']['id']}", Fore.CYAN, Style.BRIGHT)

    if not state_obj["match"]["id"]:
        logging.warning("end_match_and_update_phase called but no match ID is set.")
        return

    current_match_teams = state_obj["match"]["teams"].copy()
    current_match_players = state_obj["match"]["players"].copy()
    elimination_order = list(state_obj["match"]["eliminationOrder"])

    # Separate and sort teams …
    eliminated_teams_data = [t for t in current_match_teams.values() if t['name'] in elimination_order]
    survivor_teams_data = [t for t in current_match_teams.values() if t['name'] not in elimination_order]
    survivor_teams_data.sort(key=lambda t: t['kills'], reverse=True)
    eliminated_teams_data.sort(key=lambda t: elimination_order.index(t['name']), reverse=True)

    # Assign final ranks and placement points
    all_teams_data = survivor_teams_data + eliminated_teams_data
    final_placement = {}
    for i, team_data in enumerate(all_teams_data):
        rank = i + 1
        placement_pts = PLACEMENT_POINTS.get(rank, 0)
        final_placement[team_data["id"]] = placement_pts

    # 🔑 Inject placement points into the current match teams
    for tid, placement_pts in final_placement.items():
        if tid in state_obj["match"]["teams"]:
            state_obj["match"]["teams"][tid]["placementPointsLive"] = placement_pts

    # ✅ Now snapshot previous_match AFTER placement points are injected
    previous_match = {
        "id": state_obj["match"]["id"],
        "status": "completed",
        "winnerTeamId": state_obj["match"]["winnerTeamId"],
        "winnerTeamName": state_obj["match"]["winnerTeamName"],
        "eliminationOrder": list(state_obj["match"]["eliminationOrder"]),
        "killFeed": list(state_obj["match"]["killFeed"]),
        "teams": dict(state_obj["match"]["teams"]),
        "players": dict(state_obj["match"]["players"])
    }

    state_obj["previous_match"] = copy.deepcopy(previous_match)

    # Award WWCD
    if all_teams_data and all_teams_data[0]["liveMembers"] > 0:
        winner_name = all_teams_data[0]["name"]
        winner_entry = state_obj["phase"]["teams"].get(winner_name)
        if winner_entry:
            winner_entry["totals"]["wwcd"] += 1

    # Update phase totals
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

    # Update player totals
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

    # Recalculate total points
    for team_data in state_obj["phase"]["teams"].values():
        total_kills = team_data["totals"]["kills"]
        total_placement = team_data["totals"]["placementPoints"]
        team_data["totals"]["points"] = total_kills + total_placement

    # Reset match state - BUT DON'T RESET THE ID HERE
    # The ID will be set when a new match is detected
    state_obj["match"]["status"] = "idle"
    state_obj["match"]["winnerTeamId"] = None
    state_obj["match"]["winnerTeamName"] = None
    state_obj["match"]["eliminationOrder"] = []
    state_obj["match"]["killFeed"] = []
    state_obj["match"]["teams"] = {}
    state_obj["match"]["players"] = {}
    # Only reset ID after everything else is cleaned up
    state_obj["match"]["id"] = None
    _cleanup_old_team_mappings()

def _print_terminal_snapshot(test_mode=False):
    """Enhanced terminal output with colors and simulation progress."""
    m = state["match"]
    
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
    for pid, p in state["match"]["players"].items():
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

def _live_active_players():
    """Returns active players with health data."""
    active_players = []
    
    if state["match"]["status"] == "live":
        for player_id, player_data in state["match"]["players"].items():
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
    
    active_players.sort(key=lambda p: (
        p["teamName"],
        not p["live"]["isAlive"],
        -p["stats"]["kills"]
    ))
    
    return active_players

def _live_team_kills():
    """Returns current match team kills."""
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
    """Exports the current state to a JSON file."""
    try:
        # Use state["previous_match"] instead of the global previous_match variable
        local_previous_match = state["previous_match"] if state["previous_match"] is not None else {}
        
        data = {
            "match_state": state["match_state"],
            "phase": {
                "standings": _phase_standings(),
                "allTimeTopPlayers": _all_time_top_players()
            },
            "match": {
                "id": state["match"]["id"],
                "status": state["match"]["status"],
                "winnerTeamId": state["match"]["winnerTeamId"],
                "winnerTeamName": state["match"]["winnerTeamName"],
                "eliminationOrder": state["match"]["eliminationOrder"],
                "killFeed": state["match"]["killFeed"],
                "teams": state["match"]["teams"],
                "players": state["match"]["players"],
                "leaderboards": {
                    "currentMatchTopPlayers": _current_match_top_players()
                }
            },
            "live": {
                "activePlayers": _live_active_players(),
                "teamKills": _live_team_kills()
            },
            "previous": {
                "id": local_previous_match.get("id"),
                "status": local_previous_match.get("status"),
                "phase": local_previous_match.get("phase", {"standings": []}),
                "players": local_previous_match.get("players", {}),
                "leaderboards": local_previous_match.get("leaderboards", {"currentMatchTopPlayers": []}),
                "live": local_previous_match.get("live", {"activePlayers": [], "teamKills": {}}),
                "teams": local_previous_match.get("teams", {})  # Add teams to previous block
            }
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
    temp_before = state["match"].copy()
    temp_mapping_before = state["teamNameMapping"].copy()

    parse_and_apply(log_text, parsed_logos=parsed_logos, mode="full")

    for pid, pl in state["match"]["players"].items():
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

    state["match"].update(temp_before)
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

    for f in archived_logs:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                apply_archived_file_to_all_time(fh.read(), parsed_logos)
        except Exception as e:
            logging.warning(f"Archive error for {f}: {e}")

    save_all_time_players()
    print_colored("All-time processing complete.", Fore.GREEN)

def process_current_phase_files(log_files_to_process, parsed_logos):
    """Process current phase log files."""
    if not log_files_to_process:
        print_colored("No current phase logs to process.", Fore.WHITE)
        return
        
    print_colored("Processing current-phase logs...", Fore.CYAN)
    print_colored(f"Files to process: {len(log_files_to_process)}", Fore.WHITE)

    for f in log_files_to_process:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                log_text = fh.read()
                parse_and_apply(log_text, parsed_logos=parsed_logos, mode="full")
                _add_match_to_phase_totals()
        except Exception as e:
            logging.warning(f"Phase file error for {f}: {e}")

    print_colored("Phase processing complete.", Fore.GREEN)

def _add_match_to_phase_totals():
    """Add current match data to phase totals."""
    if not state["match"]["id"]:
        return
        
    final_placement = _calculate_final_placement_points()
    
    for tid, team in state["match"]["teams"].items():
        team_name = team["name"]
        
        phase_team = state["phase"]["teams"].setdefault(team_name, {
            "id": team_name,
            "name": team_name,
            "logo": team.get("logo", DEFAULT_TEAM_LOGO),
            "totals": {"kills": 0, "placementPoints": 0, "points": 0, "wwcd": 0}
        })

        phase_team["name"] = team_name
        phase_team["logo"] = team.get("logo", phase_team["logo"])
        phase_team["totals"]["kills"] += int(team.get("kills", 0))
        phase_team["totals"]["placementPoints"] += final_placement.get(tid, 0)
        phase_team["totals"]["points"] += int(team.get("kills", 0)) + final_placement.get(tid, 0)
        phase_team["totals"]["wwcd"] += 1 if state["match"]["winnerTeamId"] == tid else 0

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
        
        phase_player["name"] = player["name"]
        phase_player["photo"] = player.get("photo", phase_player["photo"])
        phase_player["teamName"] = team_name
        phase_player["totals"]["kills"] += int(player["stats"]["kills"])
        phase_player["totals"]["damage"] += int(player["stats"]["damage"])
        phase_player["totals"]["knockouts"] += int(player["stats"]["knockouts"])
        phase_player["totals"]["matches"] += 1

def _calculate_final_placement_points():
    """Calculate final placement points for the match."""
    points = {}
    total_teams = len(state["match"]["teams"])
    eliminated_names = state["match"]["eliminationOrder"]
    
    if len(eliminated_names) < total_teams - 1:
        # Fallback: rank by live members then kills
        all_teams_data = list(state["match"]["teams"].values())
        all_teams_data.sort(key=lambda t: (t.get("liveMembers", 0) > 0, t.get("kills", 0)), reverse=True)
        
        for i, team_data in enumerate(all_teams_data):
            rank = i + 1
            team_id = team_data["id"]
            points[team_id] = PLACEMENT_POINTS.get(rank, 0)
    else:
        # Standard elimination order method
        for i, team_name in enumerate(reversed(eliminated_names)):
            rank = total_teams - i
            team_id = _get_team_id_by_name(team_name)
            if team_id:
                points[team_id] = PLACEMENT_POINTS.get(rank, 0)
        
        # WWCD winner gets first place
        wwcd_winner = [t for t in state["match"]["teams"].values() if t.get("liveMembers", 0) > 0]
        if len(wwcd_winner) == 1:
            winner_team = wwcd_winner[0]
            points[winner_team["id"]] = PLACEMENT_POINTS.get(1, 0)

    return points

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
    # print_colored("2. Reprocess all-time players", Fore.YELLOW)
    print_colored("3. Exit", Fore.RED)
    print_colored("-"*60, Fore.BLUE)

    while True:
        choice = input(f"{Fore.CYAN}Enter your choice (1-3): {Style.RESET_ALL}").strip()
        if choice == "1":
            return "continue"
        elif choice == "2":
            return "reprocess"
        elif choice == "3":
            print_colored("Exiting...", Fore.YELLOW)
            exit(0)
        else:
            print_colored("Invalid choice. Please enter 1, 2, or 3.", Fore.RED)

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
previous_match = None

def setup_force_end_thread():
    """Setup a background thread to listen for force end commands."""
    def listen_for_commands():
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
                    complete_shutdown_requested = True
                    break
                    
                time.sleep(1)
            except Exception as e:
                logging.error(f"Error in force end listener: {e}")
                break
    
    thread = threading.Thread(target=listen_for_commands, daemon=True)
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
    # In request_finalization() and perform_finalization():
    if state["match"]["status"] in ["live", "finished"]:
        end_match_and_update_phase(state)  # If not already called
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
    # In request_finalization() and perform_finalization():
    if state["match"]["status"] in ["live", "finished"]:
        end_match_and_update_phase(state)  # If not already called
        _finalize_and_persist()
    global finalization_requested
    
    print_colored("\nPerforming finalization...", Fore.CYAN, Style.BRIGHT)
    
    # Finalize any active or finished match and update phase
    if state["match"]["id"] and (state["match"]["status"] in ["live", "finished"]):
        # Always finalize the match when timing out to preserve data
        print_colored(f"Finalizing match {state['match']['id']} and updating phase standings", Fore.CYAN)
        end_match_and_update_phase(state)
    
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

def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown."""
    import signal
    
    def signal_handler(signum, frame):
        print_colored(f"\nReceived signal {signum}. Requesting complete shutdown...", Fore.YELLOW)
        global complete_shutdown_requested
        with finalization_lock:
            complete_shutdown_requested = True
    
    # Handle common termination signals
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal_handler)

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

    # Handle reprocess mode
    if reprocess:
        print_colored("Reprocessing all archived data...", Fore.MAGENTA, Style.BRIGHT)
        process_archives_for_all_time(team_logos, force_repopulate=True)
        print_colored("Reprocessing complete!", Fore.GREEN)
        return

    # File setup confirmation
    action = confirm_file_setup()

    # Process all-time players
    if action == "reprocess":
        process_archives_for_all_time(team_logos, force_repopulate=True)
    else:
        process_archives_for_all_time(team_logos, force_repopulate=False)

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

    # Main monitoring loop - unified for both test and production
    current_log_path = None
    last_pos = 0
    last_update_time = time.time()
    last_json = 0
    last_term = 0
    no_update_start_time = None
    INACTIVITY_TIMEOUT = 60  # 1 minute

    try:
        print_colored(f"\nStarting {mode.lower()} monitoring...", Fore.CYAN, Style.BRIGHT)
        if test_mode:
            print_colored(f"Monitoring simulated log: {SIMULATED_LOG_FILE}", Fore.YELLOW)
        else:
            print_colored("Monitoring for live log files...", Fore.GREEN)

        while True:
            # Check for finalization request
            if should_finalize():
                print_colored("Finalization requested. Exiting main loop.", Fore.YELLOW)
                _finalize_and_persist() # Perform final action before break
                break
        
            now = time.time()

            
            # Find the path of the next log to process
            next_log_path = None
            if test_mode:
                if SIMULATED_LOG_FILE.exists():
                    next_log_path = SIMULATED_LOG_FILE
            else:
                all_logs = get_all_log_files(CURRENT_LOG_DIR)
                if all_logs:
                    next_log_path = all_logs[-1]

            log_was_updated = False

            # If a new log file is detected, process it from the beginning
            if next_log_path and next_log_path != current_log_path:
                current_log_path = next_log_path
                last_pos = 0
                last_update_time = now
                log_was_updated = True
                no_update_start_time = None
                
                logging.info(f"New log file detected: {current_log_path}. Reading from the start.")
                try:
                    with open(current_log_path, "r", encoding="utf-8") as f:
                        log_text = f.read()
                        # 'full' mode is only used once per new log file
                        parse_and_apply(log_text, parsed_logos=team_logos, mode="full")
                        if state["match"]["status"] == "live" and state["match"]["id"]:
                            state["match_state"]["status"] = "live"
                            state["match_state"]["last_updated"] = int(time.time())
                        last_pos = f.tell()
                        state["match"]["status"] = "live" if state["match"]["id"] else "idle"
                except FileNotFoundError:
                    logging.error(f"Log not found: {current_log_path}")
                    current_log_path = None
            
            # Otherwise, process new chunks
            elif current_log_path and current_log_path.exists():
                size = _file_size(current_log_path)
                if size > last_pos:
                    chunk, new_pos = _read_new(current_log_path, last_pos)
                    if chunk:
                        parse_and_apply(chunk, parsed_logos=team_logos, mode="chunk")
                        last_pos = new_pos
                        last_update_time = now
                        log_was_updated = True
                        no_update_start_time = None

            # Track inactivity for auto-finalization
            if not log_was_updated:
                if no_update_start_time is None:
                    no_update_start_time = now
                elif now - no_update_start_time >= INACTIVITY_TIMEOUT:
                    print_colored(f"\nLog inactive for {INACTIVITY_TIMEOUT} seconds. Auto-finalizing...", Fore.YELLOW)
                    request_finalization()
                    continue

            if test_mode and simulation_manager and simulation_manager.is_complete():
                print_colored("Simulation complete! Monitoring will continue...", Fore.GREEN)
                simulation_manager.simulation_complete = False

            # Check for match end condition (unified for both modes)
            alive_teams = [t for t in state["match"]["teams"].values() if t["liveMembers"] > 0]
            if len(alive_teams) <= 1 and state["match"]["status"] == "live":
                logging.info(f"Match end detected. Finalizing match ID: {state['match']['id']}")
                request_finalization()
                continue

            # Periodic updates
            if now - last_json >= UPDATE_INTERVAL:
                _export_json()
                last_json = now
            
            if now - last_term >= 1.0:
                _print_terminal_snapshot(test_mode)
                last_term = now

            time.sleep(FILE_CHECK_INTERVAL)
            # if os.path.exists('reset_previous.flag'):
            #     state["previous_match"] = {
            #         "status": "idle",
            #         "phase": {"standings": []},
            #         "players": {},
            #         "leaderboards": {"currentMatchTopPlayers": []},
            #         "live": {"activePlayers": [], "teamKills": {}},
            #         "teams": {}  # Add teams here
            #     }
            #     os.remove('reset_previous.flag')
            #     logging.info("Previous match data reset via flag file.")


    except KeyboardInterrupt:
        print_colored("\nStopped by user (Ctrl+C).", Fore.YELLOW)
        request_finalization()
        
    except Exception as e:
        logging.exception(f"Live monitor error: {e}")
        request_finalization()
    
    finally:
        # Perform finalization but keep server running initially
        perform_finalization(keep_server_running=True)
        
        # Start the force end monitoring thread for server-only mode
        setup_force_end_thread()
        
        # Enter server-only mode
        server_only_mode()

if __name__ == "__main__":
    main()