"""Local-file persistence for Shopify access tokens.

Tokens obtained via the client credentials grant are short-lived (24h), so we
cache them on disk to avoid re-fetching on every request and to survive backend
restarts. The file is keyed by logical store key and is gitignored — it holds
live access tokens and must never be committed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional, TypedDict

logger = logging.getLogger("invoice.tokenstore")

# On serverless (Vercel) the app directory is read-only; only /tmp is writable.
_BASE_DIR = (
    Path(tempfile.gettempdir())
    if os.getenv("VERCEL")
    else Path(__file__).resolve().parents[1]
)
_DEFAULT_PATH = _BASE_DIR / ".shopify_tokens.json"
_PATH = Path(os.getenv("SHOPIFY_TOKEN_STORE_PATH", str(_DEFAULT_PATH)))
_lock = asyncio.Lock()


class TokenEntry(TypedDict):
    access_token: str
    scope: str
    expires_at: float   # epoch seconds
    obtained_at: float  # epoch seconds


def _read_all() -> Dict[str, TokenEntry]:
    try:
        with _PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read token store at %s: %s", _PATH, exc)
        return {}


def _write_all(data: Dict[str, TokenEntry]) -> None:
    tmp = _PATH.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        tmp.replace(_PATH)  # atomic on the same filesystem
    except OSError as exc:
        logger.error("Could not write token store at %s: %s", _PATH, exc)


async def get(store_key: str) -> Optional[TokenEntry]:
    async with _lock:
        return _read_all().get(store_key)


async def set(store_key: str, entry: TokenEntry) -> None:
    async with _lock:
        data = _read_all()
        data[store_key] = entry
        _write_all(data)


async def delete(store_key: str) -> None:
    async with _lock:
        data = _read_all()
        if store_key in data:
            del data[store_key]
            _write_all(data)
