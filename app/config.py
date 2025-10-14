# app/config.py

from pathlib import Path

# ---------- Directory Configuration ----------
ROOT_DIR = Path(__file__).parent.parent  # Go up one level from app/ to project root
APP_DIR = Path(__file__).parent

# Log directories
LOGS_DIR = ROOT_DIR / "logs"
TEST_LOGS_DIR = LOGS_DIR / "test"
ARCHIVE_LOG_DIR = LOGS_DIR
CURRENT_LOG_DIR = ROOT_DIR  # Live monitor looks in project root

# Output files
OUTPUT_JSON = ROOT_DIR / "live_scoreboard.json"
ALL_TIME_PLAYERS_JSON = ROOT_DIR / "all_time_players.json"
SIMULATED_LOG_FILE = ROOT_DIR / "simulated_live.txt"

# Asset paths
LOGO_FOLDER_PATH = ROOT_DIR / "assets" / "LOGO"
ADJACENT_LOGO_FOLDER_PATH = "/assets/LOGO/"  # Changed to relative path
DEFAULT_TEAM_LOGO = "/assets/default-team-logo.jpg"  # Changed to relative path
DEFAULT_PLAYER_PHOTO = "/assets/PUBG.png"  # Changed to relative path
PLAYER_PHOTOS_FOLDER = ROOT_DIR / "assets" / "Players"
ADJACENT_PLAYER_PHOTOS_PATH = "/assets/Players/"

# Team configuration
TEAM_CONFIG_FILE = ROOT_DIR / "TeamLogoAndColor.ini"

# ---------- Timing Configuration ----------
UPDATE_INTERVAL = 0.5  # How often to update JSON output
FILE_CHECK_INTERVAL = 0.1  # How often to check for file changes
SIMULATION_SPEED = 0.005  # Seconds between simulation updates
SIMULATION_CHUNK_SIZE = 1  # How many log blocks to write at once

# ---------- Game Configuration ----------
PLACEMENT_POINTS = {1: 10, 2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 1, 8: 1}

# ---------- Server Configuration ----------
WEB_SERVER_PORT = 5000
WEB_SERVER_HOST = "0.0.0.0"  # Changed to allow network access

# ---------- Logging Configuration ----------
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"

# ---------- Create Required Directories ----------
def ensure_directories():
    """Create all required directories if they don't exist."""
    directories = [
        LOGS_DIR,
        TEST_LOGS_DIR,
        ARCHIVE_LOG_DIR,
        LOGO_FOLDER_PATH.parent,
        LOGO_FOLDER_PATH,
        PLAYER_PHOTOS_FOLDER
    ]
    
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

# ---------- Validation ----------
def validate_config():
    """Validate that all required directories and files are accessible."""
    ensure_directories()
    
    issues = []
    
    # Check if test logs directory has any files
    if not any(TEST_LOGS_DIR.glob("*.txt")):
        issues.append(f"No test log files found in {TEST_LOGS_DIR}")
    
    # Check if team config file exists
    if not TEAM_CONFIG_FILE.exists():
        issues.append(f"Team config file not found: {TEAM_CONFIG_FILE}")
    
    return issues