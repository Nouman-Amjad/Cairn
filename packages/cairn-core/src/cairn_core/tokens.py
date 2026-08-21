"""Token estimation.

Every enforcement point that matters (the 4k tool-response cap, the 60%
context high-water mark) needs a token count *before* the text is sent
anywhere. Asking a tokenizer service would add a network hop to the hot path,
so we estimate — and estimate high.

The bias is deliberate. Over-estimating truncates a response slightly early;
under-estimating overflows a context window mid-incident. Real usage numbers
come back from the provider afterwards and are what gets billed and recorded;
this is only ever used for admission control.
"""

from __future__ import annotations

import json
import math
from typing import Any

#: Conservative bytes-per-token. Real English on modern BPE tokenizers runs
#: ~3.8-4.2; log lines with UUIDs, base64 and stack traces run closer to 2.5.
#: We assume the log-line case because that is what tool results are.
_BYTES_PER_TOKEN = 3.0
_JSON_STRUCTURE_OVERHEAD = 1.15


def estimate(text: str) -> int:
    if not text:
        return 0
    return math.ceil(len(text.encode("utf-8", "ignore")) / _BYTES_PER_TOKEN)


def estimate_json(payload: Any) -> int:
    """JSON pays a structural tax: keys repeat on every element and braces,
    quotes and commas all tokenize separately."""
    raw = json.dumps(payload, default=str, separators=(",", ":"))
    return math.ceil(estimate(raw) * _JSON_STRUCTURE_OVERHEAD)


def estimate_messages(messages: list[dict[str, Any]], system: str | None = None) -> int:
    total = estimate(system or "")
    for msg in messages:
        content = msg.get("content", "")
        total += estimate(content if isinstance(content, str) else json.dumps(content))
        total += 4  # role wrapper tokens
    return total


def truncate_to(text: str, max_tokens: int, marker: str = "\n…[truncated]") -> str:
    """Cut a string to fit a token budget. Used only as the last line of
    defence; tools are expected to paginate rather than rely on this."""
    if estimate(text) <= max_tokens:
        return text
    budget_bytes = int(max_tokens * _BYTES_PER_TOKEN) - len(marker)
    return text.encode("utf-8", "ignore")[: max(0, budget_bytes)].decode("utf-8", "ignore") + marker


def _self_check() -> None:
    assert estimate("") == 0
    assert estimate("hello world") > 0
    # must over-estimate rather than under-estimate on dense machine text
    dense = "2024-01-01T03:02:11Z pod=checkout-api-7f9d id=8a3f-11ee-b2c4 status=503\n" * 50
    assert estimate(dense) > len(dense) / 4.5
    long = "x" * 10_000
    cut = truncate_to(long, 100)
    assert estimate(cut) <= 110, estimate(cut)
    assert cut.endswith("[truncated]")
    assert truncate_to("short", 100) == "short"
    assert estimate_json({"a": [1, 2, 3]}) >= estimate('{"a":[1,2,3]}')
    print("tokens self-check ok")


if __name__ == "__main__":
    _self_check()
