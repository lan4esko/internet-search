"""Narrow HTTP facade: no caller-supplied Docker paths, IDs, filters or methods."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os

from .docker_stats import DockerStats


class MetricsHandler(BaseHTTPRequestHandler):
    server_version = "ContainerMetrics"

    def do_GET(self):
        if self.path != "/metrics" or self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
            self.send_error(404)
            return
        try:
            result = self.server.stats.snapshot()
        except Exception:
            # Never return daemon responses, environment, paths or credentials to the client.
            self.send_error(503, "Container statistics unavailable")
            return
        body = json.dumps(result, allow_nan=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    # Single reader serializes CPU sample updates and bounds Docker request concurrency.
    server = HTTPServer(("0.0.0.0", 9108), MetricsHandler)
    server.stats = DockerStats(os.getenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock"),
                               os.environ["METRICS_COMPOSE_PROJECT"])
    server.serve_forever()


if __name__ == "__main__":
    main()
