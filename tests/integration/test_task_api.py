"""Smoke test for the real TASK API and physical database lifecycle."""
import json
import os
import time
import uuid
from pathlib import Path
from unittest import TestCase
from urllib.request import Request, urlopen


API = os.getenv("TASK_API_URL", "http://api:8000")
TEST_CIDR = os.getenv("TASK_TEST_CIDR", "192.0.2.1/32")


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
        body, content_type = multipart(fields, "scope.txt", (TEST_CIDR + "\n").encode())
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
        self.assertEqual(targets["items"][0]["ip"], TEST_CIDR.split("/", 1)[0])
        status, deleted = request(f"/tasks/{task['id']}", "DELETE")
        self.assertEqual(status, 204)

    def test_palo_alto_defaults_are_used_when_paths_are_omitted(self):
        for fingerprint, path in (("paloalto-globalprotect-portal", "/global-protect/login.esp"),
                                  ("paloalto-panos-admin", "/php/login.php")):
            with self.subTest(fingerprint=fingerprint):
                fields = {
                    "name": "profile-start-" + uuid.uuid4().hex[:8],
                    "masscan_chunk_size": "1", "httpx_chunk_size": "1",
                    "masscan_ports": "10443", "masscan_rate": "1",
                    "masscan_workers": "1", "httpx_workers": "1",
                    "fingerprint_id": fingerprint,
                }
                body, content_type = multipart(fields, "scope.txt", (TEST_CIDR + "\n").encode())
                status, task = request("/tasks", "POST", body, content_type)
                self.assertEqual(status, 202)
                self.assertEqual(task["config"]["httpx"]["paths"], [path])
                self.assertEqual(task["config"]["httpx"]["schemes"], ["https"])
                deadline = time.monotonic() + 90
                while task["status"] in {"provisioning", "queued", "running"} and time.monotonic() < deadline:
                    time.sleep(1)
                    _, task = request(f"/tasks/{task['id']}")
                self.assertEqual(task["status"], "completed", task)
                self.assertEqual(task["masscan_chunks"]["completed"], 1)
                self.assertEqual(task["pipeline"]["scope"]["address_count"], 1)
                status, _ = request(f"/tasks/{task['id']}", "DELETE")
                self.assertEqual(status, 204)

    def test_fortinet_can_submit_empty_title_filters(self):
        fields = {
            "name": "empty-title-" + uuid.uuid4().hex[:8],
            "masscan_chunk_size": "1", "httpx_chunk_size": "1",
            "masscan_ports": "10443", "masscan_rate": "1",
            "httpx_title_contains": "[]",
        }
        body, content_type = multipart(fields, "scope.txt", (TEST_CIDR + "\n").encode())
        status, task = request("/tasks", "POST", body, content_type)
        self.assertEqual(status, 202)
        self.assertEqual(task["config"]["httpx"]["title_contains"], [])
        deadline = time.monotonic() + 90
        while task["status"] in {"provisioning", "queued", "running"} and time.monotonic() < deadline:
            time.sleep(1)
            _, task = request(f"/tasks/{task['id']}")
        self.assertEqual(task["status"], "completed", task)
        status, _ = request(f"/tasks/{task['id']}", "DELETE")
        self.assertEqual(status, 204)

    def test_failed_masscan_chunk_marks_task_failed(self):
        fields = {
            "name": "failed-chunk-" + uuid.uuid4().hex[:8],
            "masscan_chunk_size": "1", "httpx_chunk_size": "1",
            "masscan_ports": "10443", "masscan_rate": "1",
            "masscan_extra_args": json.dumps(["--adapter", "missing-test-interface"]),
        }
        body, content_type = multipart(fields, "scope.txt", (TEST_CIDR + "\n").encode())
        status, task = request("/tasks", "POST", body, content_type)
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 90
        while task["status"] in {"provisioning", "queued", "running"} and time.monotonic() < deadline:
            time.sleep(1)
            _, task = request(f"/tasks/{task['id']}")
        self.assertEqual(task["status"], "failed", task)
        self.assertEqual(task["masscan_chunks"]["failed"], 1)
        self.assertIn("masscan чанк 1", task["error_message"])
        status, _ = request(f"/tasks/{task['id']}", "DELETE")
        self.assertEqual(status, 204)
