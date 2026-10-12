"""Re-evaluate stored HTTPX JSONL using the TASK's unchanged fingerprint."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import asyncpg

from app.main import best_endpoint_results, emit_event, load_registry, parse_httpx_output, task_url, update_counts


async def run(task_id: int, apply: bool) -> None:
    registry = await load_registry(task_id)
    if registry["status"] != "completed":
        raise ValueError("Повторная классификация разрешена только для завершённого TASK")
    connection = await asyncpg.connect(task_url(registry["database_name"]))
    try:
        meta = await connection.fetchrow("SELECT httpx_config FROM task_meta WHERE task_id=$1", task_id)
        if meta is None:
            raise ValueError("Настройки TASK отсутствуют")
        config = meta["httpx_config"]
        if isinstance(config, str):
            config = json.loads(config)
        chunks = await connection.fetch("""
            SELECT chunk_no,raw_output_path FROM task_chunks
            WHERE stage='httpx' AND status='completed' AND raw_output_bytes>0
            ORDER BY chunk_no
        """)
        parsed = {}
        for chunk in chunks:
            path = Path(chunk["raw_output_path"])
            if not path.is_file():
                raise FileNotFoundError(f"Нет сохранённого HTTPX JSONL: {path}")
            for result in best_endpoint_results(parse_httpx_output(path, chunk["chunk_no"], config)):
                key = (result["ip"], result["port"], result["protocol"], result["url"])
                previous = parsed.get(key)
                if previous is None or (result["matched"], len(result["match_details"])) > (previous["matched"], len(previous["match_details"])):
                    parsed[key] = result
        stored = await connection.fetch("SELECT host(ip) AS ip,port,protocol,url FROM httpx_results")
        keys = {(row["ip"], row["port"], row["protocol"], row["url"]) for row in stored}
        missing = keys - parsed.keys()
        if missing:
            raise ValueError(f"Для {len(missing)} сохранённых ответов отсутствует HTTPX JSONL")
        # A raw response may have been rejected because its final IP:PORT was
        # outside the claimed ip(1) chunk. Never turn such a response into ip(2).
        candidates = [parsed[key] for key in keys]
        current = await connection.fetchval("SELECT count(*) FROM httpx_results WHERE matched=true")
        proposed = sum(1 for result in candidates if result["matched"])
        print(f"TASK #{task_id}: сохранённых HTTPX-ответов {len(candidates)}, подтверждений {current} → {proposed}")
        if not apply:
            return
        async with connection.transaction():
            for result in candidates:
                updated = await connection.execute("""
                    UPDATE httpx_results SET matched=$5,match_details=$6::jsonb
                    WHERE ip=$1 AND port=$2 AND protocol=$3 AND url=$4
                """, result["ip"], result["port"], result["protocol"], result["url"],
                    result["matched"], json.dumps(result["match_details"]))
                if updated != "UPDATE 1":
                    raise ValueError(f"Ответ из JSONL отсутствует в БД: {result['url']}")
            await connection.execute("""
                UPDATE task_chunks AS chunk SET matched_count=(
                    SELECT count(*) FROM httpx_results AS result
                    WHERE result.httpx_chunk_no=chunk.chunk_no AND result.matched=true
                ) WHERE chunk.stage='httpx' AND chunk.status='completed'
            """)
            await emit_event(connection, "httpx", "Сохранённые ответы повторно проверены по fingerprint",
                             payload={"task_id": task_id, "responses": len(candidates), "matched": proposed})
        await update_counts(task_id, connection)
        print("Обновлены подтверждения, счётчики чанков и TASK")
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", type=int)
    parser.add_argument("--apply", action="store_true", help="Сохранить новую классификацию")
    args = parser.parse_args()
    asyncio.run(run(args.task_id, args.apply))


if __name__ == "__main__":
    main()
