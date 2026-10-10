"""Control database registry for isolated per-task PostgreSQL databases."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TaskRegistry(Base):
    __tablename__ = "task_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    database_name: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="provisioning")
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    target_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    open_port_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    result_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
