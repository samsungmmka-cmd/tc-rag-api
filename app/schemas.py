"""Request / response models."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChunkOut(BaseModel):
    """What the validation agent reads. It uses `section` and `text`; the rest is for people."""
    id: str
    section: str
    text: str
    kind: Literal["text", "table"]
    chunk_index: int
    page: int | None = None
    part: str | None = None


class RulesResponse(BaseModel):
    campaign_id: int
    tc_version: int | None                      # null + empty chunks = campaign has no TC
    chunks: list[ChunkOut]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: int = Field(gt=0)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)


class SearchChunk(ChunkOut):
    score: float


class SearchResponse(BaseModel):
    campaign_id: int
    tc_version: int | None
    chunks: list[SearchChunk]


class ChunkPreview(BaseModel):
    chunk_index: int
    section: str
    kind: str
    tokens: int
    page: int | None = None
    part: str | None = None
    preview: str


class UploadResponse(BaseModel):
    campaign_id: int
    status: Literal["created", "replaced", "unchanged", "dry_run"]
    tc_version: int | None
    replaced_version: int | None = None
    file_name: str
    file_hash: str
    doc_title: str | None = None
    chunks: int
    table_chunks: int
    sections: list[str]
    embed_model: str | None = None
    duration_ms: int
    preview: list[ChunkPreview] | None = None   # dry_run only: every chunk in full


class CampaignInfo(BaseModel):
    campaign_id: int
    tc_version: int
    file_name: str
    file_type: str
    file_size: int
    file_hash: str
    doc_title: str | None = None
    chunks: int
    table_chunks: int
    sections: list[str]
    embed_model: str
    embed_dim: int
    uploaded_at: str
    previous_version: int | None = None


class CampaignDetail(CampaignInfo):
    stored_chunks: int
    search_ready: bool                          # false when embedded with another model
    chunk_preview: list[ChunkPreview]


class DeleteResponse(BaseModel):
    campaign_id: int
    deleted_version: int | None
    deleted_chunks: int
