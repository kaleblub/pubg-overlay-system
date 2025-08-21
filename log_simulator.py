import re
import time
import os
from pathlib import Path
import random

# --- Configuration ---
ROOT_LOG_DIR = Path("./logs")
TEST_LOG_DIR = ROOT_LOG_DIR / "test"
CURRENT_LOG_DIR = ROOT_LOG_DIR / "current"
OUTPUT_LOG_NAME = "simulated_live.txt"
SIMULATION_SPEED = 0.1  # Seconds between updates (adjustable)
CHUNK_SIZE = 5  # How many log entries to write at once

def ensure_directories():
    """Ensure required directories exist."""
    CURRENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)

def get_test_log_files():
    """Get all test log files from the test directory."""
    log_files = []
    if TEST_LOG_DIR.exists():
        for file in TEST_LOG_DIR.glob("*.txt"):
            log_files.append(file)
    return sorted(log_files)

def parse_log_into_blocks(log_content):
    """
    Parse a log file into discrete blocks that can be written incrementally.
    Each block represents a meaningful update (player list, team info, etc.).
    """
    blocks = []
    lines = log_content.split('\n')
    current_block = []
    in_data_block = False
    data_block_type = None
    brace_count = 0
    
    for line in lines:
        # Check for start of data blocks
        if 'TotalPlayerList:' in line or 'TeamInfoList:' in line or 'GameID:' in line:
            # If we were building a block, save it first
            if current_block:
                blocks.append('\n'.join(current_block))
                current_block = []
            
            # Start new block
            current_block.append(line)
            if 'TotalPlayerList:' in line or 'TeamInfoList:' in line:
                in_data_block = True
                data_block_type = 'TotalPlayerList' if 'TotalPlayerList:' in line else 'TeamInfoList'
            elif 'GameID:' in line:
                # GameID is usually a single line
                blocks.append('\n'.join(current_block))
                current_block = []
                in_data_block = False
        elif in_data_block:
            current_block.append(line)
            # Count braces to know when the data block ends
            brace_count += line.count('{') - line.count('}')
            if brace_count <= 0 and ('{' in line or '}' in line):
                # End of data block
                blocks.append('\n'.join(current_block))
                current_block = []
                in_data_block = False
                brace_count = 0
        else:
            # Regular log lines (timestamps, POST requests, etc.)
            current_block.append(line)
            # For non-data blocks, we can end them after a reasonable number of lines
            # or when we see a timestamp indicating a new entry
            if len(current_block) > 3 and line.startswith('[') and '] POST /' in line:
                # Start of new POST request, end current block
                if len(current_block) > 1:
                    blocks.append('\n'.join(current_block[:-1]))
                    current_block = [line]
    
    # Add any remaining block
    if current_block:
        blocks.append('\n'.join(current_block))
    
    return blocks

def simulate_live_log(source_file, output_file):
    """
    Simulate a live log by copying blocks from source to output with delays.
    """
    print(f"🎮 Starting log simulation...")
    print(f"   Source: {source_file}")
    print(f"   Output: {output_file}")
    print(f"   Update interval: {SIMULATION_SPEED}s")
    print(f"   Chunk size: {CHUNK_SIZE} blocks per update")
    
    try:
        # Read the source log file
        with open(source_file, 'r', encoding='utf-8') as f:
            source_content = f.read()
        
        # Parse into blocks
        blocks = parse_log_into_blocks(source_content)
        print(f"📊 Parsed {len(blocks)} blocks from source file")
        
        # Clear the output file
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("")  # Clear the file
        
        print("🚀 Starting simulation... Press Ctrl+C to stop")
        
        # Write blocks incrementally
        block_index = 0
        current_game_id = None
        
        while block_index < len(blocks):
            # Check if the next block contains a game ID change
            if block_index < len(blocks):
                game_id_match = re.search(r"GameID:\s*['\"]?(\d+)['\"]?", blocks[block_index])
                if game_id_match:
                    new_game_id = game_id_match.group(1)
                    if current_game_id is not None and new_game_id != current_game_id:
                        print(f"A new game ID ({new_game_id}) was detected. Preparing to start new game...")
                        input("🏁 Match ended. Press Enter to continue to the next match...")
                    current_game_id = new_game_id

            # Write blocks and check for game end
            blocks_written_this_chunk = 0
            for i in range(CHUNK_SIZE):
                if block_index >= len(blocks):
                    break # End of file
                
                current_block = blocks[block_index]
                
                # Write the current block
                with open(output_file, 'a', encoding='utf-8') as f:
                    f.write(current_block + '\n')
                    f.flush()
                
                blocks_written_this_chunk += 1
                block_index += 1
                
                # Check for match end condition within TeamInfoList blocks
                if 'TeamInfoList:' in current_block:
                    teams_string = re.search(r'\[(.*)\]', current_block, re.DOTALL)
                    if teams_string:
                        live_teams = 0
                        live_members_matches = re.findall(r'liveMemberNum:\s*(\d+)', teams_string.group(1))
                        for count_str in live_members_matches:
                            if int(count_str) > 0:
                                live_teams += 1
                        
                        # Match ends when NO teams have live members (everyone eliminated)
                        # OR when only 1 team remains AND you want to pause at victory
                        if live_teams == 0:
                            print("🏁 Match end detected: All teams eliminated.")
                            input("🏁 Match ended. Press Enter to continue to the next match...")
                        elif live_teams == 1:
                            print("🏆 Match end detected: One team victorious.")
                            input("🏁 Match ended. Press Enter to continue to the next match...")
                            pass  
            
            # Show progress
            progress = (block_index / len(blocks)) * 100
            print(f"📝 Written {block_index}/{len(blocks)} blocks ({progress:.1f}%)")
            
            # Wait before next update
            if block_index < len(blocks):
                time.sleep(SIMULATION_SPEED)
        
        print("✅ Simulation complete! All blocks written.")        

    except KeyboardInterrupt:
        print(f"\n⏹️  Simulation stopped by user at block {block_index}/{len(blocks)}")
    except Exception as e:
        print(f"❌ Error during simulation: {e}")
        import traceback
        traceback.print_exc()

def interactive_simulation():
    """
    Interactive mode where user can control the simulation pace.
    """
    global SIMULATION_SPEED, CHUNK_SIZE
    
    test_files = get_test_log_files()
    
    if not test_files:
        print("❌ No test log files found in ./logs/test/")
        print("   Please place test log files (.txt) in the ./logs/test/ directory")
        return
    
    print("📁 Available test files:")
    for i, file in enumerate(test_files):
        print(f"   {i + 1}. {file.name}")
    
    try:
        choice = int(input("Choose a test file (number): ")) - 1
        if choice < 0 or choice >= len(test_files):
            print("❌ Invalid choice")
            return
        
        source_file = test_files[choice]
        output_file = CURRENT_LOG_DIR / OUTPUT_LOG_NAME
        
        print(f"\n⚙️  Simulation settings:")
        print(f"   Current speed: {SIMULATION_SPEED}s between updates")
        print(f"   Current chunk size: {CHUNK_SIZE} blocks per update")
        
        change_settings = input("Change settings? (y/n): ").lower().strip()
        
        if change_settings == 'y':
            try:
                new_speed = float(input(f"Enter update interval in seconds (current: {SIMULATION_SPEED}): "))
                new_chunk = int(input(f"Enter chunk size (current: {CHUNK_SIZE}): "))
                SIMULATION_SPEED = new_speed
                CHUNK_SIZE = new_chunk
            except ValueError:
                print("❌ Invalid input, using default settings")
        
        simulate_live_log(source_file, output_file)
        
    except ValueError:
        print("❌ Invalid input")
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")

def batch_simulation():
    """
    Run simulation with the first available test file automatically.
    """
    test_files = get_test_log_files()
    
    if not test_files:
        print("❌ No test log files found in ./logs/test/")
        return
    
    source_file = test_files[0]  # Use first file
    output_file = CURRENT_LOG_DIR / OUTPUT_LOG_NAME
    
    print(f"🤖 Automatic simulation mode")
    simulate_live_log(source_file, output_file)

def create_sample_test_data():
    """
    Create a sample test file for demonstration purposes.
    """
    sample_data = """[2025-08-19 10:00:00] Starting Game
GameID: "12345"
[2025-08-19 10:00:01] POST /totalmessage
{ TotalPlayerList: 
   [ { uId: 1001,
       playerName: 'TestPlayer1',
       teamId: 1,
       teamName: 'Test Team A',
       health: 100,
       healthMax: 100,
       liveState: 0,
       killNum: 0,
       damage: 0 },
     { uId: 1002,
       playerName: 'TestPlayer2',
       teamId: 1,
       teamName: 'Test Team A',
       health: 100,
       healthMax: 100,
       liveState: 0,
       killNum: 0,
       damage: 0 } ] }
[2025-08-19 10:00:02] POST /setteaminfo
{ TeamInfoList: 
   [ { teamId: 1,
       teamName: 'Test Team A',
       liveMemberNum: 2,
       totalKill: 0 } ] }
[2025-08-19 10:01:00] POST /totalmessage
{ TotalPlayerList: 
   [ { uId: 1001,
       playerName: 'TestPlayer1',
       teamId: 1,
       teamName: 'Test Team A',
       health: 80,
       healthMax: 100,
       liveState: 0,
       killNum: 1,
       damage: 150 },
     { uId: 1002,
       playerName: 'TestPlayer2',
       teamId: 1,
       teamName: 'Test Team A',
       health: 100,
       healthMax: 100,
       liveState: 0,
       killNum: 0,
       damage: 50 } ] }
"""
    
    sample_file = TEST_LOG_DIR / "sample_test.txt"
    TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(sample_file, 'w', encoding='utf-8') as f:
        f.write(sample_data)
    
    print(f"📝 Created sample test file: {sample_file}")

def main():
    """Main function with menu options."""
    ensure_directories()
    
    print("🎯 Log File Simulator")
    print("=" * 50)
    print("This tool simulates live log updates by copying from test files")
    print("to a current log file that can be monitored by live_monitor.py")
    print()
    print("Menu:")
    print("1. Interactive simulation (choose file and settings)")
    print("2. Quick simulation (use first available test file)")
    print("3. Create sample test data")
    print("4. Exit")
    
    try:
        choice = input("\nChoose an option (1-4): ").strip()
        
        if choice == '1':
            interactive_simulation()
        elif choice == '2':
            batch_simulation()
        elif choice == '3':
            create_sample_test_data()
        elif choice == '4':
            print("👋 Goodbye!")
        else:
            print("❌ Invalid choice")
    
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")

if __name__ == "__main__":
    main()