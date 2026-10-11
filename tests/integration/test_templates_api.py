"""API coverage for persistent scanner templates and the no-traffic dry-run."""
import json
import os
import uuid
from unittest import TestCase
from urllib.request import Request, urlopen


API = os.getenv("TASK_API_URL", "http://api:8000")


def request(path, method="GET", payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    with urlopen(Request(API + path, data=data, method=method, headers=headers), timeout=30) as response:
        return response.status, json.loads(response.read() or b"null")


class TemplateApiTests(TestCase):
    def test_template_lifecycle_and_dry_run(self):
        name = "template-test-" + uuid.uuid4().hex[:8]
        config = {
            "masscan_chunk_size": 4,
            "httpx_chunk_size": 2,
            "masscan_ports": "443,10443",
            "masscan_rate": 20,
            "masscan_output_format": "json",
            "masscan_extra_args": ["--router-mac", "00:11:22:33:44:55"],
            "masscan_workers": 1,
            "httpx_workers": 2,
            "httpx_rate_limit": 10,
            "httpx_extra_args": ["-probe"],
            "httpx_paths": ["/remote/login"],
            "httpx_schemes": ["https"],
        }
        status, template = request("/templates", "POST", {"name": name, "config": config})
        self.assertEqual(status, 201)
        self.assertEqual(template["config"]["masscan_ports"], "443,10443")
        try:
            status, result = request("/templates/validate", "POST", {
                "config": config,
                "filename": "scope.txt",
                "cidr_text": "192.0.2.0/30\n192.0.2.0/30\ninvalid\n",
                "sample_subnets": 1,
            })
            self.assertEqual(status, 200)
            self.assertTrue(result["valid"])
            self.assertFalse(result["network_traffic"])
            self.assertEqual(result["scope"]["subnet_count"], 1)
            self.assertEqual(result["scope"]["duplicate_count"], 1)
            self.assertIn("--router-mac", result["commands"]["masscan"])
        finally:
            status, _ = request(f"/templates/{template['id']}", "DELETE")
            self.assertEqual(status, 204)
