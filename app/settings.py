"""Application settings with process-environment and .env fallbacks."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VOUCHER_MAKER = "马云凤"


def _read_dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_DOTENV = _read_dotenv()


def setting(name: str, default: str = "") -> str:
    """Read a non-empty process value first, then the local .env file."""
    value = os.environ.get(name, "").strip()
    if not value:
        value = _DOTENV.get(name, "").strip()
    return value or default


def voucher_maker() -> str:
    return setting("VOUCHER_MAKER", DEFAULT_VOUCHER_MAKER)
