import json
import re
from pathlib import Path

# Paths
LOG_FILE = Path("./instructions/log-20250808.txt")
OUTPUT_JSON = Path("./live_scoreboard.json")

def quote_value(value):
    """
    Helper function to correctly format a string value for JSON.
    """
    value = value.strip()
    
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1]
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]

    if re.match(r'^\d+(\.\d+)?$|^true$|^false$', value, re.IGNORECASE):
        return value
    if value == '[Object]':
        return f'"{value}"'
    if value in ['null', ''] or not value:
        return 'null'
    
    return f'"{value}"'

def clean_log_data_to_json(log_string):
    """
    Converts a non-standard log string into a valid JSON string.
    """
    cleaned_string = log_string.strip()

    # Step 1: Add quotes to all keys and handle unquoted values
    cleaned_string = re.sub(r'(\w+):\s*([^,\]\}]+)', lambda m: f'"{m.group(1)}":{quote_value(m.group(2))}', cleaned_string)
    
    # Step 2: Ensure commas exist between objects
    cleaned_string = cleaned_string.replace("}{", "},{")

    # Step 3: Handle the location: "[Object]" case where a comma is missing
    cleaned_string = re.sub(r'(\"\[Object\]\")\s*(?=\s*\"?\w+\"?)', r'\1,', cleaned_string)

    # Step 4: Fix any lingering single quotes
    cleaned_string = cleaned_string.replace("'", '"')
    
    # Step 5: Add a leading '[' and a trailing ']' if they are missing
    if not cleaned_string.startswith('['):
        cleaned_string = '[' + cleaned_string
    if not cleaned_string.endswith(']'):
        cleaned_string = cleaned_string + ']'

    return cleaned_string

def update_live_scoreboard(log_file, output_json):
    """
    Reads the log file, processes data, and updates the live scoreboard JSON.
    """
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            log_content = f.read()
    except FileNotFoundError:
        print(f"Error: Log file not found at {log_file}")
        return

    data = {
        "GameID": None,
        "TotalPlayerList": [],
        "TeamInfoList": []
    }

    # Extract GameID
    game_id_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", log_content)
    if game_id_match:
        data["GameID"] = game_id_match.group(1)
        print(f"✅ Found GameID: {data['GameID']}")

    # --- Find and clean PlayerList Block ---
    player_list_match = re.search(r"TotalPlayerList:\s*(\[[\s\S]*?\])", log_content)
    if player_list_match:
        player_block = player_list_match.group(1)
        try:
            cleaned_players_str = clean_log_data_to_json(player_block)
            data["TotalPlayerList"] = json.loads(cleaned_players_str)
            print(f"✅ Successfully parsed {len(data['TotalPlayerList'])} player entries.")
        except json.JSONDecodeError as e:
            print(f"❌ JSON Decode Error for PlayerList: {e}")
            print("--- Cleaned String for debugging ---")
            print(cleaned_players_str)
            print("--------------------------------------")
    else:
        print("❌ Failed to find TotalPlayerList block.")

    # --- Find and clean TeamInfoList Block ---
    team_list_match = re.search(r"TeamInfoList:\s*(\[[\s\S]*?\])", log_content)
    if team_list_match:
        team_block = team_list_match.group(1)
        try:
            cleaned_teams_str = clean_log_data_to_json(team_block)
            data["TeamInfoList"] = json.loads(cleaned_teams_str)
            print(f"✅ Successfully parsed {len(data['TeamInfoList'])} team entries.")
        except json.JSONDecodeError as e:
            print(f"❌ JSON Decode Error for TeamInfoList: {e}")
            print("--- Cleaned String for debugging ---")
            print(cleaned_teams_str)
            print("--------------------------------------")
    else:
        print("❌ Failed to find TeamInfoList block.")

    # --- Generate Scoreboard JSON from Parsed Data ---
    current_game_id = data["GameID"]
    current_teams = data["TeamInfoList"]
    current_players = data["TotalPlayerList"]
    
    # Handle the case where the list is nested
    if current_teams and isinstance(current_teams[0], list):
        current_teams = current_teams[0]

    teams = []
    for team in current_teams:
        team_name = team.get("teamName", "Unknown Team")
        team_logo = team.get("logoPicUrl", None)
        live_members = int(team.get("liveMemberNum", 0))
        team_kills = int(team.get("killNum", 0))
        
        try:
            team_id = int(team.get("teamId", -1))
        except (ValueError, TypeError):
            team_id = -1

        players = []
        for p in current_players:
            try:
                player_team_id = int(p.get("teamId", -1))
            except (ValueError, TypeError):
                player_team_id = -1

            if player_team_id == team_id:
                health = int(p.get("health", 0))
                health_max = int(p.get("healthMax", 1))
                health_percent = (health / health_max) * 100 if health_max > 0 else 0

                players.append({
                    "playerName": p.get("playerName", "Unknown"),
                    "playerPhoto": p.get("picUrl", None),
                    "kills": int(p.get("killNum", 0)),
                    "damage": int(float(p.get("damage", 0))),
                    "healthPercent": health_percent,
                    "knockouts": int(p.get("knockouts", 0))
                })

        teams.append({
            "teamName": team_name,
            "teamLogo": team_logo,
            "liveMembers": live_members,
            "totalKills": team_kills,
            "players": players
        })

    live_scoreboard = {
        "GameID": current_game_id,
        "teams": teams
    }

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(live_scoreboard, f, indent=2)

    print(f"✅ Updated live scoreboard for GameID {current_game_id} with {len(teams)} teams.")

if __name__ == "__main__":
    update_live_scoreboard(LOG_FILE, OUTPUT_JSON)