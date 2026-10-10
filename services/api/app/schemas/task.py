from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    database_name: str
    source_filename: str
    source_sha256: str
    status: str
    config: dict
    target_count: int
    open_port_count: int
    result_count: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress_percent: float = 0.0
    masscan_chunks: dict = Field(default_factory=dict)
    httpx_chunks: dict = Field(default_factory=dict)
    masscan_live: dict = Field(default_factory=dict)


class TaskPage(BaseModel):
    items: list[dict]
    total: int
    limit: int
    offset: int


class TaskRetry(BaseModel):
    stage: str = Field(pattern="^(masscan|httpx)$")
    chunk_no: int = Field(ge=1)
