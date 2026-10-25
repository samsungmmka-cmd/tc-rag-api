"""Settings from environment / .env."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- service
    app_name: str = "tc-rag-api"
    log_level: str = "INFO"
    # Bearer tokens. ADMIN: upload / delete (and reads). READ: the validation agent (rules, search).
    # Both empty = no auth (local dev only; a warning is logged at startup).
    api_admin_token: str = ""
    api_read_token: str = ""

    # --- Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "tc_chunks"
    qdrant_registry_collection: str = "tc_registry"
    qdrant_timeout: int = 30

    # --- Redis (per-campaign upload lock). Empty = in-process lock (single instance only).
    redis_url: str = ""
    lock_ttl_seconds: int = 600

    # --- Embeddings (any OpenAI-compatible /embeddings endpoint)
    # "openai" calls EMBED_BASE_URL; "hash" is a local deterministic embedder for dev / tests.
    embed_mode: Literal["openai", "hash"] = "openai"
    embed_base_url: str = "https://api.openai.com/v1"
    embed_api_key: str = ""
    embed_model: str = "text-embedding-3-small"
    embed_dim: int = 1536
    embed_send_dimensions: bool = False      # send "dimensions" in the request (text-embedding-3 only)
    embed_batch_size: int = 64
    embed_timeout: float = 60.0
    embed_max_retries: int = 4

    # --- Chunking (token counts are estimates: ~4 characters per token)
    chunk_max_tokens: int = 700              # text chunk size
    chunk_overlap_tokens: int = 0            # 0: the agent reads every chunk, overlap would repeat rules
    table_max_tokens: int = 900              # a table above this is split by rows, header repeated

    # --- Upload
    max_upload_mb: int = 20

    @model_validator(mode="after")
    def _check(self):
        if self.chunk_overlap_tokens >= self.chunk_max_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be smaller than CHUNK_MAX_TOKENS")
        return self

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_admin_token or self.api_read_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
