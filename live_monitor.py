import json
import re
import time
import os
from pathlib import Path
import webserver # Assumes a webserver.py file exists to start the server
import shutil

# --- Configuration ---
ROOT_LOG_DIR = Path("./logs")
CURRENT_LOG_DIR = ROOT_LOG_DIR / "current"
ARCHIVE_LOG_DIR = ROOT_LOG_DIR / "archive"
OUTPUT_JSON = Path("./live_scoreboard.json")
DEFAULT_TEAM_LOGO = "http://localhost:5000/assets/default-team-logo.jpg"
DEFAULT_PLAYER_PHOTO = "http://localhost:5000/assets/PUBG.png"
UPDATE_INTERVAL = 0.5  # JSON updates every 0.5 seconds for live monitoring
FILE_CHECK_INTERVAL = 0.1  # Check file size every 100ms

# --- Data Structures ---
tournament_data = {
    "current_phase_teams": {},
    "current_phase_players": {},
    "all_time_players": {},
    "current_match": {
        "gameId": None,
        "teams": {},
        "players": {},
        "eliminated_players": {},
        "elimination_order": [],
        "match_finished": False,
        "winner_team_id": None,
        "kill_feed": [] # New for live display
    }
}

# --- Point System ---
PLACEMENT_POINTS = {
    1: 10, 2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 1, 8: 1
}

# --- Parsing Functions ---
def parse_data_block(block_string):
    entries = []
    try:
        object_strings = re.findall(r'\{[^{}]*\}', block_string)
        for obj_str in object_strings:
            current_entry = {}
            pairs = re.findall(r'(\w+):\s*(?:"([^"]*)"|\'([^\']*)\'|([^{},\n]+))', obj_str)
            for key, val_double_quoted, val_single_quoted, val_unquoted in pairs:
                value = val_double_quoted or val_single_quoted or val_unquoted
                if value is not None:
                    value = value.strip()
                if value is None or value.lower() in ('null', 'none', ''):
                    value = None
                elif value.lower() == 'true':
                    value = True
                elif value.lower() == 'false':
                    value = False
                elif value.replace('.', '', 1).replace('-', '', 1).isdigit():
                    if '.' in value:
                        value = float(value)
                    else:
                        if key == 'teamId':
                            value = str(value)
                        else:
                            value = int(value)
                current_entry[key] = value
            if current_entry and ('uId' in current_entry or ('teamId' in current_entry and 'teamName' in current_entry)):
                entries.append(current_entry)
    except Exception as e:
        print(f"Error in parsing data block: {e}")
    return entries

def check_match_end(data_to_update):
    if data_to_update.get("match_finished"):
        return False
    teams_with_live_members = []
    for team_id, team_data in data_to_update.get("teams", {}).items():
        live_members = team_data.get("liveMemberNum", 0)
        if live_members > 0:
            teams_with_live_members.append(team_id)
    
    if len(teams_with_live_members) <= 1 and data_to_update.get("gameId") is not None:
        print(f"🏁 Match end detected! Teams with living members: {len(teams_with_live_members)}")
        if len(teams_with_live_members) == 1:
            data_to_update["winner_team_id"] = teams_with_live_members[0]
            print(f"🍗 Winner: Team {teams_with_live_members[0]} ({data_to_update['teams'][teams_with_live_members[0]].get('teamName')})")
        data_to_update["match_finished"] = True
        print(f"🏁 Match {data_to_update['gameId']} ended. Processing final results...")
        end_of_match_processing(data_to_update)
        return True
    return False

def calculate_live_placement_points(current_match_data):
    placement_points_dict = {}
    all_team_ids = set(current_match_data["teams"].keys())
    eliminated_teams = set(current_match_data["elimination_order"])
    alive_teams = all_team_ids - eliminated_teams
    
    # Assign points to eliminated teams first, from last place up.
    elimination_rank = len(all_team_ids)
    for team_id in current_match_data["elimination_order"]:
        placement_points_dict[team_id] = PLACEMENT_POINTS.get(elimination_rank, 0)
        elimination_rank -= 1
        
    # Assign points to the winning team if a winner exists.
    if len(alive_teams) == 1:
        winner_id = list(alive_teams)[0]
        placement_points_dict[winner_id] = PLACEMENT_POINTS.get(1, 0)

    return placement_points_dict

def process_log_chunk(chunk_content, data_to_update):
    game_id_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", chunk_content)
    new_game_id = game_id_match.group(1) if game_id_match else None

    # Check for a new match starting
    if new_game_id and new_game_id != data_to_update["gameId"]:
        print(f"\n\n🏁 Match {data_to_update.get('gameId')} is now complete. Finalizing results...")
        
        # We need to process the FINAL state of the *old* match before resetting everything.
        # This requires processing the full log file of the finished game.
        # This is the most reliable way to get all final kills and elimination order.
        temp_match_data = reset_current_match_data()
        
        # Find the path for the log file of the completed game
        finished_game_path = None
        for path in get_all_log_files(CURRENT_LOG_DIR):
            if str(data_to_update["gameId"]) in str(path):
                finished_game_path = path
                break
        
        if finished_game_path:
            with open(finished_game_path, 'r', encoding='utf-8') as f:
                log_content_full = f.read()
                processed_data = process_full_log_content(log_content_full, temp_match_data)
                if processed_data["gameId"]:
                    end_of_match_processing(processed_data)
                    update_current_phase_standings()

            
            # Archive the log file
            archive_path = ARCHIVE_LOG_DIR / finished_game_path.name
            shutil.move(finished_game_path, archive_path)
            print(f"📦 Archived log file to {archive_path}")
        else:
            print("⚠️ Could not find log file for the finished match to process final data.")

        # Now, reset the data for the new match.
        data_to_update.clear()
        data_to_update.update(reset_current_match_data())
        data_to_update["gameId"] = new_game_id
        print(f"\n🎮 New match detected: {new_game_id}. Scoreboard is now live.")

    # Process the new content as a live update for the current match.
    all_blocks = re.split(r'(TotalPlayerList:|TeamInfoList:)', chunk_content)
    
    for i, block_marker in enumerate(all_blocks):
        if 'TotalPlayerList:' in block_marker and i + 1 < len(all_blocks):
            player_block_string = all_blocks[i+1]
            parsed_players = parse_data_block(player_block_string)
            for p in parsed_players:
                player_id = str(p.get("uId", ""))
                team_id = str(p.get("teamId", ""))
                if player_id and team_id:
                    # Capture new kills for kill feed
                    old_kills = data_to_update["players"].get(player_id, {}).get("killNum", 0)
                    new_kills = p.get("killNum", 0)
                    if new_kills > old_kills:
                        killer_team = data_to_update["teams"].get(team_id, {}).get("teamName", "Unknown Team")
                        killed_player = data_to_update["players"].get(player_id, {}).get("playerName", "Unknown Player")
                        # This part of the log doesn't tell us who was killed, just a new kill for a player.
                        # We'll just announce a new kill for the team for now.
                        data_to_update["kill_feed"].append(f"🔥 {p.get('playerName', 'Unknown Player')} from {killer_team} got a new kill!")
                        if len(data_to_update["kill_feed"]) > 5:
                            data_to_update["kill_feed"].pop(0)

                    if p.get("liveState") == 5:
                        if player_id not in data_to_update["eliminated_players"]:
                            print(f"💀 Player {p.get('playerName', 'Unknown')} eliminated.")
                            data_to_update["eliminated_players"][player_id] = p
                            data_to_update["players"].pop(player_id, None)
                    else:
                        data_to_update["players"][player_id] = p

        elif 'TeamInfoList:' in block_marker and i + 1 < len(all_blocks):
            team_block_string = all_blocks[i+1]
            parsed_teams = parse_data_block(team_block_string)
            for t in parsed_teams:
                team_id = str(t.get("teamId", ""))
                if team_id:
                    data_to_update["teams"][team_id] = t
                    if t.get("liveMemberNum", 0) == 0 and team_id not in data_to_update["elimination_order"]:
                        data_to_update["elimination_order"].append(team_id)
                        rank = len(data_to_update["teams"]) - len(data_to_update["elimination_order"]) + 1
                        print(f"🏆 Team {team_id} ({t.get('teamName')}) eliminated. Rank #{rank}")
    
    if not data_to_update.get("match_finished"):
        check_match_end(data_to_update)
    
    return data_to_update

def process_full_log_content(log_content, data_to_update):
    all_blocks = re.split(r'(TotalPlayerList:|TeamInfoList:)', log_content)
    
    game_id_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", log_content)
    if game_id_match:
        new_game_id = game_id_match.group(1)
        if new_game_id != data_to_update["gameId"]:
            data_to_update.clear()
            data_to_update.update(reset_current_match_data())
            data_to_update["gameId"] = new_game_id
            
    for i, block_marker in enumerate(all_blocks):
        if 'TotalPlayerList:' in block_marker and i + 1 < len(all_blocks):
            player_block_string = all_blocks[i+1]
            parsed_players = parse_data_block(player_block_string)
            for p in parsed_players:
                player_id = str(p.get("uId", ""))
                team_id = str(p.get("teamId", ""))
                if player_id and team_id:
                    if p.get("liveState") == 5:
                        data_to_update["eliminated_players"][player_id] = p
                    else:
                        data_to_update["players"][player_id] = p

        elif 'TeamInfoList:' in block_marker and i + 1 < len(all_blocks):
            team_block_string = all_blocks[i+1]
            parsed_teams = parse_data_block(team_block_string)
            for t in parsed_teams:
                team_id = str(t.get("teamId", ""))
                if team_id:
                    data_to_update["teams"][team_id] = t
                    if t.get("liveMemberNum", 0) == 0 and team_id not in data_to_update["elimination_order"]:
                        data_to_update["elimination_order"].append(team_id)
    
    if not data_to_update.get("match_finished"):
        check_match_end(data_to_update)
    return data_to_update

def update_current_phase_standings():
    """Calculate current phase standings sorted by total points."""
    teams = []
    for team_id, team_data in tournament_data["current_phase_teams"].items():
        teams.append({
            "teamId": int(team_id),
            "teamName": team_data.get("teamName", "Unknown Team"),
            "totalPoints": team_data.get("totalCurrentPhasePoints", 0),
            "kills": team_data.get("totalCurrentPhaseKills", 0),
            "placementPoints": team_data.get("totalCurrentPhasePlacementPoints", 0),
            "wwcd": team_data.get("firstPlaceFinishes", 0)
        })
    # Sort descending by total points, then kills
    teams.sort(key=lambda x: (x["totalPoints"], x["kills"]), reverse=True)
    tournament_data["current_phase_standings"] = teams


def update_json():
    current_match = tournament_data["current_match"]
    live_scoreboard_teams = calculate_live_scoreboard()
    current_match_top_players = calculate_current_match_top_players()
    overall_top_players = calculate_overall_top_players()
    
    scoreboard = {
        "gameId": current_match["gameId"],
        "matchFinished": current_match.get("match_finished", False),
        "live_scoreboard": live_scoreboard_teams,
        "current_match_top_players": current_match_top_players,
        "overall_top_players": overall_top_players,
        "kill_feed": current_match["kill_feed"],
        "current_phase_standings": tournament_data.get("current_phase_standings", [])
    }
    
    try:
        with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
            json.dump(scoreboard, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error writing JSON file: {e}")

def calculate_live_scoreboard():
    current_match = tournament_data["current_match"]
    teams_dict = {}
    
    # Start with all teams from the current phase (past matches)
    for team_id, team_data in tournament_data["current_phase_teams"].items():
        teams_dict[team_id] = {
            "teamId": int(team_id),
            "teamName": team_data.get("teamName", "Unknown Team"),
            "teamLogo": team_data.get("teamLogo", DEFAULT_TEAM_LOGO),
            "liveMembers": 0,
            "currentMatchKills": 0,
            "currentPhaseKills": team_data.get("totalCurrentPhaseKills", 0),
            "currentPhasePlacementPoints": team_data.get("totalCurrentPhasePlacementPoints", 0),
            "totalPoints": team_data.get("totalCurrentPhasePoints", 0),
            "wwcd": team_data.get("firstPlaceFinishes", 0),
            "players": []
        }
    
    # Overlay with live data from the current match log
    all_players = {**current_match["players"], **current_match["eliminated_players"]}
    for player_id, player_data in all_players.items():
        team_id = str(player_data.get("teamId", ""))
        if not team_id or team_id == "None":
            continue
        
        if team_id not in teams_dict:
            teams_dict[team_id] = {
                "teamId": int(team_id),
                "teamName": player_data.get("teamName", "Unknown Team"),
                "teamLogo": player_data.get("picUrl", DEFAULT_TEAM_LOGO),
                "liveMembers": 0,
                "currentMatchKills": 0,
                "currentPhaseKills": 0,
                "currentPhasePlacementPoints": 0,
                "totalPoints": 0,
                "wwcd": 0,
                "players": []
            }

        health = player_data.get("health", 0)
        health_max = player_data.get("healthMax", 100)
        health_percent = (health / health_max) * 100 if health_max > 0 else 0
        
        player_entry = {
            "uId": player_data.get("uId"),
            "playerName": player_data.get("playerName", "Unknown"),
            "playerPhoto": player_data.get("picUrl", DEFAULT_PLAYER_PHOTO),
            "kills": player_data.get("killNum", 0),
            "damage": player_data.get("damage", 0),
            "healthPercent": round(health_percent, 1),
            "knockouts": player_data.get("knockouts", 0)
        }
        
        teams_dict[team_id]["players"].append(player_entry)
        teams_dict[team_id]["currentMatchKills"] += player_data.get("killNum", 0)
        teams_dict[team_id]["liveMembers"] = current_match["teams"].get(team_id, {}).get("liveMemberNum", 0)

    # Now, calculate the final live total points
    teams_output = []
    for team_id, team_data in teams_dict.items():
        live_total_points = team_data["currentPhaseKills"] + team_data["currentPhasePlacementPoints"] + team_data["currentMatchKills"]
        team_data["totalPoints"] = live_total_points
        teams_output.append(team_data)
    
    teams_output.sort(key=lambda x: (x["totalPoints"], x["currentMatchKills"]), reverse=True)
    return teams_output
    
def calculate_current_match_scoreboard():
    current_match = tournament_data["current_match"]
    teams_dict = {}
    
    # Merge live + eliminated to retain kills
    all_players = {**current_match["players"], **current_match["eliminated_players"]}
    
    # Calculate kill points + live members
    for player_data in all_players.values():
        team_id = str(player_data.get("teamId", ""))
        if not team_id or team_id == "None":
            continue
            
        if team_id not in teams_dict:
            # Start with phase totals if available
            phase_data = tournament_data["current_phase_teams"].get(team_id, {})
            teams_dict[team_id] = {
                "teamId": int(team_id),
                "teamName": player_data.get("teamName", "Unknown Team"),
                "killPoints": 0,
                "placementPoints": 0,
                "currentPhaseKills": phase_data.get("totalCurrentPhaseKills", 0),
                "currentPhasePlacementPoints": phase_data.get("totalCurrentPhasePlacementPoints", 0),
                "players": []
            }
        
        # Add player data to team
        health = player_data.get("health", 0)
        health_max = player_data.get("healthMax", 100)
        health_percent = (health / health_max) * 100 if health_max > 0 else 0
        
        player_entry = {
            "uId": player_data.get("uId"),
            "playerName": player_data.get("playerName", "Unknown"),
            "playerPhoto": player_data.get("picUrl", DEFAULT_PLAYER_PHOTO),
            "kills": player_data.get("killNum", 0),
            "damage": player_data.get("damage", 0),
            "healthPercent": round(health_percent, 1),
            "knockouts": player_data.get("knockouts", 0)
        }
        teams_dict[team_id]["players"].append(player_entry)
        teams_dict[team_id]["killPoints"] += player_data.get("killNum", 0)
        
    # Calculate placement points for teams based on live elimination order
    live_placement_points = calculate_live_placement_points(current_match)
    for team_id, points in live_placement_points.items():
        if team_id in teams_dict:
            teams_dict[team_id]["placementPoints"] = points
            
    teams_output = []
    for team_id, team_data in teams_dict.items():
        total_points = team_data["killPoints"] + team_data["placementPoints"]
        teams_output.append({
            "teamId": team_data["teamId"],
            "teamName": team_data["teamName"],
            "kills": team_data["killPoints"],
            "placement": team_data["placementPoints"],
            "totalPoints": total_points,
            "liveMembers": current_match["teams"].get(team_id, {}).get("liveMemberNum", 0),
            "currentPhaseKills": team_data["currentPhaseKills"],
            "currentPhasePlacementPoints": team_data["currentPhasePlacementPoints"]
        })

    teams_output.sort(key=lambda x: (x["totalPoints"], x["kills"]), reverse=True)
    return teams_output

def calculate_current_match_top_players():
    current_match = tournament_data["current_match"]
    all_players = {**current_match["players"], **current_match["eliminated_players"]}
    players_list = []
    for player_id, player_data in all_players.items():
        players_list.append({
            "uId": player_data.get("uId"),
            "playerName": player_data.get("playerName", "Unknown"),
            "playerPhoto": player_data.get("picUrl", DEFAULT_PLAYER_PHOTO),
            "kills": player_data.get("killNum", 0),
            "damage": player_data.get("damage", 0),
            "knockouts": player_data.get("knockouts", 0)
        })
    players_list.sort(key=lambda x: (x["kills"], x["damage"], x["knockouts"]), reverse=True)
    return players_list[:5]

def calculate_overall_top_players():
    players_list = []
    for player_id, player_data in tournament_data["all_time_players"].items():
        players_list.append({
            "uId": player_data.get("uId"),
            "playerName": player_data.get("playerName", "Unknown"),
            "playerPhoto": player_data.get("picUrl", DEFAULT_PLAYER_PHOTO),
            "totalKills": player_data.get("totalKills", 0),
            "totalDamage": player_data.get("totalDamage", 0),
            "totalKnockouts": player_data.get("totalKnockouts", 0),
            "totalMatches": player_data.get("totalMatches", 0)
        })
    players_list.sort(key=lambda x: (x["totalKills"], x["totalDamage"], x["totalKnockouts"]), reverse=True)
    return players_list[:5]

def end_of_match_processing(match_data):
    if not match_data.get("match_finished"):
        match_data["match_finished"] = True
    
    all_team_ids = set(match_data["teams"].keys())
    eliminated_teams = set(match_data["elimination_order"])
    winner_team_id = list(all_team_ids - eliminated_teams)[0] if len(all_team_ids - eliminated_teams) == 1 else None
    
    # Calculate final placement points
    final_placement_points = calculate_live_placement_points(match_data)
    print("\n--- Final Match Standings ---")
    sorted_teams = sorted(match_data["teams"].keys(), key=lambda x: final_placement_points.get(x, 0), reverse=True)
    for rank, team_id in enumerate(sorted_teams, 1):
        placement_points = final_placement_points.get(team_id, 0)
        team_name = match_data['teams'].get(team_id, {}).get('teamName', 'Unknown')
        print(f"#{rank}: {team_name} - {placement_points} placement points")
    print("---------------------------\n")

    # Update cumulative standings
    for team_id, placement_points in final_placement_points.items():
        team_id = str(team_id)
        if team_id not in tournament_data["current_phase_teams"]:
            tournament_data["current_phase_teams"][team_id] = {
                "teamName": match_data["teams"].get(team_id, {}).get("teamName", "Unknown Team"),
                "totalCurrentPhaseKills": 0,
                "totalCurrentPhasePlacementPoints": 0,
                "totalCurrentPhasePoints": 0,
                "firstPlaceFinishes": 0,
            }
        
        tournament_data["current_phase_teams"][team_id]["totalCurrentPhasePlacementPoints"] += placement_points
        if placement_points == 10:
            tournament_data["current_phase_teams"][team_id]["firstPlaceFinishes"] += 1

    match_kills = 0
    all_players = {**match_data["players"], **match_data["eliminated_players"]}
    for player_data in all_players.values():
        if str(player_data.get("teamId")) == team_id:
            match_kills += player_data.get("killNum", 0)
    tournament_data["current_phase_teams"][team_id]["totalCurrentPhaseKills"] += match_kills
    tournament_data["current_phase_teams"][team_id]["totalCurrentPhasePoints"] = tournament_data["current_phase_teams"][team_id]["totalCurrentPhaseKills"] + tournament_data["current_phase_teams"][team_id]["totalCurrentPhasePlacementPoints"]


def reset_current_match_data():
    return {
        "gameId": None,
        "teams": {},
        "players": {},
        "eliminated_players": {},
        "elimination_order": [],
        "match_finished": False,
        "winner_team_id": None,
        "kill_feed": []
    }

def get_all_log_files(log_dir):
    return [f for f in log_dir.glob('**/*.log') if f.is_file()]

def get_current_round_logs():
    return [f for f in CURRENT_LOG_DIR.glob('**/*.log') if f.is_file()]

def get_file_size(file_path):
    return file_path.stat().st_size if file_path.exists() else 0

def read_new_content(file_path, last_position):
    with open(file_path, 'r', encoding='utf-8') as f:
        f.seek(last_position)
        new_content = f.read()
        new_position = f.tell()
        return new_content, new_position

def process_all_backlogs():
    print("Processing all historical logs...")
    all_historical_logs = get_all_log_files(ARCHIVE_LOG_DIR)
    if not all_historical_logs:
        print("No historical logs found to process.")
        return
    
    # Process each archived log file in order to build up overall totals
    for log_file in sorted(all_historical_logs):
        temp_match_data = reset_current_match_data()
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                log_content = f.read()
                processed_data = process_full_log_content(log_content, temp_match_data)
                
                # Update all-time player stats
                all_players_in_match = {**processed_data["players"], **processed_data["eliminated_players"]}
                for player_id, player_data in all_players_in_match.items():
                    player_id = str(player_id)
                    if player_id not in tournament_data["all_time_players"]:
                        tournament_data["all_time_players"][player_id] = {
                            "uId": player_data.get("uId"),
                            "playerName": player_data.get("playerName", "Unknown"),
                            "playerPhoto": player_data.get("picUrl", DEFAULT_PLAYER_PHOTO),
                            "totalKills": 0,
                            "totalDamage": 0,
                            "totalKnockouts": 0,
                            "totalMatches": 0
                        }
                    tournament_data["all_time_players"][player_id]["totalKills"] += player_data.get("killNum", 0)
                    tournament_data["all_time_players"][player_id]["totalDamage"] += player_data.get("damage", 0)
                    tournament_data["all_time_players"][player_id]["totalKnockouts"] += player_data.get("knockouts", 0)
                    tournament_data["all_time_players"][player_id]["totalMatches"] += 1
                
                # Update current phase teams from old logs
                end_of_match_processing(processed_data)

        except FileNotFoundError:
            print(f"Error: Log file not found at {log_file}")
        except Exception as e:
            print(f"Error processing {log_file}: {e}")
            import traceback
            traceback.print_exc()

    print("✅ Finished processing historical logs.")


def process_current_phase_logs():
    print("Processing current round logs for current phase standings...")
    # Clear current phase data to avoid carrying over from old sessions
    tournament_data["current_phase_teams"] = {}
    tournament_data["current_phase_players"] = {}
    
    current_round_logs = get_all_log_files(CURRENT_LOG_DIR)
    for log_file in sorted(current_round_logs):
        temp_match_data = reset_current_match_data()
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                log_content = f.read()
                processed_data = process_full_log_content(log_content, temp_match_data)
                
                # Process end of match stats for the current phase
                end_of_match_processing(processed_data)
                
        except FileNotFoundError:
            print(f"Error: Log file not found at {log_file}")
        except Exception as e:
            print(f"Error processing {log_file}: {e}")
            import traceback
            traceback.print_exc()
            
    update_current_phase_standings()
    print("✅ Finished processing current phase logs.")
    if not tournament_data["current_phase_teams"]:
        print("ℹ️ No teams found in current phase logs. Scoreboard may be empty until a new match starts or logs appear.")


def print_live_scoreboard():
    # Helper for terminal output, does not affect JSON
    os.system('cls' if os.name == 'nt' else 'clear')
    
    current_match = tournament_data["current_match"]
    live_scoreboard_teams = calculate_live_scoreboard()
    current_match_top_players = calculate_current_match_top_players()
    overall_top_players = calculate_overall_top_players()
    
    print("--- LIVE SCOREBOARD ---")
    if current_match.get("gameId"):
        print(f"📺 Currently monitoring Game ID: {current_match['gameId']}")
    
    print("\nCURRENT PHASE STANDINGS")
    print("-----------------------------------")
    print(f"{'Rank':<4}{'Team':<20}{'Kills':<8}{'Placement':<11}{'WWCD':<5}{'Total':<7}{'Live':<4}")
    print("-----------------------------------------------------------------")
    
    # Use the pre-calculated standings
    sorted_teams = tournament_data.get("current_phase_standings", [])
    
    if not sorted_teams:
        print("\nℹ️ No current phase standings available yet. Waiting for match to complete.")
    else:
        for rank, team in enumerate(sorted_teams, 1):
            team_name = team["teamName"]
            kills = team["kills"]
            placement_points = team["placementPoints"]
            wwcd = team["wwcd"]
            total = team["totalPoints"]
            live_status = "🟢" if current_match["teams"].get(str(team["teamId"]), {}).get("liveMemberNum", 0) > 0 else "💀"
            
            print(f"#{rank:<3}{team_name:<20}{kills:<8}{placement_points:<11}{wwcd:<5}{total:<7}{live_status:<4}")
    
    print("\nCURRENT MATCH STANDINGS")
    print("-----------------------------------")
    print(f"{'Rank':<4}{'Team':<20}{'Kills':<8}{'Placement':<11}{'Total':<7}{'Live':<4}")
    print("-----------------------------------------------------------")
    live_match_scoreboard = calculate_current_match_scoreboard()
    if not live_match_scoreboard:
        print("\nℹ️ No live match data available yet.")
    else:
        for rank, team in enumerate(live_match_scoreboard, 1):
            team_name = team["teamName"]
            kills = team["kills"]
            placement = team["placement"]
            total = team["totalPoints"]
            live_status = "🟢" if team["liveMembers"] > 0 else "💀"
            print(f"#{rank:<3}{team_name:<20}{kills:<8}{placement:<11}{total:<7}{live_status:<4}")
            
    print("\nTOP 5 PLAYERS (Current Match)")
    print("-------------------------------")
    if not current_match_top_players:
        print("\nℹ️ No player data available yet for the current match.")
    else:
        for player in current_match_top_players:
            print(f"  {player['playerName']:<20}Kills: {player['kills']:<4}Damage: {player['damage']:<5}KO's: {player['knockouts']}")
            
    print("\nOVERALL TOP 5 PLAYERS")
    print("--------------------------")
    if not overall_top_players:
        print("\nℹ️ No overall player data available yet.")
    else:
        for player in overall_top_players:
            print(f"  {player['playerName']:<20}Kills: {player['totalKills']:<4}Damage: {player['totalDamage']:<5}Matches: {player['totalMatches']}")

    print("\nKILL FEED")
    print("--------------------------")
    if not current_match["kill_feed"]:
        print("ℹ️ No kill feed events yet.")
    else:
        for kill_event in reversed(current_match["kill_feed"]):
            print(f"  {kill_event}")

def main():
    print("Starting tournament scoreboard manager...")
    os.makedirs(CURRENT_LOG_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_LOG_DIR, exist_ok=True)
    
    # First, process any old logs to get up-to-date standings
    process_all_backlogs()
    process_current_phase_logs()
    
    current_log_file = get_current_round_logs()
    if not current_log_file:
        print("Waiting for a live log file...")
        while not get_current_round_logs():
            time.sleep(1)
        current_log_file = get_current_round_logs()[0]
        print(f"🚀 Found live log file: {current_log_file}")
    else:
        current_log_file = current_log_file[0]
        print(f"🚀 Starting live monitoring of: {current_log_file}")
    
    last_position = get_file_size(current_log_file)
    last_json_update = time.time()
    last_terminal_update = time.time()
    
    try:
        while True:
            current_size = get_file_size(current_log_file)
            
            if current_size > last_position:
                new_content, new_position = read_new_content(current_log_file, last_position)
                
                if new_content:
                    process_log_chunk(new_content, tournament_data["current_match"])
                    last_position = new_position
            
            if time.time() - last_json_update >= UPDATE_INTERVAL:
                update_json()
                last_json_update = time.time()
            
            if time.time() - last_terminal_update >= 1.0: # Update terminal every second
                print_live_scoreboard()
                last_terminal_update = time.time()
            
            time.sleep(FILE_CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n⏹️  Live monitoring stopped by user.")
        if tournament_data["current_match"]["gameId"] and not tournament_data["current_match"]["match_finished"]:
            print("🏁 Processing final match results...")
            end_of_match_processing(tournament_data["current_match"])
            update_json()
        
    except Exception as e:
        print(f"\n❌ An error occurred during live monitoring: {e}")
        import traceback
        traceback.print_exc()
        try:
            update_json()
            print("💾 Final JSON state saved.")
        except:
            print("❌ Could not save final JSON state.")

if __name__ == "__main__":
    main()

