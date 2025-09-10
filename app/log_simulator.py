import re
import time
import os
import threading
import logging
from pathlib import Path
from config import *

try:
    from colorama import Fore, Style
    COLORAMA_AVAILABLE = True
except ImportError:
    COLORAMA_AVAILABLE = False
    class Fore:
        RED = YELLOW = GREEN = CYAN = MAGENTA = BLUE = WHITE = RESET = ""
    class Style:
        BRIGHT = DIM = NORMAL = RESET_ALL = ""

class SimulationManager:
    """Manages log file simulation in a separate thread."""
    
    def __init__(self, quiet=False):
        self.thread = None
        self.stop_flag = threading.Event()
        self.current_progress = 0.0
        self.total_blocks = 0
        self.current_block = 0
        self.is_running = False
        self.simulation_complete = False
        self.source_file = None
        self.teams_in_game = {}
        self.quiet = quiet
        
    def get_progress(self):
        """Get current simulation progress as percentage."""
        return self.current_progress
    
    def get_progress_string(self):
        """Get a formatted progress string with bar and percentage."""
        if self.total_blocks == 0:
            return "Waiting..."
        
        progress = self.current_progress
        bar_length = 20
        filled = int(bar_length * progress / 100)
        bar = "█" * filled + "░" * (bar_length - filled)
        return f"[{bar}] {progress:5.1f}%"

    def is_complete(self):
        """Check if simulation is complete."""
        return self.simulation_complete
    
    def start(self):
        """Start the simulation in a separate thread."""
        test_files = self._get_test_log_files()
        
        if not test_files:
            self._print_colored("No test log files found in ./logs/test/", Fore.RED)
            self._print_colored("Creating sample test data...", Fore.YELLOW)
            sample_file = self.create_sample_test_data()
            test_files = [sample_file] if sample_file else []
        
        if not test_files:
            logging.error("No test files available for simulation")
            return False
        
        self.source_file = test_files[0]
        self._print_colored(f"Using test file: {self.source_file.name}", Fore.CYAN)
        self.output_file = SIMULATED_LOG_FILE
        self.stop_flag.clear()
        
        self.thread = threading.Thread(
            target=self._simulate_live_log, 
            args=(self.source_file, self.output_file),
            daemon=True
        )
        self.thread.start()
        
        return True
    
    def stop(self):
        """Stop the simulation."""
        if self.thread and self.is_running:
            self.stop_flag.set()
            self.thread.join(timeout=5)
            self.is_running = False
    
    def _print_colored(self, text, color=Fore.WHITE):
        """Print colored text if available."""
        if not self.quiet:
            if COLORAMA_AVAILABLE:
                print(f"{color}{text}{Style.RESET_ALL}")
            else:
                print(text)
    
    def _get_test_log_files(self):
        """Get all test log files from the test directory."""
        if not TEST_LOGS_DIR.exists():
            return []
        
        log_files = []
        for file in TEST_LOGS_DIR.glob("*.txt"):
            if file.is_file():
                log_files.append(file)
        return sorted(log_files)

    def _parse_log_into_blocks(self, log_content):
        """
        Parse a log file into discrete blocks that can be written incrementally.
        Each block represents a meaningful update (player list, team info, etc.).
        """
        blocks = []
        lines = log_content.split('\n')
        current_block = []
        in_data_block = False
        brace_count = 0

        for line in lines:
            # Check for start of data blocks
            if any(marker in line for marker in ['TotalPlayerList:', 'TeamInfoList:', 'GameID:']):
                # Save current block if it exists
                if current_block:
                    blocks.append('\n'.join(current_block))
                    current_block = []

                # Start new block
                current_block.append(line)
                if 'TotalPlayerList:' in line or 'TeamInfoList:' in line:
                    in_data_block = True
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
                # Regular log lines
                current_block.append(line)
                # End block at POST requests or after reasonable length
                if len(current_block) > 3 and line.startswith('[') and '] POST /' in line:
                    if len(current_block) > 1:
                        blocks.append('\n'.join(current_block[:-1]))
                    current_block = [line]

        # Add any remaining block
        if current_block:
            blocks.append('\n'.join(current_block))

        return [block for block in blocks if block.strip()]
    
    def _simulate_live_log(self, source_file, output_file):
        """
        Simulate a live log by writing content from the source file in
        discrete blocks with a time delay.
        """
        self.is_running = True
        self.simulation_complete = False
        
        try:
            # Read the entire source file to parse it into blocks
            with open(source_file, 'r', encoding='utf-8') as f:
                log_content = f.read()
            
            blocks = self._parse_log_into_blocks(log_content)
            self.total_blocks = len(blocks)
            self._print_colored(f"Parsed {self.total_blocks} log blocks.", Fore.MAGENTA)
            
            # Clear output file before starting
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(output_file, 'w', encoding='utf-8') as f:
                f.truncate(0)
            
            logging.info(f"Starting simulation from {source_file.name}")
            
            with open(output_file, 'a', encoding='utf-8') as out:
                self.current_block = 0
                while self.current_block < self.total_blocks and not self.stop_flag.is_set():
                    
                    # Write a chunk of blocks
                    for i in range(SIMULATION_CHUNK_SIZE):
                        if self.current_block < self.total_blocks:
                            current_block = blocks[self.current_block]
                            out.write(current_block + '\n')
                            
                            # Track game state for simulation control
                            if 'GameID:' in current_block:
                                # Extract the GameID
                                match = re.search(r'GameID:\s*["\']?(\d+)["\']?', current_block)
                                if match:
                                    self.teams_in_game = {}  # Reset team tracker for the new game
                                    # logging.info(f"Starting simulation for GameID {match.group(1)}")
                            
                            self.current_block += 1
                        else:
                            break
                    
                    out.flush() # Ensures data is written to the file system
                    self.current_progress = (self.current_block / self.total_blocks) * 100
                    
                    time.sleep(SIMULATION_SPEED)
                    
        except Exception as e:
            logging.error(f"Simulation error: {e}")
        finally:
            self.is_running = False
            self.simulation_complete = True
            logging.info("Simulation completed.")

    def create_sample_test_data(self):
        """Create sample test data for demonstration."""
        ensure_directories()
        
        sample_data = """[2025-08-31 10:00:00] Starting Game
GameID: "12345"

[2025-08-31 10:00:01] POST /totalmessage
TotalPlayerList:
{ uId: 1001, playerName: 'Player1', teamId: 1, teamName: 'Team Alpha', health: 100, healthMax: 100, liveState: 0, killNum: 0, damage: 0 }
{ uId: 1002, playerName: 'Player2', teamId: 1, teamName: 'Team Alpha', health: 100, healthMax: 100, liveState: 0, killNum: 0, damage: 0 }
{ uId: 2001, playerName: 'Player3', teamId: 2, teamName: 'Team Beta', health: 100, healthMax: 100, liveState: 0, killNum: 0, damage: 0 }
{ uId: 2002, playerName: 'Player4', teamId: 2, teamName: 'Team Beta', health: 100, healthMax: 100, liveState: 0, killNum: 0, damage: 0 }
{ uId: 3001, playerName: 'Player5', teamId: 3, teamName: 'Team Gamma', health: 100, healthMax: 100, liveState: 0, killNum: 0, damage: 0 }
{ uId: 3002, playerName: 'Player6', teamId: 3, teamName: 'Team Gamma', health: 100, healthMax: 100, liveState: 0, killNum: 0, damage: 0 }

[2025-08-31 10:00:02] POST /setteaminfo
TeamInfoList:
{ teamId: 1, teamName: 'Team Alpha', liveMemberNum: 2, totalKill: 0 }
{ teamId: 2, teamName: 'Team Beta', liveMemberNum: 2, totalKill: 0 }
{ teamId: 3, teamName: 'Team Gamma', liveMemberNum: 2, totalKill: 0 }

[2025-08-31 10:01:00] POST /totalmessage
TotalPlayerList:
{ uId: 1001, playerName: 'Player1', teamId: 1, teamName: 'Team Alpha', health: 80, healthMax: 100, liveState: 0, killNum: 1, damage: 150 }
{ uId: 1002, playerName: 'Player2', teamId: 1, teamName: 'Team Alpha', health: 100, healthMax: 100, liveState: 0, killNum: 0, damage: 50 }
{ uId: 2001, playerName: 'Player3', teamId: 2, teamName: 'Team Beta', health: 0, healthMax: 100, liveState: 5, killNum: 0, damage: 25 }
{ uId: 2002, playerName: 'Player4', teamId: 2, teamName: 'Team Beta', health: 60, healthMax: 100, liveState: 0, killNum: 0, damage: 80 }
{ uId: 3001, playerName: 'Player5', teamId: 3, teamName: 'Team Gamma', health: 90, healthMax: 100, liveState: 0, killNum: 0, damage: 120 }
{ uId: 3002, playerName: 'Player6', teamId: 3, teamName: 'Team Gamma', health: 100, healthMax: 100, liveState: 0, killNum: 1, damage: 200 }

[2025-08-31 10:01:01] POST /setteaminfo
TeamInfoList:
{ teamId: 1, teamName: 'Team Alpha', liveMemberNum: 2, totalKill: 1 }
{ teamId: 2, teamName: 'Team Beta', liveMemberNum: 1, totalKill: 0 }
{ teamId: 3, teamName: 'Team Gamma', liveMemberNum: 2, totalKill: 1 }

[2025-08-31 10:02:00] POST /totalmessage
TotalPlayerList:
{ uId: 1001, playerName: 'Player1', teamId: 1, teamName: 'Team Alpha', health: 100, healthMax: 100, liveState: 0, killNum: 2, damage: 300 }
{ uId: 1002, playerName: 'Player2', teamId: 1, teamName: 'Team Alpha', health: 100, healthMax: 100, liveState: 0, killNum: 1, damage: 180 }
{ uId: 2001, playerName: 'Player3', teamId: 2, teamName: 'Team Beta', health: 0, healthMax: 100, liveState: 5, killNum: 0, damage: 25 }
{ uId: 2002, playerName: 'Player4', teamId: 2, teamName: 'Team Beta', health: 0, healthMax: 100, liveState: 5, killNum: 0, damage: 80 }
{ uId: 3001, playerName: 'Player5', teamId: 3, teamName: 'Team Gamma', health: 40, healthMax: 100, liveState: 0, killNum: 0, damage: 120 }
{ uId: 3002, playerName: 'Player6', teamId: 3, teamName: 'Team Gamma', health: 80, healthMax: 100, liveState: 0, killNum: 2, damage: 400 }

[2025-08-31 10:02:01] POST /setteaminfo
TeamInfoList:
{ teamId: 1, teamName: 'Team Alpha', liveMemberNum: 2, totalKill: 3 }
{ teamId: 2, teamName: 'Team Beta', liveMemberNum: 0, totalKill: 0 }
{ teamId: 3, teamName: 'Team Gamma', liveMemberNum: 2, totalKill: 2 }

[2025-08-31 10:03:00] POST /totalmessage
TotalPlayerList:
{ uId: 1001, playerName: 'Player1', teamId: 1, teamName: 'Team Alpha', health: 100, healthMax: 100, liveState: 0, killNum: 3, damage: 450 }
{ uId: 1002, playerName: 'Player2', teamId: 1, teamName: 'Team Alpha', health: 100, healthMax: 100, liveState: 0, killNum: 1, damage: 180 }
{ uId: 2001, playerName: 'Player3', teamId: 2, teamName: 'Team Beta', health: 0, healthMax: 100, liveState: 5, killNum: 0, damage: 25 }
{ uId: 2002, playerName: 'Player4', teamId: 2, teamName: 'Team Beta', health: 0, healthMax: 100, liveState: 5, killNum: 0, damage: 80 }
{ uId: 3001, playerName: 'Player5', teamId: 3, teamName: 'Team Gamma', health: 0, healthMax: 100, liveState: 5, killNum: 0, damage: 120 }
{ uId: 3002, playerName: 'Player6', teamId: 3, teamName: 'Team Gamma', health: 20, healthMax: 100, liveState: 0, killNum: 2, damage: 400 }

[2025-08-31 10:03:01] POST /setteaminfo
TeamInfoList:
{ teamId: 1, teamName: 'Team Alpha', liveMemberNum: 2, totalKill: 4 }
{ teamId: 2, teamName: 'Team Beta', liveMemberNum: 0, totalKill: 0 }
{ teamId: 3, teamName: 'Team Gamma', liveMemberNum: 1, totalKill: 2 }

[2025-08-31 10:04:00] POST /totalmessage
TotalPlayerList:
{ uId: 1001, playerName: 'Player1', teamId: 1, teamName: 'Team Alpha', health: 100, healthMax: 100, liveState: 0, killNum: 4, damage: 600 }
{ uId: 1002, playerName: 'Player2', teamId: 1, teamName: 'Team Alpha', health: 100, healthMax: 100, liveState: 0, killNum: 1, damage: 180 }
{ uId: 2001, playerName: 'Player3', teamId: 2, teamName: 'Team Beta', health: 0, healthMax: 100, liveState: 5, killNum: 0, damage: 25 }
{ uId: 2002, playerName: 'Player4', teamId: 2, teamName: 'Team Beta', health: 0, healthMax: 100, liveState: 5, killNum: 0, damage: 80 }
{ uId: 3001, playerName: 'Player5', teamId: 3, teamName: 'Team Gamma', health: 0, healthMax: 100, liveState: 5, killNum: 0, damage: 120 }
{ uId: 3002, playerName: 'Player6', teamId: 3, teamName: 'Team Gamma', health: 0, healthMax: 100, liveState: 5, killNum: 2, damage: 400 }

[2025-08-31 10:04:01] POST /setteaminfo
TeamInfoList:
{ teamId: 1, teamName: 'Team Alpha', liveMemberNum: 2, totalKill: 5 }
{ teamId: 2, teamName: 'Team Beta', liveMemberNum: 0, totalKill: 0 }
{ teamId: 3, teamName: 'Team Gamma', liveMemberNum: 0, totalKill: 2 }
"""
        
        sample_file = TEST_LOGS_DIR / "sample_match.txt"
        with open(sample_file, 'w', encoding='utf-8') as f:
            f.write(sample_data)
        
        if COLORAMA_AVAILABLE:
            print(f"{Fore.GREEN}Created sample test file: {sample_file}{Style.RESET_ALL}")
        else:
            print(f"Created sample test file: {sample_file}")
        
        return sample_file

# Standalone functions for backward compatibility
def start_simulation_thread():
    """Start simulation in a separate thread (for backward compatibility)."""
    manager = SimulationManager()
    if manager.start():
        return manager.thread
    return None

def simulate_live_log(source_file, output_file):
    """Standalone simulation function."""
    manager = SimulationManager()
    manager._simulate_live_log(source_file, output_file)

def get_test_log_files():
    """Public function to get test log files."""
    if not TEST_LOGS_DIR.exists():
        return []
    
    log_files = []
    for file in TEST_LOGS_DIR.glob("*.txt"):
        if file.is_file():
            log_files.append(file)
    return sorted(log_files)

def interactive_test_setup():
    """Interactive setup for choosing test files."""
    test_files = get_test_log_files()
    
    if not test_files:
        if COLORAMA_AVAILABLE:
            print(f"{Fore.RED}No test log files found!{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}Looking in: {TEST_LOGS_DIR}{Style.RESET_ALL}")
        else:
            print("No test log files found!")
            print(f"Looking in: {TEST_LOGS_DIR}")
        
        create_sample = input("Create sample test data? (y/n): ").lower().strip()
        if create_sample == 'y':
            manager = SimulationManager()
            manager.create_sample_test_data()
            test_files = get_test_log_files()
        
        if not test_files:
            if COLORAMA_AVAILABLE:
                print(f"{Fore.RED}No test files available for simulation.{Style.RESET_ALL}")
            else:
                print("No test files available for simulation.")
            return None

    if COLORAMA_AVAILABLE:
        print(f"{Fore.CYAN}Available test files:{Style.RESET_ALL}")
    else:
        print("Available test files:")
        
    for i, file in enumerate(test_files):
        file_size = file.stat().st_size if file.exists() else 0
        size_kb = file_size / 1024
        if COLORAMA_AVAILABLE:
            print(f"{Fore.WHITE}  {i + 1}. {file.name} ({size_kb:.1f} KB){Style.RESET_ALL}")
        else:
            print(f"  {i + 1}. {file.name} ({size_kb:.1f} KB)")

    try:
        choice = int(input("Choose a test file (number): ")) - 1
        if 0 <= choice < len(test_files):
            return test_files[choice]
        else:
            if COLORAMA_AVAILABLE:
                print(f"{Fore.RED}Invalid choice{Style.RESET_ALL}")
            else:
                print("Invalid choice")
            return None
    except ValueError:
        if COLORAMA_AVAILABLE:
            print(f"{Fore.RED}Invalid input{Style.RESET_ALL}")
        else:
            print("Invalid input")
        return None

if __name__ == "__main__":
    # If run directly, provide a simple interface
    print("PUBG Log Simulator")
    print("This module is designed to be imported by the main launcher.")
    print("Use launch.py to run the complete system.")
    
    choice = input("Run simulation anyway? (y/n): ").lower().strip()
    if choice == 'y':
        test_files = get_test_log_files()
        if test_files:
            simulate_live_log(test_files[0], SIMULATED_LOG_FILE)
        else:
            print("No test files found. Creating sample data...")
            manager = SimulationManager()
            manager.create_sample_test_data()
