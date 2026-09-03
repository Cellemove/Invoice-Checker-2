"""Regression check: quotation 'Upsell' sheet pricing (run from backend/).

Supplier billing model for new-format quotations: first unit at the regular
qty-1 price, every additional unit in the package at the product's Upsell rate.
"""
from pathlib import Path

from app.models import InvoiceLineItem
from app.parsers import parse_invoice
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

    # 3 units of one product bill the qty-3 tier price (confirmed against the
    # A-8.1 invoice: GB x3 = 13.95 tier, not first + 2x upsell = 14.00).
    p3, err = q.expected_order_price([_item("CE001", 3)], "DE")
    assert err is None and abs(p3 - 16.87) < 0.01, (p3, err)

    # Mixed products: primary (most expensive) at regular, add-on product's
    # units at ITS upsell rate.
    pm, err = q.expected_order_price(
        [_item("CE001", 1), _item("CE003", 2)], "CZ"
    )
    assert err is None and abs(pm - (7.077584 + 2 * 0.537057)) < 0.01, (pm, err)

    # Sep-format quotation: extra product sheets (V2 leggings) and multiline
    # SKU cells ('CE023\nS-3XL') in the Upsell sheet.
    sep = _parse(Path("Farid-Cellumove quotation-Sep.01.xlsx"))
    assert sep.upsell_rate("CE023", "CZ") is not None
    # Primary at its tier-3 price + the add-on product at ITS upsell rate.
    pv, err = sep.expected_order_price(
        [_item("AEP001", 3), _item("CE003", 1)], "DE"
    )
    assert err is None and abs(pv - (17.4568 + 0.4994)) < 0.01, (pv, err)
    # 'NEW line to US' resolves the main sheet's NEW-line column.
    assert sep.price("CE001", 1, "NEW line to US") is not None

    # Old-format quotation (no Upsell sheet): tier-table path unchanged.
    old = _parse(Path("Farid-Cellumove quotation-June.5.xlsx"))
    assert old.upsell_rate("CE001", "CZ") is None
    p2, err = old.expected_order_price([_item("CE001", 2)], "CZ")
    assert err is None and abs(p2 - old.price("CE001", 2, "CZ")) < 0.01, (p2, err)

    # Continuation rows (blank order number, same package) inherit the order
    # so multi-item orders aren't reduced to their first line.
    csv = (
        "Order number,Package Number,Cost,Upsell,QTY,Item,Country\n"
        "1001,P1,6.46,,1,CE001,GB\n"
        ",P1,,3.77,1,CE003,\n"
        ",P2,5.00,,1,CE002,GB\n"
    )
    parsed = parse_invoice("t.csv", csv.encode())
    assert [i.order_id for i in parsed.line_items] == ["1001", "1001", None], [
        i.order_id for i in parsed.line_items
    ]

    print("OK: upsell pricing checks passed")


if __name__ == "__main__":
    main()
