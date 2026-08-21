from __future__ import annotations

import uvicorn

from cairn_core.config import settings


def main() -> None:
    cfg = settings()
    uvicorn.run(
        "cairn_router.app:app",
        host="0.0.0.0",  # noqa: S104 - in-cluster, fronted by a NetworkPolicy
        port=cfg.port,
        access_log=False,  # request bodies carry prompts; uvicorn logs nothing
    )


if __name__ == "__main__":
    main()
