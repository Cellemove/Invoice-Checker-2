"""Application configuration and multi-store Shopify credential resolution.

Environment variables are the single source of truth for all secrets.
Nothing here is hard-coded; everything is read from the process environment
(typically populated from a local `.env` during development).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

# Load .env once at import time (no-op in production if the file is absent).
load_dotenv()

# Matches "<PREFIX>_SHOPIFY_STORE_DOMAIN" env vars, e.g.
#   CELLUMOVE_SHOPIFY_STORE_DOMAIN        -> prefix "CELLUMOVE"
#   CELLUMOVE_CZ_SHOPIFY_STORE_DOMAIN     -> prefix "CELLUMOVE_CZ"
_DOMAIN_RE = re.compile(r"^(?P<prefix>.+)_SHOPIFY_STORE_DOMAIN$")
# Two/three-letter region suffix on a prefix, e.g. "CELLUMOVE_CZ" -> "CZ".
_REGION_RE = re.compile(r"^(?P<base>.+)_(?P<region>[A-Za-z]{2,3})$")


def _normalise_domain(domain: str) -> str:
    """Turn a bare shop domain into a full https URL with no trailing slash."""
    d = (domain or "").strip()
    if not d:
        return ""
    if not d.startswith(("http://", "https://")):
        d = "https://" + d
    return d.rstrip("/")


@dataclass(frozen=True)
class ShopifyStore:
    """Credentials for a single Shopify store.

    A store is usable if it has EITHER a static `access_token` (pasted from the
    Shopify admin) OR an `api_key` + `api_secret` pair, which the app exchanges
    for a short-lived token via the OAuth client credentials grant.
    """

    key: str          # logical name used by the frontend (e.g. "default", "eu")
    store_url: str    # e.g. https://my-store.myshopify.com
    api_version: str
    access_token: str = ""   # optional static override (skips token fetch)
    api_key: str = ""        # client_id for the client credentials grant
    api_secret: str = ""     # client_secret for the client credentials grant

    @property
    def admin_base(self) -> str:
        base = self.store_url.rstrip("/")
        return f"{base}/admin/api/{self.api_version}"

    @property
    def oauth_token_url(self) -> str:
        base = self.store_url.rstrip("/")
        return f"{base}/admin/oauth/access_token"

    @property
    def can_client_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_token or self.can_client_credentials)


class Settings:
    """Lazily-validated application settings."""

    def __init__(self) -> None:
        # Supabase ---------------------------------------------------------
        self.supabase_url: str = os.getenv("SUPABASE_URL", "")
        self.supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")
        self.supabase_service_role_key: str = os.getenv(
            "SUPABASE_SERVICE_ROLE_KEY", ""
        )
        self.supabase_jwt_secret: str = os.getenv("SUPABASE_JWT_SECRET", "")

        # Auth -------------------------------------------------------------
        # Auth is disconnected by default for this internal tool. Set
        # AUTH_REQUIRED=true to re-enable strict JWT verification on every
        # endpoint. When disabled, requests are attributed to DEFAULT_USER_ID.
        self.auth_required: bool = os.getenv("AUTH_REQUIRED", "false").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        self.default_user_id: str = os.getenv(
            "DEFAULT_USER_ID", "00000000-0000-0000-0000-000000000000"
        )

        # Server -----------------------------------------------------------
        self.port: int = int(os.getenv("PORT", "8000"))
        self.cors_origins: List[str] = [
            o.strip()
            for o in os.getenv(
                "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            ).split(",")
            if o.strip()
        ]

        # Shopify ----------------------------------------------------------
        self.default_api_version: str = os.getenv("SHOPIFY_API_VERSION", "2024-07")
        self._stores: Dict[str, ShopifyStore] = self._discover_stores()

    # -- Shopify store discovery -------------------------------------------
    def _build_store(self, key: str, suffix: str) -> ShopifyStore | None:
        """Build a store from an env suffix ("" for the default store).

        Valid when a store URL is present AND either a static access token or an
        api_key/api_secret pair is configured.
        """
        url = os.getenv(f"SHOPIFY_STORE_URL{suffix}")
        if not url:
            return None
        access_token = os.getenv(f"SHOPIFY_ACCESS_TOKEN{suffix}", "") or ""
        api_key = os.getenv(f"SHOPIFY_API_KEY{suffix}", "") or ""
        api_secret = os.getenv(f"SHOPIFY_API_SECRET{suffix}", "") or ""
        if not access_token and not (api_key and api_secret):
            return None
        return ShopifyStore(
            key=key,
            store_url=url,
            api_version=os.getenv(
                f"SHOPIFY_API_VERSION{suffix}", self.default_api_version
            )
            or self.default_api_version,
            access_token=access_token,
            api_key=api_key,
            api_secret=api_secret,
        )

    def _domain_store(self, prefix: str, domain: str) -> Optional[Tuple[str, ShopifyStore]]:
        """Build a store from a "<PREFIX>_SHOPIFY_STORE_DOMAIN" entry.

        The store key is the region suffix on the prefix (e.g. CELLUMOVE_CZ ->
        "cz"); a prefix with no region maps to "default". Credentials are looked
        up on the prefix and then progressively shorter base prefixes, so a
        single shared app (CELLUMOVE_SHOPIFY_CLIENT_ID / _CLIENT_SECRET) covers
        every CELLUMOVE_<REGION> store.
        """
        url = _normalise_domain(domain)
        if not url:
            return None

        m = _REGION_RE.match(prefix)
        key = m.group("region").lower() if m else "default"

        # Try the full prefix, then each shorter base ("A_B_C" -> "A_B" -> "A").
        bases: List[str] = []
        p = prefix
        while True:
            bases.append(p)
            if "_" in p:
                p = p.rsplit("_", 1)[0]
            else:
                break

        api_key = api_secret = access_token = ""
        for base in bases:
            if not (api_key and api_secret):
                cid = (
                    os.getenv(f"{base}_SHOPIFY_CLIENT_ID")
                    or os.getenv(f"{base}_SHOPIFY_API_KEY")
                    or ""
                )
                csec = (
                    os.getenv(f"{base}_SHOPIFY_CLIENT_SECRET")
                    or os.getenv(f"{base}_SHOPIFY_API_SECRET")
                    or ""
                )
                if cid and csec:
                    api_key, api_secret = cid, csec
            if not access_token:
                access_token = os.getenv(f"{base}_SHOPIFY_ACCESS_TOKEN", "") or ""
            if (api_key and api_secret) or access_token:
                break

        if not access_token and not (api_key and api_secret):
            return None

        return key, ShopifyStore(
            key=key,
            store_url=url,
            api_version=self.default_api_version,
            access_token=access_token,
            api_key=api_key,
            api_secret=api_secret,
        )

    def _discover_stores(self) -> Dict[str, ShopifyStore]:
        """Discover every configured Shopify store from the environment.

        Two schemes are supported (each accepts a static ACCESS_TOKEN or an
        API key/secret pair for the client credentials grant):

          1. SHOPIFY_STORE_URL / SHOPIFY_STORE_URL_<KEY>
             (+ SHOPIFY_ACCESS_TOKEN / SHOPIFY_API_KEY / SHOPIFY_API_SECRET)

          2. <PREFIX>_SHOPIFY_STORE_DOMAIN  (bare shop domain), with credentials
             from <PREFIX>_SHOPIFY_CLIENT_ID / _CLIENT_SECRET, falling back to a
             shorter base prefix so one app can serve many regional stores.
        """
        stores: Dict[str, ShopifyStore] = {}

        # (1) Legacy SHOPIFY_STORE_URL[_KEY] scheme.
        default_store = self._build_store("default", "")
        if default_store:
            stores["default"] = default_store
        for env_key in os.environ:
            if not env_key.startswith("SHOPIFY_STORE_URL_"):
                continue
            suffix = env_key[len("SHOPIFY_STORE_URL") :]  # keeps leading "_<KEY>"
            key = suffix[1:].lower()
            store = self._build_store(key, suffix)
            if store:
                stores[key] = store

        # (2) <PREFIX>_SHOPIFY_STORE_DOMAIN scheme (overrides legacy on key clash).
        for env_key, value in os.environ.items():
            match = _DOMAIN_RE.match(env_key)
            if not match or not value.strip():
                continue
            built = self._domain_store(match.group("prefix"), value)
            if built:
                key, store = built
                stores[key] = store

        return stores

    def get_store(self, key: str | None = None) -> ShopifyStore:
        """Return a configured store by key, falling back to the default.

        Raises a ValueError (translated to HTTP 400/500 by the caller) when no
        matching store credentials are configured.
        """
        if not self._stores:
            raise ValueError(
                "No Shopify store credentials configured. Set SHOPIFY_STORE_URL "
                "and SHOPIFY_ACCESS_TOKEN (and optionally suffixed stores)."
            )
        if key is None or key == "" or key == "default":
            if "default" in self._stores:
                return self._stores["default"]
            # No explicit default — use the first configured store.
            return next(iter(self._stores.values()))
        store = self._stores.get(key.lower())
        if store is None:
            raise ValueError(
                f"Unknown Shopify store '{key}'. Configured stores: "
                f"{', '.join(self.list_store_keys()) or '(none)'}"
            )
        return store

    def list_store_keys(self) -> List[str]:
        return sorted(self._stores.keys())

    # -- Validation helpers ------------------------------------------------
    def require_supabase(self) -> None:
        missing = [
            name
            for name, val in (
                ("SUPABASE_URL", self.supabase_url),
                ("SUPABASE_SERVICE_ROLE_KEY", self.supabase_service_role_key),
                ("SUPABASE_JWT_SECRET", self.supabase_jwt_secret),
            )
            if not val
        ]
        if missing:
            raise ValueError(
                "Missing required Supabase environment variables: "
                + ", ".join(missing)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
