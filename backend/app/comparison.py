"""Three-dimensional reconciliation between invoice line items and Shopify.

For every invoice line item we attempt to:
  1. Match it to a Shopify order by order id / order number.
  2. Compare unit price, quantity and line total against the matched order line.
  3. Verify the SKU/product exists in Shopify inventory.

We also surface Shopify order lines that have *no* corresponding invoice line
("missing in invoice"). The output is a flat list of ComparisonRow plus an
aggregate ComparisonSummary.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from .models import (
    STATUS_MATCHED,
    STATUS_MISSING_IN_INVOICE,
    STATUS_MISSING_IN_SHOPIFY,
    STATUS_NO_QUOTE,
    STATUS_NOT_BILLED,
    STATUS_PRICE_MISMATCH,
    STATUS_QUANTITY_MISMATCH,
    STATUS_SKU_NOT_FOUND,
    ComparisonResult,
    ComparisonRow,
    ComparisonSummary,
    InvoiceLineItem,
)
from .quotation import Quotation
from .shopify_client import ShopifyOrder, ShopifyLineItem, normalise_order_ref


# Common region-code aliases -> canonical two-letter store keys.
_REGION_ALIASES = {
    "usa": "us",
    "uk": "gb",
    "gbr": "gb",
    "deu": "de",
    "ger": "de",
    "esp": "es",
    "fra": "fr",
    "prt": "pt",
    "pol": "pl",
    "cze": "cz",
    "rou": "ro",
    "mex": "mx",
    "grc": "gr",
    "gre": "gr",
}


def resolve_store_key(region: Optional[str], store_keys: List[str]) -> Optional[str]:
    """Map a region code (from a store label) to a configured store key."""
    if not region:
        return None
    keys = {k.lower() for k in store_keys}
    r = region.lower()
    if r in keys:
        return r
    alias = _REGION_ALIASES.get(r)
    if alias and alias in keys:
        return alias
    if r[:2] in keys:  # e.g. "deplus" -> "de"
        return r[:2]
    return None


def merge_results(
    results: List[ComparisonResult], unrouted_regions: List[str]
) -> ComparisonResult:
    """Combine per-store ComparisonResults into a single file-level result."""
    rows: List[ComparisonRow] = []
    matched = mismatch = missing = sku_issues = 0
    invoice_total_sum = 0.0
    shopify_total = 0.0
    expected_total = 0.0
    unpriced_total = 0.0
    total_variance = 0.0
    orders_checked: List[str] = []
    orders_not_found: List[str] = []
    stores_used: List[str] = []
    have_invoice_total = False

    for res in results:
        rows.extend(res.rows)
        s = res.summary
        matched += s.matched_count
        mismatch += s.mismatch_count
        missing += s.missing_count
        sku_issues += s.sku_issues
        shopify_total += s.shopify_total or 0.0
        expected_total += s.expected_total or 0.0
        total_variance += s.total_variance or 0.0
        unpriced_total += s.unpriced_invoice_total or 0.0
        orders_checked.extend(s.orders_checked)
        orders_not_found.extend(s.orders_not_found)
        stores_used.extend(s.stores_used)
        if s.invoice_total is not None:
            invoice_total_sum += s.invoice_total
            have_invoice_total = True

    summary = ComparisonSummary(
        total_rows=len(rows),
        matched_count=matched,
        mismatch_count=mismatch,
        missing_count=missing,
        sku_issues=sku_issues,
        invoice_total=invoice_total_sum if have_invoice_total else None,
        shopify_total=round(shopify_total, 2),
        expected_total=round(expected_total, 2),
        total_variance=round(total_variance, 2),
        unpriced_invoice_total=round(unpriced_total, 2),
        orders_checked=sorted(set(orders_checked)),
        orders_not_found=sorted(set(orders_not_found)),
        stores_used=sorted(set(stores_used)),
        unrouted_regions=sorted(set(unrouted_regions)),
    )
    return ComparisonResult(
        summary=summary, rows=rows, store="auto"
    )


def _first_attr(items: List[InvoiceLineItem], attr: str) -> Optional[str]:
    for it in items:
        v = getattr(it, attr, None)
        if v:
            return v
    return None


def _compose_address(item: InvoiceLineItem) -> Optional[str]:
    parts = [item.address, item.zip_code, item.city, item.province]
    return ", ".join(p for p in parts if p) or None


def _line_expected(it: InvoiceLineItem) -> float:
    """Pre-tax billed amount for a line: Cost + Upsell + remote fee (no tax).

    Falls back to line_total - tax_fee when the cost columns are absent.
    """
    if it.cost or it.upsell or it.remote_fee:
        return (it.cost or 0.0) + (it.upsell or 0.0) + (it.remote_fee or 0.0)
    return (it.line_total or 0.0) - (it.tax_fee or 0.0)


def _products_summary(items: List[InvoiceLineItem], limit: int = 25) -> Optional[str]:
    parts: List[str] = []
    for it in items:
        label = it.sku or it.product or "item"
        q = it.quantity or 0
        qd = int(q) if float(q).is_integer() else q
        parts.append(f"{label} ×{qd}")
    if not parts:
        return None
    summary = ", ".join(parts[:limit])
    if len(parts) > limit:
        summary += f", +{len(parts) - limit} more"
    return summary


DEFAULT_QUOTATION_TOLERANCE = 0.15


def reconcile_against_quotation(
    *,
    invoice_items: List[InvoiceLineItem],
    quotation: Quotation,
    tolerance: float = DEFAULT_QUOTATION_TOLERANCE,
) -> ComparisonResult:
    """Order-level reconciliation of the invoice against the quotation price list.

    Per order: 'price on invoice' = pre-tax charge (Cost + Upsell); 'price it
    should be' = the quotation's combined-shipment expected price. An order is
    'matched' when the two agree within tolerance, 'price mismatch' when they
    differ, and 'no quotation' when the price list can't price it.
    """
    groups: Dict[str, List[InvoiceLineItem]] = {}
    display: Dict[str, str] = {}
    for it in invoice_items:
        ref = normalise_order_ref(it.order_id) if it.order_id else ""
        groups.setdefault(ref, []).append(it)
        display.setdefault(ref, it.order_id or "(no order ref)")

    rows: List[ComparisonRow] = []
    matched = mismatch = no_quote = not_billed = 0
    shown_total_sum = 0.0
    expected_total_sum = 0.0
    unpriced_invoice_sum = 0.0
    total_variance = 0.0

    for ref, items in groups.items():
        shown = display.get(ref) or ref or "(no order ref)"
        country = _first_attr(items, "country")
        customer_name = _first_attr(items, "customer_name")
        addr_item = next(
            (it for it in items if it.address or it.city or it.zip_code), items[0]
        )
        address = _compose_address(addr_item)
        products = _products_summary(items)
        inv_qty = round(sum(i.quantity for i in items), 4)
        price_on_invoice = round(sum(_line_expected(i) for i in items), 2)
        shown_total_sum += price_on_invoice

        # Orders may ship (and bill) as several packages — each package gets
        # its own tier pricing, so price per package and sum.
        by_package: Dict[str, List[InvoiceLineItem]] = {}
        for it in items:
            by_package.setdefault(it.package or "", []).append(it)
        expected: Optional[float] = 0.0
        reason: Optional[str] = None
        for pkg_items in by_package.values():
            pkg_expected, pkg_reason = quotation.expected_order_price(
                pkg_items, country
            )
            if pkg_expected is None:
                expected, reason = None, pkg_reason
                break
            expected += pkg_expected
        if expected is not None:
            expected = round(expected, 2)

        if expected is None:
            no_quote += 1
            unpriced_invoice_sum += price_on_invoice
            rows.append(
                ComparisonRow(
                    status=STATUS_NO_QUOTE,
                    order_id=shown,
                    country=country,
                    customer_name=customer_name,
                    address=address,
                    products=products,
                    product=f"{len(items)} line item(s)",
                    invoice_quantity=inv_qty,
                    invoice_line_total=price_on_invoice,
                    notes=f"Could not price against quotation: {reason}.",
                )
            )
            continue

        expected_total_sum += expected
        variance = round(price_on_invoice - expected, 2)
        total_variance += variance

        if abs(price_on_invoice) < 0.005 and expected > tolerance:
            # Shipped but 0.00 on the invoice — typically a free reshipment
            # (SAV). Reported apart so it doesn't drown real price mismatches.
            status_val = STATUS_NOT_BILLED
            not_billed += 1
            note = (
                f"No charge on the invoice; quotation price would be {expected}. "
                "Possibly a free reshipment."
            )
        elif abs(variance) <= tolerance:
            status_val = STATUS_MATCHED
            matched += 1
            note = None
        else:
            status_val = STATUS_PRICE_MISMATCH
            mismatch += 1
            direction = "over" if variance > 0 else "under"
            note = f"Invoice {direction}charged by {abs(variance)} vs quotation."

        rows.append(
            ComparisonRow(
                status=status_val,
                order_id=shown,
                country=country,
                customer_name=customer_name,
                address=address,
                products=products,
                product=f"{len(items)} line item(s)",
                invoice_quantity=inv_qty,
                invoice_line_total=price_on_invoice,
                expected_price=expected,
                variance=variance,
                notes=note,
            )
        )

    summary = ComparisonSummary(
        total_rows=len(rows),
        matched_count=matched,
        mismatch_count=mismatch,
        missing_count=no_quote,
        sku_issues=0,
        invoice_total=round(shown_total_sum, 2),
        expected_total=round(expected_total_sum, 2),
        total_variance=round(total_variance, 2),
        not_billed_count=not_billed,
        unpriced_invoice_total=round(unpriced_invoice_sum, 2),
    )
    return ComparisonResult(summary=summary, rows=rows, store="quotation")


def reconcile_by_order(
    *,
    invoice_items: List[InvoiceLineItem],
    orders: Dict[str, ShopifyOrder],
    inventory_skus: Set[str],
    store_key: Optional[str],
    invoice_total: Optional[float] = None,
    price_tolerance: float = 0.01,
    check_prices: bool = True,
    missing_note: Optional[str] = None,
) -> ComparisonResult:
    """Order-level reconciliation: one result row per invoice order.

    An order is 'matched' when it exists in the store; totals are shown for
    information (and flagged only when check_prices is on). SKU existence is
    checked against the store catalog, and skipped entirely when the catalog
    has no SKUs at all.
    """
    catalog_has_skus = bool(inventory_skus)

    # Group invoice lines by normalised order reference.
    groups: Dict[str, List[InvoiceLineItem]] = {}
    display: Dict[str, str] = {}
    for it in invoice_items:
        ref = normalise_order_ref(it.order_id) if it.order_id else ""
        groups.setdefault(ref, []).append(it)
        display.setdefault(ref, it.order_id or "(no order ref)")

    rows: List[ComparisonRow] = []
    matched = mismatch = missing = sku_issues = 0
    shopify_total_sum = 0.0
    expected_total_sum = 0.0
    shown_total_sum = 0.0
    total_variance = 0.0
    orders_checked: List[str] = []
    orders_not_found: List[str] = []

    for ref, items in groups.items():
        inv_total = round(sum(i.line_total for i in items), 2)
        should_be = round(sum(_line_expected(i) for i in items), 2)
        inv_qty = round(sum(i.quantity for i in items), 4)
        skus = [i.sku for i in items if i.sku]
        shown = display.get(ref) or ref or "(no order ref)"
        expected_total_sum += should_be
        shown_total_sum += inv_total

        # Customer / product detail carried from the invoice file.
        customer_name = _first_attr(items, "customer_name")
        country = _first_attr(items, "country")
        addr_item = next(
            (it for it in items if it.address or it.city or it.zip_code), items[0]
        )
        address = _compose_address(addr_item)
        products = _products_summary(items)

        order = orders.get(ref) if ref else None

        if order is None:
            missing += 1
            orders_not_found.append(ref or shown)
            variance = round(inv_total - should_be, 2)
            total_variance += variance
            rows.append(
                ComparisonRow(
                    status=STATUS_MISSING_IN_SHOPIFY,
                    store=store_key,
                    order_id=shown,
                    product=f"{len(items)} line item(s)",
                    products=products,
                    customer_name=customer_name,
                    address=address,
                    country=country,
                    invoice_quantity=inv_qty,
                    invoice_line_total=inv_total,
                    expected_price=should_be,
                    variance=variance,
                    notes=missing_note or f"Order not found in store '{store_key}'.",
                )
            )
            continue

        orders_checked.append(ref)
        shop_total = round(order.total_price, 2)
        shop_qty = round(sum(li.quantity for li in order.line_items), 4)
        shopify_total_sum += shop_total
        variance = round(inv_total - should_be, 2)
        total_variance += variance

        # SKU catalog coverage (skipped when the catalog has no SKUs at all).
        sku_flag: Optional[bool] = None
        sku_note: Optional[str] = None
        if not catalog_has_skus:
            sku_note = "Store catalog has no SKUs; SKU check skipped."
        elif skus:
            missing_skus = sorted({s for s in skus if s.strip() not in inventory_skus})
            present = len(set(skus)) - len(missing_skus)
            sku_flag = not missing_skus
            if missing_skus:
                sku_issues += len(missing_skus)
                sku_note = (
                    f"{present}/{len(set(skus))} SKUs in catalog; missing: "
                    + ", ".join(missing_skus[:5])
                    + ("…" if len(missing_skus) > 5 else "")
                )
            else:
                sku_note = f"All {len(set(skus))} SKUs in catalog."

        if check_prices and abs(variance) > price_tolerance:
            status_val = STATUS_PRICE_MISMATCH
            mismatch += 1
            note = f"Amount differs by {variance}." + (f" {sku_note}" if sku_note else "")
        else:
            status_val = STATUS_MATCHED
            matched += 1
            note = sku_note

        rows.append(
            ComparisonRow(
                status=status_val,
                store=store_key,
                order_id=shown,
                product=f"{len(items)} line item(s)",
                products=products,
                customer_name=customer_name,
                address=address,
                country=country,
                invoice_quantity=inv_qty,
                shopify_quantity=shop_qty,
                invoice_line_total=inv_total,
                shopify_line_total=shop_total,
                expected_price=should_be,
                variance=variance,
                sku_in_inventory=sku_flag,
                notes=note,
            )
        )

    summary = ComparisonSummary(
        total_rows=len(rows),
        matched_count=matched,
        mismatch_count=mismatch,
        missing_count=missing,
        sku_issues=sku_issues,
        invoice_total=round(shown_total_sum, 2),
        shopify_total=round(shopify_total_sum, 2),
        expected_total=round(expected_total_sum, 2),
        total_variance=round(total_variance, 2),
        orders_checked=sorted(set(orders_checked)),
        orders_not_found=sorted(set(orders_not_found)),
        stores_used=[store_key] if store_key else [],
    )
    return ComparisonResult(
        summary=summary, rows=rows, store=store_key or "(unrouted)"
    )


def _match_key(sku: Optional[str], product: Optional[str]) -> str:
    """Key used to pair an invoice line with a Shopify order line."""
    if sku:
        return f"sku::{sku.strip().lower()}"
    if product:
        return f"title::{product.strip().lower()}"
    return "∅"


def _within(a: float, b: float, tol: float) -> bool:
    return abs(round(a - b, 4)) <= tol


def reconcile(
    *,
    invoice_items: List[InvoiceLineItem],
    orders: Dict[str, ShopifyOrder],
    inventory_skus: Set[str],
    invoice_total: Optional[float],
    store_key: str,
    price_tolerance: float = 0.01,
    check_prices: bool = True,
) -> ComparisonResult:
    rows: List[ComparisonRow] = []

    matched_count = 0
    mismatch_count = 0
    missing_count = 0
    sku_issues = 0
    total_variance = 0.0
    shopify_total = 0.0

    orders_checked = sorted(orders.keys())
    requested_refs: Set[str] = set()
    # Track which Shopify order lines get consumed so leftovers => missing in invoice.
    consumed: Dict[str, Set[int]] = {k: set() for k in orders}

    # Pre-index each order's line items by match key.
    order_line_index: Dict[str, Dict[str, List[Tuple[int, ShopifyLineItem]]]] = {}
    for key, order in orders.items():
        idx: Dict[str, List[Tuple[int, ShopifyLineItem]]] = {}
        for i, li in enumerate(order.line_items):
            idx.setdefault(_match_key(li.sku, li.title), []).append((i, li))
        order_line_index[key] = idx

    # ── Pass 1: walk every invoice line ───────────────────────────────────
    for item in invoice_items:
        ref = normalise_order_ref(item.order_id) if item.order_id else ""
        if ref:
            requested_refs.add(ref)

        sku_in_inventory: Optional[bool] = None
        if item.sku:
            sku_in_inventory = item.sku.strip() in inventory_skus

        order = orders.get(ref) if ref else None

        # ---- Dimension 1: order not found in Shopify ----------------------
        if order is None:
            missing_count += 1
            if sku_in_inventory is False:
                sku_issues += 1
            note = "Order not found in Shopify."
            if not ref:
                note = "Invoice line has no order reference to match against."
            rows.append(
                ComparisonRow(
                    status=STATUS_MISSING_IN_SHOPIFY,
                    order_id=item.order_id,
                    sku=item.sku,
                    product=item.product,
                    invoice_quantity=item.quantity,
                    invoice_unit_price=item.unit_price,
                    invoice_line_total=item.line_total,
                    variance=item.line_total,
                    sku_in_inventory=sku_in_inventory,
                    notes=note,
                )
            )
            total_variance += item.line_total
            continue

        # ---- Find the matching Shopify line within the order --------------
        key = _match_key(item.sku, item.product)
        candidates = order_line_index[ref].get(key, [])
        match: Optional[Tuple[int, ShopifyLineItem]] = None
        for cand_idx, cand_li in candidates:
            if cand_idx not in consumed[ref]:
                match = (cand_idx, cand_li)
                break

        if match is None:
            # Order exists but this specific item/SKU isn't on the order.
            missing_count += 1
            if sku_in_inventory is False:
                sku_issues += 1
            rows.append(
                ComparisonRow(
                    status=STATUS_MISSING_IN_SHOPIFY,
                    order_id=item.order_id,
                    sku=item.sku,
                    product=item.product,
                    invoice_quantity=item.quantity,
                    invoice_unit_price=item.unit_price,
                    invoice_line_total=item.line_total,
                    variance=item.line_total,
                    sku_in_inventory=sku_in_inventory,
                    notes="Order found, but this line item/SKU is not on the order.",
                )
            )
            total_variance += item.line_total
            continue

        # ---- Dimension 2: compare price & quantity ------------------------
        cand_idx, sli = match
        consumed[ref].add(cand_idx)

        variance = round(item.line_total - sli.line_total, 2)
        total_variance += variance

        # In match-only mode we treat every located line as matched and never
        # emit price/quantity mismatches (the invoice amount isn't the sale price).
        price_ok = (not check_prices) or _within(
            item.unit_price, sli.unit_price, price_tolerance
        )
        qty_ok = (not check_prices) or _within(item.quantity, sli.quantity, 0.0001)

        notes_parts: List[str] = []
        if not sku_in_inventory and item.sku:
            notes_parts.append("SKU not found in inventory.")

        if price_ok and qty_ok:
            status = STATUS_MATCHED
            matched_count += 1
        elif not price_ok:
            status = STATUS_PRICE_MISMATCH
            mismatch_count += 1
            notes_parts.append(
                f"Price differs by {round(item.unit_price - sli.unit_price, 2)}."
            )
            if not qty_ok:
                notes_parts.append(
                    f"Quantity differs by {round(item.quantity - sli.quantity, 2)}."
                )
        else:  # quantity mismatch only
            status = STATUS_QUANTITY_MISMATCH
            mismatch_count += 1
            notes_parts.append(
                f"Quantity differs by {round(item.quantity - sli.quantity, 2)}."
            )

        if item.sku and sku_in_inventory is False:
            sku_issues += 1

        shopify_total += sli.line_total
        rows.append(
            ComparisonRow(
                status=status,
                order_id=item.order_id,
                sku=item.sku,
                product=item.product or sli.title,
                invoice_quantity=item.quantity,
                shopify_quantity=sli.quantity,
                invoice_unit_price=item.unit_price,
                shopify_unit_price=sli.unit_price,
                invoice_line_total=item.line_total,
                shopify_line_total=sli.line_total,
                variance=variance,
                sku_in_inventory=sku_in_inventory,
                notes=" ".join(notes_parts) or None,
            )
        )

    # ── Pass 2: Shopify order lines with no invoice counterpart ───────────
    for ref, order in orders.items():
        for i, sli in enumerate(order.line_items):
            if i in consumed[ref]:
                continue
            missing_count += 1
            variance = round(-sli.line_total, 2)
            total_variance += variance
            shopify_total += sli.line_total
            rows.append(
                ComparisonRow(
                    status=STATUS_MISSING_IN_INVOICE,
                    order_id=order.name,
                    sku=sli.sku,
                    product=sli.title,
                    shopify_quantity=sli.quantity,
                    shopify_unit_price=sli.unit_price,
                    shopify_line_total=sli.line_total,
                    variance=variance,
                    sku_in_inventory=(sli.sku in inventory_skus) if sli.sku else None,
                    notes="Present on the Shopify order but not on the invoice.",
                )
            )

    orders_not_found = sorted(requested_refs - set(orders.keys()))

    # Stamp every row with the store it was reconciled against.
    for r in rows:
        r.store = store_key

    summary = ComparisonSummary(
        total_rows=len(rows),
        matched_count=matched_count,
        mismatch_count=mismatch_count,
        missing_count=missing_count,
        sku_issues=sku_issues,
        invoice_total=invoice_total,
        shopify_total=round(shopify_total, 2),
        total_variance=round(total_variance, 2),
        orders_checked=orders_checked,
        orders_not_found=orders_not_found,
        stores_used=[store_key],
    )

    return ComparisonResult(summary=summary, rows=rows, store=store_key)
