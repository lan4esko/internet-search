"""Publish bounded, expiring telemetry to Redis; no Engine socket access here."""
import json
import os
import time
from urllib.request import urlopen

from redis import Redis


KEY = "internet-search:container-metrics"


def main():
    interval = max(2, min(60, int(os.getenv("METRICS_INTERVAL_SECONDS", "5"))))
    stale_after = min(3600, max(interval * 3, int(os.getenv("METRICS_STALE_AFTER_SECONDS", "20"))))
    redis = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"),
                          socket_connect_timeout=3, socket_timeout=3)
    url = os.getenv("METRICS_PROXY_URL", "http://metrics-proxy:9108/metrics")
    while True:
        started = time.monotonic()
        try:
            with urlopen(url, timeout=12) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("Metrics payload too large")
            payload = json.loads(body)
            payload.update(interval_seconds=interval, stale_after_seconds=stale_after)
            redis.set(KEY, json.dumps(payload, allow_nan=False), ex=stale_after * 3)
        except Exception as error:
            # Retain last known sample until expiry; API labels stale samples explicitly.
            print(f"Container metrics collection failed: {type(error).__name__}", flush=True)
        time.sleep(max(0.1, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
