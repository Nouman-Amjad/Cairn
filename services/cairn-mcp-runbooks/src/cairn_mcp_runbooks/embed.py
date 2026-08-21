"""Embedding and reranking clients.

Both are Text Embeddings Inference (TEI) servers running as sidecars on the
GPU node alongside vLLM. Same hardware, no extra cost, and no runbook text
ever leaves the cluster.

The reranker is optional. Without it retrieval still works — RRF over dense
and BM25 is a decent ordering on its own — it is just less precise. That
degradation is worth having: a reranker that is down should not take runbook
search down with it.
"""

from __future__ import annotations

import httpx

from cairn_core.config import EmbeddingSettings
from cairn_core.telemetry import get_logger

log = get_logger(__name__)


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    def __init__(self, cfg: EmbeddingSettings | None = None) -> None:
        self.cfg = cfg or EmbeddingSettings()
        self._client = httpx.AsyncClient(base_url=self.cfg.url, timeout=self.cfg.timeout_s)
        self._reranker = (
            httpx.AsyncClient(base_url=self.cfg.reranker_url, timeout=self.cfg.timeout_s)
            if self.cfg.reranker_url
            else None
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._reranker:
            await self._reranker.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for start in range(0, len(texts), self.cfg.batch_size):
            batch = texts[start : start + self.cfg.batch_size]
            try:
                resp = await self._client.post("/embed", json={"inputs": batch})
                resp.raise_for_status()
                vectors = resp.json()
            except httpx.HTTPError as exc:
                raise EmbeddingError(f"embedding service unavailable: {exc}") from exc
            if len(vectors) != len(batch):
                raise EmbeddingError("embedding service returned the wrong count")
            for vector in vectors:
                if len(vector) != self.cfg.dims:
                    # A dimension mismatch means the served model changed
                    # under us. Writing those vectors would silently corrupt
                    # the index for every future search.
                    raise EmbeddingError(
                        f"expected {self.cfg.dims} dims, got {len(vector)}; "
                        "the embedding model does not match the schema"
                    )
                out.append([float(v) for v in vector])
        return out

    async def embed_one(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]

    async def rerank(
        self, query: str, texts: list[str], top_k: int
    ) -> list[tuple[int, float]] | None:
        """Returns (original_index, score) pairs, or None if unavailable."""
        if not self._reranker or not texts:
            return None
        try:
            resp = await self._reranker.post(
                "/rerank", json={"query": query, "texts": texts, "raw_scores": False}
            )
            resp.raise_for_status()
            ranked = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("rerank_unavailable", error=str(exc))
            return None
        pairs = [(int(r["index"]), float(r["score"])) for r in ranked]
        pairs.sort(key=lambda p: p[1], reverse=True)
        return pairs[:top_k]
