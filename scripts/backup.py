#!/usr/bin/env python3
"""Back up an idle deployment, including the physical database of every TASK."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from bootstrap import APPLICATION_SERVICES, Compose, ROOT

# Pending/running TASKs are included: stopping a runner mid-chunk leaves work in
# a recoverable state, but a backup should still represent a quiescent system.
IDLE_CHECK = """
import asyncio,json,os,asyncpg
async def main():
    c=await asyncpg.connect(os.environ['DATABASE_URL'].replace('+asyncpg',''))
    try:
        counts={'active_tasks': await c.fetchval("SELECT count(*) FROM task_registry WHERE status IN ('provisioning','queued','running')")}
        if await c.fetchval("SELECT to_regclass('public.v2_tasks') IS NOT NULL"):
            counts['active_v2_tasks']=await c.fetchval("SELECT count(*) FROM v2_tasks WHERE status IN ('provisioning','queued','running','cancelling')")
        print(json.dumps(counts))
    finally:
        await c.close()
asyncio.run(main())
"""


def require_idle(compose):
    raw = compose.run("run", "--rm", "--no-deps", "-T", "api", "python", "-c", IDLE_CHECK,
                      capture_output=True, text=True).stdout
    counts = json.loads(raw)
    if any(counts.values()):
        raise RuntimeError("Перед backup завершите ожидающие и активные задания: " + json.dumps(counts))


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for private backup files")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--project-name")
    args = parser.parse_args()
    compose = Compose(args.env_file, args.project_name)
    compose.validate()
    previously_running = compose.running()
    if not {"postgres", "redis"}.issubset(previously_running):
        raise RuntimeError("Для backup PostgreSQL и Redis должны работать")
    require_idle(compose)
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    stop = [name for name in APPLICATION_SERVICES if name in previously_running]
    paused = False
    try:
        paused = True
        if stop:
            compose.run("stop", "--timeout", "60", *stop)
        # Recheck after stopping producers: a job could have arrived during preflight.
        require_idle(compose)
        for filename, command in (
            ("postgres.dump", ("exec", "-T", "postgres", "sh", "-c",
                               'exec pg_dumpall --username "$POSTGRES_USER" --no-role-passwords')),
            ("scope.tar.gz", ("run", "--rm", "--no-deps", "-T", "api", "python", "-c",
                              "import sys,tarfile; a=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz'); a.add('/data/scope',arcname='.'); a.close()")),
        ):
            with (output / filename).open("xb") as stream:
                os.chmod(stream.fileno(), 0o600)
                compose.run(*command, stdout=stream)
        compose.run("exec", "-T", "redis", "redis-cli", "SAVE")
        compose.run("stop", "--timeout", "60", "redis")
        with (output / "redis.tar.gz").open("xb") as stream:
            os.chmod(stream.fileno(), 0o600)
            compose.run("run", "--rm", "--no-deps", "-T", "--entrypoint", "tar",
                        "redis", "-C", "/data", "-czf", "-", ".", stdout=stream)
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        manifest = {
            "format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "git_commit": revision.stdout.strip() if revision.returncode == 0 else None,
            "postgres_major": 17, "redis_major": 8,
            "files": {path.name: {"sha256": sha256_file(path), "size": path.stat().st_size}
                      for path in sorted(output.iterdir())},
            "environment_included": False,
        }
        with (output / "manifest.json").open("x", encoding="utf-8") as stream:
            os.chmod(stream.fileno(), 0o600)
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(f"Backup готов: {output}. .env сохраните отдельно в защищённом хранилище.")
    finally:
        if paused:
            # Preserve the original running/stopped state; never enable a stopped scanner.
            compose.run("up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis")
            if stop:
                compose.run("start", *stop)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Backup остановлен: {error}. Каталог без manifest.json не является полной копией.", file=sys.stderr)
        raise SystemExit(1)
