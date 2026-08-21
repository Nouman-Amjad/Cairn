from __future__ import annotations

import uvicorn

from cairn_core.config import settings


def main() -> None:
    uvicorn.run(
        "cairn_approval.app:app",
        host="0.0.0.0",  # noqa: S104 - in-cluster, NetworkPolicy fronted
        port=settings().port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
