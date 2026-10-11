"""Run the real TASK pipeline: expand IPs, Masscan, then HTTPX."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from scanlib.masscan_formats import parse_records


LOGGER = logging.getLogger(__name__)
CONTROL_URL = os.getenv("DATABASE_URL", "postgresql://app:change_me@postgres:5432/internet_search").replace("+asyncpg", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
MASSCAN_WORKERS = max(1, min(32, int(os.getenv("MASSCAN_WORKERS", "1"))))
HTTPX_WORKERS = max(1, min(64, int(os.getenv("HTTPX_WORKERS", "1"))))
ARTIFACT_ROOT = Path(os.getenv("SCOPE_DATA_DIR", "/data/scope")) / "tasks"
GROUP = "task-runners"
STREAM = "tasks.created"
MASSCAN_OUTPUT_EXTENSIONS = {"json": "json", "list": "txt", "xml": "xml"}


def task_url(database_name: str) -> str:
    parts = CONTROL_URL.rsplit("/", 1)
    return parts[0] + "/" + database_name


async def control_connection() -> asyncpg.Connection:
    return await asyncpg.connect(CONTROL_URL)


async def set_registry(task_id: int, status: str, error: str | None = None) -> None:
    connection = await control_connection()
    try:
        await connection.execute(
            "UPDATE task_registry SET status=$2::text,error_message=$3,started_at=CASE WHEN $2::text='running' THEN COALESCE(started_at,now()) ELSE started_at END,finished_at=CASE WHEN $2::text IN ('completed','failed','cancelled') THEN now() ELSE finished_at END WHERE id=$1",
            task_id, status, error,
        )
    finally:
        await connection.close()


async def load_registry(task_id: int) -> asyncpg.Record:
    connection = await control_connection()
    try:
        row = await connection.fetchrow("SELECT * FROM task_registry WHERE id=$1", task_id)
    finally:
        await connection.close()
    if row is None:
        raise ValueError(f"TASK {task_id} не найден")
    return row


async def queued_task_ids() -> list[int]:
    connection = await control_connection()
    try:
        return [int(row["id"]) for row in await connection.fetch("SELECT id FROM task_registry WHERE status='queued' ORDER BY id")]
    finally:
        await connection.close()


async def update_counts(task_id: int, task_connection: asyncpg.Connection) -> None:
    counts = await task_connection.fetchrow("""
      SELECT (SELECT count(*) FROM task_targets) AS targets,
             (SELECT count(*) FROM task_open_ports) AS ports,
             (SELECT count(*) FROM httpx_results) AS results
    """)
    control = await control_connection()
    try:
        await control.execute("UPDATE task_registry SET target_count=$2,open_port_count=$3,result_count=$4 WHERE id=$1", task_id, counts["targets"], counts["ports"], counts["results"])
    finally:
        await control.close()


async def emit_event(connection: asyncpg.Connection, stage: str, message: str,
                     level: str = "info", payload: dict | None = None) -> None:
    """Store a bounded operator-facing event in the physical TASK database."""
    await connection.execute(
        """INSERT INTO task_events(stage,level,message,payload)
           VALUES($1,$2,$3,$4::jsonb)""",
        stage, level, message[:2000], json.dumps(payload or {}, ensure_ascii=False),
    )


async def expand_scope(connection: asyncpg.Connection, task_id: int) -> None:
    await connection.execute("UPDATE task_meta SET status='expanding' WHERE task_id=$1", task_id)
    rows = await connection.fetch("SELECT network::text,source_line,original_value FROM task_cidrs ORDER BY network")
    chunk_size = await connection.fetchval("SELECT masscan_chunk_size FROM task_meta WHERE task_id=$1", task_id)
    batch: list[tuple[str, int]] = []
    ip_index = 0
    for row in rows:
        network = ipaddress.ip_network(row["network"], strict=True)
        for address in network:  # Includes network and broadcast addresses by design.
            ip_index += 1
            batch.append((str(address), ((ip_index - 1) // chunk_size) + 1))
            if len(batch) >= 5000:
                await connection.executemany("INSERT INTO task_targets(ip,source_chunk_no) VALUES($1,$2) ON CONFLICT(ip) DO NOTHING", batch)
                batch.clear()
    if batch:
        await connection.executemany("INSERT INTO task_targets(ip,source_chunk_no) VALUES($1,$2) ON CONFLICT(ip) DO NOTHING", batch)
    await connection.execute("""
      INSERT INTO task_chunks(stage,chunk_no,item_count)
      SELECT 'masscan',source_chunk_no,count(*) FROM task_targets GROUP BY source_chunk_no
      ON CONFLICT(stage,chunk_no) DO NOTHING
    """)
    await connection.execute("UPDATE task_meta SET status='masscan' WHERE task_id=$1", task_id)
    target_count = await connection.fetchval("SELECT count(*) FROM task_targets")
    chunk_count = await connection.fetchval("SELECT count(*) FROM task_chunks WHERE stage='masscan'")
    await emit_event(connection, "scope", "CIDR-развёртка завершена", payload={
        "targets": int(target_count or 0), "chunks": int(chunk_count or 0),
    })


async def claim_masscan(connection: asyncpg.Connection, workers: int) -> list[int]:
    async with connection.transaction():
        rows = await connection.fetch("""
          SELECT chunk_no FROM task_chunks
          WHERE stage='masscan' AND status='pending'
          ORDER BY chunk_no FOR UPDATE SKIP LOCKED LIMIT $1
        """, workers)
        if rows:
            await connection.execute("UPDATE task_chunks SET status='running',started_at=now() WHERE stage='masscan' AND chunk_no=ANY($1::bigint[])", [row["chunk_no"] for row in rows])
    return [row["chunk_no"] for row in rows]


def masscan_command(input_path: Path, output_path: Path, meta: asyncpg.Record) -> list[str]:
    extra_args = json.loads(meta["masscan_extra_args"] or "[]") if isinstance(meta["masscan_extra_args"], str) else list(meta["masscan_extra_args"] or [])
    return ["masscan", "--includefile", str(input_path), "--ports", meta["masscan_ports"],
            "--rate", str(meta["masscan_rate"]), "--output-format", meta["masscan_output_format"],
            "--output-filename", str(output_path), "--wait", "5", "--retries", "0",
            *extra_args]


async def run_process(command: list[str], timeout: int | None = None, on_stderr=None) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout_task = asyncio.create_task(process.stdout.read())
    tail_lines = deque(maxlen=80)

    async def read_stderr():
        carry = ""
        while True:
            block = await process.stderr.read(1024)
            if not block:
                break
            carry += block.decode(errors="replace")
            parts = re.split(r"[\r\n]+", carry)
            carry = parts.pop()
            for part in parts:
                if part.strip():
                    tail_lines.append(part[-512:])
                    if on_stderr:
                        await on_stderr(part[-512:])
        if carry.strip():
            tail_lines.append(carry[-512:])
            if on_stderr:
                await on_stderr(carry[-512:])

    stderr_task = asyncio.create_task(read_stderr())
    try:
        if timeout:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        else:
            await process.wait()
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        await stderr_task
        stdout_task.cancel()
        raise RuntimeError("Процесс превысил timeout")
    await stderr_task
    stdout = await stdout_task
    return process.returncode, stdout.decode(errors="replace"), "\r".join(tail_lines)[-4096:]


async def process_masscan_chunk(connection: asyncpg.Connection, chunk_no: int, meta: asyncpg.Record) -> None:
    # PostgreSQL renders an INET with a /32 host suffix when cast directly to
    # text.  The Masscan parser returns a bare address, so always normalize
    # task IPs with host() before comparing or writing endpoint files.
    rows = await connection.fetch("SELECT host(ip) AS ip FROM task_targets WHERE source_chunk_no=$1 AND masscan_status='pending' ORDER BY ip", chunk_no)
    if not rows:
        await connection.execute("UPDATE task_chunks SET status='completed',finished_at=now() WHERE stage='masscan' AND chunk_no=$1", chunk_no)
        return
    output_format = meta["masscan_output_format"]
    raw_path = ARTIFACT_ROOT / str(meta["task_id"]) / "masscan" / f"chunk-{chunk_no}.{MASSCAN_OUTPUT_EXTENSIONS[output_format]}"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    await emit_event(connection, "masscan", f"Запуск Masscan для чанка {chunk_no}", payload={"chunk": chunk_no})
    with tempfile.TemporaryDirectory(prefix=f"task-{meta['task_id']}-masscan-") as directory:
        input_path, output_path = Path(directory) / "targets.txt", Path(directory) / "masscan.json"
        input_path.write_text("".join(f"{row['ip']}\n" for row in rows), encoding="ascii")
        command = masscan_command(input_path, output_path, meta)

        last_event_at = 0.0

        async def status_update(line: str):
            nonlocal last_event_at
            match = re.search(r"rate:\s*([\d.]+)-kpps,\s*([\d.]+)% done,.*?found=(\d+)", line)
            if not match:
                return
            await connection.execute(
                "UPDATE task_chunks SET status_tail=$2,rate_pps=$3,progress_percent=$4,found_count=$5 WHERE stage='masscan' AND chunk_no=$1",
                chunk_no, line[-2048:], float(match.group(1)) * 1000, float(match.group(2)), int(match.group(3)),
            )
            now = time.monotonic()
            if now - last_event_at >= 1.0:
                last_event_at = now
                await emit_event(connection, "masscan", line.strip(), payload={"chunk": chunk_no})

        returncode, _, tail = await run_process(command, on_stderr=status_update)
        if returncode != 0:
            # Keep the raw buffer even when Masscan exits non-zero.  The
            # artifact is needed to distinguish an empty scan from a parser
            # or process failure during diagnosis and retry.
            if output_path.exists():
                shutil.copyfile(output_path, raw_path)
            await connection.execute("UPDATE task_chunks SET status='failed',error_message=$2,status_tail=$3,finished_at=now() WHERE stage='masscan' AND chunk_no=$1", chunk_no, tail or f"masscan exit {returncode}", tail)
            await connection.execute("UPDATE task_targets SET masscan_status='failed',ready_for_masscan=false,error_message=$2 WHERE source_chunk_no=$1 AND masscan_status='pending'", chunk_no, tail[:500])
            await emit_event(connection, "masscan", f"Masscan завершился с ошибкой в чанке {chunk_no}", "error", {"chunk": chunk_no, "returncode": returncode})
            return
        if output_path.exists():
            shutil.copyfile(output_path, raw_path)
        observations = parse_records(output_path, meta["masscan_output_format"]) if output_path.exists() else []
    requested = {row["ip"] for row in rows}
    observations = [item for item in observations if item["ip_address"] in requested]
    async with connection.transaction():
        await connection.execute("UPDATE task_targets SET masscan_status='closed',ready_for_masscan=false,masscan_finished_at=now() WHERE source_chunk_no=$1 AND masscan_status='pending'", chunk_no)
        for item in observations:
            await connection.execute("""
              INSERT INTO task_open_ports(ip,port,protocol,masscan_open,httpx_ready,service_name,banner)
              VALUES($1,$2,$3,true,true,$4,$5)
              ON CONFLICT(ip,port,protocol) DO UPDATE SET masscan_open=true,httpx_ready=true,service_name=EXCLUDED.service_name,banner=EXCLUDED.banner
            """, item["ip_address"], item["port"], item["protocol"], item["service_name"], item["banner"])
            await connection.execute("UPDATE task_targets SET masscan_status='open',masscan_finished_at=now() WHERE ip=$1", item["ip_address"])
        await connection.execute("UPDATE task_chunks SET status='completed',found_count=$2,status_tail=$3,progress_percent=100,finished_at=now(),raw_output_path=$4,raw_output_bytes=$5 WHERE stage='masscan' AND chunk_no=$1", chunk_no, len(observations), tail, str(raw_path), raw_path.stat().st_size if raw_path.exists() else 0)
    await emit_event(connection, "masscan", f"Masscan завершил чанк {chunk_no}: открытых портов {len(observations)}", payload={
        "chunk": chunk_no, "open_ports": len(observations), "artifact": str(raw_path),
    })


async def masscan_stage(connection: asyncpg.Connection, meta: asyncpg.Record) -> None:
    database_name = meta["database_name"]
    while True:
        chunks = await claim_masscan(connection, meta["masscan_workers"])
        if not chunks:
            pending = await connection.fetchval("SELECT count(*) FROM task_chunks WHERE stage='masscan' AND status IN ('pending','running')")
            if not pending:
                return
            await asyncio.sleep(1)
            continue
        async def process_claimed(chunk_no: int) -> None:
            chunk_connection = await asyncpg.connect(task_url(database_name))
            try:
                await process_masscan_chunk(chunk_connection, chunk_no, meta)
            finally:
                await chunk_connection.close()

        await asyncio.gather(*(process_claimed(chunk) for chunk in chunks))


async def claim_httpx(connection: asyncpg.Connection, chunk_size: int) -> tuple[int, list[asyncpg.Record]] | None:
    async with connection.transaction():
        rows = await connection.fetch("""
          SELECT host(ip) AS ip,port,protocol FROM task_open_ports
          WHERE httpx_ready=true AND httpx_status='pending'
          ORDER BY ip,port FOR UPDATE SKIP LOCKED LIMIT $1
        """, chunk_size)
        if not rows:
            return None
        chunk_no = await connection.fetchval("UPDATE task_meta SET next_httpx_chunk=next_httpx_chunk+1 RETURNING next_httpx_chunk-1")
        await connection.executemany("UPDATE task_open_ports SET httpx_ready=false,httpx_status='running',httpx_chunk_no=$1 WHERE ip=$2 AND port=$3 AND protocol=$4", [(chunk_no, row["ip"], row["port"], row["protocol"]) for row in rows])
        await connection.execute("INSERT INTO task_chunks(stage,chunk_no,item_count,status) VALUES('httpx',$1,$2,'running')", chunk_no, len(rows))
    return int(chunk_no), rows


def httpx_command(input_path: Path, output_path: Path, config: dict) -> list[str]:
    command = ["httpx", "-list", str(input_path), "-output", str(output_path), "-json", "-silent", "-no-color",
               "-disable-update-check", "-no-fallback-scheme", "-include-response", "-threads", str(config["threads"]),
               "-timeout", str(config["timeout"]), "-follow-redirects", "-follow-host-redirects",
               "-path", ",".join(config["paths"]), "-status-code", "-title", "-content-type", "-web-server", "-response-time"]
    for value in config["match_strings"]:
        command.extend(["-match-string", value])
    if config.get("rate_limit"):
        command.extend(["-rate-limit", str(config["rate_limit"])])
    command.extend(config.get("extra_args") or [])
    return command


def parse_httpx_output(path: Path, chunk_no: int, config: dict) -> list[dict]:
    if not path.exists():
        return []
    results = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        url = payload.get("url") or payload.get("input") or payload.get("host")
        if not isinstance(url, str):
            continue
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            continue
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            continue
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # JSON escaping hides quotes in HTML (for example name="magic").
        # Match against both the serialized record and the decoded response
        # fields so the configured fingerprint has the same semantics as the
        # proven debug_scan_2.0.sh implementation.
        evidence_parts = [json.dumps(payload, ensure_ascii=False)]
        for key in ("body", "response", "header", "headers", "title", "webserver"):
            value = payload.get(key)
            if value is not None:
                evidence_parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        evidence = "\n".join(evidence_parts)
        present = [value for value in config["match_strings"] if value in evidence]
        policy = config.get("match_policy", "any")
        marker_match = bool(present) if policy == "any" else bool(config["match_strings"]) and len(present) == len(config["match_strings"])
        status_codes = {int(value) for value in config.get("status_codes", [])}
        raw_status = payload.get("status_code", payload.get("status-code"))
        status_match = not status_codes or (raw_status is not None and int(raw_status) in status_codes)
        title = str(payload.get("title") or "")
        title_match = all(value.lower() in title.lower() for value in config.get("title_contains", []))
        results.append({
            "ip": ip, "port": port, "protocol": parsed.scheme or "http", "url": f"{parsed.scheme}://{host}:{port}",
            "status_code": payload.get("status_code", payload.get("status-code")), "title": payload.get("title"),
            "webserver": payload.get("webserver"), "headers": payload.get("header", payload.get("headers", {})),
            "body_preview": payload.get("body", payload.get("response")), "matched": marker_match and status_match and title_match,
            "match_details": present, "httpx_chunk_no": chunk_no,
        })
    return results


async def process_httpx_chunk(connection: asyncpg.Connection, task_id: int, chunk_no: int, rows: list[asyncpg.Record], config: dict) -> None:
    raw_path = ARTIFACT_ROOT / str(task_id) / "httpx" / f"chunk-{chunk_no}.jsonl"
    await emit_event(connection, "httpx", f"Запуск HTTPX для чанка {chunk_no}", payload={"chunk": chunk_no, "endpoints": len(rows)})
    with tempfile.TemporaryDirectory(prefix="task-httpx-") as directory:
        input_path, output_path = Path(directory) / "open-endpoints.txt", Path(directory) / "httpx.jsonl"
        schemes = config.get("schemes") or ["http", "https"]
        input_path.write_text("".join(f"{scheme}://{row['ip']}:{row['port']}\n" for row in rows for scheme in schemes), encoding="ascii")
        last_event_at = 0.0

        async def status_update(line: str):
            nonlocal last_event_at
            now = time.monotonic()
            if now - last_event_at >= 1.0:
                last_event_at = now
                await connection.execute(
                    "UPDATE task_chunks SET status_tail=$2 WHERE stage='httpx' AND chunk_no=$1",
                    chunk_no, line[-2048:],
                )
                await emit_event(connection, "httpx", line.strip(), payload={"chunk": chunk_no})

        returncode, _, tail = await run_process(httpx_command(input_path, output_path, config), timeout=None, on_stderr=status_update)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            shutil.copyfile(output_path, raw_path)
        results = parse_httpx_output(output_path, chunk_no, config)
    async with connection.transaction():
        if returncode == 0:
            for result in results:
                await connection.execute("""INSERT INTO httpx_results(ip,port,protocol,url,status_code,title,webserver,headers,body_preview,matched,match_details,httpx_chunk_no)
                  VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11::jsonb,$12)
                  ON CONFLICT(ip,port,protocol,url) DO UPDATE SET status_code=EXCLUDED.status_code,title=EXCLUDED.title,webserver=EXCLUDED.webserver,headers=EXCLUDED.headers,body_preview=EXCLUDED.body_preview,matched=EXCLUDED.matched,match_details=EXCLUDED.match_details""",
                    result["ip"], result["port"], result["protocol"], result["url"], result["status_code"], result["title"], result["webserver"], json.dumps(result["headers"] or {}), result["body_preview"], result["matched"], json.dumps(result["match_details"]), chunk_no)
            await connection.execute("UPDATE task_open_ports SET httpx_status='completed' WHERE httpx_chunk_no=$1", chunk_no)
            await connection.execute("UPDATE task_chunks SET status='completed',response_count=$2,matched_count=$2,status_tail=$3,finished_at=now(),raw_output_path=$4,raw_output_bytes=$5 WHERE stage='httpx' AND chunk_no=$1", chunk_no, len(results), tail, str(raw_path), raw_path.stat().st_size if raw_path.exists() else 0)
            await emit_event(connection, "httpx", f"HTTPX завершил чанк {chunk_no}: ответов {len(results)}", payload={
                "chunk": chunk_no, "responses": len(results), "artifact": str(raw_path),
            })
        else:
            await connection.execute("UPDATE task_open_ports SET httpx_status='failed',httpx_ready=true WHERE httpx_chunk_no=$1", chunk_no)
            await connection.execute("UPDATE task_chunks SET status='failed',error_message=$2,status_tail=$3,finished_at=now() WHERE stage='httpx' AND chunk_no=$1", chunk_no, tail or f"httpx exit {returncode}", tail)
            await emit_event(connection, "httpx", f"HTTPX завершился с ошибкой в чанке {chunk_no}", "error", {"chunk": chunk_no, "returncode": returncode})


async def httpx_stage(connection: asyncpg.Connection, meta: asyncpg.Record,
                      masscan_done: asyncio.Event, database_name: str) -> None:
    config = json.loads(meta["httpx_config"]) if isinstance(meta["httpx_config"], str) else dict(meta["httpx_config"])
    while True:
        claimed = []
        for _ in range(meta["httpx_workers"]):
            item = await claim_httpx(connection, meta["httpx_chunk_size"])
            if item:
                claimed.append(item)
        if not claimed:
            pending = await connection.fetchval("SELECT count(*) FROM task_open_ports WHERE httpx_ready=true AND httpx_status='pending'")
            if masscan_done.is_set() and not pending:
                return
            await asyncio.sleep(1)
            continue
        await connection.execute("UPDATE task_meta SET status='httpx' WHERE task_id=$1", meta["task_id"])
        async def process_claimed(chunk_no: int, rows: list[asyncpg.Record]) -> None:
            chunk_connection = await asyncpg.connect(task_url(database_name))
            try:
                await process_httpx_chunk(chunk_connection, meta["task_id"], chunk_no, rows, config)
            finally:
                await chunk_connection.close()

        await asyncio.gather(*(process_claimed(chunk_no, rows) for chunk_no, rows in claimed))


async def run_task(task_id: int) -> None:
    registry = await load_registry(task_id)
    connection = await asyncpg.connect(task_url(registry["database_name"]))
    try:
        meta = await connection.fetchrow("SELECT * FROM task_meta WHERE task_id=$1", task_id)
        if meta is None:
            raise ValueError("task_meta отсутствует")
        status = meta["status"]
        if status == "completed":
            await update_counts(task_id, connection)
            await set_registry(task_id, "completed")
            return
        # A process/container restart must not leave claimed work permanently
        # stuck.  Replaying a claimed chunk is safe because all inserts are
        # idempotent and endpoint/result keys are unique.
        await connection.execute("UPDATE task_chunks SET status='pending',started_at=NULL WHERE stage='masscan' AND status='running'")
        # HTTPX chunk numbers are allocation records, not the identity of an
        # endpoint.  If the process dies after claiming a chunk, release its
        # endpoints and remove the unfinished allocation; otherwise the next
        # claim would create a second chunk while the orphan stayed pending.
        await connection.execute("UPDATE task_open_ports SET httpx_status='pending',httpx_ready=true,httpx_chunk_no=NULL WHERE httpx_status='running'")
        await connection.execute("DELETE FROM task_chunks WHERE stage='httpx' AND status IN ('running','pending')")
        await set_registry(task_id, "running")
        await emit_event(connection, "task", "TASK запущен worker-ом", payload={"task_id": task_id})
        if status == "provisioning":
            await expand_scope(connection, task_id)
            meta = await connection.fetchrow("SELECT * FROM task_meta WHERE task_id=$1", task_id)
            await update_counts(task_id, connection)
        # Keep the database name with the task metadata so stage workers can
        # open an isolated connection per concurrently processed chunk.
        meta = dict(meta)
        meta["database_name"] = registry["database_name"]
        # Run both stages together. HTTPX waits for the first open-port rows,
        # then claims each completed Masscan chunk immediately; it only exits
        # after Masscan is done and the HTTPX queue is empty.
        masscan_connection = await asyncpg.connect(task_url(registry["database_name"]))
        httpx_connection = await asyncpg.connect(task_url(registry["database_name"]))
        masscan_done = asyncio.Event()

        async def run_masscan_stage():
            try:
                await masscan_stage(masscan_connection, meta)
            finally:
                masscan_done.set()

        try:
            await asyncio.gather(
                run_masscan_stage(),
                httpx_stage(httpx_connection, meta, masscan_done, registry["database_name"]),
            )
        finally:
            await masscan_connection.close()
            await httpx_connection.close()
        await update_counts(task_id, connection)
        await connection.execute("UPDATE task_meta SET status='completed',finished_at=now() WHERE task_id=$1", task_id)
        await emit_event(connection, "task", "TASK завершён", payload={"task_id": task_id})
        await update_counts(task_id, connection)
        await set_registry(task_id, "completed")
    except Exception as error:
        LOGGER.exception("TASK %s failed", task_id)
        await connection.execute("UPDATE task_meta SET status='failed',error_message=$2,finished_at=now() WHERE task_id=$1", task_id, str(error)[:1000])
        try:
            await emit_event(connection, "task", f"TASK завершился с ошибкой: {str(error)[:500]}", "error", {"task_id": task_id})
        except Exception:
            LOGGER.exception("Не удалось записать событие TASK %s", task_id)
        await set_registry(task_id, "failed", str(error)[:1000])
    finally:
        await connection.close()


async def main() -> None:
    redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        try:
            await redis_client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise
        first_read = True
        idle_reads = 0
        while True:
            messages = await redis_client.xreadgroup(GROUP, "runner-1", {STREAM: "0" if first_read else ">"}, count=1, block=1000)
            first_read = False
            if not messages:
                idle_reads += 1
                if idle_reads >= 5:
                    idle_reads = 0
                    # Redis notification is a wake-up hint. The control DB is
                    # authoritative, so a crash between commit and XADD does
                    # not leave a queued TASK permanently idle.
                    for task_id in await queued_task_ids():
                        await run_task(task_id)
                continue
            idle_reads = 0
            for _, events in messages:
                for event_id, fields in events:
                    try:
                        await run_task(int(fields["task_id"]))
                    except Exception:
                        LOGGER.exception("Ошибка обработки события TASK %s", fields.get("task_id"))
                    finally:
                        await redis_client.xack(STREAM, GROUP, event_id)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(main())
