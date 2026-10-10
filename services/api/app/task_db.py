"""Provision and access one physical PostgreSQL database per TASK."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit

import asyncpg


TASK_DATABASE_RE = re.compile(r"^task_[0-9a-f]{32}$")


def control_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql+asyncpg://app:change_me@postgres:5432/internet_search").replace(
        "+asyncpg", ""
    )


def database_url(database_name: str) -> str:
    if not TASK_DATABASE_RE.fullmatch(database_name):
        raise ValueError("Недопустимое имя базы TASK")
    parts = urlsplit(control_url())
    return urlunsplit((parts.scheme, parts.netloc, "/" + database_name, parts.query, parts.fragment))


TASK_SCHEMA_SQL = """
CREATE TABLE task_meta (
  task_id BIGINT PRIMARY KEY,
  task_name TEXT NOT NULL,
  source_filename TEXT NOT NULL,
  source_sha256 CHAR(64) NOT NULL,
  masscan_chunk_size INTEGER NOT NULL CHECK (masscan_chunk_size > 0),
  httpx_chunk_size INTEGER NOT NULL CHECK (httpx_chunk_size > 0),
  masscan_rate INTEGER NOT NULL CHECK (masscan_rate > 0),
  masscan_ports TEXT NOT NULL,
  masscan_output_format TEXT NOT NULL DEFAULT 'json' CHECK (masscan_output_format IN ('json','list','xml')),
  masscan_extra_args JSONB NOT NULL DEFAULT '[]'::jsonb,
  masscan_workers INTEGER NOT NULL CHECK (masscan_workers > 0),
  httpx_workers INTEGER NOT NULL CHECK (httpx_workers > 0),
  httpx_config JSONB NOT NULL,
  status TEXT NOT NULL,
  next_httpx_chunk BIGINT NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  error_message TEXT
);
CREATE TABLE task_cidrs (
  network CIDR PRIMARY KEY,
  source_line INTEGER NOT NULL,
  original_value TEXT NOT NULL
);
CREATE TABLE task_targets (
  ip INET PRIMARY KEY,
  source_chunk_no BIGINT NOT NULL,
  masscan_status TEXT NOT NULL DEFAULT 'pending',
  ready_for_masscan BOOLEAN NOT NULL DEFAULT TRUE,
  masscan_started_at TIMESTAMPTZ,
  masscan_finished_at TIMESTAMPTZ,
  error_message TEXT
);
CREATE INDEX ix_task_targets_masscan_queue ON task_targets (source_chunk_no, masscan_status, ready_for_masscan);
CREATE TABLE task_chunks (
  stage TEXT NOT NULL CHECK (stage IN ('masscan', 'httpx')),
  chunk_no BIGINT NOT NULL,
  item_count BIGINT NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  process_id INTEGER,
  packets_sent BIGINT,
  rate_pps DOUBLE PRECISION,
  progress_percent DOUBLE PRECISION,
  found_count BIGINT NOT NULL DEFAULT 0,
  response_count BIGINT NOT NULL DEFAULT 0,
  matched_count BIGINT NOT NULL DEFAULT 0,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  raw_output_path TEXT,
  raw_output_bytes BIGINT,
  status_tail TEXT,
  error_message TEXT,
  PRIMARY KEY (stage, chunk_no)
);
CREATE TABLE task_open_ports (
  ip INET NOT NULL REFERENCES task_targets(ip) ON DELETE CASCADE,
  port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
  protocol TEXT NOT NULL DEFAULT 'tcp',
  masscan_open BOOLEAN NOT NULL DEFAULT TRUE,
  httpx_ready BOOLEAN NOT NULL DEFAULT TRUE,
  httpx_status TEXT NOT NULL DEFAULT 'pending',
  httpx_chunk_no BIGINT,
  service_name TEXT,
  banner TEXT,
  PRIMARY KEY (ip, port, protocol)
);
CREATE INDEX ix_task_open_ports_httpx_queue ON task_open_ports (httpx_status, httpx_ready, httpx_chunk_no);
CREATE TABLE httpx_results (
  id BIGSERIAL PRIMARY KEY,
  ip INET NOT NULL,
  port INTEGER NOT NULL,
  protocol TEXT NOT NULL,
  url TEXT NOT NULL,
  status_code INTEGER,
  title TEXT,
  webserver TEXT,
  headers JSONB,
  body_preview TEXT,
  matched BOOLEAN NOT NULL DEFAULT FALSE,
  match_details JSONB NOT NULL DEFAULT '[]'::jsonb,
  httpx_chunk_no BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (ip, port, protocol, url)
);
CREATE INDEX ix_httpx_results_matched ON httpx_results (matched);
CREATE TABLE task_events (
  id BIGSERIAL PRIMARY KEY,
  stage TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  message TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def connect(database_name: str) -> asyncpg.Connection:
    return await asyncpg.connect(database_url(database_name))


async def create_database(database_name: str) -> None:
    if not TASK_DATABASE_RE.fullmatch(database_name):
        raise ValueError("Недопустимое имя базы TASK")
    connection = await asyncpg.connect(control_url())
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", database_name)
        if not exists:
            await connection.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await connection.close()


async def drop_database(database_name: str) -> None:
    if not TASK_DATABASE_RE.fullmatch(database_name):
        raise ValueError("Недопустимое имя базы TASK")
    connection = await asyncpg.connect(control_url())
    try:
        await connection.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1 AND pid <> pg_backend_pid()", database_name)
        await connection.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
    finally:
        await connection.close()


async def initialize_database(database_name: str, values: dict) -> None:
    connection = await connect(database_name)
    try:
        await connection.execute(TASK_SCHEMA_SQL)
        await connection.execute(
            """INSERT INTO task_meta(task_id,task_name,source_filename,source_sha256,
               masscan_chunk_size,httpx_chunk_size,masscan_rate,masscan_ports,
               masscan_output_format,masscan_extra_args,masscan_workers,httpx_workers,httpx_config,status)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::text,$10::jsonb,$11,$12,$13,'provisioning')""",
            values["task_id"], values["name"], values["source_filename"], values["source_sha256"],
            values["masscan_chunk_size"], values["httpx_chunk_size"], values["masscan_rate"],
            values["masscan_ports"], values["masscan_output_format"], values["masscan_extra_args"],
            values["masscan_workers"], values["httpx_workers"], values["httpx_config"],
        )
    finally:
        await connection.close()
