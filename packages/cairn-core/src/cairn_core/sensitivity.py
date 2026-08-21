"""Deterministic sensitivity classification.

Rules, in this order, highest wins:
  1. Source namespace carries `data-classification: restricted`.
  2. Content matches a PII pattern.
  3. Source is an internal service log/trace payload.
  4. Otherwise public.

A model never decides what is sensitive. This module is the whole decision,
and it is called at the tool-result boundary in the MCP servers, before the
payload has any chance to reach the router.
"""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Any, Final


class Sensitivity(IntEnum):
    """Ordered so that `max()` is the monotonic upgrade operation."""

    PUBLIC = 0
    INTERNAL = 1
    RESTRICTED = 2

    def __str__(self) -> str:  # what lands in JSON and DB columns
        return self.name.lower()

    @classmethod
    def parse(cls, value: str | Sensitivity | None) -> Sensitivity:
        if value is None:
            return cls.PUBLIC
        if isinstance(value, Sensitivity):
            return value
        try:
            return cls[value.strip().upper()]
        except KeyError as exc:
            raise ValueError(f"unknown sensitivity {value!r}") from exc


# Patterns are deliberately conservative: a false positive costs a slower
# local route, a false negative costs a data leak. The asymmetry decides the
# tuning direction every time.
_PII_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "email": re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    "ipv4_public": re.compile(
        r"\b(?!10\.)(?!127\.)(?!192\.168\.)(?!172\.(?:1[6-9]|2\d|3[01])\.)"
        r"(?:\d{1,3}\.){3}\d{1,3}\b"
    ),
    "credit_card": re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "us_ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone_e164": re.compile(r"(?<![\w.])\+[1-9]\d{7,14}(?![\w.])"),
    "bearer_token": re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}"),
    "aws_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

#: Fields on a structured payload whose *presence* implies user data even
#: when the value looks innocuous.
_PII_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "email",
        "user_email",
        "customer_id",
        "customer_email",
        "ssn",
        "phone",
        "phone_number",
        "address",
        "full_name",
        "date_of_birth",
        "dob",
        "card_number",
        "authorization",
        "cookie",
        "set-cookie",
        "password",
    }
)


#: Issuer prefixes, in the shapes that actually appear in payment logs.
#: Luhn alone is not enough: floating-point noise like 0.9000000000000001
#: yields a 16-digit Luhn-valid run, and treating that as a card number
#: classifies every metric result restricted, which routes 100% of traffic
#: local and quietly deletes the product.
_CARD_PREFIXES: Final[tuple[str, ...]] = (
    "4",  # Visa
    "34",
    "37",  # Amex
    "51",
    "52",
    "53",
    "54",
    "55",  # Mastercard
    "6011",
    "644",
    "645",
    "646",
    "647",
    "648",
    "649",
    "65",  # Discover
    "35",  # JCB
    "36",
    "38",
    "39",  # Diners
)


def _luhn(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    if not "".join(str(d) for d in digits).startswith(_CARD_PREFIXES):
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


#: Patterns that need a second opinion before they count. Without this, a
#: unix-millisecond timestamp or a float with a long mantissa reads as a card
#: number, every metric result classifies restricted, and the router sends
#: 100% of traffic local — a "safe" default that quietly deletes the product.
_VALIDATORS: Final[dict[str, Any]] = {"credit_card": _luhn}


def scan_text(text: str) -> list[str]:
    """Return the names of PII patterns found. Empty list means clean."""
    hits: list[str] = []
    for name, pat in _PII_PATTERNS.items():
        validator = _VALIDATORS.get(name)
        if validator is None:
            if pat.search(text):
                hits.append(name)
        elif any(validator(m.group(0)) for m in pat.finditer(text)):
            hits.append(name)
    return hits


def scan_fields(payload: object, _depth: int = 0) -> list[str]:
    """Walk a JSON-ish structure looking for known-sensitive key names."""
    if _depth > 6:
        return []
    hits: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in _PII_FIELD_NAMES:
                hits.append(f"field:{key}")
            hits.extend(scan_fields(value, _depth + 1))
    elif isinstance(payload, list):
        for item in payload[:200]:  # ponytail: sample lists; full walk if recall drops
            hits.extend(scan_fields(item, _depth + 1))
    return hits


def classify(
    *,
    text: str = "",
    payload: object = None,
    namespace_labels: dict[str, str] | None = None,
    source_kind: str = "unknown",
) -> tuple[Sensitivity, list[str]]:
    """Classify a tool result. Returns (level, reasons).

    `source_kind` is one of: metrics, logs, traces, deploys, docs, unknown.
    Aggregate metrics and deploy metadata are public by construction; log and
    trace payloads are internal unless they trip a PII rule.
    """
    reasons: list[str] = []
    level = Sensitivity.PUBLIC

    labels = namespace_labels or {}
    if labels.get("data-classification", "").lower() == "restricted":
        return Sensitivity.RESTRICTED, ["namespace:data-classification=restricted"]

    if source_kind in ("logs", "traces"):
        level = Sensitivity.INTERNAL
        reasons.append(f"source:{source_kind}")

    pattern_hits = scan_text(text) if text else []
    field_hits = scan_fields(payload) if payload is not None else []
    if pattern_hits or field_hits:
        level = Sensitivity.RESTRICTED
        reasons.extend(f"pii:{h}" for h in (*pattern_hits, *field_hits))

    if extra := _plugin_scan(text):
        level = Sensitivity.RESTRICTED
        reasons.extend(f"presidio:{e}" for e in extra)

    return level, reasons


def _plugin_scan(text: str) -> list[str]:
    """Optional Presidio pass. Absent in dev, present in the prod image.

    Deliberately soft: if the analyzer is not installed we run on regex alone
    rather than failing closed on every request, because failing closed here
    means the product does not work at all.
    """
    if not text:
        return []
    try:
        from cairn_core._presidio import analyze
    except ImportError:
        return []
    hits: list[str] = analyze(text)
    return hits


def redact(text: str) -> str:
    """Mask known PII. Used before anything is written to a log or a trace
    attribute. Never used to 'downgrade' a payload's classification."""
    for name, pat in _PII_PATTERNS.items():
        text = pat.sub(f"<{name}>", text)
    return text


def escalate(current: Sensitivity, incoming: Sensitivity) -> Sensitivity:
    """Sensitivity is monotonic for the life of a trajectory."""
    return max(current, incoming)


def _self_check() -> None:
    assert classify(text="all fine", source_kind="metrics")[0] is Sensitivity.PUBLIC
    assert classify(text="pod restarted", source_kind="logs")[0] is Sensitivity.INTERNAL
    lvl, why = classify(text="contact a@b.com", source_kind="logs")
    assert lvl is Sensitivity.RESTRICTED and any("email" in w for w in why), why
    assert (
        classify(
            text="nothing here",
            source_kind="metrics",
            namespace_labels={"data-classification": "restricted"},
        )[0]
        is Sensitivity.RESTRICTED
    )
    assert classify(payload={"user_email": "x"}, source_kind="logs")[0] is Sensitivity.RESTRICTED
    # private ranges must not trip the ipv4 rule, or every k8s log is restricted
    assert classify(text="dial 10.0.4.2:8080 failed", source_kind="logs")[0] is Sensitivity.INTERNAL
    assert escalate(Sensitivity.RESTRICTED, Sensitivity.PUBLIC) is Sensitivity.RESTRICTED
    assert Sensitivity.parse("internal") is Sensitivity.INTERNAL
    assert str(Sensitivity.RESTRICTED) == "restricted"
    assert redact("mail a@b.com now") == "mail <email> now"

    # A Luhn-valid card is PII; a unix timestamp and a long float are not.
    assert "credit_card" in scan_text("card 4242424242424242 charged")
    assert "credit_card" in scan_text("card 4242-4242-4242-4242 charged")
    assert scan_text("[[1700000060, 0.30000000000000004]]") == []
    assert scan_text("trace 1234567890123456789 span 987654321098765") == []
    # float artifacts that happen to be Luhn-valid must not read as cards
    assert scan_text("latency 0.9000000000000001 seconds") == []
    assert "credit_card" in scan_text("pan=5555555555554444")
    print("sensitivity self-check ok")


if __name__ == "__main__":
    _self_check()
