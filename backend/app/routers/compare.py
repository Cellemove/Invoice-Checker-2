"""POST /compare — full three-part reconciliation, plus GET /history."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import AuthUser, get_optional_user
from ..comparison import (
    merge_results,
    reconcile_against_quotation,
    reconcile_by_order,
    resolve_store_key,
)
from ..config import ShopifyStore, get_settings
from ..gdrive import DriveError
from ..quotation import QuotationError, get_quotation, get_quotation_by_drive_id
from ..models import (
    CompareRequest,
    ComparisonResult,
    ComparisonRun,
    InvoiceLineItem,
)
from ..shopify_auth import ShopifyAuthError
from ..shopify_client import ShopifyClient, ShopifyError, normalise_order_ref
from ..supabase_client import insert_comparison_run, list_comparison_runs

logger = logging.getLogger("invoice.compare")
router = APIRouter(tags=["compare"])


async def _reconcile_store(
    store_cfg: ShopifyStore,
    items: List[InvoiceLineItem],
    payload: CompareRequest,
    invoice_total: float | None,
) -> ComparisonResult:
    """Fetch a store's orders + inventory and reconcile the given items."""
    refs = sorted({normalise_order_ref(it.order_id) for it in items if it.order_id})
    client = ShopifyClient(store_cfg)
    orders = await client.fetch_orders_by_refs(refs) if refs else {}
    inventory_skus = await client.fetch_inventory_skus()
    return reconcile_by_order(
        invoice_items=items,
        orders=orders,
        inventory_skus=inventory_skus,
        invoice_total=invoice_total,
        store_key=store_cfg.key,
        price_tolerance=payload.price_tolerance,
        check_prices=payload.check_prices,
    )


def _missing_result(
    items: List[InvoiceLineItem], store_key: str | None, note: str
) -> ComparisonResult:
    """Order-level result whose orders are all 'missing in Shopify' with a note.

    Used for regions that route to no configured store, or stores that errored.
    """
    return reconcile_by_order(
        invoice_items=items,
        orders={},
        inventory_skus=set(),
        store_key=store_key,
        missing_note=note,
    )


async def _compare_single(payload: CompareRequest, settings) -> ComparisonResult:
    try:
        store_cfg = settings.get_store(payload.store)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    try:
        return await _reconcile_store(
            store_cfg, payload.line_items, payload, payload.invoice_total
        )
    except (ShopifyError, ShopifyAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Shopify error: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Reconciliation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reconciliation failed: {exc}",
        ) from exc


async def _compare_auto(payload: CompareRequest, settings) -> ComparisonResult:
    """Route each line to the store matching its region and merge results."""
    store_keys = settings.list_store_keys()
    if not store_keys:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Shopify stores are configured.",
        )

    groups: Dict[str, List[InvoiceLineItem]] = defaultdict(list)
    unrouted: Dict[str, List[InvoiceLineItem]] = defaultdict(list)
    for it in payload.line_items:
        skey = resolve_store_key(it.source_region, store_keys)
        # Only fall back to 'default' when there is no region hint at all.
        if not skey and not it.source_region and "default" in store_keys:
            skey = "default"
        if skey:
            groups[skey].append(it)
        else:
            unrouted[it.source_region or "?"].append(it)

    async def _run(skey: str, items: List[InvoiceLineItem]) -> ComparisonResult:
        cfg = settings.get_store(skey)
        try:
            return await _reconcile_store(cfg, items, payload, None)
        except (ShopifyError, ShopifyAuthError) as exc:
            logger.error("Store '%s' failed during auto compare: %s", skey, exc)
            return _missing_result(
                items, skey, f"Could not reconcile store '{skey}': {exc}"
            )

    results: List[ComparisonResult] = list(
        await asyncio.gather(*(_run(k, v) for k, v in groups.items()))
    )
    for region, items in unrouted.items():
        results.append(
            _missing_result(
                items,
                None,
                f"No Shopify store configured for region '{region}' "
                f"(store label: {items[0].source_store or 'n/a'}).",
            )
        )

    merged = merge_results(results, unrouted_regions=list(unrouted.keys()))
    return merged


async def run_compare(payload: CompareRequest, user: AuthUser) -> ComparisonResult:
    """Full comparison pipeline for already-parsed invoice line items.

    Shared by POST /compare (browser-supplied items) and POST /drive/check
    (server-side parse — keeps payloads tiny for serverless limits).
    """
    if not payload.line_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No invoice line items supplied for comparison.",
        )

    # Only reconcile lines that have an order number; skip the rest.
    all_count = len(payload.line_items)
    payload.line_items = [
        it for it in payload.line_items
        if it.order_id and str(it.order_id).strip()
    ]
    skipped_no_order = all_count - len(payload.line_items)
    if not payload.line_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No invoice lines contain an order number to check.",
        )

    # Quotation-based reconciliation (invoice vs price list) is the primary
    # mode; it needs no Shopify calls. The request may pin a specific Drive
    # quotation (stateless / serverless); otherwise the locally active file is
    # used. Falls back to Shopify order matching when neither exists.
    quotation = None
    if payload.quotation_file_id:
        try:
            quotation = get_quotation_by_drive_id(payload.quotation_file_id)
        except QuotationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Could not parse the selected quotation: {exc}",
            ) from exc
        except DriveError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not fetch the selected quotation from Drive: {exc}",
            ) from exc
    if quotation is None:
        quotation = get_quotation()
    if quotation is not None:
        result = reconcile_against_quotation(
            invoice_items=payload.line_items,
            quotation=quotation,
            tolerance=max(payload.price_tolerance, 0.15),
        )
    else:
        settings = get_settings()
        auto = payload.store in (None, "", "auto")
        if auto:
            result = await _compare_auto(payload, settings)
        else:
            result = await _compare_single(payload, settings)

    result.summary.skipped_no_order = skipped_no_order

    # Persist the run (non-fatal on failure). user_id always comes from the JWT.
    if payload.persist:
        run_id = insert_comparison_run(
            user_id=user.id,
            filename=payload.filename,
            matched_count=result.summary.matched_count,
            mismatch_count=result.summary.mismatch_count,
            missing_count=result.summary.missing_count,
        )
        result.run_id = run_id

    logger.info(
        "user=%s compared file=%s matched=%s mismatch=%s missing=%s",
        user.id,
        payload.filename,
        result.summary.matched_count,
        result.summary.mismatch_count,
        result.summary.missing_count,
    )
    return result


@router.post("/compare", response_model=ComparisonResult)
async def compare(
    payload: CompareRequest,
    user: AuthUser = Depends(get_optional_user),
) -> ComparisonResult:
    return await run_compare(payload, user)


@router.get("/history", response_model=List[ComparisonRun])
async def history(
    limit: int = Query(50, ge=1, le=200),
    user: AuthUser = Depends(get_optional_user),
) -> List[ComparisonRun]:
    rows = list_comparison_runs(user_id=user.id, limit=limit)
    runs: List[ComparisonRun] = []
    for r in rows:
        runs.append(
            ComparisonRun(
                id=str(r.get("id")),
                user_id=str(r.get("user_id")),
                filename=r.get("filename", ""),
                run_at=str(r.get("run_at", "")),
                matched_count=int(r.get("matched_count") or 0),
                mismatch_count=int(r.get("mismatch_count") or 0),
                missing_count=int(r.get("missing_count") or 0),
            )
        )
    return runs
