# Live PUBG Tournament Scoreboard

> ⚠️ **Note:** This is initially a test setup. Verify that the data and overlays are accurate. If everything looks correct, you may use them for live coverage—but we recommend a quick double-check before going fully live.

---

## 1. How to Run the Script

The Python script (`live_monitor.py`) parses live log files, calculates tournament standings, and generates a JSON file that your web overlays can read.

1. **Save the provided Python script** (`live_monitor.py`) in your project folder.
2. **Open a terminal or command prompt** in that directory.
3. **Install dependencies** (if not already installed):
    ```bash
    pip install -r requirements.txt
    ```
4. **Run the script** with:
    ```bash
    python live_monitor.py
    ```
5. The script will start monitoring for log files and continuously update the `live_scoreboard.json` file. Keep the terminal window open during testing.

---

## 2. Log File Setup

The script requires access to tournament log files.

* **Live Logs:** Place your live log file(s) in the base log folder (e.g., `./logs/`). The script will monitor the newest log automatically.
* **Archive Logs:** Move older logs to `./logs/All other logs/`. The script will process these for all-time player stats.
* **Directories:** The script will automatically create the required directories if they do not exist.

---

## 3. Setting Up the Overlays

The HTML files (e.g., `overall-standings.html`, `match-standings.html`) are intended for use as "browser sources" in streaming software like OBS Studio.

1. **Add a Browser Source:** In your streaming software, add a **Browser** source.
2. **Enter the Local URL:** Use the URL for your overlay, e.g.:
    * `http://localhost:5000/overlays/overall-standings.html`
    * `http://localhost:5000/overlays/match-standings.html`
3. **Adjust Size:** Set the width and height to match your canvas size (e.g., 1920x1080).
4. **Enable the following settings**:
    * Shutdown Source when not visible
    * Refresh Browser when scene becomes active
5. **Test and Verify:** Open the overlay in a browser to confirm the data is appearing correctly. Compare with known standings to ensure accuracy.

---

## 4. Dynamic Team Logos

The overlays support dynamic logos for teams.

* **Logo Files:** Place the `LOGO` folder in the `assets/` folder (e.g., `assets/LOGO/cs6.png`).
* **Fallback Logo:** `default-team-logo.jpg` will be used if a team logo is missing. Place it in the same `assets/` folder.
* **URL Path:** HTML templates fetch logos from `http://localhost:5000/assets/LOGO`.
* **TeamLogoAndColor.ini:** The current round ini file should be placed in the root folder with this script. The reason for this is that it reads that file to find all the team logos and their names. If the `TeamLogoAndColor.ini` file is created new and the LOGO folder is created new, I can try to have the script find them where they are generated on your computer.

---

## 5. Notes for Testing

* This setup is intended for testing first. Run the script and check that numbers (kills, placements, etc.) match expectations.
* No overlays should be used for live broadcasts until you are confident in the accuracy of the data.
* If the results look correct, the same setup can be used live, but always verify before the tournament begins.

---

## 6. Requirements

Python 3.8 or higher and the following packages (listed in `requirements.txt`):