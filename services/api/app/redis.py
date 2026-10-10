import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from redis.asyncio import Redis


def create_redis_client() -> Redis:
    return Redis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"),
        decode_responses=True,
    )


@asynccontextmanager
async def redis_lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.redis = create_redis_client()
    yield
    await app.state.redis.aclose()


def get_redis(request: Request) -> Redis:
    return request.app.state.redis
