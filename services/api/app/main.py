import os

import asyncpg
from fastapi import FastAPI, Request

from app.api.routes.metrics import router as metrics_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.tasks import templates_router
from app.redis import redis_lifespan

app = FastAPI(title="Internet Search API", lifespan=redis_lifespan)
app.include_router(metrics_router)
app.include_router(tasks_router)
app.include_router(templates_router)


@app.get("/health/postgres")
async def health_postgres():
    connection = await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "app"),
        password=os.getenv("POSTGRES_PASSWORD", "change_me"),
        database=os.getenv("POSTGRES_DB", "internet_search"),
    )

    result = await connection.fetchval("SELECT 1")

    await connection.close()

    return {"postgres": "ok", "result": result}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/redis")
async def health_redis(request: Request):
    result = await request.app.state.redis.ping()

    return {"redis": "ok", "ping": result}
