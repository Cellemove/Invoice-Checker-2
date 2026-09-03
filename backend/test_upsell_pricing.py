"""Regression check: quotation 'Upsell' sheet pricing (run from backend/).

Supplier billing model for new-format quotations: first unit at the regular
qty-1 price, every additional unit in the package at the product's Upsell rate.
"""
from pathlib import Path

from app.models import InvoiceLineItem
from app.quotation import _parse


def _item(product: str, qty: float) -> InvoiceLineItem:
    return InvoiceLineItem(row=1, product=product, quantity=qty)


def main() -> None:
    q = _parse(Path(".active_quotation.xlsx"))

    # Upsell sheet parsed and rates resolve.
    rate = q.upsell_rate("CE001", "DE")
    assert rate is not None and abs(rate - 4.48649) < 0.001, rate

    # Single unit: regular qty-1 price, no upsell involved.
    p1, err = q.expected_order_price([_item("CE001", 1)], "DE")
    assert err is None and abs(p1 - 7.96) < 0.01, (p1, err)

    # 3 units of one product: first at regular, 2 extras at the upsell rate
    # (NOT the qty-3 tier price).
    p3, err = q.expected_order_price([_item("CE001", 3)], "DE")
    assert err is None and abs(p3 - (7.956705 + 2 * 4.486491)) < 0.01, (p3, err)

    # Mixed products: primary (most expensive) at regular, add-on product's
    # units at ITS upsell rate.
    pm, err = q.expected_order_price(
        [_item("CE001", 1), _item("CE003", 2)], "CZ"
    )
    assert err is None and abs(pm - (7.077584 + 2 * 0.537057)) < 0.01, (pm, err)

    # Old-format quotation (no Upsell sheet): tier-table path unchanged.
    old = _parse(Path("Farid-Cellumove quotation-June.5.xlsx"))
    assert old.upsell_rate("CE001", "CZ") is None
    p2, err = old.expected_order_price([_item("CE001", 2)], "CZ")
    assert err is None and abs(p2 - old.price("CE001", 2, "CZ")) < 0.01, (p2, err)

    print("OK: upsell pricing checks passed")


if __name__ == "__main__":
    main()
