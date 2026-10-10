"""Read-only cached metrics; this service never accesses the Docker daemon."""
from datetime import datetime, timezone
import json
from typing import Literal

from fastapi import APIRouter, Depends
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
