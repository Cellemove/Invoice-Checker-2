"""Invoice file parsing for .xlsx and .csv uploads.

The parser is tolerant of column naming: it normalises headers and maps a wide
range of common aliases onto the canonical invoice schema. Unknown columns are
ignored. Rows that contain no usable data are skipped.
"""
from __future__ import annotations

import io
import re
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .models import InvoiceLineItem, ParsedInvoice

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB upload ceiling.

# Canonical field -> set of accepted (normalised) header aliases.
COLUMN_ALIASES: Dict[str, set[str]] = {
    "order_id": {
        "orderid",
        "ordernumber",
        "orderno",
        "order",
        "ordernum",
        "invoiceref",
        "invoicereference",
        "reference",
        "ref",
        "ordername",
    },
    "sku": {"sku", "skucode", "variantsku", "itemsku", "productcode", "barcode"},
    "product": {
        "product",
        "productname",
        "producttitle",
        "title",
        "item",
        "itemname",
        "description",
        "name",
    },
    "quantity": {"quantity", "qty", "units", "count", "amountordered"},
    "unit_price": {
        "unitprice",
        "price",
        "priceperunit",
        "rate",
        "cost",
        "unitcost",
        "amount",
    },
    "line_total": {
        "linetotal",
        "total",
        "lineamount",
        "extendedprice",
        "subtotal",
        "amounttotal",
        "totalprice",
    },
    "cost": {"cost", "basecost", "shippingcost", "baseprice"},
    "upsell": {"upsell", "upsellamount", "upsells"},
    "tax_fee": {"eutaxfee", "taxfee", "tax", "vat", "eutax", "taxes"},
    "remote_fee": {"remotefee", "remoteareafee", "remotesurcharge"},
    "package": {"packagenumber", "package", "packageno", "parcelnumber"},
    "source_store": {"store", "shop", "storename", "shopname", "storelabel"},
    "customer_name": {
        "shippingname",
        "customername",
        "customer",
        "recipient",
        "buyer",
        "shiptoname",
        "consignee",
        "fullname",
    },
    "address": {"address", "address1", "street", "shippingaddress", "streetaddress"},
    "city": {"city", "town"},
    "province": {"province", "state"},
    "zip_code": {"zipcode", "zip", "postalcode", "postcode", "postal"},
    "country": {"country", "countrycode", "countryname"},
}

INVOICE_TOTAL_ALIASES = {
    "invoicetotal",
    "grandtotal",
    "totaldue",
    "documenttotal",
    "balancedue",
}


class InvoiceParseError(ValueError):
    """Raised when a file cannot be parsed into invoice line items."""


def extract_region(store_label: Optional[str]) -> Optional[str]:
    """Extract a region/country code from a store label.

    'Cellumove DE(Plus)' -> 'de', 'Farid-Cellumove GR' -> 'gr',
    'Cellumove USA' -> 'usa'. Parenthetical qualifiers are dropped and the last
    alphabetic token is taken as the region.
    """
    if not store_label:
        return None
    cleaned = re.sub(r"\(.*?\)", " ", str(store_label))
    tokens = re.findall(r"[A-Za-z]{2,}", cleaned)
    return tokens[-1].lower() if tokens else None


def _normalise(header: object) -> str:
    """Lowercase a header and strip everything that isn't alphanumeric."""
    return re.sub(r"[^a-z0-9]", "", str(header).strip().lower())


def _build_header_map(columns: List[object]) -> Dict[str, str]:
    """Map canonical field -> actual dataframe column name."""
    mapping: Dict[str, str] = {}
    for col in columns:
        norm = _normalise(col)
        for field, aliases in COLUMN_ALIASES.items():
            if field in mapping:
                continue
            if norm in aliases or norm == field:
                mapping[field] = col
    return mapping


def _to_float(value: object) -> float:
    """Best-effort numeric coercion that tolerates currency symbols/commas."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        try:
            if pd.isna(value):  # type: ignore[arg-type]
                return 0.0
        except (TypeError, ValueError):
            pass
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    # Strip currency symbols, thousands separators and stray whitespace.
    text = re.sub(r"[^0-9.\-]", "", text.replace(",", ""))
    if text in ("", "-", ".", "-."):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _sanitize(text: str) -> str:
    """Drop lone surrogate code points that break UTF-8/JSON serialization.

    Some spreadsheet cells decode to lone surrogates (e.g. \\udc81 from a stray
    0x81 byte); these cannot be encoded to UTF-8 and would 500 the API.
    """
    if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        return "".join(c for c in text if not 0xD800 <= ord(c) <= 0xDFFF)
    return text


def _clean_str(value: object) -> Optional[str]:
    if value is None:
        return None
    try:
        if pd.isna(value):  # type: ignore[arg-type]
            return None
    except (TypeError, ValueError):
        pass
    text = _sanitize(str(value).strip())
    if not text or text.lower() == "nan":
        return None
    # Excel often turns "1001" into "1001.0" — trim a trailing .0 on int-like ids.
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


# Excel auto-recovery artifact sheets ("Recovered_Sheet1", ...) are skipped:
# they are crash-recovery snapshots, not intentional invoice data.
_RECOVERY_RE = re.compile(r"^recovered[_\s]?sheet", re.IGNORECASE)


def _read_dataframe(
    filename: str, content: bytes
) -> Tuple[pd.DataFrame, Optional[List[str]], Optional[List[str]]]:
    """Return (dataframe, sheets_used, sheets_skipped).

    For multi-sheet workbooks, every sheet that has recognisable invoice columns
    is combined into one dataframe (Excel recovery-artifact sheets are skipped),
    and fully-identical duplicate rows are dropped.
    """
    name = filename.lower()
    buffer = io.BytesIO(content)
    if name.endswith(".csv"):
        try:
            return pd.read_csv(buffer, dtype=str, keep_default_na=False), None, None
        except Exception as exc:  # noqa: BLE001
            raise InvoiceParseError(f"Could not parse CSV file: {exc}") from exc
    if name.endswith((".xlsx", ".xlsm")):
        try:
            xl = pd.ExcelFile(buffer, engine="openpyxl")
            names = xl.sheet_names
            # Skip Excel recovery-artifact sheets BEFORE parsing — they mirror
            # the real sheets and can double the parse time of large files.
            if len(names) > 1:
                wanted = [
                    n for n in names if not _RECOVERY_RE.match(str(n).strip())
                ] or names
            else:
                wanted = names
            sheets = {n: xl.parse(n, dtype=str) for n in wanted}
        except Exception as exc:  # noqa: BLE001
            raise InvoiceParseError(f"Could not parse Excel file: {exc}") from exc
        pre_skipped = [n for n in names if n not in wanted]
        df, used, skipped = _combine_sheets(sheets)
        return df, used, (list(pre_skipped) + list(skipped or []) or None)
    raise InvoiceParseError(
        "Unsupported file type. Please upload a .csv or .xlsx file."
    )


def _combine_sheets(
    sheets: Dict[str, pd.DataFrame]
) -> Tuple[pd.DataFrame, Optional[List[str]], Optional[List[str]]]:
    if not sheets:
        raise InvoiceParseError("The Excel file contains no worksheets.")

    # A single-sheet workbook is used as-is (no recovery filtering needed).
    if len(sheets) == 1:
        only_name, only_df = next(iter(sheets.items()))
        return only_df, [only_name], None

    skipped: List[str] = []
    candidates: Dict[str, pd.DataFrame] = {}
    for sheet_name, df in sheets.items():
        if _RECOVERY_RE.match(str(sheet_name).strip()):
            skipped.append(sheet_name)
            continue
        candidates[sheet_name] = df

    # If filtering removed everything, fall back to all sheets.
    if not candidates:
        candidates = dict(sheets)
        skipped = []

    frames: List[pd.DataFrame] = []
    used: List[str] = []
    for sheet_name, df in candidates.items():
        if df is None or df.empty:
            continue
        # Only combine sheets that actually look like invoice data.
        if _build_header_map(list(df.columns)):
            frames.append(df)
            used.append(sheet_name)
        else:
            skipped.append(sheet_name)

    if not frames:
        raise InvoiceParseError(
            "No worksheets with recognisable invoice columns were found. "
            f"Sheets seen: {', '.join(str(s) for s in sheets)}"
        )

    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined = combined.drop_duplicates().reset_index(drop=True)
    return combined, used, (skipped or None)


def _extract_invoice_total(
    df: pd.DataFrame, header_map: Dict[str, str]
) -> Optional[float]:
    for col in df.columns:
        if _normalise(col) in INVOICE_TOTAL_ALIASES:
            series = df[col].dropna()
            if not series.empty:
                return _to_float(series.iloc[0])
    return None


def parse_invoice(filename: str, content: bytes) -> ParsedInvoice:
    """Parse raw upload bytes into a normalised ParsedInvoice.

    Raises InvoiceParseError for unsupported / unreadable / empty files or when
    none of the expected columns can be identified.
    """
    if not content:
        raise InvoiceParseError("Uploaded file is empty.")
    if len(content) > MAX_FILE_BYTES:
        raise InvoiceParseError(
            f"File too large ({len(content)} bytes). Maximum is {MAX_FILE_BYTES} bytes."
        )

    df, sheets_used, sheets_skipped = _read_dataframe(filename, content)
    if df.empty:
        raise InvoiceParseError("The file contains no rows.")

    header_map = _build_header_map(list(df.columns))
    if not header_map:
        raise InvoiceParseError(
            "Could not identify any recognised invoice columns. Expected at least "
            "one of: order id, sku, product, quantity, price, total. "
            f"Found columns: {', '.join(str(c) for c in df.columns)}"
        )

    invoice_total = _extract_invoice_total(df, header_map)

    line_items: List[InvoiceLineItem] = []
    for idx, (_, raw) in enumerate(df.iterrows(), start=2):  # row 1 == header
        order_id = (
            _clean_str(raw[header_map["order_id"]])
            if "order_id" in header_map
            else None
        )
        sku = _clean_str(raw[header_map["sku"]]) if "sku" in header_map else None
        product = (
            _clean_str(raw[header_map["product"]])
            if "product" in header_map
            else None
        )
        quantity = (
            _to_float(raw[header_map["quantity"]])
            if "quantity" in header_map
            else 0.0
        )
        unit_price = (
            _to_float(raw[header_map["unit_price"]])
            if "unit_price" in header_map
            else 0.0
        )
        line_total = (
            _to_float(raw[header_map["line_total"]])
            if "line_total" in header_map
            else 0.0
        )
        source_store = (
            _clean_str(raw[header_map["source_store"]])
            if "source_store" in header_map
            else None
        )

        def _col(field: str) -> Optional[str]:
            return (
                _clean_str(raw[header_map[field]]) if field in header_map else None
            )

        cost = (
            _to_float(raw[header_map["cost"]]) if "cost" in header_map else 0.0
        )
        upsell = (
            _to_float(raw[header_map["upsell"]]) if "upsell" in header_map else 0.0
        )
        tax_fee = (
            _to_float(raw[header_map["tax_fee"]]) if "tax_fee" in header_map else 0.0
        )
        remote_fee = (
            _to_float(raw[header_map["remote_fee"]])
            if "remote_fee" in header_map
            else 0.0
        )
        package = _col("package")
        customer_name = _col("customer_name")
        address = _col("address")
        city = _col("city")
        province = _col("province")
        zip_code = _col("zip_code")
        country = _col("country")

        # Newer invoice layouts drop the base-code 'Item' column entirely; the
        # base product code is then the SKU up to its first variant dash
        # (e.g. 'CE001A(Pocket)-Black/5XL' -> 'CE001A(Pocket)').
        if not product and sku:
            product = sku.split("-", 1)[0].strip() or None

        # Skip entirely-empty rows.
        if not any([order_id, sku, product]) and not any(
            [quantity, unit_price, line_total]
        ):
            continue

        # Derive line_total when absent but qty * price is available.
        if not line_total and quantity and unit_price:
            line_total = round(quantity * unit_price, 2)
        # Derive unit_price when absent but total / qty is available.
        if not unit_price and line_total and quantity:
            unit_price = round(line_total / quantity, 4)

        line_items.append(
            InvoiceLineItem(
                row=idx,
                order_id=order_id,
                sku=sku,
                product=product,
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
                cost=cost,
                upsell=upsell,
                tax_fee=tax_fee,
                remote_fee=remote_fee,
                package=package,
                source_store=source_store,
                source_region=extract_region(source_store),
                customer_name=customer_name,
                address=address,
                city=city,
                province=province,
                zip_code=zip_code,
                country=country,
            )
        )

    if not line_items:
        raise InvoiceParseError("No usable invoice line items were found in the file.")

    return ParsedInvoice(
        filename=filename,
        line_items=line_items,
        invoice_total=invoice_total,
        row_count=len(line_items),
        sheets_used=sheets_used,
        sheets_skipped=sheets_skipped,
    )
