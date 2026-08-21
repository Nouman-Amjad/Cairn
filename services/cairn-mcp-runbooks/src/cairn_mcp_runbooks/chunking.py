"""Markdown-header-aware chunking.

Target 512 tokens with 64 tokens of overlap, and never split a code block or
a table. Runbooks are mostly commands and tables; a chunk that ends halfway
through a `kubectl` invocation retrieves badly and, worse, reads as complete.

Every chunk is prefixed with its heading path (`Checkout API > Runbooks >
High latency`). That costs about fifteen tokens and measurably improves
retrieval on chunks that are ambiguous standing alone — "restart the pod" is
useless without knowing whose pod.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cairn_core.tokens import estimate

TARGET_TOKENS = 512
OVERLAP_TOKENS = 64
MAX_TOKENS = 900  # a single indivisible block may exceed target, not this

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SERVICE_HINT = re.compile(r"\b([a-z0-9]+(?:-[a-z0-9]+){1,4})\b")


@dataclass(slots=True)
class Chunk:
    index: int
    heading_path: list[str]
    content: str
    token_count: int
    services: list[str] = field(default_factory=list)

    @property
    def enriched(self) -> str:
        """What actually gets embedded and returned."""
        if not self.heading_path:
            return self.content
        return " > ".join(self.heading_path) + "\n\n" + self.content


@dataclass(slots=True)
class _Block:
    """An atomic unit: a paragraph, a fenced code block, or a whole table."""

    text: str
    heading_path: list[str]
    splittable: bool


def _blocks(markdown: str) -> list[_Block]:
    path: list[str] = []
    blocks: list[_Block] = []
    buffer: list[str] = []
    in_fence = False
    fence_marker = ""
    in_table = False

    def flush(splittable: bool = True) -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip("\n")
        if text.strip():
            blocks.append(_Block(text, list(path), splittable))
        buffer = []

    for line in markdown.splitlines():
        fence = _FENCE.match(line)
        if fence and not in_fence:
            flush()
            in_fence, fence_marker = True, fence.group(1)
            buffer.append(line)
            continue
        if in_fence:
            buffer.append(line)
            if line.strip().startswith(fence_marker):
                in_fence = False
                flush(splittable=False)
            continue

        is_table_row = bool(_TABLE_ROW.match(line))
        if is_table_row and not in_table:
            flush()
            in_table = True
        elif in_table and not is_table_row:
            flush(splittable=False)
            in_table = False

        heading = _HEADING.match(line)
        if heading and not in_table:
            flush()
            level = len(heading.group(1))
            path = path[: level - 1]
            while len(path) < level - 1:
                path.append("")
            path.append(heading.group(2).strip())
            continue

        if not line.strip() and not in_table:
            flush()
            continue
        buffer.append(line)

    flush(splittable=not in_table)
    return blocks


def chunk_markdown(markdown: str, *, known_services: set[str] | None = None) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[str] = []
    current_path: list[str] = []
    current_tokens = 0

    def emit() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        text = "\n\n".join(current).strip()
        if text:
            chunks.append(
                Chunk(
                    index=len(chunks),
                    heading_path=[h for h in current_path if h],
                    content=text,
                    token_count=estimate(text),
                    services=detect_services(text, known_services),
                )
            )
        current = []
        current_tokens = 0

    for block in _blocks(markdown):
        block_tokens = estimate(block.text)

        if block.heading_path != current_path and current:
            emit()
        current_path = block.heading_path

        if block_tokens > MAX_TOKENS and block.splittable:
            emit()
            for piece in _split_paragraph(block.text):
                chunks.append(
                    Chunk(
                        index=len(chunks),
                        heading_path=[h for h in block.heading_path if h],
                        content=piece,
                        token_count=estimate(piece),
                        services=detect_services(piece, known_services),
                    )
                )
            continue

        if current_tokens + block_tokens > TARGET_TOKENS and current:
            tail = current[-1] if estimate(current[-1]) <= OVERLAP_TOKENS else None
            emit()
            # Carry the previous paragraph forward so a procedure split across
            # a boundary still has its preamble.
            if tail:
                current.append(tail)
                current_tokens += estimate(tail)

        current.append(block.text)
        current_tokens += block_tokens

    emit()
    return chunks


def _split_paragraph(text: str) -> list[str]:
    """Last resort for a single oversized prose block: split on sentences."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    pieces: list[str] = []
    buffer: list[str] = []
    total = 0
    for sentence in sentences:
        cost = estimate(sentence)
        if total + cost > TARGET_TOKENS and buffer:
            pieces.append(" ".join(buffer))
            buffer, total = [], 0
        buffer.append(sentence)
        total += cost
    if buffer:
        pieces.append(" ".join(buffer))
    return pieces


def detect_services(text: str, known: set[str] | None) -> list[str]:
    """Tag a chunk with the services it mentions.

    Restricted to a known inventory when one is available. Free-form
    extraction on an ops corpus tags every chunk with `content-type` and
    `x-request-id`, which makes the service filter useless.
    """
    if not known:
        return []
    found = {m.group(1) for m in _SERVICE_HINT.finditer(text.lower())}
    return sorted(found & known)


def _self_check() -> None:
    doc = """# Checkout API

Owned by the payments team.

## Runbooks

### High latency

Check the p95 first:

```bash
kubectl -n checkout get pods
promtool query instant http_p95{app="checkout-api"}
```

| symptom | cause | action |
|---|---|---|
| 503s | pool exhausted | scale up |
| slow | GC pause | roll back |

If the deploy timeline shows a release inside the window, roll it back before
investigating further.
"""
    chunks = chunk_markdown(doc, known_services={"checkout-api", "payments-api"})
    assert chunks, "produced no chunks"

    joined = "\n".join(c.content for c in chunks)
    assert "kubectl -n checkout get pods" in joined
    # a code fence must survive intact inside exactly one chunk
    holders = [c for c in chunks if "```bash" in c.content]
    assert len(holders) == 1
    assert holders[0].content.count("```") == 2, "code fence was split"
    # the table must not be broken across chunks
    table_holders = [c for c in chunks if "| symptom |" in c.content]
    assert len(table_holders) == 1
    assert "| slow | GC pause | roll back |" in table_holders[0].content

    deep = [c for c in chunks if "High latency" in c.heading_path]
    assert deep and deep[0].heading_path == ["Checkout API", "Runbooks", "High latency"]
    assert deep[0].enriched.startswith("Checkout API > Runbooks > High latency")
    assert any("checkout-api" in c.services for c in chunks)

    big = chunk_markdown("# T\n\n" + ("sentence about pods. " * 800))
    assert len(big) > 1
    assert all(c.token_count <= MAX_TOKENS for c in big), [c.token_count for c in big]
    print("chunking self-check ok")


if __name__ == "__main__":
    _self_check()
