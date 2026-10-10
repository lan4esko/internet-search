"""Версионируемый профиль запуска; сериализованный профиль сохраняется с планом."""
import os
import re
import regex
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator


HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
NUMERIC_FIELDS = {"status_code", "content_length", "port", "response_time"}


def canonical_ports(value: str) -> str:
    """Объединяет интервалы портов без разворачивания полного TCP диапазона."""
    ranges = []
    for part in value.split(","):
        part = part.strip()
        if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", part):
            raise ValueError("Порты задаются числами или диапазонами через запятую")
        bounds = [int(number) for number in part.split("-")]
        start, end = bounds[0], bounds[-1]
        if not 1 <= start <= end <= 65535:
            raise ValueError("Допустимы порты от 1 до 65535 и возрастающие диапазоны")
        ranges.append((start, end))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return ",".join(str(start) if start == end else f"{start}-{end}" for start, end in merged)


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def reject_nul_strings(cls, value):
        # JSONB PostgreSQL не поддерживает буквальный NUL в строках.
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str) and "\x00" in item:
                raise ValueError("Строки профиля не могут содержать символ NUL")
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend(item)
        return value


class MasscanProfile(ProfileModel):
    ports: str = Field(default="80,443", min_length=1, max_length=16384)
    rate: int = Field(default=1000, ge=1, le=100_000)
    output_format: Literal["json", "list", "xml"] = "json"
    wait: int | None = Field(default=10, ge=0, le=300)
    retries: int | None = Field(default=0, ge=0, le=10)
    adapter: str | None = Field(default=None, max_length=64)
    source_port: int | None = Field(default=None, ge=1, le=65535)
    seed: int | None = Field(default=None, ge=0, le=2**64 - 1)
    banners: bool = False

    @field_validator("wait", "retries", "adapter", "source_port", "seed", mode="before")
    @classmethod
    def blank_optional_value(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("ports")
    @classmethod
    def validate_ports(cls, value: str) -> str:
        normalized = canonical_ports(value)
        if len(normalized) > 1024:
            raise ValueError("Нормализованный список портов не может превышать 1024 символа")
        return normalized

    @field_serializer("seed", when_used="json")
    def serialize_seed(self, value: int | None) -> str | None:
        # JSON число uint64 теряет точность при чтении JavaScript Number.
        return str(value) if value is not None else None

    @field_validator("adapter")
    @classmethod
    def validate_adapter(cls, value: str | None):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}", value):
            raise ValueError("Некорректное имя сетевого интерфейса")
        return value


class HttpxProfile(ProfileModel):
    enabled: bool = True
    schemes: list[Literal["http", "https"]] = Field(default_factory=lambda: ["http", "https"], min_length=1, max_length=2)
    paths: list[str] | None = Field(default_factory=lambda: ["/"], min_length=1, max_length=64)
    method: Literal["GET", "HEAD"] | None = "GET"
    headers: list[str] = Field(default_factory=list, max_length=64)
    threads: int | None = Field(default=20, ge=1, le=200)
    rate_limit: int | None = Field(default=50, ge=1, le=10000)
    timeout: int | None = Field(default=10, ge=1, le=120)
    retries: int | None = Field(default=1, ge=0, le=10)
    follow_redirects: bool = False
    max_redirects: int | None = Field(default=3, ge=0, le=20)
    tech_detect: bool = True
    favicon: bool = False
    response_size_limit: int = Field(default=262144, ge=1024, le=10_485_760)

    @field_validator("method", "threads", "rate_limit", "timeout", "retries", "max_redirects", mode="before")
    @classmethod
    def blank_optional_value(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("paths", mode="before")
    @classmethod
    def blank_paths(cls, value):
        return None if value == [] or value == "" else value

    @field_validator("schemes")
    @classmethod
    def unique_schemes(cls, value):
        return list(dict.fromkeys(value))

    @field_validator("paths")
    @classmethod
    def relative_paths(cls, value):
        if value is None:
            return None
        for path in value:
            parsed = urlsplit(path)
            if "," in path:
                raise ValueError("Запятая в HTTP пути должна быть закодирована как %2C")
            if (not path.startswith("/") or path.startswith("//") or len(path) > 2048
                    or parsed.scheme or parsed.netloc or parsed.fragment
                    or any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in path)):
                raise ValueError("HTTP пути должны быть относительными, начинаться с / и не содержать управляющих символов")
        return list(dict.fromkeys(value))

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value):
        for header in value:
            name, separator, _ = header.partition(":")
            if (not separator or not HEADER_TOKEN.fullmatch(name) or len(header) > 8192
                    or any(ord(char) < 32 or ord(char) == 127 for char in header)):
                raise ValueError("Заголовок задаётся как Name: value без управляющих символов")
        return value


class IdentificationRule(ProfileModel):
    name: str = Field(min_length=1, max_length=255)
    field: Literal["status_code", "title", "body", "header", "server", "technology", "favicon_hash", "content_type", "content_length", "url", "ip", "port", "response_time"]
    operator: Literal["equals", "contains", "regex", "in", "gt", "lt"]
    value: str = Field(min_length=1, max_length=4096)
    case_sensitive: bool = False
    negate: bool = False
    header_name: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def valid_comparison(self):
        if not self.name.strip():
            raise ValueError("Название правила не может быть пустым")
        if self.field == "header" and (not self.header_name or not HEADER_TOKEN.fullmatch(self.header_name)):
            raise ValueError("Правило по заголовку требует корректный header_name")
        if self.operator in {"gt", "lt"} and self.field not in NUMERIC_FIELDS:
            raise ValueError("Операторы gt/lt применимы только к числовым полям")
        if self.field in NUMERIC_FIELDS and self.operator in {"equals", "in", "gt", "lt"}:
            values = self.value.split(",") if self.operator == "in" else [self.value]
            try:
                numbers = [float(item.strip()) for item in values]
                if not numbers or any(number != number or abs(number) == float("inf") for number in numbers):
                    raise ValueError
            except ValueError:
                raise ValueError("Значение числового правила должно быть конечным числом")
        if self.operator == "regex":
            try:
                regex.compile(self.value)
            except regex.error:
                raise ValueError("Некорректное регулярное выражение")
        return self


class IdentificationProfile(ProfileModel):
    mode: Literal["all", "any"] = "all"
    rules: list[IdentificationRule] = Field(default_factory=list, max_length=32)


class ScanProfile(ProfileModel):
    version: Literal[1] = 1
    name: str = Field(default="Стандартный запуск", min_length=1, max_length=255)
    masscan: MasscanProfile = Field(default_factory=MasscanProfile)
    httpx: HttpxProfile = Field(default_factory=HttpxProfile)
    identification: IdentificationProfile = Field(default_factory=IdentificationProfile)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if not value.strip():
            raise ValueError("Имя профиля не может быть пустым")
        return value.strip()

    @model_validator(mode="after")
    def validate_collected_evidence(self):
        rules = self.identification.rules
        if rules and not self.httpx.enabled:
            raise ValueError("Правила идентификации требуют включённой стадии HTTP")
        fields = {rule.field for rule in rules}
        if "favicon_hash" in fields and not self.httpx.favicon:
            raise ValueError("Правило favicon_hash требует httpx.favicon=true")
        if "technology" in fields and not self.httpx.tech_detect:
            raise ValueError("Правило technology требует httpx.tech_detect=true")
        if "body" in fields and self.httpx.method == "HEAD":
            raise ValueError("Правило body требует HTTP метод GET")
        return self


def default_profile() -> ScanProfile:
    return ScanProfile(masscan=MasscanProfile(
        ports=os.getenv("SCANNER_PORTS", "80,443"), rate=int(os.getenv("SCANNER_RATE", "1000")),
    ))


def effective_httpx_config(config: dict) -> dict:
    """Resolve native httpx v1.12 defaults for accounting, without adding flags.

    Explicit null means the CLI flag is absent. Missing JSON fields still acquire
    the original application defaults through ScanProfile, keeping v1 profiles
    backward compatible. These native values must track the pinned binary:
    https://github.com/projectdiscovery/httpx/blob/v1.12.0/runner/options.go
    """
    native_defaults = {"threads": 50, "rate_limit": 150, "timeout": 10,
                       "retries": 0, "method": "GET", "paths": ["/"], "max_redirects": 10}
    return {**config, **{key: default for key, default in native_defaults.items()
                        if config.get(key) is None}}
