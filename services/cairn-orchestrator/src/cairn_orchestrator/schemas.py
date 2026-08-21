"""JSON schemas for structured model output.

These are passed to the router, which turns them into grammar-constrained
decoding on the local tier (`guided_json`) and a prefill hint on the cloud
tier. The local path is the reason they exist: an 8B model free-forming JSON
fails validation often enough to be useless, and the same model constrained
by a grammar is reliable.

`additionalProperties: false` everywhere is deliberate. A model that invents
a field is a model whose output nobody is checking.
"""

from __future__ import annotations

from typing import Any

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                },
                "required": ["goal", "tools"],
                "additionalProperties": False,
            },
        },
        "hypotheses": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["steps"],
    "additionalProperties": False,
}

ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        # Free-form: the argument shape belongs to the tool's own schema,
        # which the MCP server validates. Duplicating it here would mean two
        # definitions drifting apart.
        "args": {"type": "object"},
        "why": {"type": "string"},
        "done": {"type": "boolean"},
    },
    "additionalProperties": False,
}

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "integer"},
                    "fact": {"type": "string"},
                },
                "required": ["fact"],
                "additionalProperties": False,
            },
        },
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["root_cause", "confidence"],
    "additionalProperties": False,
}

CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict"],
    "additionalProperties": False,
}
