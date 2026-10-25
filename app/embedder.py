"""Embeddings through any OpenAI-compatible /embeddings endpoint.

OpenAIEmbedder  OpenAI, Azure OpenAI (v1 path), vLLM, Ollama, LiteLLM, OpenRouter...
HashEmbedder    local and deterministic, no API key. Dev and tests only: it matches
                shared words, it does not understand meaning.
"""
import hashlib
import logging
import math
import re
from typing import Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import Settings

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Embedding provider failed or returned something unusable. Maps to HTTP 502."""


class Embedder(Protocol):
    model: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def aclose(self) -> None: ...


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class OpenAIEmbedder:
    def __init__(self, s: Settings):
        self.model, self.dim = s.embed_model, s.embed_dim
        self.batch_size = s.embed_batch_size
        self.max_retries = s.embed_max_retries
        self.send_dimensions = s.embed_send_dimensions
        headers = {"Authorization": f"Bearer {s.embed_api_key}"} if s.embed_api_key else {}
        self.http = httpx.AsyncClient(base_url=s.embed_base_url.rstrip("/"), timeout=s.embed_timeout,
                                      headers=headers)

    async def _batch(self, texts: list[str]) -> list[list[float]]:
        body: dict = {"model": self.model, "input": texts}
        if self.send_dimensions:
            body["dimensions"] = self.dim
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_retries),
                wait=wait_exponential(multiplier=1, max=20),
                retry=retry_if_exception(_retryable),
                reraise=True,
            ):
                with attempt:
                    resp = await self.http.post("/embeddings", json=body)
                    resp.raise_for_status()
                    data = resp.json()["data"]
        except httpx.HTTPStatusError as e:
            raise EmbeddingError(f"embedding API HTTP {e.response.status_code}: {e.response.text[:300]}") from e
        except httpx.TransportError as e:
            raise EmbeddingError(f"embedding API unreachable: {e!r}") from e
        except (KeyError, ValueError, TypeError) as e:
            raise EmbeddingError(f"unexpected embedding API response: {e!r}") from e

        vectors = [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]
        if len(vectors) != len(texts):
            raise EmbeddingError(f"embedding API returned {len(vectors)} vectors for {len(texts)} inputs")
        bad = {len(v) for v in vectors if len(v) != self.dim}
        if bad:
            raise EmbeddingError(f"model '{self.model}' returned {bad.pop()} dimensions, EMBED_DIM is {self.dim}")
        return vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out += await self._batch(texts[i:i + self.batch_size])
        return out

    async def aclose(self) -> None:
        await self.http.aclose()


class HashEmbedder:
    """Feature hashing of words and word pairs into a fixed-size unit vector."""

    def __init__(self, dim: int, model: str = "local-hash"):
        self.model, self.dim = model, dim

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        words = re.findall(r"[a-z0-9]+", text.lower())
        feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    async def aclose(self) -> None:
        return None


def build_embedder(s: Settings) -> Embedder:
    if s.embed_mode == "hash":
        log.warning("EMBED_MODE=hash: local hashing embedder, for development only")
        return HashEmbedder(s.embed_dim)
    return OpenAIEmbedder(s)
