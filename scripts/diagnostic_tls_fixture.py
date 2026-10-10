#!/usr/bin/env python3
"""Owned HTTPS fixture for scanner diagnostics; never publish its Docker ports.

10443 /remote/login imitates only harmless Fortinet response markers. 10444
is unrelated HTTPS, 10445 is closed, and 10446 accepts TCP but rejects TLS.
10443 /redirect-a and /redirect-b converge on the same positive login path.
Use a disposable self-signed certificate passed through --cert and --key.
"""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import socketserver
import ssl
import threading


class Handler(BaseHTTPRequestHandler):
    server_version = "ScannerDiagnosticFixture/1.0"

    def do_GET(self):
        if self.server.server_port == 10443 and self.path in {"/redirect-a", "/redirect-b"}:
            self.send_response(302)
            self.send_header("Location", "/remote/login")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        positive = self.server.server_port == 10443 and self.path == "/remote/login"
        body = (
            b"<!doctype html><title>FortiGate Diagnostic Fixture</title>"
            b"<p>fortinet: harmless owned scanner test</p>"
            if positive else
            b"<!doctype html><title>Unrelated Diagnostic Service</title>"
            b"<p>Ordinary service without product markers.</p>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if positive:
            self.send_header("Set-Cookie", "SVPNCOOKIE=diagnostic-only; Secure; HttpOnly")
        self.end_headers()
        self.wfile.write(body)
        with self.server.log_lock:
            with self.server.request_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                         "peer": self.client_address[0],
                                         "port": self.server.server_port,
                                         "path": self.path, "positive_fixture": positive}) + "\n")

    def log_message(self, *args):
        pass


class RejectTLSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        try:
            self.request.recv(4096)
        except OSError:
            pass
        # Closing after ClientHello gives a reproducible TCP-positive/TLS-negative
        # service, without a successful HTTP response.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--request-log", required=True, type=Path)
    args = parser.parse_args()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    lock = threading.Lock()
    servers = []
    for port in (10443, 10444):
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        server.request_log = args.request_log
        server.log_lock = lock
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    rejected = socketserver.ThreadingTCPServer(("0.0.0.0", 10446), RejectTLSHandler)
    rejected.daemon_threads = True
    threading.Thread(target=rejected.serve_forever, daemon=True).start()
    print(json.dumps({"ready": True, "open_ports": [10443, 10444, 10446], "closed_port": 10445}), flush=True)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *args: stopped.set())
    signal.signal(signal.SIGINT, lambda *args: stopped.set())
    stopped.wait()
    for server in (*servers, rejected):
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
