"""Shopify access-token acquisition via the OAuth client credentials grant.

Flow (https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/client-credentials-grant):

    POST https://{shop}.myshopify.com/admin/oauth/access_token
    Content-Type: application/x-www-form-urlencoded
    grant_type=client_credentials&client_id=...&client_secret=...

    -> { "access_token": "...", "scope": "...", "expires_in": 86399 }

Tokens are cached on disk (see token_store) and transparently refreshed shortly
before they expire. A statically-provided SHOPIFY_ACCESS_TOKEN always wins and
skips the exchange entirely.
"""
from __future__ import annotations

import logging
import time

import httpx

from . import token_store
from .config import ShopifyStore

logger = logging.getLogger("invoice.shopify_auth")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_EXPIRY_MARGIN = 120  # refresh this many seconds before actual expiry


class ShopifyAuthError(RuntimeError):
    """Raised when an access token cannot be obtained."""


def _entry_valid(entry: token_store.TokenEntry | None) -> bool:
    return bool(
        entry
        and entry.get("access_token")
        and float(entry.get("expires_at", 0)) - _EXPIRY_MARGIN > time.time()
    )


async def get_access_token(store: ShopifyStore, *, force_refresh: bool = False) -> str:
    """Return a usable access token for the store, fetching/refreshing as needed."""
    if store.access_token:
        return store.access_token

    if not store.can_client_credentials:
        raise ShopifyAuthError(
            f"Store '{store.key}' has no SHOPIFY_ACCESS_TOKEN and no "
            f"SHOPIFY_API_KEY/SHOPIFY_API_SECRET for the client credentials grant."
        )

    if not force_refresh:
        cached = await token_store.get(store.key)
        if _entry_valid(cached):
            return cached["access_token"]  # type: ignore[index]

    return await _request_token(store)


async def _request_token(store: ShopifyStore) -> str:
    body = {
        "grant_type": "client_credentials",
        "client_id": store.api_key,
        "client_secret": store.api_secret,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(
                store.oauth_token_url,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise ShopifyAuthError(
                f"Network error contacting Shopify token endpoint for store "
                f"'{store.key}': {exc}"
            ) from exc

    if resp.status_code in (400, 401, 403):
        raise ShopifyAuthError(
            f"Shopify rejected the client credentials for store '{store.key}' "
            f"(HTTP {resp.status_code}). Verify SHOPIFY_API_KEY / "
            f"SHOPIFY_API_SECRET and that the app is installed on the store. "
            f"Response: {resp.text[:200]}"
        )
    if resp.status_code >= 400:
        raise ShopifyAuthError(
            f"Failed to obtain access token for store '{store.key}' "
            f"({resp.status_code}): {resp.text[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise ShopifyAuthError(
            f"Malformed token response from Shopify for store '{store.key}': {exc}"
        ) from exc

    token = data.get("access_token")
    if not token:
        raise ShopifyAuthError(
            f"Shopify token response for store '{store.key}' did not include an "
            f"access_token: {str(data)[:200]}"
        )

    now = time.time()
    expires_in = float(data.get("expires_in", 86399) or 86399)
    entry: token_store.TokenEntry = {
        "access_token": token,
        "scope": data.get("scope", "") or "",
        "expires_at": now + expires_in,
        "obtained_at": now,
    }
    await token_store.set(store.key, entry)
    logger.info(
        "Obtained Shopify token for store '%s' (scope=%s, expires_in=%ss)",
        store.key,
        entry["scope"],
        int(expires_in),
    )
    return token


async def get_status(store: ShopifyStore) -> dict:
    """Report the connection state for a store without exposing the token."""
    if store.access_token:
        return {
            "store": store.key,
            "connected": True,
            "source": "env_access_token",
            "scope": None,
            "expires_at": None,
            "can_connect": True,
        }
    if not store.can_client_credentials:
        return {
            "store": store.key,
            "connected": False,
            "source": "none",
            "scope": None,
            "expires_at": None,
            "can_connect": False,
            "detail": "No SHOPIFY_ACCESS_TOKEN and no API key/secret configured.",
        }

    cached = await token_store.get(store.key)
    if _entry_valid(cached):
        return {
            "store": store.key,
            "connected": True,
            "source": "client_credentials",
            "scope": cached.get("scope"),  # type: ignore[union-attr]
            "expires_at": cached.get("expires_at"),  # type: ignore[union-attr]
            "can_connect": True,
        }
    return {
        "store": store.key,
        "connected": False,
        "source": "client_credentials",
        "scope": None,
        "expires_at": cached.get("expires_at") if cached else None,
        "can_connect": True,
        "detail": "No valid token yet — connect to fetch one.",
    }
