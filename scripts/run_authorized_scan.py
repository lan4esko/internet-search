#!/usr/bin/env python3
"""Create and monitor one TASK through the real API.

The script is a non-interactive alternative to the web panel. It never starts
the removed snapshot/shard controllers and never invents scan results.
"""
import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen


def api_json(base: str, path: str, method: str = "GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = Request(base.rstrip("/") + path, data=data, method=method,
                      headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def create_task(base: str, args):
    source = Path(args.scope).read_bytes()
    fields = {
        "name": args.name,
        "masscan_chunk_size": str(args.masscan_chunk_size),
        "httpx_chunk_size": str(args.httpx_chunk_size),
        "masscan_ports": args.masscan_ports,
        "masscan_rate": str(args.masscan_rate),
        "masscan_workers": str(args.masscan_workers),
        "httpx_workers": str(args.httpx_workers),
        "httpx_paths": json.dumps(args.httpx_paths),
        "httpx_schemes": json.dumps(args.httpx_schemes),
    }
    boundary = "----internet-search-task"
    chunks = []
    for name, value in fields.items():
        chunks += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()]
    chunks += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{Path(args.scope).name}\"\r\nContent-Type: text/plain\r\n\r\n".encode(), source, b"\r\n", f"--{boundary}--\r\n".encode()]
    request = Request(base.rstrip("/") + "/tasks", data=b"".join(chunks), method="POST",
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", type=Path, help="TXT с IPv4 CIDR")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--name", default="CLI TASK")
    parser.add_argument("--masscan-chunk-size", type=int, default=8192)
    parser.add_argument("--httpx-chunk-size", type=int, default=200)
    parser.add_argument("--masscan-ports", default="443,4443,8443,10443")
    parser.add_argument("--masscan-rate", type=int, default=1000)
    parser.add_argument("--masscan-workers", type=int, default=1)
    parser.add_argument("--httpx-workers", type=int, default=1)
    parser.add_argument("--httpx-paths", nargs="+", default=["/remote/login", "/remote/index", "/ssl-vpn/login.html"])
    parser.add_argument("--httpx-schemes", nargs="+", choices=["http", "https"], default=["http", "https"])
    parser.add_argument("--poll", type=float, default=2.0)
    args = parser.parse_args()
    task = create_task(args.api, args)
    print(json.dumps(task, ensure_ascii=False, indent=2))
    while task["status"] in {"provisioning", "queued", "running"}:
        time.sleep(max(0.2, args.poll))
        task = api_json(args.api, f"/tasks/{task['id']}")
        print(f"TASK #{task['id']}: {task['status']} {task['progress_percent']}% "
              f"IP={task['target_count']} open={task['open_port_count']} results={task['result_count']}", flush=True)
    print(json.dumps(api_json(args.api, f"/tasks/{task['id']}/export"), ensure_ascii=False, indent=2))
    return 0 if task["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
