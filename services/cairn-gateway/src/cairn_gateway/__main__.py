from __future__ import annotations

import uvicorn

from cairn_core.config import settings


def main() -> None:
    uvicorn.run(
        "cairn_gateway.app:app",
        host="0.0.0.0",  # noqa: S104 - fronted by the ALB
        port=settings().port,
        access_log=False,  # query text is user data; it does not go to stdout
        timeout_graceful_shutdown=30,
    )


if __name__ == "__main__":
    main()
