import json
import re
import time
import os
from pathlib import Path
import webserver # Assumes a webserver.py file exists to start the server
import socketserver # To handle server errors gracefully
import argparse # To handle command-line arguments

# --- Configuration ---
ROOT_LOG_DIR = Path("./logs")
CURRENT_LOG_DIR = ROOT_LOG_DIR / "current"
ARCHIVE_LOG_DIR = ROOT_LOG_DIR / "archive"
OUTPUT_JSON = Path("./live_scoreboard.json")
DEFAULT_TEAM_LOGO = "teamlogo.png"
DEFAULT_PLAYER_PHOTO = "playerlogo.png"

# --- Data Structures ---
# This dictionary holds all tournament and match data.
tournament_data = {
    "tournament_teams": {}, # Stores cumulative data for the entire tournament (from ALL logs)
    "tournament_players": {}, # Stores cumulative data for all players
    "current_match": { # Stores data for the currently monitored live match
        "gameId": None,
        "teams": {},
        "players": {},
        "eliminated_players": {},
        "elimination_order": []
    }
}

# --- Point System ---
PLACEMENT_POINTS = {
    1: 10,
    2: 6,
    3: 5,
    4: 4,
    5: 3,
    6: 2,
    7: 1,
    8: 1
}

# --- Parsing Functions ---
def parse_data_block(block_string):
    """
    Parses a block of log data containing JavaScript object notation.
    """
    entries = []
    
    try:
        # A more robust regex to find and parse each object independently
        object_strings = re.findall(r'\{[^{}]*\}', block_string)
        
        for obj_str in object_strings:
            current_entry = {}
            
            # Extract key-value pairs. Updated regex to be more robust.
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

def process_log_data(log_content, data_to_update, live_mode=False):
    """
    Processes a full log file content and updates the provided data dictionary.
    This function is generic and can be used for both historical and live data.
    `live_mode` controls whether pauses are introduced.
    """
    # Use re.split to correctly separate the blocks by their markers
    all_blocks = re.split(r'(TotalPlayerList:|TeamInfoList:)', log_content)
    
    # Check for a new game ID at the beginning of the content
    game_id_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", log_content)
    if game_id_match:
        new_game_id = game_id_match.group(1)
        if new_game_id != data_to_update["gameId"]:
            # This is a new game, reset match data
            # Check if there was a previous game to calculate final scores for
            if data_to_update["gameId"] is not None:
                end_of_match_processing(data_to_update, tournament_data["tournament_teams"])
                update_json()
                print(f"✅ Final scores for match {data_to_update['gameId']} have been calculated and saved.")

            data_to_update.clear()
            data_to_update.update(reset_current_match_data())
            data_to_update["gameId"] = new_game_id
            if live_mode:
                print(f"New match detected: {new_game_id}. Resetting scoreboard...")

    # Iterate through the blocks to find player and team info
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
                        data_to_update["players"].pop(player_id, None)
                    else:
                        data_to_update["players"][player_id] = p
                    if live_mode:
                        time.sleep(0.01) # Small pause for player updates
                
        elif 'TeamInfoList:' in block_marker and i + 1 < len(all_blocks):
            team_block_string = all_blocks[i+1]
            parsed_teams = parse_data_block(team_block_string)
            for t in parsed_teams:
                team_id = str(t.get("teamId", ""))
                if team_id:
                    data_to_update["teams"][team_id] = t
                    if t.get("liveMemberNum", 0) == 0 and team_id not in data_to_update["elimination_order"]:
                        data_to_update["elimination_order"].append(team_id)
                        
                        eliminated_count = len(data_to_update["elimination_order"])
                        if live_mode:
                            print(f"Team {t.get('teamId')} ({t.get('teamName')}) eliminated. Eliminated #: {eliminated_count}")
                            time.sleep(1) # The requested 1 second pause
                            update_json() # Write to JSON immediately after a team is eliminated
    
    return data_to_update

# --- Core Logic Functions ---
def update_json():
    """Calculates and writes the live scoreboard data to the JSON file."""
    teams_output = []
    top_players_output = []
    current_match = tournament_data["current_match"]
    teams_dict = {}

    # 1. Populate teams_dict with all known team IDs and info.
    for team_id, team_data in current_match["teams"].items():
        team_id_str = str(team_id)
        teams_dict[team_id_str] = {
            "teamId": int(team_id_str),
            "teamName": team_data.get("teamName", "Unknown Team"),
            "teamLogo": team_data.get("logoPicUrl", team_data.get("picUrl", DEFAULT_TEAM_LOGO)),
            "liveMembers": 0,
            "totalKills": 0,
            "players": []
        }

    # 2. Process all players and associate them with teams
    all_players = {**current_match["players"], **current_match["eliminated_players"]}
    
    for player_id, player_data in all_players.items():
        team_id_raw = player_data.get("teamId")
        team_id = str(team_id_raw) if team_id_raw is not None else None
        
        if not team_id or team_id == "None" or team_id == "":
            continue
        
        if team_id not in teams_dict:
            team_info = current_match["teams"].get(team_id, {})
            teams_dict[team_id] = {
                "teamId": int(team_id),
                "teamName": team_info.get("teamName", player_data.get("teamName", "Unknown Team")),
                "teamLogo": team_info.get("logoPicUrl", team_info.get("picUrl", DEFAULT_TEAM_LOGO)),
                "liveMembers": 0,
                "totalKills": 0,
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
        teams_dict[team_id]["totalKills"] += player_data.get("killNum", 0)
        
        if player_data.get("liveState") != 5:
            teams_dict[team_id]["liveMembers"] += 1
        
    for team_id, team_data in teams_dict.items():
        tournament_team = tournament_data["tournament_teams"].get(team_id, {})
        tournament_points = tournament_team.get("totalTournamentPoints", 0)
        
        teams_output.append({
            "teamId": team_data["teamId"],
            "teamName": team_data["teamName"],
            "teamLogo": team_data["teamLogo"],
            "liveMembers": team_data["liveMembers"],
            "totalKills": team_data["totalKills"],
            "totalPoints": tournament_points + team_data["totalKills"],
            "players": team_data["players"]
        })
    
    teams_output.sort(key=lambda x: (x["totalPoints"], x["totalKills"]), reverse=True)

    # Create a sorted list of top players for the JSON output
    # Sort by kills, then damage, then knockouts
    sorted_players = sorted(
        tournament_data["tournament_players"].values(),
        key=lambda x: (x["totalKills"], x["totalDamage"], x["totalKnockouts"]),
        reverse=True
    )
    # Get the top 5 players
    top_players_output = sorted_players[:5]

    scoreboard = {
        "gameId": current_match["gameId"],
        "teams": teams_output,
        "top_players": top_players_output # NEW field
    }
    
    try:
        with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
            json.dump(scoreboard, f, indent=2, ensure_ascii=False)
        print(f"✅ JSON updated with {len(teams_output)} teams and top players.")
    except Exception as e:
        print(f"Error writing JSON file: {e}")

def end_of_match_processing(match_data, target_teams_dict):
    """
    Calculates and applies final scores for an ended match to a given tournament totals dictionary.
    """
    match_teams = match_data["teams"]
    all_team_ids = set(match_teams.keys())
    elimination_order = match_data["elimination_order"]
    
    # Correctly identify teams that were not eliminated (i.e., the winners)
    not_eliminated_teams = list(all_team_ids - set(elimination_order))
    
    # The final rankings list should put the winning teams first, followed by the eliminated teams
    # in reverse order of elimination (last eliminated gets a higher rank).
    final_rankings = not_eliminated_teams + elimination_order[::-1]
    
    # Now, process the final rankings to assign points
    for i, team_id in enumerate(final_rankings):
        team_id = str(team_id)
        position = i + 1 # Rank 1 goes to the first team in the list
        points = PLACEMENT_POINTS.get(position, 0)
        
        if team_id not in target_teams_dict:
            target_teams_dict[team_id] = {
                "teamName": match_teams.get(team_id, {}).get("teamName", "Unknown Team"),
                "totalTournamentKills": 0,
                "totalTournamentPoints": 0,
                "firstPlaceFinishes": 0,
                "totalPlacementPoints": 0,
                "lastMatchRank": 0
            }
        
        if position == 1:
            target_teams_dict[team_id]["firstPlaceFinishes"] += 1
        
        target_teams_dict[team_id]["totalPlacementPoints"] += points
        target_teams_dict[team_id]["lastMatchRank"] = position
        
        team_kills = 0
        all_players = {**match_data["players"], **match_data["eliminated_players"]}
        for player_data in all_players.values():
            if str(player_data.get("teamId")) == team_id:
                team_kills += player_data.get("killNum", 0)
        
        target_teams_dict[team_id]["totalTournamentKills"] += team_kills
        
        total_points = target_teams_dict[team_id]["totalPlacementPoints"] + target_teams_dict[team_id]["totalTournamentKills"]
        target_teams_dict[team_id]["totalTournamentPoints"] = total_points

    # Aggregate player data after each match with new stats
    all_players_in_match = {**match_data["players"], **match_data["eliminated_players"]}
    for player_id, player_data in all_players_in_match.items():
        player_id = str(player_id)
        if player_id not in tournament_data["tournament_players"]:
            tournament_data["tournament_players"][player_id] = {
                "uId": player_data.get("uId"),
                "playerName": player_data.get("playerName", "Unknown"),
                "playerPhoto": player_data.get("picUrl", DEFAULT_PLAYER_PHOTO),
                "totalKills": 0,
                "totalDamage": 0,
                "totalKnockouts": 0,
                "totalMatches": 0
            }
        
        # Add stats from this match to the player's total
        tournament_data["tournament_players"][player_id]["totalKills"] += player_data.get("killNum", 0)
        tournament_data["tournament_players"][player_id]["totalDamage"] += player_data.get("damage", 0)
        tournament_data["tournament_players"][player_id]["totalKnockouts"] += player_data.get("knockouts", 0)
        tournament_data["tournament_players"][player_id]["totalMatches"] += 1

    print("Final match rankings:", final_rankings)
    print("Tournament totals updated.")

# --- Helper Functions ---
def get_all_log_files(log_dir):
    """Recursively finds all log files in a given directory."""
    log_files = []
    if not log_dir.exists():
        return log_files
    for root, dirs, files in os.walk(log_dir):
        for file in files:
            if file.endswith(".txt"):
                log_files.append(Path(root) / file)
    return log_files

def reset_current_match_data():
    """Resets the current match data for a new game."""
    return {
        "gameId": None,
        "teams": {},
        "players": {},
        "eliminated_players": {},
        "elimination_order": []
    }

def process_all_backlogs():
    """
    Processes all historical logs to populate the total tournament standings.
    """
    print("Processing all log files for Overall Tournament standings...")
    
    # Get all log files from the archive directory
    all_historical_logs = get_all_log_files(ARCHIVE_LOG_DIR)
    
    for log_file in sorted(all_historical_logs):
        temp_match_data = reset_current_match_data()
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                log_content = f.read()
                processed_data = process_log_data(log_content, temp_match_data)
                end_of_match_processing(processed_data, tournament_data["tournament_teams"])
                print(f"  - Processed historical log: {log_file}")
        except FileNotFoundError:
            print(f"Error: Log file not found at {log_file}")
        except Exception as e:
            print(f"Error processing {log_file}: {e}")
            
    print("✅ Finished processing historical logs.")
    
def get_current_round_logs():
    """Returns a sorted list of log files from the current directory."""
    current_round_logs = get_all_log_files(CURRENT_LOG_DIR)
    return sorted(current_round_logs)

def live_monitoring_loop():
    """Monitors a log file that is actively being appended to."""
    current_logs = get_current_round_logs()
    if not current_logs:
        print("🔴 No current log files found. Exiting.")
        return
    
    LIVE_LOG_FILE = current_logs[-1]
    last_file_size = os.path.getsize(LIVE_LOG_FILE)
    
    while True:
        try:
            current_file_size = os.path.getsize(LIVE_LOG_FILE)
            if current_file_size > last_file_size:
                print("\n--- New data detected. Processing new content... ---")
                
                with open(LIVE_LOG_FILE, 'r', encoding='utf-8') as f:
                    f.seek(last_file_size)
                    new_content = f.read()
                
                process_log_data(new_content, tournament_data["current_match"], live_mode=True)
                update_json()
                
                last_file_size = current_file_size
                print("--- Processing complete. Waiting for next update... ---")
            
            time.sleep(1)
        
        except FileNotFoundError:
            print("🔴 Live log file not found. Please ensure the game is logging to the correct path.")
            time.sleep(5)
        except Exception as e:
            print(f"An unexpected error occurred during live monitoring: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)

def simulate_live_processing(file_path, speed):
    """Simulates live processing of a historical log file."""
    print(f"\n--- Starting live simulation of {file_path} at speed {speed}s per block. Press Ctrl+C to stop. ---")
    
    current_block = ""
    start_of_block = False
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                # Check for the start of a new data block
                if "TotalPlayerList:" in line or "TeamInfoList:" in line:
                    start_of_block = True
                    if current_block:
                        # Process the previous block if it exists
                        process_log_data(current_block, tournament_data["current_match"], live_mode=True)
                        update_json()
                        current_block = "" # Reset the block
                        time.sleep(speed) # Pause to simulate a live delay
                
                if start_of_block:
                    current_block += line
                    # A simple check to see if the block is likely complete
                    if current_block.strip().endswith('}'):
                        # Process the block
                        process_log_data(current_block, tournament_data["current_match"], live_mode=True)
                        update_json()
                        current_block = "" # Reset the block
                        time.sleep(speed) # Pause to simulate a live delay
                        start_of_block = False # End of a single-entry block
                
                # Check for new game ID, as it might appear mid-stream
                game_id_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", line)
                if game_id_match:
                    new_game_id = game_id_match.group(1)
                    if new_game_id != tournament_data["current_match"]["gameId"]:
                        # Reset for a new game, process the end of the previous one
                        if tournament_data["current_match"]["gameId"] is not None:
                            end_of_match_processing(tournament_data["current_match"], tournament_data["tournament_teams"])
                            update_json()
                        tournament_data["current_match"] = reset_current_match_data()
                        tournament_data["current_match"]["gameId"] = new_game_id
                        print(f"New match detected: {new_game_id}. Resetting scoreboard...")
                        time.sleep(speed)

    except FileNotFoundError:
        print(f"❌ Error: Log file not found at {file_path}")
    except KeyboardInterrupt:
        print("\n⏹️  Simulation stopped by user.")
    except Exception as e:
        print(f"An unexpected error occurred during simulation: {e}")
        import traceback
        traceback.print_exc()
    
    # Process any remaining block at the end of the file
    if current_block:
        process_log_data(current_block, tournament_data["current_match"], live_mode=True)
        update_json()
        print("✅ Finished processing the remaining log content.")
    
def main():
    """
    Orchestrates the entire log processing and web server management.
    """
    parser = argparse.ArgumentParser(description="Processes tournament logs and runs a live scoreboard.")
    parser.add_argument("-s", "--simulate", type=str, help="Path to a historical log file to simulate live processing from.")
    parser.add_argument("-p", "--speed", type=float, default=0.5, help="Delay in seconds between updates for simulation mode.")
    args = parser.parse_args()
    
    server = None
    try:
        process_all_backlogs()
        server = webserver.start_server()
        print("\n✅ Server started at http://localhost:5000")
        
        if args.simulate:
            simulate_live_processing(Path(args.simulate), args.speed)
        else:
            print("\n✅ Starting live monitoring for new updates. Press Ctrl+C to stop.")
            live_monitoring_loop()
            
    except KeyboardInterrupt:
        print("\n⏹️  Script stopped by user.")
    except socketserver.AddressAlreadyInUseError:
        print("❌ Error: The web server port is already in use. Please close any other programs using this port and try again.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if server:
            print("Shutting down the server...")
            server.shutdown()
            server.server_close()
            print("Server shut down.")

if __name__ == "__main__":
    main()
