import sys
import os
from pathlib import Path
import threading
import time

# Add app directory to Python path
app_dir = Path(__file__).parent / "app"
sys.path.insert(0, str(app_dir))

try:
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
    COLORAMA_AVAILABLE = True
except ImportError:
    print("Warning: colorama not installed. Install with: pip install colorama")
    COLORAMA_AVAILABLE = False
    # Fallback color definitions
    class Fore:
        RED = YELLOW = GREEN = CYAN = MAGENTA = BLUE = WHITE = RESET = ""
    class Back:
        BLACK = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = WHITE = RESET = ""
    class Style:
        BRIGHT = DIM = NORMAL = RESET_ALL = ""

def print_colored(text, color=Fore.WHITE, style=Style.NORMAL):
    """Print colored text if colorama is available."""
    if COLORAMA_AVAILABLE:
        print(f"{style}{color}{text}{Style.RESET_ALL}")
    else:
        print(text)

def print_banner():
    """Print a colorful banner for the application."""
    banner = [
        "╔══════════════════════════════════════════════════════════════╗",
        "║                    PUBG LIVE MONITOR                         ║",
        "║                     Tournament Suite                         ║",
        "╚══════════════════════════════════════════════════════════════╝"
    ]
    
    print_colored("\n", Fore.CYAN)
    for line in banner:
        print_colored(line, Fore.CYAN, Style.BRIGHT)
    print_colored("", Fore.CYAN)

def print_menu():
    """Print the main menu with colors."""
    print_colored("┌──────────────────────────────────────────────────────────────┐", Fore.BLUE)
    print_colored("│                        MAIN MENU                            │", Fore.BLUE, Style.BRIGHT)
    print_colored("├──────────────────────────────────────────────────────────────┤", Fore.BLUE)
    print_colored("│  1. Live Monitor (Production Mode)                          │", Fore.GREEN)
    print_colored("│     Monitor real PUBG log files                             │", Fore.WHITE, Style.DIM)
    print_colored("│                                                              │", Fore.WHITE)
    print_colored("│  2. Test Mode (Simulation)                                  │", Fore.YELLOW)
    print_colored("│     Simulate live match using test log files                │", Fore.WHITE, Style.DIM)
    print_colored("│                                                              │", Fore.WHITE)
    print_colored("│  3. Reprocess Archived Data                                 │", Fore.MAGENTA)
    print_colored("│     Rebuild all-time statistics from archived logs          │", Fore.WHITE, Style.DIM)
    print_colored("│                                                              │", Fore.WHITE)
    print_colored("│  4. Exit                                                     │", Fore.RED)
    print_colored("└──────────────────────────────────────────────────────────────┘", Fore.BLUE)

def get_user_choice():
    """Get and validate user input."""
    while True:
        try:
            choice = input(f"{Fore.CYAN}Enter your choice (1-4): {Style.RESET_ALL}").strip()
            if choice in ['1', '2', '3', '4']:
                return choice
            print_colored("Invalid choice. Please enter a number from 1 to 4.", Fore.RED)
        except KeyboardInterrupt:
            print_colored("\nExiting...", Fore.YELLOW)
            sys.exit(0)

def check_dependencies():
    """Check if all required modules are available."""
    missing = []
    
    try:
        import live_monitor
    except ImportError as e:
        missing.append(f"live_monitor ({e})")
    
    try:
        import log_simulator
    except ImportError as e:
        missing.append(f"log_simulator ({e})")
    
    if missing:
        print_colored(f"Error: Missing required modules: {', '.join(missing)}", Fore.RED)
        print_colored("Please ensure all files are in the app/ directory.", Fore.YELLOW)
        print_colored("Current working directory:", Fore.WHITE)
        print_colored(f"  {os.getcwd()}", Fore.WHITE, Style.DIM)
        print_colored("App directory:", Fore.WHITE)
        print_colored(f"  {app_dir}", Fore.WHITE, Style.DIM)
        sys.exit(1)

def run_test_mode():
    """Run the test mode with integrated simulation."""
    try:
        from live_monitor import main as live_monitor_main
        from log_simulator import SimulationManager

        # Initialize the simulation manager in quiet mode to suppress terminal output
        simulation_manager = SimulationManager(quiet=True)
        live_monitor_main.simulation_manager = simulation_manager

        # Start the simulation thread
        if simulation_manager.start():
            # Run the live monitor in test mode, which will read the simulated log file
            print_colored("Starting Live Monitor (Test Mode)...", Fore.YELLOW, Style.BRIGHT)
            live_monitor_main(test_mode=True, reprocess=False)
        else:
            print_colored("Failed to start simulation.", Fore.RED)

    except KeyboardInterrupt:
        print_colored("\nTest mode stopped by user.", Fore.YELLOW)
        if 'simulation_manager' in locals():
            simulation_manager.stop()
    except Exception as e:
        print_colored(f"Unexpected error in test mode: {e}", Fore.RED)

def main():
    """Main entry point."""
    check_dependencies()
    
    while True:
        print_banner()
        print_menu()
        choice = get_user_choice()

        if choice == '1':
            print_colored("Starting Live Monitor (Production Mode)...", Fore.GREEN, Style.BRIGHT)
            print_colored("Monitoring real PUBG log files...", Fore.WHITE)
            try:
                import live_monitor
                live_monitor.main(test_mode=False, reprocess=False)
            except KeyboardInterrupt:
                print_colored("\nLive monitor stopped by user.", Fore.YELLOW)
            break
            
        elif choice == '2':
            run_test_mode()
            break
            
        elif choice == '3':
            print_colored("Reprocessing Archived Data...", Fore.MAGENTA, Style.BRIGHT)
            print_colored("This will rebuild all-time statistics from archived logs.", Fore.WHITE)
            try:
                import live_monitor
                live_monitor.main(test_mode=False, reprocess=True)
            except KeyboardInterrupt:
                print_colored("\nReprocessing stopped by user.", Fore.YELLOW)
            break
            
        elif choice == '4':
            print_colored("Thank you for using PUBG Live Monitor!", Fore.GREEN, Style.BRIGHT)
            print_colored("Goodbye!", Fore.CYAN)
            sys.exit(0)

if __name__ == "__main__":
    main()