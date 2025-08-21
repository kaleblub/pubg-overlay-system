import json
import re
import time
from pathlib import Path

# Paths
LOG_FILE = Path("./instructions/log-20250808.txt")
OUTPUT_JSON = Path("./live_scoreboard.json")

def parse_data_block(block_string):
    """
    Parses a block of log data by splitting it into individual entries
    and then parsing each entry's key-value pairs.
    """
    entries = []
    entries_list = block_string.split("},")
    
    for entry_string in entries_list:
        entry_string = entry_string.strip()
        if not entry_string or entry_string in ['[', '{', '}']:
            continue
        
        current_entry = {}
        pairs = re.findall(r'(\w+):\s*([^,}\n]+)', entry_string)
        
        for key, value in pairs:
            value = value.strip().replace("'", "")
            
            if value.lower() == 'null' or not value:
                value = None
            elif value.lower() == 'false':
                value = False
            elif value.lower() == 'true':
                value = True
            elif key == 'picUrl':
                value = value.strip('"') if value.startswith('"') and value.endswith('"') else value
            elif value.replace('.', '', 1).isdigit():
                if '.' in value:
                    value = float(value)
                else:
                    value = int(value)
            
            current_entry[key] = value

        if current_entry:
            entries.append(current_entry)
            
    return entries

def process_log(log_file, data):
    """
    Processes the entire log file from start to finish, returning a data structure.
    This is for the initial read.
    """
    current_block_lines = []
    in_player_list = False
    in_team_list = False

    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                if "GameID:" in line and data["GameID"] is None:
                    game_id_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", line)
                    if game_id_match:
                        data["GameID"] = game_id_match.group(1)

                if "TotalPlayerList:" in line:
                    in_player_list = True
                    in_team_list = False
                    current_block_lines = []
                    continue
                
                if "TeamInfoList:" in line:
                    in_player_list = False
                    in_team_list = True
                    current_block_lines = []
                    continue

                if in_player_list or in_team_list:
                    current_block_lines.append(line)
                
                if in_player_list and "UseEmergencyCallTime: 0" in line:
                    full_block_string = "".join(current_block_lines)
                    parsed_players = parse_data_block(full_block_string)
                    for p in parsed_players:
                        player_id = p.get("uId")
                        if player_id:
                            data["players"][str(player_id)] = p
                    in_player_list = False
                    current_block_lines = []

                if in_team_list and "liveMemberNum" in line:
                    full_block_string = "".join(current_block_lines)
                    parsed_teams = parse_data_block(full_block_string)
                    for t in parsed_teams:
                        team_id = str(t.get("teamId"))
                        if team_id:
                            data["teams"][team_id] = t
                    in_team_list = False
                    current_block_lines = []
    except FileNotFoundError:
        print(f"Error: Log file not found at {log_file}")
    
    return data

def update_json(data):
    """
    Generates and writes the scoreboard JSON from the consolidated data.
    """
    current_game_id = data["GameID"]
    teams_dict = {}
    
    # 1. Start with a blank slate and build teams from player data
    for player in data["players"].values():
        team_id = str(player.get("teamId"))
        
        if team_id not in teams_dict:
            teams_dict[team_id] = {
                "teamId": int(team_id),
                "teamName": player.get("teamName", "Unknown Team"),
                "teamLogo": None,
                "liveMembers": 0,
                "totalKills": 0,
                "players": []
            }

    # 2. Consolidate info from the team data block
    for team_id, team_data in data["teams"].items():
        if team_id in teams_dict:
            # Overwrite with more specific team info if available
            teams_dict[team_id].update({
                "teamName": team_data.get("teamName", teams_dict[team_id]["teamName"]),
                "teamLogo": team_data.get("logoPicUrl", team_data.get("picUrl", None))
            })
        else:
            # If a team has no players in the current log, still include it
            teams_dict[team_id] = {
                "teamId": int(team_id),
                "teamName": team_data.get("teamName", "Unknown Team"),
                "teamLogo": team_data.get("logoPicUrl", team_data.get("picUrl", None)),
                "liveMembers": 0,
                "totalKills": 0,
                "players": []
            }

    # 3. Populate players and aggregate stats based on player data
    for player in data["players"].values():
        team_id = str(player.get("teamId"))
        if team_id in teams_dict:
            health = player.get("health", 0)
            health_max = player.get("healthMax", 100)
            health_percent = (health / health_max) * 100 if health_max > 0 else 0
            
            player_data = {
                "uId": player.get("uId"),  # Add uId here
                "playerName": player.get("playerName", "Unknown"),
                "playerPhoto": player.get("picUrl", None),
                "kills": player.get("killNum", 0),
                "damage": player.get("damage", 0),
                "healthPercent": health_percent,
                "knockouts": player.get("knockouts", 0)
            }
            
            teams_dict[team_id]["players"].append(player_data)
            teams_dict[team_id]["totalKills"] += player.get("killNum", 0)
            
            # Recalculate live members based on liveState
            if player.get("liveState") != 5:
                teams_dict[team_id]["liveMembers"] += 1

    live_scoreboard = {
        "GameID": current_game_id,
        "teams": list(teams_dict.values())
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(live_scoreboard, f, indent=2)

def main():
    """
    Executes the two-phase log processing.
    """
    data = {
        "GameID": None,
        "teams": {},
        "players": {}
    }

    print("🚀 Starting initial processing of log file...")
    data = process_log(LOG_FILE, data)
    
    print("✅ Initial processing complete. Printing processed data:")
    print(json.dumps(data, indent=2, default=str))
    
    update_json(data)
    
    print("\n✅ JSON file has been updated. Starting live monitoring for new updates. Press Ctrl+C to stop.")
    
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            f.seek(0, 2)
            current_block_lines = []
            in_player_list = False
            in_team_list = False

            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    continue

                if "TotalPlayerList:" in line:
                    in_player_list = True
                    in_team_list = False
                    current_block_lines = []
                    continue
                
                if "TeamInfoList:" in line:
                    in_player_list = False
                    in_team_list = True
                    current_block_lines = []
                    continue

                if in_player_list or in_team_list:
                    current_block_lines.append(line)
                
                if in_player_list and "UseEmergencyCallTime: 0" in line:
                    full_block_string = "".join(current_block_lines)
                    parsed_players = parse_data_block(full_block_string)
                    for p in parsed_players:
                        player_id = p.get("uId")
                        if player_id:
                            data["players"][str(player_id)] = p
                    in_player_list = False
                    print("\n✅ Found and processed new player data. Updated dictionary:")
                    print(json.dumps(data, indent=2, default=str))
                    current_block_lines = []
                    update_json(data)

                if in_team_list and "liveMemberNum" in line:
                    full_block_string = "".join(current_block_lines)
                    parsed_teams = parse_data_block(full_block_string)
                    for t in parsed_teams:
                        team_id = str(t.get("teamId"))
                        if team_id:
                            data["teams"][team_id] = t
                    in_team_list = False
                    print("\n✅ Found and processed new team data. Updated dictionary:")
                    print(json.dumps(data, indent=2, default=str))
                    current_block_lines = []
                    update_json(data)
    except FileNotFoundError:
        print(f"Error: Log file not found at {LOG_FILE}")
    except Exception as e:
        print(f"An error occurred in the live monitoring loop: {e}")

if __name__ == "__main__":
    main()