"""Hybrid retrieval: dense + BM25, fused with Reciprocal Rank Fusion.

Hybrid matters here specifically, more than in most RAG systems. Error
strings, metric names and service identifiers are exactly the tokens dense
retrieval fumbles: `NoBrokersAvailable` has no useful neighbourhood in
embedding space, but it is a perfect lexical match. Pure dense search on an
ops corpus underperforms, and it is the single most common retrieval mistake
in this domain.

RRF is used rather than score interpolation because cosine similarity and
`ts_rank_cd` are not on comparable scales, and any weighting between them is
a magic number that will be wrong on the next corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RRF_K = 60
CANDIDATES = 40


@dataclass(slots=True)
class Hit:
    chunk_id: int
    doc_id: str
    doc_title: str | None
    doc_url: str | None
    heading_path: list[str]
    content: str
    services: list[str]
    source_author: str | None = None
    dense_rank: int | None = None
    lexical_rank: int | None = None
    score: float = 0.0
    rerank_score: float | None = None

    @property
    def why(self) -> str:
        """How this hit was found. Shown to the agent so it can weigh a
        lexical-only match differently from one both retrievers agreed on."""
        parts = []
        if self.dense_rank is not None:
            parts.append(f"semantic #{self.dense_rank + 1}")
        if self.lexical_rank is not None:
            parts.append(f"keyword #{self.lexical_rank + 1}")
        return " + ".join(parts) or "unranked"


_DENSE_SQL = """
SELECT id, doc_id, doc_title, doc_url, heading_path, content, services, source_author
FROM runbook_chunks
WHERE (:services::text[] IS NULL OR services && :services::text[])
ORDER BY embedding <=> CAST(:qvec AS vector)
LIMIT :limit
"""

_LEXICAL_SQL = """
SELECT id, doc_id, doc_title, doc_url, heading_path, content, services, source_author,
       ts_rank_cd(tsv, query) AS rank
FROM runbook_chunks, websearch_to_tsquery('english', :q) query
WHERE tsv @@ query
  AND (:services::text[] IS NULL OR services && :services::text[])
ORDER BY rank DESC
LIMIT :limit
"""


async def hybrid_search(
    sess: AsyncSession,
    *,
    query: str,
    query_vector: list[float],
    services: list[str] | None = None,
    candidates: int = CANDIDATES,
) -> list[Hit]:
    params: dict[str, Any] = {
        "q": query,
        "qvec": _vector_literal(query_vector),
        "services": services or None,
        "limit": candidates,
    }

    dense_rows = (await sess.execute(text(_DENSE_SQL), params)).mappings().all()
    lexical_rows = (await sess.execute(text(_LEXICAL_SQL), params)).mappings().all()

    return fuse([dict(r) for r in dense_rows], [dict(r) for r in lexical_rows])


def fuse(dense: list[dict[str, Any]], lexical: list[dict[str, Any]]) -> list[Hit]:
    """Reciprocal Rank Fusion over two ranked lists."""
    hits: dict[int, Hit] = {}

    for rank, row in enumerate(dense):
        hit = _as_hit(row)
        hit.dense_rank = rank
        hit.score = 1 / (RRF_K + rank + 1)
        hits[hit.chunk_id] = hit

    for rank, row in enumerate(lexical):
        existing = hits.get(row["id"])
        if existing is None:
            existing = _as_hit(row)
            hits[existing.chunk_id] = existing
        existing.lexical_rank = rank
        existing.score += 1 / (RRF_K + rank + 1)

    return sorted(hits.values(), key=lambda h: h.score, reverse=True)


def _as_hit(row: dict[str, Any]) -> Hit:
    return Hit(
        chunk_id=row["id"],
        doc_id=row["doc_id"],
        doc_title=row.get("doc_title"),
        doc_url=row.get("doc_url"),
        heading_path=list(row.get("heading_path") or []),
        content=row["content"],
        services=list(row.get("services") or []),
        source_author=row.get("source_author"),
    )


def _vector_literal(vector: list[float]) -> str:
    """pgvector's text input format. Bound as a parameter, never spliced."""
    return "[" + ",".join(f"{v:.7g}" for v in vector) + "]"


_SIMILAR_SQL = """
SELECT t.id::text AS trajectory_id, t.query, t.answer, t.confidence, t.started_at,
       e.summary, e.services,
       1 - (e.embedding <=> CAST(:qvec AS vector)) AS similarity
FROM trajectory_embeddings e
JOIN trajectories t ON t.id = e.trajectory_id
WHERE t.state IN ('COMPLETE','PARTIAL')
  AND (:exclude::uuid IS NULL OR t.id <> :exclude::uuid)
ORDER BY e.embedding <=> CAST(:qvec AS vector)
LIMIT :limit
"""


async def similar_incidents(
    sess: AsyncSession,
    *,
    query_vector: list[float],
    limit: int = 5,
    exclude_trajectory: str | None = None,
    min_similarity: float = 0.55,
) -> list[dict[str, Any]]:
    """Past investigations that look like this one.

    Once a few hundred trajectories exist, "we have seen this before, here is
    what it was" beats fresh reasoning and costs one embedding call. The
    similarity floor matters: an unrelated incident presented as a precedent
    is worse than no precedent at all.
    """
    rows = (
        (
            await sess.execute(
                text(_SIMILAR_SQL),
                {
                    "qvec": _vector_literal(query_vector),
                    "limit": limit,
                    "exclude": exclude_trajectory,
                },
            )
        )
        .mappings()
        .all()
    )

    return [
        {
            "trajectory_id": r["trajectory_id"],
            "query": r["query"],
            "root_cause": r["summary"],
            "confidence": r["confidence"],
            "services": list(r["services"] or []),
            "when": r["started_at"].isoformat() if r["started_at"] else None,
            "similarity": round(float(r["similarity"]), 3),
        }
        for r in rows
        if float(r["similarity"]) >= min_similarity
    ]


def _self_check() -> None:
    dense = [{"id": i, "doc_id": f"d{i}", "content": f"c{i}", "heading_path": []} for i in range(5)]
    lexical = [
        {"id": 7, "doc_id": "d7", "content": "NoBrokersAvailable", "heading_path": []},
        {"id": 2, "doc_id": "d2", "content": "c2", "heading_path": []},
    ]
    fused = fuse(dense, lexical)

    ids = [h.chunk_id for h in fused]
    assert set(ids) == {0, 1, 2, 3, 4, 7}
    # a chunk both retrievers found outranks one only dense found at the same rank
    by_id = {h.chunk_id: h for h in fused}
    assert by_id[2].score > by_id[3].score
    # a lexical-only hit still surfaces: this is the NoBrokersAvailable case
    assert by_id[7].dense_rank is None and by_id[7].lexical_rank == 0
    assert by_id[7] in fused[:4], "lexical-only hits must not be buried"
    assert by_id[2].why == "semantic #3 + keyword #2"

    assert _vector_literal([1.0, 0.5]) == "[1,0.5]"
    print("search self-check ok")


if __name__ == "__main__":
    _self_check()
