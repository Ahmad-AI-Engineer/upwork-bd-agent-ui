from __future__ import annotations

import asyncio
import logging
import socket
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
for folder in ("orchestration", "ui"):
    sys.path.insert(0, str(BASE_DIR / folder))

import uvicorn

from job_poller import configure_logging, run_forever

logger = logging.getLogger("run")

UI_HOST = "127.0.0.1"
UI_PORT = 8000


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((host, port)) == 0


async def serve_ui() -> None:
    config = uvicorn.Config(
        "config_ui:app",
        host=UI_HOST,
        port=UI_PORT,
        log_level="warning",
        access_log=False,
    )
    await uvicorn.Server(config).serve()


async def main() -> None:
    configure_logging()

    if port_in_use(UI_HOST, UI_PORT):
        logger.error(
            "Port %d is already in use. The Docker stack is probably running, "
            "which also means a second poller would share the same Upwork token. "
            "Stop it first with: docker compose down",
            UI_PORT,
        )
        raise SystemExit(1)

    logger.info("Config UI: http://%s:%d", UI_HOST, UI_PORT)
    logger.info("Starting poller")
    await asyncio.gather(serve_ui(), run_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped")
