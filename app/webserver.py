import http.server
import socketserver
import threading
import json
import os

class MyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == '/api/live_data':
                json_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'live_data.json')
                with open(json_file_path, 'r') as f:
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(f.read().encode('utf-8'))
            else:
                super().do_GET()
        except Exception as e:
            logging.error(f"Server error on {self.path}: {e}")
            self.send_error(500, f"Server Error: {str(e)}")

def start_server():
    PORT = 5000
    Handler = MyHandler
    server = ThreadingTCPServer(("", PORT), Handler)
    server.timeout = 1
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    print(f"Server started at http://localhost:{PORT}")
    
if __name__ == '__main__':
    start_server()