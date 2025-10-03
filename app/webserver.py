import threading
import json
import os
from flask import Flask, send_from_directory, Response

app = Flask(__name__)

# Get project root directory (parent of 'app' folder)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@app.route('/api/live_data')
def get_live_data():
    json_file_path = os.path.join(PROJECT_ROOT, 'live_scoreboard.json')
    app.logger.debug(f"Looking for JSON file at: {json_file_path}")
    if not os.path.exists(json_file_path):
        app.logger.error(f"JSON file not found: {json_file_path}")
        return Response("File Not Found: live_scoreboard.json", status=404)
    try:
        with open(json_file_path, 'r', encoding='utf-8') as f:
            return Response(f.read(), status=200, mimetype='application/json')
    except Exception as e:
        app.logger.error(f"Error reading JSON file: {e}")
        return Response(f"Error reading file: {e}", status=500)

@app.route('/<path:path>')
def serve_static(path):
    app.logger.debug(f"Attempting to serve file: {os.path.join(PROJECT_ROOT, path)}")
    try:
        return send_from_directory(PROJECT_ROOT, path)
    except Exception as e:
        app.logger.error(f"Error serving file {path}: {e}")
        return Response(f"File Not Found: {path}", status=404)

@app.route('/')
def index():
    app.logger.debug("Serving root endpoint")
    return Response("Flask server is running!", status=200)

def start_server():
    try:
        app.logger.info("Starting Flask server...")
        server_thread = threading.Thread(target=app.run, kwargs={
            'host': '0.0.0.0',
            'port': 5000,
            'debug': True,  # Enable debug for detailed logs
            'use_reloader': False,
            'threaded': True
        })
        server_thread.daemon = True
        server_thread.start()
        app.logger.info("Server started at http://0.0.0.0:5000 (accessible on local network)")
        print("Server started at http://0.0.0.0:5000 (accessible on local network)")
    except Exception as e:
        app.logger.error(f"Failed to start server: {e}")
        print(f"Failed to start server: {e}")

if __name__ == '__main__':
    start_server()