from __future__ import annotations

import uvicorn

from cairn_core.config import settings


def main() -> None:
    uvicorn.run(
        "cairn_orchestrator.app:app",
        host="0.0.0.0",  # noqa: S104 - in-cluster, NetworkPolicy fronted
        port=settings().port,
        access_log=False,
        # Graceful shutdown must outlive a running investigation, or a spot
        # reclaim kills an incident query mid-answer.
        timeout_graceful_shutdown=120,
    )


if __name__ == "__main__":
    main()
