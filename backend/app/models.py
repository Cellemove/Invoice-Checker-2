"""Pydantic models shared across the API surface."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# ── Invoice parsing ────────────────────────────────────────────────────────
class InvoiceLineItem(BaseModel):
    """A single normalised line item parsed from an uploaded invoice."""

    row: int = Field(..., description="1-based source row number in the file.")
    order_id: Optional[str] = Field(
        None, description="Order id / order number / invoice reference."
    )
    sku: Optional[str] = None
    product: Optional[str] = Field(None, description="Product / item title.")
    quantity: float = 0.0
    unit_price: float = 0.0
    line_total: float = 0.0
    # Invoice cost components (for the pre-tax "price it should be").
    cost: float = 0.0
    upsell: float = 0.0
    tax_fee: float = 0.0
    remote_fee: float = 0.0
    package: Optional[str] = Field(
        None, description="Package number — orders may ship (and bill) as several packages."
    )
    source_store: Optional[str] = Field(
        None, description="Raw store/shop label from the file (e.g. 'Cellumove DE(Plus)')."
    )
    source_region: Optional[str] = Field(
        None, description="Region code extracted from the store label (e.g. 'de')."
    )
    # Customer / shipping details carried from the invoice file (if present).
    customer_name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    zip_code: Optional[str] = None
    country: Optional[str] = None


class ParsedInvoice(BaseModel):
    filename: str
    line_items: List[InvoiceLineItem]
    invoice_total: Optional[float] = Field(
        None, description="Document-level total if present in the file."
    )
    row_count: int
    sheets_used: Optional[List[str]] = Field(
        None, description="Worksheet names combined into this invoice (xlsx only)."
    )
    sheets_skipped: Optional[List[str]] = Field(
        None, description="Worksheets ignored (e.g. Excel recovery artifacts)."
    )


# ── Comparison ─────────────────────────────────────────────────────────────
class CompareRequest(BaseModel):
    filename: str = "invoice"
    line_items: List[InvoiceLineItem]
    invoice_total: Optional[float] = None
    store: Optional[str] = Field(
        None,
        description=(
            "Logical Shopify store key. Use 'auto' (or leave null) to route each "
            "line to the store matching its region and merge into one result."
        ),
    )
    price_tolerance: float = Field(
        0.01,
        ge=0,
        description="Absolute currency tolerance when comparing prices/totals.",
    )
    quotation_file_id: Optional[str] = Field(
        None,
        description=(
            "Google Drive file id of the quotation to price against. Keeps the "
            "backend stateless (required for serverless); falls back to the "
            "locally active quotation file when omitted."
        ),
    )
    check_prices: bool = Field(
        True,
        description=(
            "When False, reconcile by order/SKU only and never flag price or "
            "quantity mismatches (useful when the invoice amount is a fulfilment "
            "cost rather than the Shopify sale price)."
        ),
    )
    persist: bool = Field(
        True, description="Whether to store this run in comparison history."
    )


# Per-row comparison status values.
STATUS_MATCHED = "matched"
STATUS_PRICE_MISMATCH = "price mismatch"
STATUS_QUANTITY_MISMATCH = "quantity mismatch"
STATUS_MISSING_IN_SHOPIFY = "missing in Shopify"
STATUS_MISSING_IN_INVOICE = "missing in invoice"
STATUS_SKU_NOT_FOUND = "sku not found in inventory"
STATUS_NO_QUOTE = "no quotation"
STATUS_NOT_BILLED = "not billed"


class ComparisonRow(BaseModel):
    status: str
    store: Optional[str] = Field(
        None, description="Store key this line was reconciled against."
    )
    order_id: Optional[str] = None
    sku: Optional[str] = None
    product: Optional[str] = None
    # Order-level detail (populated by order-level reconciliation).
    products: Optional[str] = Field(
        None, description="Summary of the order's products (sku ×qty)."
    )
    customer_name: Optional[str] = None
    address: Optional[str] = None
    country: Optional[str] = None

    invoice_quantity: Optional[float] = None
    shopify_quantity: Optional[float] = None

    invoice_unit_price: Optional[float] = None
    shopify_unit_price: Optional[float] = None

    invoice_line_total: Optional[float] = None
    shopify_line_total: Optional[float] = None
    expected_price: Optional[float] = Field(
        None, description="Price it should be (invoice pre-tax: Cost + Upsell)."
    )

    variance: Optional[float] = Field(
        None, description="invoice price shown - price it should be."
    )
    sku_in_inventory: Optional[bool] = None
    notes: Optional[str] = None


class ComparisonSummary(BaseModel):
    total_rows: int
    matched_count: int
    mismatch_count: int
    missing_count: int
    sku_issues: int
    invoice_total: Optional[float] = None
    shopify_total: Optional[float] = None
    expected_total: Optional[float] = Field(
        None, description="Sum of the pre-tax 'price it should be' across orders."
    )
    total_variance: float = 0.0
    orders_checked: List[str] = []
    orders_not_found: List[str] = []
    stores_used: List[str] = []
    skipped_no_order: int = Field(
        0, description="Invoice lines skipped because they had no order number."
    )
    not_billed_count: int = Field(
        0,
        description=(
            "Orders present on the invoice with a 0.00 charge (likely free "
            "reshipments) — reported separately from price mismatches."
        ),
    )
    unpriced_invoice_total: float = Field(
        0.0,
        description=(
            "Invoice value of 'no quotation' orders — included in invoice_total "
            "but excluded from expected_total and total_variance."
        ),
    )
    unrouted_regions: List[str] = Field(
        default_factory=list,
        description="Regions in the file with no matching configured store.",
    )


class ComparisonResult(BaseModel):
    summary: ComparisonSummary
    rows: List[ComparisonRow]
    store: str
    run_id: Optional[str] = None


# ── History ────────────────────────────────────────────────────────────────
class ComparisonRun(BaseModel):
    id: str
    user_id: str
    filename: str
    run_at: str
    matched_count: int
    mismatch_count: int
    missing_count: int
