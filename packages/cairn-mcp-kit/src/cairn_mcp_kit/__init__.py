"""Shared scaffolding for Cairn's MCP servers."""

from cairn_mcp_kit.guard import guarded
from cairn_mcp_kit.identity import current_claims, dev_claims, set_claims
from cairn_mcp_kit.results import deliver, error, not_configured, store
from cairn_mcp_kit.server import build, http_app, run
from cairn_mcp_kit.versioning import deprecated, deprecation_note, is_breaking

__all__ = [
    "build",
    "current_claims",
    "deliver",
    "deprecated",
    "deprecation_note",
    "dev_claims",
    "error",
    "guarded",
    "http_app",
    "is_breaking",
    "not_configured",
    "run",
    "set_claims",
    "store",
]
