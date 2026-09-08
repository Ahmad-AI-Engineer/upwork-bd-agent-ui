from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env() -> None:
    load_dotenv(ENV_PATH, override=False)


load_env()
