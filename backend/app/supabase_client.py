"""Thin wrapper around the Supabase Python client for persistence.

Uses the service-role key on the server so it can write rows on behalf of
authenticated users. Row ownership is always stamped with the verified
`user_id` from the JWT — never trusted from the request body.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, List, Optional

from supabase import Client, create_client

from .config import get_settings

logger = logging.getLogger("invoice.supabase")

COMPARISON_TABLE = "comparison_runs"


@lru_cache(maxsize=1)
def get_supabase() -> Optional[Client]:
    """Return a cached service-role Supabase client, or None if unconfigured."""
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        logger.warning("Supabase not configured; persistence is disabled.")
        return None
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def insert_comparison_run(
    *,
    user_id: str,
    filename: str,
    matched_count: int,
    mismatch_count: int,
    missing_count: int,
) -> Optional[str]:
    """Persist a comparison run. Returns the new row id, or None on failure.

    Persistence failures are logged but never crash a comparison — the diff is
    still returned to the user.
    """
    client = get_supabase()
    if client is None:
        return None
    try:
        resp = (
            client.table(COMPARISON_TABLE)
            .insert(
                {
                    "user_id": user_id,
                    "filename": filename,
                    "matched_count": matched_count,
                    "mismatch_count": mismatch_count,
                    "missing_count": missing_count,
                }
            )
            .execute()
        )
        if resp.data:
            return str(resp.data[0].get("id"))
    except Exception as exc:  # noqa: BLE001 - persistence must never be fatal
        logger.error("Failed to insert comparison run: %s", exc)
    return None


def list_comparison_runs(*, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Return the most recent comparison runs for a user."""
    client = get_supabase()
    if client is None:
        return []
    try:
        resp = (
            client.table(COMPARISON_TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("run_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list comparison runs: %s", exc)
        return []
