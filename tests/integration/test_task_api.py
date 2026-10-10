"""Smoke test for the real TASK API and physical database lifecycle."""
import json
import os
import time
import uuid
from pathlib import Path
from unittest import TestCase
from urllib.request import Request, urlopen


API = os.getenv("TASK_API_URL", "http://api:8000")


def multipart(fields, filename, content):
    boundary = "----task-test-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    chunks.extend([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n".encode(),
        content, b"\r\n", f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def request(path, method="GET", body=None, content_type=None):
    headers = {"Content-Type": content_type} if content_type else {}
    with urlopen(Request(API + path, data=body, method=method, headers=headers), timeout=30) as response:
        return response.status, json.loads(response.read() or b"null")


class TaskApiTests(TestCase):
    def test_create_real_task_and_read_physical_pipeline(self):
        fields = {
            "name": "integration-task-" + uuid.uuid4().hex[:8],
            "masscan_chunk_size": "1", "httpx_chunk_size": "1",
            "masscan_ports": "1", "masscan_rate": "1",
            "masscan_workers": "1", "httpx_workers": "1",
            "httpx_paths": json.dumps(["/remote/login"]),
            "httpx_schemes": json.dumps(["http"]),
        }
        body, content_type = multipart(fields, "scope.txt", b"192.0.2.1/32\n")
        status, task = request("/tasks", "POST", body, content_type)
        self.assertEqual(status, 202)
        self.assertRegex(task["database_name"], r"^task_[0-9a-f]{32}$")
        deadline = time.monotonic() + 90
        while task["status"] in {"provisioning", "queued", "running"} and time.monotonic() < deadline:
            time.sleep(1)
            _, task = request(f"/tasks/{task['id']}")
        self.assertEqual(task["status"], "completed", task)
        self.assertEqual(task["target_count"], 1)
        self.assertEqual(task["masscan_chunks"]["total"], 1)
        self.assertEqual(task["masscan_chunks"]["completed"], 1)
        status, targets = request(f"/tasks/{task['id']}/targets")
        self.assertEqual(status, 200)
        self.assertEqual(targets["items"][0]["ip"], "192.0.2.1")
        status, deleted = request(f"/tasks/{task['id']}", "DELETE")
        self.assertEqual(status, 204)
