"""Local-only HTTP fixture for real Masscan/httpx pipeline checks."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b'{"status":"ok"}' if self.path.startswith("/health") else
                b"<html><title>Internet Search Lab Console</title><body>authorized local fixture</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path.startswith("/health") else "text/html")
        self.send_header("X-Lab-Product", "InternetSearchTest")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
