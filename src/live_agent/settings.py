from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LIVE_", env_file=".env", extra="ignore")
    mode: Literal["shadow", "assist", "auto"] = "shadow"
    cdp_url: str = "http://127.0.0.1:9333"
    room_url: str = ""
    room_id: str = "room-1"
    account_id: str = "default"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    db_path: Path = Path("data/agent.sqlite3")
    allow_public: bool = False
    allow_bags: bool = False
    messages_per_minute: int = 4
    candidate_ttl: int = 45
    bump_ttl: int = 180
    max_public_per_hour: int = 20
    max_queue: int = 100
    max_bag_budget_cents: int = 10000
    max_bag_runs: int = 10
    model_timeout: float = 3.0
    embedding_model: str = ""
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_dimensions: int = Field(default=1024, ge=1, le=8192)
    vector_db_path: Path = Path("data/knowledge.sqlite3")
    semantic_threshold: float = 0.80
