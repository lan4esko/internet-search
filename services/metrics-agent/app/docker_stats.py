"""Read only the Engine endpoints required for this Compose project's telemetry."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
import json
import re
import socket
from urllib.parse import urlencode


SERVICES = frozenset({"web", "api", "postgres", "redis", "task-runner", "metrics-agent", "metrics-proxy", "cadvisor", "prometheus", "grafana"})
METRIC_FIELDS = ("cpu_percent", "memory_usage_bytes", "memory_limit_bytes", "memory_percent",
                 "pids", "network_rx_bytes", "network_tx_bytes", "block_read_bytes", "block_write_bytes", "rss_bytes")


def now():
    return datetime.now(timezone.utc).isoformat()


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=3):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerStats:
    def __init__(self, socket_path, project):
        if not project:
            raise ValueError("Compose project name is required")
        self.socket_path = socket_path
        self.project = project
        self.version = None
        self.previous = {}

    def _get(self, path):
        connection = UnixHTTPConnection(self.socket_path)
        try:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"Docker statistics endpoint returned {response.status}")
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise ValueError("Docker statistics response is too large")
            return json.loads(body)
        finally:
            connection.close()

    def _api(self, path):
        if self.version is None:
            version = self._get("/version").get("ApiVersion", "")
            if not re.fullmatch(r"[0-9]+\.[0-9]+", version):
                raise ValueError("Invalid Docker API version")
            self.version = version
        return self._get(f"/v{self.version}{path}")

    def containers(self):
        filters = json.dumps({"label": [f"com.docker.compose.project={self.project}"]})
        rows = self._api("/containers/json?" + urlencode({"all": "true", "filters": filters}))
        # Check labels locally as well: client input never selects another project or service.
        return [row for row in rows
                if row.get("Labels", {}).get("com.docker.compose.project") == self.project
                and row.get("Labels", {}).get("com.docker.compose.service") in SERVICES
                and re.fullmatch(r"[a-f0-9]{64}", row.get("Id", ""))]

    def _sample(self, container):
        identity = container["Id"]
        item = {"service": container["Labels"]["com.docker.compose.service"],
                "name": (container.get("Names") or [identity[:12]])[0].lstrip("/")[:128],
                "state": str(container.get("State", "unknown"))[:32],
                "sampled_at": None, "health": None, "restart_count": None, "uptime_seconds": None, **dict.fromkeys(METRIC_FIELDS)}
        if item["state"] != "running":
            self.previous.pop(identity, None)
            return item
        try:
            inspect=self._api(f"/containers/{identity}/json")
            state=inspect.get('State') or {}
            item['health']=(state.get('Health') or {}).get('Status')
            item['restart_count']=nonnegative(inspect.get('RestartCount'))
            started=state.get('StartedAt')
            if started:
                try:item['uptime_seconds']=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(started.replace('Z','+00:00'))).total_seconds())
                except ValueError:pass
            stats = self._api(f"/containers/{identity}/stats?stream=false&one-shot=true")
            item.update(metric_values(stats, self.previous.get(identity)))
            item["sampled_at"] = now()
            self.previous[identity] = stats.get("cpu_stats", {})
        except (OSError, ValueError, RuntimeError, http.client.HTTPException, TypeError, KeyError):
            # A stopped/recreated container must not look idle or retain an old CPU sample.
            self.previous.pop(identity, None)
            item["state"] = "metrics_unavailable"
        return item

    def snapshot(self):
        containers = self.containers()
        ids = {row["Id"] for row in containers}
        self.previous = {key: value for key, value in self.previous.items() if key in ids}
        with ThreadPoolExecutor(max_workers=8) as pool:
            items = list(pool.map(self._sample, containers))
        return {"sampled_at": now(), "containers": sorted(items, key=lambda item: (item["service"], item["name"]))}


def nonnegative(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def metric_values(stats, previous=None):
    values = dict.fromkeys(METRIC_FIELDS)
    cpu = stats.get("cpu_stats") or {}
    previous = previous or stats.get("precpu_stats") or {}
    current_usage = nonnegative((cpu.get("cpu_usage") or {}).get("total_usage"))
    old_usage = nonnegative((previous.get("cpu_usage") or {}).get("total_usage"))
    current_system = nonnegative(cpu.get("system_cpu_usage"))
    old_system = nonnegative(previous.get("system_cpu_usage"))
    cpus = nonnegative(cpu.get("online_cpus")) or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [])
    if None not in (current_usage, old_usage, current_system, old_system) and cpus:
        cpu_delta, system_delta = current_usage - old_usage, current_system - old_system
        if cpu_delta >= 0 and system_delta > 0 and old_system > 0:
            values["cpu_percent"] = round(cpu_delta / system_delta * cpus * 100, 2)
    memory = stats.get("memory_stats") or {}
    usage, limit = nonnegative(memory.get("usage")), nonnegative(memory.get("limit"))
    details = memory.get("stats") or {}
    values["rss_bytes"]=nonnegative(details.get("rss",details.get("anon")))
    cache = nonnegative(details.get("total_inactive_file"))
    if cache is None:
        cache = nonnegative(details.get("inactive_file"))
    if cache is None:
        cache = nonnegative(details.get("cache")) or 0
    if usage is not None:
        # Match docker stats on Linux; invalid cache counters must not erase actual usage.
        usage = usage - cache if cache < usage else usage
        values["memory_usage_bytes"] = usage
    values["memory_limit_bytes"] = limit
    if usage is not None and limit:
        values["memory_percent"] = round(usage / limit * 100, 2)
    values["pids"] = nonnegative((stats.get("pids_stats") or {}).get("current"))
    networks = stats.get("networks")
    if isinstance(networks, dict) and networks:
        for direction in ("rx", "tx"):
            values[f"network_{direction}_bytes"] = sum(nonnegative(net.get(f"{direction}_bytes")) or 0 for net in networks.values())
    block = (stats.get("blkio_stats") or {}).get("io_service_bytes_recursive")
    if isinstance(block, list):
        for operation in ("read", "write"):
            values[f"block_{operation}_bytes"] = sum(nonnegative(row.get("value")) or 0 for row in block
                                                      if str(row.get("op", "")).lower() == operation)
    return values
