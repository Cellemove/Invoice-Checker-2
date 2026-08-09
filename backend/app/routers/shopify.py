"""GET /shopify/orders — fetch Shopify orders by reference, and store listing."""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import AuthUser, get_optional_user
from ..config import get_settings
from ..shopify_auth import ShopifyAuthError, get_access_token, get_status
from ..shopify_client import ShopifyClient, ShopifyError

logger = logging.getLogger("invoice.shopify_route")
router = APIRouter(tags=["shopify"])


def _resolve_store(store: str | None):
    settings = get_settings()
    try:
        return settings.get_store(store)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.get("/shopify/stores")
async def list_stores(user: AuthUser = Depends(get_optional_user)) -> dict:
    settings = get_settings()
    return {"stores": settings.list_store_keys()}


@router.get("/shopify/auth/status")
async def auth_status(
    store: str | None = Query(None, description="Shopify store key."),
    user: AuthUser = Depends(get_optional_user),
) -> dict:
    """Report whether we hold a usable token for the store (no secrets exposed)."""
    return await get_status(_resolve_store(store))


@router.post("/shopify/auth/connect")
async def auth_connect(
    store: str | None = Query(None, description="Shopify store key."),
    force: bool = Query(False, description="Force a token refresh."),
    user: AuthUser = Depends(get_optional_user),
) -> dict:
    """Acquire/refresh an access token via the client credentials grant.

    Returns the resulting connection status — never the token value itself.
    """
    store_cfg = _resolve_store(store)
    try:
        await get_access_token(store_cfg, force_refresh=force)
    except ShopifyAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    return await get_status(store_cfg)


@router.get("/shopify/orders")
async def get_orders(
    refs: List[str] = Query(
        ...,
        description="Order numbers / invoice references to look up (repeatable).",
    ),
    store: str | None = Query(None, description="Shopify store key."),
    user: AuthUser = Depends(get_optional_user),
) -> dict:
    store_cfg = _resolve_store(store)
    client = ShopifyClient(store_cfg)
    try:
        orders = await client.fetch_orders_by_refs(refs)
    except (ShopifyError, ShopifyAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    found = {
        key: {
            "id": o.id,
            "name": o.name,
            "total_price": o.total_price,
            "line_items": [
                {
                    "sku": li.sku,
                    "title": li.title,
                    "quantity": li.quantity,
                    "unit_price": li.unit_price,
                    "line_total": li.line_total,
                }
                for li in o.line_items
            ],
        }
        for key, o in orders.items()
    }
    requested = {r.lstrip("#").strip() for r in refs if r}
    return {
        "store": store_cfg.key,
        "orders": found,
        "not_found": sorted(requested - set(found.keys())),
    }
