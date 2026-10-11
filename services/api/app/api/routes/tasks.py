"""Real TASK API backed by one physical PostgreSQL database per task."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shlex
from pathlib import Path
from uuid import uuid4

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.task import SearchTemplate, TaskRegistry
from app.redis import get_redis
from app.schemas.task import (
    ConfigValidationRequest,
    SearchTemplateCreate,
    SearchTemplatePage,
    SearchTemplateRead,
    TaskPage,
    TaskRead,
    TaskRetry,
)
from app.task_db import connect, create_database, drop_database, initialize_database


router = APIRouter(prefix="/tasks", tags=["tasks"])
templates_router = APIRouter(prefix="/templates", tags=["templates"])
TASK_DATA_DIR = Path(os.getenv("SCOPE_DATA_DIR", "/data/scope")) / "tasks"
UPLOAD_CHUNK = 1024 * 1024
FORTINET_PATHS = ["/remote/login", "/remote/index", "/ssl-vpn/login.html"]
FORTINET_MATCH_STRINGS = ["ftnt-fortinet-grid", 'name="magic"', "SVPNCOOKIE"]
PALO_ALTO_PATHS = ["/global-protect/login.esp"]
PALO_ALTO_MATCH_STRINGS = [
    "GlobalProtect Portal",
    'id="login_form"',
    'name="prot"',
    "/global-protect/portal/css/login.css",
    "/global-protect/portal/images/logo-pan-",
    'id="heading"',
]
PALO_ALTO_PANOS_ADMIN_PATHS = ["/php/login.php"]
PALO_ALTO_PANOS_ADMIN_MATCH_STRINGS = [
    "window.Pan",
    "Panos.browser",
    "panos-panos-login.js",
    "/styles/login/css/login-admin.css",
    "/login/images/panw_new_logo_302_53.png",
    'id="login_form"',
    'name="authType"',
    'name="challengeCookie"',
]
FINGERPRINT_DEFAULTS = {
    "fortinet-debug-scan-2.0": {
        "paths": FORTINET_PATHS,
        "schemes": ["http", "https"],
        "match_strings": FORTINET_MATCH_STRINGS,
        "match_policy": "any",
    },
    "paloalto-globalprotect-portal": {
        "paths": PALO_ALTO_PATHS,
        "schemes": ["https"],
        "match_strings": PALO_ALTO_MATCH_STRINGS,
        "match_policy": "all",
        "status_codes": [200],
        "title_contains": ["GlobalProtect Portal"],
    },
    "paloalto-panos-admin": {
        "paths": PALO_ALTO_PANOS_ADMIN_PATHS,
        "schemes": ["https"],
        "match_strings": PALO_ALTO_PANOS_ADMIN_MATCH_STRINGS,
        "match_policy": "all",
        "status_codes": [200],
        "title_contains": ["Login"],
    },
}
MASSCAN_FORMATS = {"json", "list", "xml"}
MASSCAN_RESERVED_ARGS = {"--includefile", "--ports", "--rate", "--output-format", "--output-filename", "--wait", "--retries"}
HTTPX_RESERVED_ARGS = {
    "-list", "-output", "-o", "-json", "-silent", "-no-color", "-disable-update-check",
    "-no-fallback-scheme", "-include-response", "-threads", "-timeout", "-follow-redirects",
    "-follow-host-redirects", "-path", "-match-string", "-rate-limit", "-status-code", "-title",
    "-content-type", "-content-length", "-web-server", "-response-time",
}


def parse_json_list(raw: str, field: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=422, detail=f"{field}: нужен JSON-массив") from error
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise HTTPException(status_code=422, detail=f"{field}: нужен непустой массив строк")
    return value


def parse_extra_args(raw: str | list[str] | None, field: str, reserved: set[str]) -> list[str]:
    """Parse an argv fragment without ever turning it into a shell command."""
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        if not raw.strip():
            return []
        try:
            values = json.loads(raw) if raw.lstrip().startswith("[") else shlex.split(raw, comments=False, posix=True)
        except (json.JSONDecodeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=f"{field}: JSON-массив или корректная строка аргументов") from error
    if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
        raise HTTPException(status_code=422, detail=f"{field}: ожидается массив строк")
    if len(values) > 64 or sum(len(item) for item in values) > 8192:
        raise HTTPException(status_code=422, detail=f"{field}: слишком много аргументов")
    for value in values:
        if "\x00" in value or any(character in value for character in ("\n", "\r")):
            raise HTTPException(status_code=422, detail=f"{field}: недопустимые управляющие символы")
        # The web form stores one line per item, so a managed flag can arrive
        # together with its value (for example, `-match-string \"marker\"`).
        # Inspect the first argv token to prevent fingerprint flags from being
        # smuggled through the free-form argument list.
        try:
            first_token = shlex.split(value, comments=False, posix=True)[0] if value.strip() else ""
        except ValueError as error:
            raise HTTPException(status_code=422, detail=f"{field}: JSON-массив содержит некорректный argv-токен") from error
        flag = first_token.split("=", 1)[0]
        if flag in reserved:
            raise HTTPException(status_code=422, detail=f"{field}: аргумент {flag} управляется TASK")
    return values


def _integer(value, field: str, minimum: int, maximum: int, default=None) -> int:
    if value is None or value == "":
        value = default
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=f"{field}: ожидается целое число") from error
    if result < minimum or result > maximum:
        raise HTTPException(status_code=422, detail=f"{field}: допустимый диапазон {minimum}..{maximum}")
    return result


def _boolean(value, field: str, default: bool = False) -> bool:
    """Parse HTML/API booleans without treating the string 'false' as true."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise HTTPException(status_code=422, detail=f"{field}: ожидается true или false")


def _string_list(value, field: str, default: list[str] | None = None) -> list[str]:
    if value is None:
        value = default
    if isinstance(value, str):
        value = parse_json_list(value, field)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise HTTPException(status_code=422, detail=f"{field}: нужен непустой массив строк")
    if len(value) > 64 or sum(len(item) for item in value) > 16_384:
        raise HTTPException(status_code=422, detail=f"{field}: слишком много данных")
    return value


def _integer_list(value, field: str, default: list[int] | None = None) -> list[int]:
    if value is None or value == "":
        value = default or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=422, detail=f"{field}: нужен JSON-массив чисел") from error
    if not isinstance(value, list) or any(isinstance(item, bool) for item in value):
        raise HTTPException(status_code=422, detail=f"{field}: нужен массив чисел")
    result = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=f"{field}: ожидаются коды HTTP") from error
        if number < 100 or number > 599:
            raise HTTPException(status_code=422, detail=f"{field}: код HTTP должен быть 100..599")
        result.append(number)
    return sorted(set(result))


def normalize_config(payload: dict) -> dict:
    """Validate UI/template values and produce the one canonical TASK config."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="config: нужен JSON-объект")
    nested_httpx = payload.get("httpx") if isinstance(payload.get("httpx"), dict) else {}
    raw_fingerprint = payload.get("fingerprint_id", nested_httpx.get("fingerprint_id"))
    if raw_fingerprint is None and isinstance(payload.get("fingerprint"), str):
        raw_fingerprint = payload["fingerprint"]
    fingerprint_id = str(raw_fingerprint or "fortinet-debug-scan-2.0").strip().lower()
    fingerprint_defaults = FINGERPRINT_DEFAULTS.get(fingerprint_id)
    if fingerprint_defaults is None:
        raise HTTPException(status_code=422, detail=f"Неизвестный fingerprint: {fingerprint_id}")
    raw_ports = payload.get("masscan_ports")
    if isinstance(raw_ports, list):
        raw_ports = ",".join(str(item) for item in raw_ports)
    if not isinstance(raw_ports, str):
        raise HTTPException(status_code=422, detail="masscan_ports: нужна строка портов через запятую")
    try:
        ports = [int(item.strip()) for item in raw_ports.split(",") if item.strip()]
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Masscan ports должны быть числами") from error
    if not ports or any(port < 1 or port > 65535 for port in ports):
        raise HTTPException(status_code=422, detail="Masscan ports должны быть от 1 до 65535")
    output_format = str(payload.get("masscan_output_format", "json"))
    if output_format not in MASSCAN_FORMATS:
        raise HTTPException(status_code=422, detail="masscan_output_format: json, list или xml")
    paths = _string_list(payload.get("httpx_paths", nested_httpx.get("paths")), "httpx_paths", fingerprint_defaults["paths"])
    schemes = _string_list(
        payload.get("httpx_schemes", nested_httpx.get("schemes")),
        "httpx_schemes", fingerprint_defaults.get("schemes", ["http", "https"]),
    )
    if any(scheme not in {"http", "https"} for scheme in schemes):
        raise HTTPException(status_code=422, detail="httpx_schemes допускает только http и https")
    has_rate_limit = "httpx_rate_limit" in payload or "rate_limit" in nested_httpx
    rate_limit = payload.get("httpx_rate_limit", nested_httpx.get("rate_limit"))
    if rate_limit in ("", 0, "0"):
        rate_limit = None
    if rate_limit is not None:
        rate_limit = _integer(rate_limit, "httpx_rate_limit", 1, 100_000)
    match_strings = _string_list(
        payload.get("httpx_match_strings", nested_httpx.get("match_strings")),
        "httpx_match_strings", fingerprint_defaults["match_strings"],
    )
    raw_match_policy = payload.get("httpx_match_policy") or nested_httpx.get("match_policy") or fingerprint_defaults.get("match_policy", "any")
    match_policy = str(raw_match_policy).lower()
    if match_policy not in {"any", "all"}:
        raise HTTPException(status_code=422, detail="httpx_match_policy допускает только any или all")
    status_codes = _integer_list(payload.get("httpx_status_codes", nested_httpx.get("status_codes", fingerprint_defaults.get("status_codes"))), "httpx_status_codes")
    raw_title_contains = payload.get("httpx_title_contains")
    if raw_title_contains is None:
        raw_title_contains = nested_httpx.get("title_contains")
    title_default = fingerprint_defaults.get("title_contains", [])
    if isinstance(raw_title_contains, str):
        try:
            raw_title_contains = json.loads(raw_title_contains)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=422, detail="httpx_title_contains: нужен JSON-массив строк") from error
    if raw_title_contains == [] or (raw_title_contains is None and not title_default):
        title_contains = []
    else:
        title_contains = _string_list(raw_title_contains, "httpx_title_contains", title_default)
    httpx_method = str(payload.get("httpx_method", nested_httpx.get("method", "GET")) or "GET").upper()
    if httpx_method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "OPTIONS", "DELETE"}:
        raise HTTPException(status_code=422, detail="httpx_method: поддерживаются GET, HEAD, POST, PUT, PATCH, OPTIONS и DELETE")
    httpx_config = {
        "paths": paths,
        "schemes": schemes,
        "threads": _integer(payload.get("httpx_threads", nested_httpx.get("threads")), "httpx_threads", 1, 2_000, int(os.getenv("HTTPX_THREADS", "500"))),
        "timeout": _integer(payload.get("httpx_timeout", nested_httpx.get("timeout")), "httpx_timeout", 1, 120, int(os.getenv("HTTPX_TIMEOUT", "5"))),
        "method": httpx_method,
        "probe": _boolean(payload.get("httpx_probe", nested_httpx.get("probe")), "httpx_probe", False),
        "follow_redirects": _boolean(payload.get("httpx_follow_redirects", nested_httpx.get("follow_redirects")), "httpx_follow_redirects", True),
        "follow_host_redirects": _boolean(payload.get("httpx_follow_host_redirects", nested_httpx.get("follow_host_redirects")), "httpx_follow_host_redirects", True),
        "status_code": _boolean(payload.get("httpx_status_code", nested_httpx.get("status_code")), "httpx_status_code", True),
        "title": _boolean(payload.get("httpx_title", nested_httpx.get("title")), "httpx_title", True),
        "content_type": _boolean(payload.get("httpx_content_type", nested_httpx.get("content_type")), "httpx_content_type", True),
        "content_length": _boolean(payload.get("httpx_content_length", nested_httpx.get("content_length")), "httpx_content_length", True),
        "web_server": _boolean(payload.get("httpx_web_server", nested_httpx.get("web_server")), "httpx_web_server", True),
        "response_time": _boolean(payload.get("httpx_response_time", nested_httpx.get("response_time")), "httpx_response_time", True),
        "include_response": _boolean(payload.get("httpx_include_response", nested_httpx.get("include_response")), "httpx_include_response", True),
        "tech_detect": _boolean(payload.get("httpx_tech_detect", nested_httpx.get("tech_detect")), "httpx_tech_detect", False),
        "response_size_to_read": _integer(payload.get("httpx_response_size_to_read", nested_httpx.get("response_size_to_read")), "httpx_response_size_to_read", 1, 10_485_760, 262_144),
        "response_size_to_save": _integer(payload.get("httpx_response_size_to_save", nested_httpx.get("response_size_to_save")), "httpx_response_size_to_save", 1, 10_485_760, 262_144),
        "match_strings": match_strings,
        "match_policy": match_policy,
        "status_codes": status_codes,
        "title_contains": title_contains,
    }
    if rate_limit is not None:
        httpx_config["rate_limit"] = rate_limit
    elif not has_rate_limit and os.getenv("HTTPX_RATE_LIMIT"):
        httpx_config["rate_limit"] = int(os.environ["HTTPX_RATE_LIMIT"])
    config = {
        "masscan_chunk_size": _integer(payload.get("masscan_chunk_size"), "masscan_chunk_size", 1, 1_000_000),
        "httpx_chunk_size": _integer(payload.get("httpx_chunk_size"), "httpx_chunk_size", 1, 100_000),
        "masscan_ports": ",".join(str(port) for port in sorted(set(ports))),
        "masscan_rate": _integer(payload.get("masscan_rate"), "masscan_rate", 1, 100_000),
        "masscan_output_format": output_format,
        "masscan_extra_args": parse_extra_args(payload.get("masscan_extra_args"), "masscan_extra_args", MASSCAN_RESERVED_ARGS),
        "masscan_workers": _integer(payload.get("masscan_workers", 1), "masscan_workers", 1, 32),
        "httpx_workers": _integer(payload.get("httpx_workers", 1), "httpx_workers", 1, 64),
        "httpx": httpx_config,
        "httpx_extra_args": parse_extra_args(payload.get("httpx_extra_args", nested_httpx.get("extra_args")), "httpx_extra_args", HTTPX_RESERVED_ARGS),
        "fingerprint": fingerprint_id,
    }
    config["commands"] = command_preview(config)
    return config


def command_preview(config: dict) -> dict[str, list[str]]:
    masscan = [
        "masscan", "--includefile", "{CIDR_SHARD}", "--ports", config["masscan_ports"],
        "--rate", str(config["masscan_rate"]), "--output-format", config["masscan_output_format"],
        "--output-filename", "{OUTPUT_FILE}", "--wait", "5", "--retries", "0",
        *config["masscan_extra_args"],
    ]
    httpx = ["httpx", "-list", "{OPEN_ENDPOINTS_FILE}", "-output", "{OUTPUT_JSONL}", "-json", "-silent",
             "-no-color", "-disable-update-check", "-no-fallback-scheme"]
    httpx_config = config["httpx"]
    if httpx_config.get("include_response", True):
        httpx.append("-include-response")
    if httpx_config.get("probe"):
        httpx.append("-probe")
    httpx.extend(["-threads", str(httpx_config["threads"]), "-timeout", str(httpx_config["timeout"])])
    if httpx_config.get("follow_redirects", True):
        httpx.append("-follow-redirects")
    if httpx_config.get("follow_host_redirects", True):
        httpx.append("-follow-host-redirects")
    httpx.extend(["-path", ",".join(httpx_config["paths"])])
    for key, flag in (("status_code", "-status-code"), ("title", "-title"), ("content_type", "-content-type"),
                      ("content_length", "-content-length"), ("web_server", "-web-server"),
                      ("response_time", "-response-time"), ("tech_detect", "-tech-detect")):
        if httpx_config.get(key, True):
            httpx.append(flag)
    if httpx_config.get("method", "GET") != "GET":
        httpx.extend(["-x", httpx_config["method"]])
    httpx.extend(["-response-size-to-read", str(httpx_config.get("response_size_to_read", 262_144)),
                  "-response-size-to-save", str(httpx_config.get("response_size_to_save", 262_144))])
    httpx.extend(sum((["-match-string", value] for value in httpx_config["match_strings"]), []))
    if config["httpx"].get("rate_limit"):
        httpx.extend(["-rate-limit", str(config["httpx"]["rate_limit"])])
    httpx.extend(config["httpx_extra_args"])
    return {"masscan": masscan, "httpx": httpx}


async def save_upload(file: UploadFile, database_name: str) -> tuple[Path, str, str, list[tuple[str, int, str]]]:
    TASK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = TASK_DATA_DIR / f"{database_name}.txt"
    source_filename = Path(file.filename or "scope.txt").name[:255] or "scope.txt"
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
    return path, digest.hexdigest(), source_filename, list(cidrs.values())


def analyze_cidr_text(raw_text: str, filename: str = "scope.txt", sample_subnets: int = 10) -> dict:
    data = raw_text.encode("utf-8")
    if len(data) > 64 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CIDR-файл больше 64 МиБ")
    networks: dict[str, int] = {}
    invalid = 0
    empty = 0
    valid_lines = 0
    for raw in raw_text.splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            empty += 1
            continue
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError:
            invalid += 1
            continue
        if not isinstance(network, ipaddress.IPv4Network):
            invalid += 1
            continue
        valid_lines += 1
        networks.setdefault(str(network), network.num_addresses)
    return {
        "filename": filename or "scope.txt",
        "sha256": hashlib.sha256(data).hexdigest(),
        "subnet_count": len(networks),
        "address_count": sum(networks.values()),
        "duplicate_count": max(0, valid_lines - len(networks)),
        "invalid_count": invalid,
        "empty_or_comment_count": empty,
        "sample_subnets": list(networks)[:sample_subnets],
    }


async def preview_upload(file: UploadFile) -> dict:
    """Validate a CIDR feed without creating a TASK or materialising IP rows."""
    data = await file.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CIDR-файл больше 64 МиБ")
    return analyze_cidr_text(data.decode("utf-8", errors="replace"), file.filename or "scope.txt")


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
    masscan_output_format: str = Form("json"),
    masscan_extra_args: str | None = Form(None),
    masscan_workers: int = Form(1, ge=1, le=32),
    httpx_workers: int = Form(1, ge=1, le=64),
    httpx_rate_limit: int | None = Form(None, ge=1, le=100_000),
    httpx_extra_args: str | None = Form(None),
    httpx_threads: int | None = Form(None, ge=1, le=2_000),
    httpx_timeout: int | None = Form(None, ge=1, le=120),
    httpx_method: str | None = Form(None),
    httpx_probe: str | None = Form(None),
    httpx_follow_redirects: str | None = Form(None),
    httpx_follow_host_redirects: str | None = Form(None),
    httpx_status_code: str | None = Form(None),
    httpx_title: str | None = Form(None),
    httpx_content_type: str | None = Form(None),
    httpx_content_length: str | None = Form(None),
    httpx_web_server: str | None = Form(None),
    httpx_response_time: str | None = Form(None),
    httpx_include_response: str | None = Form(None),
    httpx_tech_detect: str | None = Form(None),
    httpx_response_size_to_read: int | None = Form(None, ge=1, le=10_485_760),
    httpx_response_size_to_save: int | None = Form(None, ge=1, le=10_485_760),
    httpx_paths: str | None = Form(None),
    httpx_schemes: str | None = Form(None),
    fingerprint_id: str = Form("fortinet-debug-scan-2.0"),
    httpx_match_strings: str | None = Form(None),
    httpx_match_policy: str | None = Form(None),
    httpx_status_codes: str | None = Form(None),
    httpx_title_contains: str | None = Form(None),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    redis_client: Redis = Depends(get_redis),
):
    if not name.strip():
        raise HTTPException(status_code=422, detail="Название TASK не может быть пустым")
    config = normalize_config({
        "masscan_chunk_size": masscan_chunk_size,
        "httpx_chunk_size": httpx_chunk_size,
        "masscan_ports": masscan_ports,
        "masscan_rate": masscan_rate,
        "masscan_output_format": masscan_output_format,
        "masscan_extra_args": masscan_extra_args,
        "masscan_workers": masscan_workers,
        "httpx_workers": httpx_workers,
        "httpx_rate_limit": httpx_rate_limit,
        "httpx_extra_args": httpx_extra_args,
        "httpx_threads": httpx_threads,
        "httpx_timeout": httpx_timeout,
        "httpx_method": httpx_method,
        "httpx_probe": httpx_probe,
        "httpx_follow_redirects": httpx_follow_redirects,
        "httpx_follow_host_redirects": httpx_follow_host_redirects,
        "httpx_status_code": httpx_status_code,
        "httpx_title": httpx_title,
        "httpx_content_type": httpx_content_type,
        "httpx_content_length": httpx_content_length,
        "httpx_web_server": httpx_web_server,
        "httpx_response_time": httpx_response_time,
        "httpx_include_response": httpx_include_response,
        "httpx_tech_detect": httpx_tech_detect,
        "httpx_response_size_to_read": httpx_response_size_to_read,
        "httpx_response_size_to_save": httpx_response_size_to_save,
        "httpx_paths": httpx_paths,
        "httpx_schemes": httpx_schemes,
        "fingerprint_id": fingerprint_id,
        "httpx_match_strings": httpx_match_strings,
        "httpx_match_policy": httpx_match_policy,
        "httpx_status_codes": httpx_status_codes,
        "httpx_title_contains": httpx_title_contains,
    })

    database_name = f"task_{uuid4().hex}"
    path, checksum, filename, cidrs = await save_upload(file, database_name)
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
            "masscan_ports": config["masscan_ports"], "masscan_output_format": masscan_output_format,
            "masscan_extra_args": json.dumps(config["masscan_extra_args"]), "masscan_workers": config["masscan_workers"],
            "httpx_workers": config["httpx_workers"],
            "httpx_config": json.dumps({**config["httpx"], "extra_args": config["httpx_extra_args"]}),
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


@router.post("/preview-cidr")
async def preview_cidr(file: UploadFile = File(...)):
    return await preview_upload(file)


@templates_router.get("", response_model=SearchTemplatePage)
async def list_templates(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(SearchTemplate).order_by(SearchTemplate.updated_at.desc(), SearchTemplate.id.desc()))).scalars().all()
    return {"items": rows, "total": len(rows)}


@templates_router.post("", response_model=SearchTemplateRead, status_code=status.HTTP_201_CREATED)
async def create_template(payload: SearchTemplateCreate, session: AsyncSession = Depends(get_session)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Название шаблона не может быть пустым")
    config = normalize_config(payload.config)
    existing = await session.scalar(select(SearchTemplate).where(SearchTemplate.name == name))
    if existing is not None:
        raise HTTPException(status_code=409, detail="Шаблон с таким названием уже существует")
    template = SearchTemplate(name=name, config=config)
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


@templates_router.put("/{template_id}", response_model=SearchTemplateRead)
async def update_template(template_id: int, payload: SearchTemplateCreate, session: AsyncSession = Depends(get_session)):
    template = await session.get(SearchTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Название шаблона не может быть пустым")
    conflict = await session.scalar(select(SearchTemplate).where(SearchTemplate.name == name, SearchTemplate.id != template_id))
    if conflict is not None:
        raise HTTPException(status_code=409, detail="Шаблон с таким названием уже существует")
    template.name = name
    template.config = normalize_config(payload.config)
    await session.commit()
    await session.refresh(template)
    return template


@templates_router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(template_id: int, session: AsyncSession = Depends(get_session)):
    template = await session.get(SearchTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    await session.delete(template)
    await session.commit()


@templates_router.post("/validate")
async def validate_template(payload: ConfigValidationRequest):
    config = normalize_config(payload.config)
    scope = None
    if payload.cidr_text is not None:
        scope = analyze_cidr_text(payload.cidr_text, payload.filename, payload.sample_subnets)
    return {
        "valid": True,
        "mode": "dry-run",
        "network_traffic": False,
        "message": "Конфигурация проверена. Masscan и HTTPX не запускались, пакеты не отправлялись.",
        "config": config,
        "commands": config["commands"],
        "scope": scope,
    }


@router.get("", response_model=TaskPage)
async def list_tasks(
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    total = await session.scalar(select(text("count(*)")).select_from(text("task_registry")))
    rows = (await session.execute(select(TaskRegistry).order_by(TaskRegistry.id.desc()).limit(limit).offset(offset))).scalars().all()
    items = []
    for row in rows:
        # Keep the list view consistent with /tasks/{id}: progress and counters
        # live in the physical TASK database, not only in task_registry.
        try:
            items.append(await get_task(row.id, session))
        except Exception:
            # A provisioning/failed TASK can briefly exist before its physical
            # database is available. Preserve the registry row in that case.
            item = TaskRead.model_validate(row).model_dump()
            if row.status == "completed":
                item["progress_percent"] = 100.0
            items.append(item)
    return {"items": items, "total": int(total or 0), "limit": limit, "offset": offset}


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
                 (SELECT count(*) FROM task_cidrs) AS cidrs,
                 (SELECT count(DISTINCT source_chunk_no) FROM task_targets) AS target_chunks,
                 (SELECT count(*) FROM task_targets WHERE masscan_status='pending') AS targets_pending,
                 (SELECT count(*) FROM task_targets WHERE masscan_status='open') AS targets_open,
                 (SELECT count(*) FROM task_targets WHERE masscan_status='closed') AS targets_closed,
                 (SELECT count(*) FROM task_targets WHERE masscan_status='failed') AS targets_failed,
                 (SELECT count(*) FROM task_open_ports) AS ports,
                 (SELECT count(*) FROM task_open_ports WHERE httpx_status='pending') AS ports_pending,
                 (SELECT count(*) FROM task_open_ports WHERE httpx_status='running') AS ports_running,
                 (SELECT count(*) FROM task_open_ports WHERE httpx_status='completed') AS ports_completed,
                 (SELECT count(*) FROM task_open_ports WHERE httpx_status='failed') AS ports_failed,
                 (SELECT count(*) FROM httpx_results) AS results,
                 (SELECT count(*) FROM httpx_results WHERE matched=true) AS matched_results,
                 (SELECT count(*) FROM task_targets WHERE masscan_status='open') AS open_targets,
                 (SELECT count(*) FROM task_chunks WHERE stage='masscan') AS masscan_total,
                 (SELECT count(*) FROM task_chunks WHERE stage='masscan' AND status='completed') AS masscan_completed,
                 (SELECT count(*) FROM task_chunks WHERE stage='masscan' AND status='running') AS masscan_running,
                 (SELECT count(*) FROM task_chunks WHERE stage='masscan' AND status='failed') AS masscan_failed,
                 (SELECT count(*) FROM task_chunks WHERE stage='httpx') AS httpx_total,
                 (SELECT count(*) FROM task_chunks WHERE stage='httpx' AND status='completed') AS httpx_completed,
                 (SELECT count(*) FROM task_chunks WHERE stage='httpx' AND status='running') AS httpx_running,
                 (SELECT count(*) FROM task_chunks WHERE stage='httpx' AND status='failed') AS httpx_failed,
                 (SELECT COALESCE(sum(packets_sent), 0) FROM task_chunks WHERE stage='masscan') AS masscan_packets_sent,
                 (SELECT COALESCE(sum(found_count), 0) FROM task_chunks WHERE stage='masscan') AS masscan_found_count,
                 (SELECT COALESCE(sum(response_count), 0) FROM task_chunks WHERE stage='httpx') AS httpx_response_count,
                 (SELECT COALESCE(sum(matched_count), 0) FROM task_chunks WHERE stage='httpx') AS httpx_matched_count,
                 (SELECT COALESCE(avg(rate_pps), 0) FROM task_chunks WHERE stage='masscan' AND rate_pps IS NOT NULL) AS masscan_avg_rate,
                 (SELECT count(*) FROM task_events) AS event_count,
                 (SELECT status FROM task_meta WHERE task_id=$1) AS pipeline_stage,
                 (SELECT COALESCE(sum(power(2::numeric, 32-masklen(network))), 0)::bigint FROM task_cidrs) AS scope_address_count,
                 (SELECT httpx_chunk_size FROM task_meta WHERE task_id=$1) AS httpx_chunk_size
        """, task_id)
        live_masscan = await connection.fetchrow("""
          SELECT chunk_no,status_tail,rate_pps,progress_percent,found_count
          FROM task_chunks WHERE stage='masscan' AND status='running'
          ORDER BY chunk_no LIMIT 1
        """)
        live_httpx = await connection.fetchrow("""
          SELECT chunk_no,item_count,status_tail,started_at
          FROM task_chunks WHERE stage='httpx' AND status='running'
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
    task.result_count = int(counts["matched_results"] or 0)
    await session.commit()
    await session.refresh(task)
    result = TaskRead.model_validate(task).model_dump()
    result["progress_percent"] = round(masscan_progress + httpx_progress, 2)
    result["masscan_chunks"] = {"total": masscan_total, "completed": int(counts["masscan_completed"] or 0), "running": int(counts["masscan_running"] or 0), "failed": int(counts["masscan_failed"] or 0)}
    result["httpx_chunks"] = {"total": httpx_total, "completed": int(counts["httpx_completed"] or 0), "running": int(counts["httpx_running"] or 0), "failed": int(counts["httpx_failed"] or 0)}
    result["masscan_live"] = dict(live_masscan) if live_masscan else {}
    result["httpx_live"] = dict(live_httpx) if live_httpx else {}
    result["stats"] = {
        "targets": total,
        "open_ports": int(counts["ports"] or 0),
        "results": int(counts["matched_results"] or 0),
        "masscan": {
            "packets_sent": int(counts["masscan_packets_sent"] or 0),
            "found_count": int(counts["masscan_found_count"] or 0),
            "average_rate_pps": round(float(counts["masscan_avg_rate"] or 0), 2),
        },
        "httpx": {
            "responses": int(counts["httpx_response_count"] or 0),
            "matched": int(counts["httpx_matched_count"] or 0),
        },
        "events": int(counts["event_count"] or 0),
    }
    result["pipeline"] = {
        "scope": {
            "status": str(counts["pipeline_stage"]),
            "address_count": int(counts["scope_address_count"] or 0),
            "cidrs": int(counts["cidrs"] or 0),
            "targets": total,
            "chunks": int(counts["target_chunks"] or 0),
            "pending": int(counts["targets_pending"] or 0),
            "open": int(counts["targets_open"] or 0),
            "closed": int(counts["targets_closed"] or 0),
            "failed": int(counts["targets_failed"] or 0),
        },
        "masscan": {
            "chunks": result["masscan_chunks"],
            "open_ports": int(counts["ports"] or 0),
            "found": int(counts["masscan_found_count"] or 0),
            "packets_sent": int(counts["masscan_packets_sent"] or 0),
            "average_rate_pps": round(float(counts["masscan_avg_rate"] or 0), 2),
        },
        "httpx": {
            "chunks": result["httpx_chunks"],
            "input_open_ports": int(counts["ports"] or 0),
            "pending": int(counts["ports_pending"] or 0),
            "running": int(counts["ports_running"] or 0),
            "completed": int(counts["ports_completed"] or 0),
            "failed": int(counts["ports_failed"] or 0),
            "responses": int(counts["httpx_response_count"] or 0),
            "matched": int(counts["httpx_matched_count"] or 0),
        },
        "results": {
            "responses": int(counts["httpx_response_count"] or 0),
            "confirmed": int(counts["matched_results"] or 0),
        },
    }
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


@router.get("/{task_id}/events")
async def get_events(
    task_id: int,
    after_id: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    tail: bool = False,
    session: AsyncSession = Depends(get_session),
):
    task = await task_or_404(task_id, session)
    connection = await connect(task.database_name)
    try:
        if tail and after_id == 0:
            rows = list(reversed(await connection.fetch(
                """SELECT id,stage,level,message,payload,created_at
                   FROM task_events ORDER BY id DESC LIMIT $1""", limit,
            )))
        else:
            rows = await connection.fetch(
                """SELECT id,stage,level,message,payload,created_at
                   FROM task_events WHERE id > $1 ORDER BY id LIMIT $2""",
                after_id, limit,
            )
    finally:
        await connection.close()
    items = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("payload"), str):
            try:
                item["payload"] = json.loads(item["payload"])
            except json.JSONDecodeError:
                pass
        items.append(item)
    return {"items": items, "last_id": int(items[-1]["id"]) if items else after_id}


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
async def export_results(task_id: int, session: AsyncSession = Depends(get_session)):
    task = await task_or_404(task_id, session)
    connection = await connect(task.database_name)
    try:
        rows = await connection.fetch("""SELECT DISTINCT protocol || '://' || host(ip) || ':' || port::text AS target
                                       FROM httpx_results WHERE matched=true ORDER BY target""")
    finally:
        await connection.close()
    return {"task_id": task.id, "count": len(rows), "targets": [row["target"] for row in rows]}
