"""Accuracy and isolation checks for the container telemetry boundary."""
from datetime import datetime, timedelta, timezone
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock

from app.api.routes.metrics import container_metrics, decode_metrics
from redis.exceptions import ConnectionError


ROOT = Path(__file__).resolve().parents[2]
package = importlib.util.spec_from_file_location(
    "container_metrics_fixture", ROOT / "services/metrics-agent/app/__init__.py",
    submodule_search_locations=[str(ROOT / "services/metrics-agent/app")],
)
module = importlib.util.module_from_spec(package)
sys.modules[package.name] = module
package.loader.exec_module(module)
from container_metrics_fixture.docker_stats import DockerStats, metric_values
from container_metrics_fixture.proxy import MetricsHandler


class MetricsCalculationTests(TestCase):
    def test_cpu_reports_one_core_as_100_percent_and_keeps_multiple_cores(self):
        previous = {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000}
        current = {"cpu_stats": {"cpu_usage": {"total_usage": 300},
                                 "system_cpu_usage": 1800, "online_cpus": 8}}
        self.assertEqual(metric_values(current, previous)["cpu_percent"], 200)

    def test_first_sample_and_counter_restart_do_not_invent_cpu_zero(self):
        current = {"cpu_stats": {"cpu_usage": {"total_usage": 100},
                                 "system_cpu_usage": 1000, "online_cpus": 4}}
        self.assertIsNone(metric_values(current)["cpu_percent"])
        previous = {"cpu_usage": {"total_usage": 900}, "system_cpu_usage": 900}
        self.assertIsNone(metric_values(current, previous)["cpu_percent"])
        self.assertIsNone(metric_values(current, current["cpu_stats"])["cpu_percent"])

    def test_cgroup_v1_v2_memory_cache_matches_docker_cli(self):
        for counter in ("total_inactive_file", "inactive_file", "cache"):
            with self.subTest(counter=counter):
                result = metric_values({"memory_stats": {"usage": 800, "limit": 2000,
                                                         "stats": {counter: 300}}})
                self.assertEqual(result["memory_usage_bytes"], 500)
                self.assertEqual(result["memory_percent"], 25)
        self.assertEqual(metric_values({"memory_stats": {"usage": 800, "limit": 2000,
                                                         "stats": {"cache": 900}}})["memory_usage_bytes"], 800)

    def test_io_sums_interfaces_and_devices_without_counting_total_twice(self):
        result = metric_values({
            "networks": {"eth0": {"rx_bytes": 12, "tx_bytes": 20}, "eth1": {"rx_bytes": 30, "tx_bytes": 4}},
            "blkio_stats": {"io_service_bytes_recursive": [
                {"op": "Read", "value": 3}, {"op": "Read", "value": 5},
                {"op": "Write", "value": 7}, {"op": "Total", "value": 15},
            ]}, "pids_stats": {"current": 8},
        })
        self.assertEqual(result["network_rx_bytes"], 42)
        self.assertEqual(result["network_tx_bytes"], 24)
        self.assertEqual(result["block_read_bytes"], 8)
        self.assertEqual(result["block_write_bytes"], 7)
        self.assertEqual(result["pids"], 8)

    def test_missing_metrics_remain_unknown(self):
        self.assertTrue(all(value is None for value in metric_values({}).values()))


class DockerBoundaryTests(TestCase):
    def setUp(self):
        self.stats = DockerStats("/unused/socket", "project-a")
        self.container = {"Id": "a" * 64, "Names": ["/project-a-postgres-1"], "State": "running",
                          "Labels": {"com.docker.compose.project": "project-a", "com.docker.compose.service": "postgres"}}

    def test_only_known_services_of_this_project_can_be_read(self):
        unrelated_project = {**self.container, "Labels": {**self.container["Labels"], "com.docker.compose.project": "project-b"}}
        unrelated_service = {**self.container, "Labels": {**self.container["Labels"], "com.docker.compose.service": "secret-manager"}}
        invalid_id = {**self.container, "Id": "../other/inspect"}
        self.stats._api = Mock(return_value=[self.container, unrelated_project, unrelated_service, invalid_id])
        self.assertEqual(self.stats.containers(), [self.container])
        requested = self.stats._api.call_args.args[0]
        self.assertIn("/containers/json?", requested)
        self.assertIn("project-a", requested)

    def test_projection_does_not_expose_labels_environment_or_image(self):
        self.stats._api = Mock(return_value={"memory_stats": {"usage": 1, "limit": 100}, "Env": ["PASSWORD=secret"]})
        item = self.stats._sample({**self.container, "Image": "private.example/secret", "Env": ["PASSWORD=secret"]})
        self.assertEqual(item["memory_usage_bytes"], 1)
        self.assertNotIn("secret", json.dumps(item))
        self.assertNotIn("Labels", item)
        self.assertNotIn("Id", item)

    def test_stopped_or_unreadable_container_cannot_look_idle(self):
        self.stats._api = Mock(side_effect=RuntimeError("private daemon error"))
        self.stats.previous[self.container["Id"]] = {"old": True}
        failed = self.stats._sample(self.container)
        self.assertEqual(failed["state"], "metrics_unavailable")
        self.assertIsNone(failed["cpu_percent"])
        self.assertIsNone(failed["sampled_at"])
        self.assertNotIn(self.container["Id"], self.stats.previous)
        stopped = self.stats._sample({**self.container, "State": "exited"})
        self.assertEqual(stopped["state"], "exited")
        self.assertIsNone(stopped["memory_usage_bytes"])

    def test_proxy_rejects_arbitrary_docker_routes_queries_and_mutations(self):
        class Connection:
            def __init__(self, raw):
                self.reader = BytesIO(raw)
                self.response = bytearray()

            def makefile(self, *args):
                return self.reader

            def sendall(self, data):
                self.response.extend(data)

        server = Mock()
        server.stats.snapshot.return_value = {"sampled_at": "2026-01-01T00:00:00Z", "containers": []}
        for method, path, expected in (
            ("GET", "/metrics", 200), ("POST", "/metrics", 501),
            ("GET", "/containers/json", 404), ("GET", "/metrics?project=other", 404),
            ("GET", "/v1.50/containers/abc/exec", 404), ("DELETE", "/containers/abc", 501),
        ):
            with self.subTest(method=method, path=path):
                connection = Connection(f"{method} {path} HTTP/1.0\r\nHost: metrics-proxy\r\n\r\n".encode())
                MetricsHandler(connection, ("127.0.0.1", 12345), server)
                self.assertEqual(int(connection.response.split(b" ", 2)[1]), expected)
        server.stats.snapshot.assert_called_once()


class MetricsCacheTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 9, tzinfo=timezone.utc)
        self.payload = {"sampled_at": self.now.isoformat(), "interval_seconds": 5, "stale_after_seconds": 20,
                        "containers": [{"service": "web", "name": "project-web-1", "state": "running", "cpu_percent": None}]}

    def test_expired_and_missing_cache_are_distinguished(self):
        raw = json.dumps(self.payload)
        self.assertEqual(decode_metrics(raw, now=self.now).status, "ok")
        self.assertEqual(decode_metrics(raw, now=self.now + timedelta(seconds=21)).status, "stale")
        self.assertEqual(decode_metrics(None, now=self.now).status, "unavailable")

    def test_unknown_fields_are_not_returned_to_api_clients(self):
        self.payload["containers"][0]["environment"] = ["PASSWORD=secret"]
        self.payload["docker_config"] = "secret"
        parsed = decode_metrics(json.dumps(self.payload), now=self.now)
        self.assertNotIn("secret", parsed.model_dump_json())
        self.assertIsNone(parsed.containers[0].cpu_percent)

    def test_bad_or_unbounded_cache_fails_closed(self):
        for raw in ("invalid", "[]", "null", "x" * (1024 * 1024 + 1),
                    json.dumps({**self.payload, "sampled_at": "2026-10-09T00:00:00"}),
                    json.dumps({**self.payload, "containers": [{"service": "web", "name": "web", "state": "running", "cpu_percent": -1}]})):
            with self.subTest(raw=raw[:80]):
                self.assertEqual(decode_metrics(raw, now=self.now).status, "unavailable")

    async def test_redis_failure_is_a_small_honest_response_without_secrets(self):
        redis = AsyncMock()
        redis.get.side_effect = ConnectionError("redis://user:secret@host")
        result = await container_metrics(redis)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.containers, [])
        self.assertNotIn("secret", result.model_dump_json())
