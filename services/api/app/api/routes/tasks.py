"""Real TASK API backed by one physical PostgreSQL database per task."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.task import TaskRegistry
from app.redis import get_redis
from app.schemas.task import TaskPage, TaskRead, TaskRetry
from app.task_db import connect, create_database, drop_database, initialize_database


router = APIRouter(prefix="/tasks", tags=["tasks"])
TASK_DATA_DIR = Path(os.getenv("SCOPE_DATA_DIR", "/data/scope")) / "tasks"
UPLOAD_CHUNK = 1024 * 1024
FORTINET_PATHS = ["/remote/login", "/remote/index", "/ssl-vpn/login.html"]
FORTINET_MATCH_STRINGS = ["ftnt-fortinet-grid", 'name="magic"', "SVPNCOOKIE"]


def parse_json_list(raw: str, field: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=422, detail=f"{field}: нужен JSON-массив") from error
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise HTTPException(status_code=422, detail=f"{field}: нужен непустой массив строк")
    return value


async def save_upload(file: UploadFile, database_name: str) -> tuple[Path, str, str, list[tuple[str, int, str]]]:
    TASK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = TASK_DATA_DIR / f"{database_name}.txt"
    digest = hashlib.sha256()
    cidrs: dict[str, tuple[str, int, str]] = {}
    line_no = 0
    with path.open("xb") as destination:
        while chunk := await file.read(UPLOAD_CHUNK):
            destination.write(chunk)
            digest.update(chunk)
    await file.close()
    raw = path.read_text(encoding="utf-8")
    for line_no, raw_line in enumerate(raw.splitlines(), 1):
        original = raw_line.strip()
        if not original or original.startswith("#"):
            continue
        try:
            network = ipaddress.ip_network(original, strict=True)
        except ValueError as error:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=f"Строка {line_no}: некорректный IPv4 CIDR") from error
        if not isinstance(network, ipaddress.IPv4Network):
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=f"Строка {line_no}: разрешён только IPv4 CIDR")
        cidrs.setdefault(str(network), (str(network), line_no, original))
    if not cidrs:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Файл не содержит IPv4 CIDR")
    return path, digest.hexdigest(), path.name, list(cidrs.values())


async def update_registry(session: AsyncSession, task: TaskRegistry, **values) -> None:
    for key, value in values.items():
        setattr(task, key, value)
    await session.commit()
    await session.refresh(task)


@router.post("", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def create_task(
    name: str = Form(...),
    masscan_chunk_size: int = Form(..., ge=1, le=1_000_000),
    httpx_chunk_size: int = Form(..., ge=1, le=100_000),
    masscan_ports: str = Form(...),
    masscan_rate: int = Form(..., ge=1, le=100_000),
    masscan_workers: int = Form(1, ge=1, le=32),
    httpx_workers: int = Form(1, ge=1, le=64),
    httpx_rate_limit: int | None = Form(None, ge=1, le=100_000),
    httpx_paths: str = Form(json.dumps(FORTINET_PATHS)),
    httpx_schemes: str = Form('["http", "https"]'),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    redis_client: Redis = Depends(get_redis),
):
    if not name.strip():
        raise HTTPException(status_code=422, detail="Название TASK не может быть пустым")
    try:
        ports = [int(item.strip()) for item in masscan_ports.split(",") if item.strip()]
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Masscan ports должны быть числами") from error
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise HTTPException(status_code=422, detail="Masscan ports должны быть от 1 до 65535")
    paths = parse_json_list(httpx_paths, "httpx_paths")
    schemes = parse_json_list(httpx_schemes, "httpx_schemes")
    if any(scheme not in {"http", "https"} for scheme in schemes):
        raise HTTPException(status_code=422, detail="httpx_schemes допускает только http и https")

    database_name = f"task_{uuid4().hex}"
    path, checksum, filename, cidrs = await save_upload(file, database_name)
    httpx_config = {
        "paths": paths,
        "schemes": schemes,
        "threads": int(os.getenv("HTTPX_THREADS", "500")),
        "timeout": int(os.getenv("HTTPX_TIMEOUT", "5")),
        "follow_redirects": True,
        "match_strings": FORTINET_MATCH_STRINGS,
    }
    if httpx_rate_limit is not None:
        httpx_config["rate_limit"] = httpx_rate_limit
    elif os.getenv("HTTPX_RATE_LIMIT"):
        httpx_config["rate_limit"] = int(os.environ["HTTPX_RATE_LIMIT"])
    config = {
        "masscan_chunk_size": masscan_chunk_size,
        "httpx_chunk_size": httpx_chunk_size,
        "masscan_ports": ",".join(str(port) for port in sorted(set(ports))),
        "masscan_rate": masscan_rate,
        "masscan_workers": masscan_workers,
        "httpx_workers": httpx_workers,
        "httpx": httpx_config,
        "fingerprint": "fortinet-debug-scan-2.0",
    }
    task = TaskRegistry(
        name=name.strip(), database_name=database_name, source_filename=filename,
        source_path=str(path), source_sha256=checksum, config=config,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    try:
        await create_database(database_name)
        await initialize_database(database_name, {
            "task_id": task.id, "name": task.name, "source_filename": filename,
            "source_sha256": checksum, "masscan_chunk_size": masscan_chunk_size,
            "httpx_chunk_size": httpx_chunk_size, "masscan_rate": masscan_rate,
            "masscan_ports": config["masscan_ports"], "masscan_workers": masscan_workers,
            "httpx_workers": httpx_workers, "httpx_config": json.dumps(httpx_config),
        })
        task_connection = await connect(database_name)
        try:
            await task_connection.executemany(
                "INSERT INTO task_cidrs(network,source_line,original_value) VALUES($1,$2,$3)", cidrs
            )
        finally:
            await task_connection.close()
        await update_registry(session, task, status="queued")
        await redis_client.xadd("tasks.created", {"task_id": str(task.id)})
    except Exception as error:
        await update_registry(session, task, status="failed", error_message=str(error)[:1000])
    return task


@router.get("", response_model=TaskPage)
async def list_tasks(
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    total = await session.scalar(select(text("count(*)")).select_from(text("task_registry")))
    rows = (await session.execute(select(TaskRegistry).order_by(TaskRegistry.id.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [TaskRead.model_validate(row).model_dump() for row in rows], "total": int(total or 0), "limit": limit, "offset": offset}


async def task_or_404(task_id: int, session: AsyncSession) -> TaskRegistry:
    task = await session.get(TaskRegistry, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="TASK не найден")
    return task


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: int, session: AsyncSession = Depends(get_session)):
    task = await task_or_404(task_id, session)
    connection = await connect(task.database_name)
    try:
        counts = await connection.fetchrow("""
          SELECT (SELECT count(*) FROM task_targets) AS targets,
                 (SELECT count(*) FROM task_open_ports) AS ports,
                 (SELECT count(*) FROM httpx_results) AS results,
                 (SELECT count(*) FROM task_targets WHERE masscan_status='open') AS open_targets,
                 (SELECT count(*) FROM task_chunks WHERE stage='masscan') AS masscan_total,
                 (SELECT count(*) FROM task_chunks WHERE stage='masscan' AND status='completed') AS masscan_completed,
                 (SELECT count(*) FROM task_chunks WHERE stage='masscan' AND status='running') AS masscan_running,
                 (SELECT count(*) FROM task_chunks WHERE stage='masscan' AND status='failed') AS masscan_failed,
                 (SELECT count(*) FROM task_chunks WHERE stage='httpx') AS httpx_total,
                 (SELECT count(*) FROM task_chunks WHERE stage='httpx' AND status='completed') AS httpx_completed,
                 (SELECT count(*) FROM task_chunks WHERE stage='httpx' AND status='running') AS httpx_running,
                 (SELECT count(*) FROM task_chunks WHERE stage='httpx' AND status='failed') AS httpx_failed,
                 (SELECT httpx_chunk_size FROM task_meta WHERE task_id=$1) AS httpx_chunk_size
        """, task_id)
        live_masscan = await connection.fetchrow("""
          SELECT chunk_no,status_tail,rate_pps,progress_percent,found_count
          FROM task_chunks WHERE stage='masscan' AND status='running'
          ORDER BY chunk_no LIMIT 1
        """)
    finally:
        await connection.close()
    total = int(counts["targets"] or 0)
    masscan_total = int(counts["masscan_total"] or 0)
    observed_httpx_total = int(counts["httpx_total"] or 0)
    httpx_chunk_size = max(1, int(counts["httpx_chunk_size"] or 1))
    httpx_total = ((int(counts["ports"] or 0) + httpx_chunk_size - 1) // httpx_chunk_size
                   if counts["ports"] else observed_httpx_total)
    masscan_progress = (int(counts["masscan_completed"] or 0) / masscan_total * 50) if masscan_total else 0
    if live_masscan and masscan_total:
        live_percent = min(100.0, max(0.0, float(live_masscan["progress_percent"] or 0)))
        masscan_progress += live_percent / 100 * (50 / masscan_total)
    httpx_progress = (int(counts["httpx_completed"] or 0) / httpx_total * 50) if httpx_total else (50 if task.status == "completed" else 0)
    task.target_count = total
    task.open_port_count = int(counts["ports"] or 0)
    task.result_count = int(counts["results"] or 0)
    await session.commit()
    await session.refresh(task)
    result = TaskRead.model_validate(task).model_dump()
    result["progress_percent"] = round(masscan_progress + httpx_progress, 2)
    result["masscan_chunks"] = {"total": masscan_total, "completed": int(counts["masscan_completed"] or 0), "running": int(counts["masscan_running"] or 0), "failed": int(counts["masscan_failed"] or 0)}
    result["httpx_chunks"] = {"total": httpx_total, "completed": int(counts["httpx_completed"] or 0), "running": int(counts["httpx_running"] or 0), "failed": int(counts["httpx_failed"] or 0)}
    result["masscan_live"] = dict(live_masscan) if live_masscan else {}
    return result


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: int, session: AsyncSession = Depends(get_session)):
    task = await task_or_404(task_id, session)
    if task.status in {"provisioning", "running"}:
        raise HTTPException(status_code=409, detail="Активный TASK нельзя удалить")
    database_name = task.database_name
    await drop_database(database_name)
    await session.delete(task)
    await session.commit()


@router.get("/{task_id}/targets", response_model=TaskPage)
async def get_targets(task_id: int, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), session: AsyncSession = Depends(get_session)):
    task = await task_or_404(task_id, session)
    connection = await connect(task.database_name)
    try:
        total = await connection.fetchval("SELECT count(*) FROM task_targets")
        rows = await connection.fetch("SELECT host(ip) AS ip,source_chunk_no,masscan_status,ready_for_masscan FROM task_targets ORDER BY ip LIMIT $1 OFFSET $2", limit, offset)
    finally:
        await connection.close()
    return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/{task_id}/open-ports", response_model=TaskPage)
async def get_open_ports(task_id: int, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), session: AsyncSession = Depends(get_session)):
    task = await task_or_404(task_id, session)
    connection = await connect(task.database_name)
    try:
        total = await connection.fetchval("SELECT count(*) FROM task_open_ports")
        rows = await connection.fetch("SELECT host(ip) AS ip,port,protocol,masscan_open,httpx_ready,httpx_status,httpx_chunk_no FROM task_open_ports ORDER BY ip,port LIMIT $1 OFFSET $2", limit, offset)
    finally:
        await connection.close()
    return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/{task_id}/results", response_model=TaskPage)
async def get_results(task_id: int, matched_only: bool = False, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), session: AsyncSession = Depends(get_session)):
    task = await task_or_404(task_id, session)
    connection = await connect(task.database_name)
    try:
        where = "WHERE matched=true" if matched_only else ""
        total = await connection.fetchval(f"SELECT count(*) FROM httpx_results {where}")
        rows = await connection.fetch(f"SELECT id,host(ip) AS ip,port,protocol,url,status_code,title,webserver,headers,body_preview,matched,match_details,httpx_chunk_no,created_at FROM httpx_results {where} ORDER BY id LIMIT $1 OFFSET $2", limit, offset)
    finally:
        await connection.close()
    return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/{task_id}/retry", response_model=dict)
async def retry_chunk(task_id: int, payload: TaskRetry, session: AsyncSession = Depends(get_session), redis_client: Redis = Depends(get_redis)):
    task = await task_or_404(task_id, session)
    if task.status in {"provisioning", "running"}:
        raise HTTPException(status_code=409, detail="Активный TASK нельзя перезапустить")
    connection = await connect(task.database_name)
    try:
        if payload.stage == "masscan":
            result = await connection.execute("UPDATE task_chunks SET status='pending',error_message=NULL WHERE stage='masscan' AND chunk_no=$1 AND status IN ('failed','cancelled')", payload.chunk_no)
            await connection.execute("UPDATE task_targets SET masscan_status='pending',ready_for_masscan=false,masscan_finished_at=NULL,error_message=NULL WHERE source_chunk_no=$1 AND masscan_status='failed'", payload.chunk_no)
            await connection.execute("UPDATE task_meta SET status='masscan',error_message=NULL,finished_at=NULL WHERE task_id=$1", task.id)
        else:
            result = await connection.execute("UPDATE task_open_ports SET httpx_status='pending',httpx_ready=true,httpx_chunk_no=NULL WHERE httpx_chunk_no=$1 AND httpx_status IN ('failed','cancelled')", payload.chunk_no)
            await connection.execute("UPDATE task_chunks SET status='pending',error_message=NULL WHERE stage='httpx' AND chunk_no=$1 AND status IN ('failed','cancelled')", payload.chunk_no)
            await connection.execute("UPDATE task_meta SET status='httpx',error_message=NULL,finished_at=NULL WHERE task_id=$1", task.id)
    finally:
        await connection.close()
    updated = int(result.split()[-1]) if result.split()[-1].isdigit() else 0
    if updated:
        task.status = "queued"
        task.error_message = None
        task.finished_at = None
        await session.commit()
        await redis_client.xadd("tasks.created", {"task_id": str(task.id)})
    return {"task_id": task.id, "stage": payload.stage, "chunk_no": payload.chunk_no, "updated": updated > 0}


@router.get("/{task_id}/export", response_model=dict)
async def export_results(task_id: int, matched_only: bool = True, session: AsyncSession = Depends(get_session)):
    task = await task_or_404(task_id, session)
    connection = await connect(task.database_name)
    try:
        where = "WHERE matched=true" if matched_only else ""
        rows = await connection.fetch(f"SELECT DISTINCT protocol || '://' || host(ip) || ':' || port::text AS target FROM httpx_results {where} ORDER BY target")
    finally:
        await connection.close()
    return {"task_id": task.id, "count": len(rows), "targets": [row["target"] for row in rows]}
