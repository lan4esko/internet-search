"""Read-only cached metrics; this service never accesses the Docker daemon."""
from datetime import datetime, timezone
import json
from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.redis import get_redis


router = APIRouter(prefix="/operations", tags=["operations"])
KEY = "internet-search:container-metrics"


class ContainerMetric(BaseModel):
    service: str = Field(max_length=64)
    name: str = Field(max_length=128)
    state: str = Field(max_length=32)
    sampled_at: datetime | None = None
    cpu_percent: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    memory_usage_bytes: int | None = Field(default=None, ge=0)
    memory_limit_bytes: int | None = Field(default=None, ge=0)
    memory_percent: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    pids: int | None = Field(default=None, ge=0)
    network_rx_bytes: int | None = Field(default=None, ge=0)
    network_tx_bytes: int | None = Field(default=None, ge=0)
    block_read_bytes: int | None = Field(default=None, ge=0)
    block_write_bytes: int | None = Field(default=None, ge=0)


class ContainerMetricsRead(BaseModel):
    status: Literal["ok", "stale", "unavailable"] = "unavailable"
    sampled_at: datetime | None = None
    interval_seconds: int = Field(default=5, ge=2, le=60)
    stale_after_seconds: int = Field(default=20, ge=2, le=3600)
    containers: list[ContainerMetric] = Field(default_factory=list, max_length=256)


def decode_metrics(raw, *, now=None):
    if not raw or len(raw) > 1024 * 1024:
        return ContainerMetricsRead()
    try:
        payload = ContainerMetricsRead.model_validate(json.loads(raw))
        if payload.sampled_at is None or payload.sampled_at.tzinfo is None:
            return ContainerMetricsRead()
        age = ((now or datetime.now(timezone.utc)) - payload.sampled_at).total_seconds()
        payload.status = "stale" if age > payload.stale_after_seconds or age < -5 else "ok"
        if not payload.containers:
            payload.status = "unavailable"
        return payload
    except (ValueError, TypeError, ValidationError):
        return ContainerMetricsRead()


@router.get("/container-metrics", response_model=ContainerMetricsRead)
async def container_metrics(redis: Redis = Depends(get_redis)):
    try:
        raw = await redis.get(KEY)
    except RedisError:
        return ContainerMetricsRead()
    return decode_metrics(raw)


@router.get("/container-metrics/history")
async def container_metrics_history(limit: int = 120, redis: Redis = Depends(get_redis)):
    """Return recent samples for a dashboard without exposing Redis directly."""
    limit = max(1, min(720, limit))
    try:
        rows = await redis.lrange("internet-search:container-metrics:history", 0, limit - 1)
    except RedisError:
        return {"items": []}
    items = []
    for raw in reversed(rows):
        try:
            item = ContainerMetricsRead.model_validate(json.loads(raw))
        except (ValueError, TypeError, ValidationError):
            continue
        items.append(item.model_dump(mode="json"))
    return {"items": items}


@router.get("/prometheus")
async def prometheus_metrics(
    redis: Redis = Depends(get_redis),
):
    """Prometheus-compatible snapshot for the optional Grafana stack."""
    # Keep the regular metrics module independent from the asyncpg driver at
    # import time; lightweight metrics unit tests do not need the control DB.
    from sqlalchemy import func, select
    from app.db.session import AsyncSessionLocal
    from app.models.task import TaskRegistry

    current = await container_metrics(redis)
    lines = [
        "# HELP internet_search_container_cpu_percent Container CPU usage.",
        "# TYPE internet_search_container_cpu_percent gauge",
        "# HELP internet_search_container_memory_usage_bytes Container memory usage.",
        "# TYPE internet_search_container_memory_usage_bytes gauge",
        "# HELP internet_search_container_memory_percent Container memory percentage.",
        "# TYPE internet_search_container_memory_percent gauge",
        "# HELP internet_search_container_network_rx_bytes Cumulative received bytes.",
        "# TYPE internet_search_container_network_rx_bytes counter",
        "# HELP internet_search_container_network_tx_bytes Cumulative transmitted bytes.",
        "# TYPE internet_search_container_network_tx_bytes counter",
        "# HELP internet_search_container_pids Number of container processes.",
        "# TYPE internet_search_container_pids gauge",
    ]
    for item in current.containers:
        labels = f'service="{item.service}",container="{item.name}"'
        for metric, value in (
            ("cpu_percent", item.cpu_percent),
            ("memory_usage_bytes", item.memory_usage_bytes),
            ("memory_percent", item.memory_percent),
            ("network_rx_bytes", item.network_rx_bytes),
            ("network_tx_bytes", item.network_tx_bytes),
            ("pids", item.pids),
        ):
            if value is not None:
                lines.append(f"internet_search_container_{metric}{{{labels}}} {value}")
    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(TaskRegistry.status, func.count()).group_by(TaskRegistry.status))
        task_statuses = list(rows)
    lines += [
        "# HELP internet_search_tasks_total Number of TASKs by status.",
        "# TYPE internet_search_tasks_total gauge",
    ]
    for status, count in task_statuses:
        safe_status = str(status).replace('"', "")
        lines.append(f'internet_search_tasks_total{{status="{safe_status}"}} {int(count)}')
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
