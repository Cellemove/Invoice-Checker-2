"""Shopify Admin REST API client.

Responsibilities:
  * Fetch orders by order name/number (e.g. "#1001", "1001").
  * Build a SKU index of the store's product variants for inventory checks.

Designed to be resilient to rate limits (429 + Retry-After), network failures
and malformed responses. All public methods raise `ShopifyError` on
unrecoverable failures so callers can translate them to clean HTTP errors.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import httpx

from .config import ShopifyStore
from .shopify_auth import ShopifyAuthError, get_access_token

logger = logging.getLogger("invoice.shopify")

_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_MAX_RETRIES = 4


class ShopifyError(RuntimeError):
    """Raised for unrecoverable Shopify API failures."""


@dataclass
class ShopifyLineItem:
    sku: Optional[str]
    title: Optional[str]
    quantity: float
    unit_price: float

    @property
    def line_total(self) -> float:
        return round(self.quantity * self.unit_price, 2)


@dataclass
class ShopifyOrder:
    id: int
    name: str  # e.g. "#1001"
    total_price: float
    line_items: List[ShopifyLineItem] = field(default_factory=list)


def normalise_order_ref(ref: str) -> str:
    """Normalise an order reference for matching: strip '#' and whitespace."""
    return str(ref).strip().lstrip("#").strip()


class ShopifyClient:
    def __init__(self, store: ShopifyStore) -> None:
        self.store = store
        self._headers: Optional[dict] = None

    async def _ensure_headers(self) -> dict:
        """Resolve the access token (cached/fetched) and build request headers."""
        if self._headers is None:
            token = await get_access_token(self.store)
            self._headers = {
                "X-Shopify-Access-Token": token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        return self._headers

    async def _request(
        self, client: httpx.AsyncClient, path: str, params: Optional[dict] = None
    ) -> dict:
        """GET a path under the admin base with retry/backoff for 429 & 5xx."""
        url = f"{self.store.admin_base}/{path.lstrip('/')}"
        last_exc: Optional[Exception] = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.get(url, params=params, headers=self._headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(min(2 ** attempt, 8))
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2"))
                logger.warning("Shopify rate limited; retrying in %ss", retry_after)
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code in (401, 403):
                raise ShopifyError(
                    "Shopify authentication failed — check SHOPIFY_ACCESS_TOKEN "
                    f"for store '{self.store.key}' (HTTP {resp.status_code})."
                )
            if resp.status_code == 404:
                return {}
            if resp.status_code >= 500:
                last_exc = ShopifyError(f"Shopify server error {resp.status_code}")
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code >= 400:
                raise ShopifyError(
                    f"Shopify request failed ({resp.status_code}): {resp.text[:300]}"
                )
            # Redirect that wasn't followed → almost always a wrong host.
            if 300 <= resp.status_code < 400:
                location = resp.headers.get("Location", "?")
                raise ShopifyError(
                    f"Shopify redirected ({resp.status_code} -> {location}) for "
                    f"{url}. SHOPIFY_STORE_URL for store '{self.store.key}' should "
                    f"be the canonical https://<shop>.myshopify.com Admin API host, "
                    f"not a custom domain or password-protected storefront."
                )
            body = resp.text
            if not body.strip():
                raise ShopifyError(
                    f"Shopify returned an empty body (HTTP {resp.status_code}) for "
                    f"{url}. Check that SHOPIFY_STORE_URL points to your "
                    f"https://<shop>.myshopify.com Admin API host."
                )
            try:
                return resp.json()
            except ValueError as exc:
                ctype = resp.headers.get("Content-Type", "unknown")
                snippet = " ".join(body[:200].split())
                raise ShopifyError(
                    f"Expected JSON from Shopify but received '{ctype}' (HTTP "
                    f"{resp.status_code}) for {url}. First bytes: {snippet!r}. "
                    f"This is usually a wrong SHOPIFY_STORE_URL (use "
                    f"https://<shop>.myshopify.com) or a password-protected store."
                ) from exc

        raise ShopifyError(
            f"Shopify request to '{path}' failed after {_MAX_RETRIES} attempts: "
            f"{last_exc}"
        )

    @staticmethod
    def _parse_order(raw: dict) -> Optional[ShopifyOrder]:
        try:
            line_items = []
            for li in raw.get("line_items", []) or []:
                line_items.append(
                    ShopifyLineItem(
                        sku=(li.get("sku") or None),
                        title=li.get("title") or li.get("name"),
                        quantity=float(li.get("quantity") or 0),
                        unit_price=float(li.get("price") or 0),
                    )
                )
            return ShopifyOrder(
                id=int(raw.get("id")),
                name=str(raw.get("name", "")),
                total_price=float(raw.get("total_price") or 0),
                line_items=line_items,
            )
        except (TypeError, ValueError) as exc:
            logger.error("Skipping malformed Shopify order: %s", exc)
            return None

    async def fetch_orders_by_refs(
        self, refs: List[str]
    ) -> Dict[str, ShopifyOrder]:
        """Fetch orders for the given references.

        Returns a dict keyed by the *normalised* reference. References that
        resolve to no order are simply absent from the result.
        """
        wanted: Set[str] = {normalise_order_ref(r) for r in refs if r}
        if not wanted:
            return {}

        await self._ensure_headers()
        found: Dict[str, ShopifyOrder] = {}
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT, follow_redirects=True
        ) as client:
            # Query each distinct reference by name. Shopify's `name` filter
            # matches the human order number (with or without '#').
            async def _one(ref: str) -> None:
                for candidate in (f"#{ref}", ref):
                    data = await self._request(
                        client,
                        "orders.json",
                        params={"name": candidate, "status": "any", "limit": 50},
                    )
                    for raw in data.get("orders", []) or []:
                        order = self._parse_order(raw)
                        if order is None:
                            continue
                        key = normalise_order_ref(order.name)
                        found[key] = order
                        if key == ref:
                            return
                    if ref in found:
                        return

            # Bounded concurrency to stay friendly with Shopify rate limits.
            sem = asyncio.Semaphore(4)

            async def _guarded(ref: str) -> None:
                async with sem:
                    await _one(ref)

            await asyncio.gather(*(_guarded(r) for r in wanted))
        return found

    async def fetch_inventory_skus(self, max_products: int = 2000) -> Set[str]:
        """Return the set of SKUs across the store's product variants.

        Paginated via Shopify cursor-based `page_info`. Bounded by
        `max_products` to avoid unbounded crawls on very large catalogs.
        """
        await self._ensure_headers()
        skus: Set[str] = set()
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT, follow_redirects=True
        ) as client:
            params: Optional[dict] = {
                "limit": 250,
                "fields": "id,variants",
            }
            url_path = "products.json"
            seen = 0
            while True:
                resp = await client.get(
                    f"{self.store.admin_base}/{url_path}",
                    params=params,
                    headers=self._headers,
                )
                if resp.status_code == 429:
                    await asyncio.sleep(float(resp.headers.get("Retry-After", "2")))
                    continue
                if resp.status_code in (401, 403):
                    raise ShopifyError(
                        "Shopify authentication failed while reading inventory "
                        f"for store '{self.store.key}'."
                    )
                if resp.status_code >= 400:
                    raise ShopifyError(
                        f"Failed to read inventory ({resp.status_code}): "
                        f"{resp.text[:200]}"
                    )
                if 300 <= resp.status_code < 400:
                    raise ShopifyError(
                        f"Shopify redirected ({resp.status_code} -> "
                        f"{resp.headers.get('Location', '?')}) while reading "
                        f"inventory for store '{self.store.key}'. SHOPIFY_STORE_URL "
                        f"should be https://<shop>.myshopify.com."
                    )
                body = resp.text
                if not body.strip():
                    raise ShopifyError(
                        f"Shopify returned an empty body (HTTP {resp.status_code}) "
                        f"while reading inventory for store '{self.store.key}'. "
                        f"Check SHOPIFY_STORE_URL."
                    )
                try:
                    data = resp.json()
                except ValueError as exc:
                    ctype = resp.headers.get("Content-Type", "unknown")
                    snippet = " ".join(body[:200].split())
                    raise ShopifyError(
                        f"Expected JSON inventory from Shopify but received "
                        f"'{ctype}' (HTTP {resp.status_code}). First bytes: "
                        f"{snippet!r}. Verify SHOPIFY_STORE_URL is "
                        f"https://<shop>.myshopify.com."
                    ) from exc

                products = data.get("products", []) or []
                for product in products:
                    for variant in product.get("variants", []) or []:
                        sku = (variant.get("sku") or "").strip()
                        if sku:
                            skus.add(sku)
                seen += len(products)

                # Cursor pagination via the Link header.
                link = resp.headers.get("Link", "")
                next_info = _extract_page_info(link)
                if not next_info or seen >= max_products or not products:
                    break
                params = {"limit": 250, "page_info": next_info}
        return skus


def _extract_page_info(link_header: str) -> Optional[str]:
    """Pull the `page_info` cursor for rel="next" out of a Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start = part.find("page_info=")
            if start == -1:
                continue
            start += len("page_info=")
            end = part.find(">", start)
            if end == -1:
                end = part.find("&", start)
            return part[start:end] if end != -1 else part[start:]
    return None
