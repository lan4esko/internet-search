#!/usr/bin/env python3
"""Create private configuration and bootstrap this checkout using Docker Compose."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
APPLICATION_SERVICES = ("web", "api", "task-runner", "metrics-agent", "metrics-proxy")


def initialize_environment(path):
    """Write once, with restrictive permissions; never rotate an existing password."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        return False
    password = secrets.token_hex(32)
    grafana_password = secrets.token_urlsafe(24)
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    content = template.replace("POSTGRES_PASSWORD=\n", f"POSTGRES_PASSWORD={password}\n")
    content = content.replace("DATABASE_URL=\n", f"DATABASE_URL=postgresql+asyncpg://app:{password}@postgres:5432/internet_search\n")
    content = content.replace("GRAFANA_ADMIN_PASSWORD=\n", f"GRAFANA_ADMIN_PASSWORD={grafana_password}\n")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    return True


class Compose:
    def __init__(self, env_file, project_name=None):
        self.env_file = Path(env_file).resolve()
        self.environment = dict(os.environ, APP_ENV_FILE=str(self.env_file))
        self.command = ["docker", "compose", "--project-directory", str(ROOT),
                        "--env-file", str(self.env_file)]
        if project_name:
            self.environment["COMPOSE_PROJECT_NAME"] = project_name
            self.command += ["--project-name", project_name]

    def run(self, *arguments, **kwargs):
        return subprocess.run(self.command + list(arguments), cwd=ROOT,
                              env=self.environment, check=True, **kwargs)

    def running(self):
        return self.run("ps", "--status", "running", "--services",
                        capture_output=True, text=True).stdout.splitlines()

    def validate(self):
        self.run("version", "--short")
        # config --quiet verifies interpolation without printing passwords.
        self.run("config", "--quiet")
        subprocess.run(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], check=True)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--project-name", help="Separate Compose project and volumes")
    parser.add_argument("--init-only", action="store_true", help="Generate .env without running Docker")
    parser.add_argument("--skip-build", action="store_true", help="Use already built images")
    return parser.parse_args()


def main():
    args = arguments()
    created = initialize_environment(args.env_file)
    print(f"Конфигурация {'создана с правами 0600' if created else 'сохранена без изменений'}: {args.env_file}", flush=True)
    if args.init_only:
        return 0
    compose = Compose(args.env_file, args.project_name)
    compose.validate()
    running_application = sorted(set(compose.running()).intersection(APPLICATION_SERVICES))
    if running_application:
        raise RuntimeError("Сервисы приложения уже работают: " + ", ".join(running_application)
                           + ". Для обновления сначала завершите задачи и остановите их по docs/operations.md.")
    if not args.skip_build:
        compose.run("build")
    compose.run("up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis")
    compose.run("run", "--rm", "--no-deps", "api", "alembic", "upgrade", "head")
    compose.run("up", "-d", "--wait", "--wait-timeout", "120")
    compose.run("exec", "-T", "api", "python", "-c",
                "import json,urllib.request; "
                "paths=['/health','/health/postgres','/health/redis','/tasks']; "
                "[(urllib.request.urlopen('http://127.0.0.1:8000'+p,timeout=10).read(),print(p+' OK')) for p in paths]")
    compose.run("ps")
    print("Развёртывание проверено. Порты: docker compose port web 80 / docker compose port api 8000. "
          "Создайте TASK через веб-панель или POST /tasks; подробности см. docs/deployment.md.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Развёртывание остановлено: {error}", file=sys.stderr)
        raise SystemExit(1)
