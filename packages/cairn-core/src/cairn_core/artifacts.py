"""Reference-and-fetch for oversized tool results.

A `query_logs` call can match 40,000 tokens of text against a 16k window. The
fix is not to summarize harder; it is to never put the payload in the window
at all. The full result goes to S3, and the agent receives a handle plus the
things it actually needed: a count, facet distributions, and a small sample.

Facets are what make this work. "1,247 matches, 89% are 503s from one pod"
answers the question without a single raw line entering the context.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cairn_core.config import S3Settings
from cairn_core.sensitivity import Sensitivity
from cairn_core.tokens import estimate_json

MAX_SAMPLE_ROWS = 5
MAX_FACET_VALUES = 5


@dataclass(slots=True)
class ArtifactRef:
    artifact_id: str
    s3_key: str
    byte_size: int
    row_count: int
    sensitivity: Sensitivity
    facets: dict[str, dict[str, int]] = field(default_factory=dict)


def new_artifact_id() -> str:
    return f"art_{uuid.uuid4().hex[:24]}"


def compute_facets(rows: list[dict[str, Any]], fields: list[str]) -> dict[str, dict[str, int]]:
    """Value distribution per field, top-N by count.

    Only scalar fields are faceted. Faceting a free-text `message` field gives
    1,247 buckets of size 1, which is noise that costs tokens.
    """
    out: dict[str, dict[str, int]] = {}
    for name in fields:
        counter: Counter[str] = Counter()
        for row in rows:
            value = row.get(name)
            if value is None or isinstance(value, (dict, list)):
                continue
            counter[str(value)] += 1
        if counter:
            out[name] = dict(counter.most_common(MAX_FACET_VALUES))
    return out


def describe(
    rows: list[dict[str, Any]],
    *,
    facet_fields: list[str],
    total_matched: int | None = None,
    sample_key: str | None = None,
) -> dict[str, Any]:
    """Build the small object the agent sees in place of the payload."""
    total = total_matched if total_matched is not None else len(rows)
    facets = compute_facets(rows, facet_fields)
    sample = [(row.get(sample_key) if sample_key else row) for row in rows[:MAX_SAMPLE_ROWS]]
    return {
        "total_matched": total,
        "facets": facets,
        "sample": sample,
        "summary": _prose(total, facets),
    }


def _prose(total: int, facets: dict[str, dict[str, int]]) -> str:
    """One sentence of natural language. Models weight prose more reliably
    than a bare dict, and it costs ~20 tokens."""
    if not total:
        return "No matches."
    parts = [f"{total:,} matches."]
    for name, values in list(facets.items())[:2]:
        top, count = next(iter(values.items()))
        pct = round(100 * count / total)
        if pct >= 25:
            parts.append(f"{pct}% have {name}={top}.")
    return " ".join(parts)


class ArtifactStore:
    """S3-backed, with an in-memory backend for tests and local dev.

    Bodies are gzipped JSON-lines. Nothing here ever returns raw bytes to a
    caller that did not ask for a specific slice.
    """

    def __init__(self, cfg: S3Settings | None = None) -> None:
        self.cfg = cfg or S3Settings()
        self._memory: dict[str, bytes] = {}
        self._client: Any | None = None

    @property
    def in_memory(self) -> bool:
        return self.cfg.bucket == ":memory:"

    def _s3(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3", region_name=self.cfg.region, endpoint_url=self.cfg.endpoint_url
            )
        return self._client

    async def put(
        self,
        rows: list[dict[str, Any]],
        *,
        tool: str,
        trajectory_id: str | None,
        sensitivity: Sensitivity,
        facet_fields: list[str] | None = None,
    ) -> ArtifactRef:
        artifact_id = new_artifact_id()
        day = datetime.now(UTC).strftime("%Y/%m/%d")
        key = f"artifacts/{day}/{trajectory_id or 'unbound'}/{artifact_id}.jsonl.gz"
        body = gzip.compress(b"\n".join(json.dumps(r, default=str).encode() for r in rows))

        if self.in_memory:
            self._memory[key] = body
        else:
            extra: dict[str, Any] = {
                "ServerSideEncryption": "aws:kms" if self.cfg.kms_key_id else "AES256"
            }
            if self.cfg.kms_key_id:
                extra["SSEKMSKeyId"] = self.cfg.kms_key_id
            await asyncio.to_thread(
                self._s3().put_object,
                Bucket=self.cfg.bucket,
                Key=key,
                Body=body,
                ContentType="application/x-ndjson",
                ContentEncoding="gzip",
                **extra,
            )

        return ArtifactRef(
            artifact_id=artifact_id,
            s3_key=key,
            byte_size=len(body),
            row_count=len(rows),
            sensitivity=sensitivity,
            facets=compute_facets(rows, facet_fields or []),
        )

    async def get_rows(self, s3_key: str) -> list[dict[str, Any]]:
        if self.in_memory:
            body = self._memory[s3_key]
        else:
            obj = await asyncio.to_thread(self._s3().get_object, Bucket=self.cfg.bucket, Key=s3_key)
            body = await asyncio.to_thread(obj["Body"].read)
        raw = gzip.decompress(body).decode("utf-8", "replace")
        return [json.loads(line) for line in raw.splitlines() if line]


def apply_filter(rows: list[dict[str, Any]], expr: str | None) -> list[dict[str, Any]]:
    """Drill-down filter for `fetch_artifact`.

    Grammar is deliberately tiny: `field=value`, `field!=value`, `field~regex`,
    space-separated and ANDed. No eval, no expression language, no operator
    the model can smuggle a side effect through.
    """
    if not expr:
        return rows
    clauses: list[tuple[str, str, str]] = []
    for term in expr.split():
        match = re.fullmatch(r"([\w.]+)(!=|=|~)(.+)", term)
        if not match:
            raise ValueError(
                f"bad filter term {term!r}; use field=value, field!=value or field~regex"
            )
        clauses.append((match.group(1), match.group(2), match.group(3)))

    def keep(row: dict[str, Any]) -> bool:
        for name, op, want in clauses:
            got = str(row.get(name, ""))
            if op == "=" and got != want:
                return False
            if op == "!=" and got == want:
                return False
            if op == "~" and not re.search(want, got):
                return False
        return True

    return [r for r in rows if keep(r)]


def page(
    rows: list[dict[str, Any]], *, limit: int, cursor: str | None, max_tokens: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Offset paging with a hard token ceiling.

    The token ceiling wins over `limit`: a caller asking for 500 rows of
    stack traces gets however many fit in 4,000 tokens and a cursor.
    """
    start = int(cursor) if cursor else 0
    out: list[dict[str, Any]] = []
    used = 0
    idx = start
    while idx < len(rows) and len(out) < limit:
        cost = estimate_json(rows[idx])
        if out and used + cost > max_tokens:
            break
        out.append(rows[idx])
        used += cost
        idx += 1
    next_cursor = str(idx) if idx < len(rows) else None
    return out, next_cursor


async def _self_check() -> None:
    rows = [{"pod": "checkout-api-7f9", "status": 503, "msg": f"error {i}"} for i in range(100)] + [
        {"pod": "checkout-api-2ab", "status": 200, "msg": "ok"} for _ in range(10)
    ]

    facets = compute_facets(rows, ["pod", "status"])
    assert facets["status"]["503"] == 100
    assert facets["pod"]["checkout-api-7f9"] == 100

    desc = describe(rows, facet_fields=["pod", "status"])
    assert desc["total_matched"] == 110
    assert len(desc["sample"]) == MAX_SAMPLE_ROWS
    assert "91% have pod=" in desc["summary"], desc["summary"]

    store = ArtifactStore(S3Settings(bucket=":memory:"))
    ref = await store.put(
        rows,
        tool="query_logs",
        trajectory_id="t1",
        sensitivity=Sensitivity.INTERNAL,
        facet_fields=["pod"],
    )
    assert ref.row_count == 110
    back = await store.get_rows(ref.s3_key)
    assert back == rows

    only_503 = apply_filter(back, "status=503")
    assert len(only_503) == 100
    assert len(apply_filter(back, "status!=503")) == 10
    assert len(apply_filter(back, "pod~7f9 status=503")) == 100
    try:
        apply_filter(back, "__import__('os')")
        raise AssertionError("filter grammar must reject arbitrary expressions")
    except ValueError:
        pass

    first, cursor = page(back, limit=1000, cursor=None, max_tokens=200)
    assert cursor is not None and len(first) < 110, "token ceiling must beat limit"
    second, _ = page(back, limit=1000, cursor=cursor, max_tokens=200)
    assert second[0] == back[len(first)]
    print("artifacts self-check ok")


if __name__ == "__main__":
    asyncio.run(_self_check())
