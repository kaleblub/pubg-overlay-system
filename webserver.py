import http.server
import socketserver
import threading
import json
import os

class MyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/live_data':
            json_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'live_data.json')
            try:
                with open(json_file_path, 'r') as f:
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(f.read().encode('utf-8'))
            except FileNotFoundError:
                self.send_error(404, "File Not Found: live_data.json")
        else:
            # Handle other requests, like for CSS/JS files
            super().do_GET()

def start_server():
    PORT = 5000
    Handler = MyHandler
    server = socketserver.TCPServer(("", PORT), Handler)
    
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    print(f"Server started at http://localhost:{PORT}")
    
if __name__ == '__main__':
    start_server()