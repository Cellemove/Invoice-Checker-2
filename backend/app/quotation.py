"""Quotation price-list lookup and combined-shipment expected pricing.

The quotation is an .xlsx price list keyed by base product code × quantity ×
destination country (sheet ``QTY=1-5``). For a single-product order the expected
pre-tax price is a direct lookup. For a multi-product order the shipment pays one
product's full quotation price plus a per-unit add-on (the quotation's tier-to-
tier increment) for every other product — this module reconstructs that.

The active quotation file is resolved from ``QUOTATION_FILE`` (env) or, failing
that, the first ``*quotation*.xlsx`` found in the backend directory.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import openpyxl

from .models import InvoiceLineItem

logger = logging.getLogger("invoice.quotation")

_BACKEND_DIR = Path(__file__).resolve().parents[1]
# On serverless (Vercel) the app directory is read-only; only /tmp is writable.
_STATE_DIR = Path(tempfile.gettempdir()) if os.getenv("VERCEL") else _BACKEND_DIR
_QUOTATION_SHEET = os.getenv("QUOTATION_SHEET", "QTY=1-5")
# A quotation imported from Google Drive is cached here and preferred.
ACTIVE_QUOTATION = _STATE_DIR / ".active_quotation.xlsx"
ACTIVE_META = _STATE_DIR / ".active_quotation.json"


class QuotationError(RuntimeError):
    """Raised when the quotation file cannot be found or parsed."""


def _norm_key(value: object) -> str:
    """Normalise a product code for matching.

    NFKC folds full-width characters (e.g. ＣＥ００１（Ｐｏｃｋｅｔ） → CE001(Pocket))
    — supplier files are often typed with a CJK keyboard — then whitespace is
    removed and case folded, so 'CE001 (Pocket)' == 'ce001(pocket)'.
    """
    text = unicodedata.normalize("NFKC", str(value))
    return "".join(text.casefold().split())


def _within_one_edit(a: str, b: str) -> bool:
    """True when a and b are at most one insert/delete/substitute apart.

    Catches single-letter supplier typos like 'poket' vs 'pocket'.
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la > lb:  # ensure a is the shorter
        a, b, la, lb = b, a, lb, la
    i = j = 0
    edited = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        if edited:
            return False
        edited = True
        if la == lb:
            i += 1  # substitution
        j += 1  # (or insertion into the longer string)
    return True


def _resolve_path() -> Optional[Path]:
    env = os.getenv("QUOTATION_FILE")
    if env:
        p = Path(env)
        return p if p.exists() else None
    if ACTIVE_QUOTATION.exists():
        return ACTIVE_QUOTATION
    # Otherwise pick the first *quotation*.xlsx in the backend directory.
    matches = sorted(
        p
        for p in _BACKEND_DIR.glob("*.xlsx")
        if "quotation" in p.name.lower()
    )
    return matches[0] if matches else None


def set_active_quotation(content: bytes, source: Optional[str] = None) -> "Quotation":
    """Persist a Drive-imported quotation as the active file and reload it."""
    ACTIVE_QUOTATION.write_bytes(content)
    try:
        ACTIVE_META.write_text(
            json.dumps({"source": source or ""}), encoding="utf-8"
        )
    except OSError:
        pass
    get_quotation.cache_clear()
    q = get_quotation()
    if q is None:
        raise QuotationError("Imported quotation could not be parsed.")
    return q


def active_source() -> Optional[str]:
    """Name of the Drive file backing the active quotation, if any."""
    try:
        if ACTIVE_META.exists():
            data = json.loads(ACTIVE_META.read_text(encoding="utf-8"))
            return data.get("source") or None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _extract_country_label(header: object) -> Optional[str]:
    """'Total to \\nCZ' -> 'CZ'; 'Total to GB-remote area' -> 'GB-REMOTE AREA'.

    The full (collapsed, uppercased) label is kept so invoice country values
    like 'GB-REMOTE AREA' match the column directly. Newer files also add
    columns headed just 'NEW line to US' (no 'Total to' prefix) for the
    special shipping line — accepted verbatim as 'NEW LINE TO US'.
    """
    if not header:
        return None
    text = " ".join(str(header).split()).upper()
    if "TOTAL TO" in text:
        label = text.split("TOTAL TO", 1)[1].strip()
    elif text.startswith("NEW LINE TO "):
        label = text
    else:
        return None
    if not label:
        return None
    # Skip the free-text aggregate "remote countries ZM KE LK ..." column.
    if label.startswith("REMOTE COUNTRIES") or len(label) > 28:
        return None
    return label


def _zip_digits(value: object) -> Optional[int]:
    """Normalise a postcode to an int ('4555', '4555.0', ' 4555 ' → 4555)."""
    if value is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _zip_from_address(*texts: Optional[str]) -> Optional[int]:
    """Best-effort AU postcode from free-text address parts.

    Australian postcodes are 3–4 digit numbers; when several digit groups
    appear (house number vs postcode) the LAST 4-digit group wins.
    """
    for text in texts:
        if not text:
            continue
        groups = re.findall(r"\b(\d{3,4})\b", str(text))
        four = [g for g in groups if len(g) == 4]
        pick = four[-1] if four else (groups[-1] if groups else None)
        if pick:
            return int(pick)
    return None


class PriceTable:
    """One pricing sheet: standard line ('Total QTY') or special line."""

    def __init__(
        self,
        lookup: Dict[str, Dict[int, Dict[str, float]]],
        marginal: Dict[str, Dict[str, float]],
        alias: Dict[str, str],
        special: bool,
    ) -> None:
        self.lookup = lookup      # sku -> qty -> country_label -> price
        self.marginal = marginal  # sku -> country_label -> per-unit add-on
        self.alias = alias        # invoice-country alias -> column label
        self.special = special

    def resolve_label(self, c: str) -> Optional[str]:
        hit = self.alias.get(c)
        if hit is None:
            hit = self.alias.get(c + " AREA")
        if hit is None and "-" in c:
            hit = self.alias.get(c.split("-", 1)[0].strip())
        return hit


class Quotation:
    def __init__(
        self,
        tables: List[PriceTable],
        countries: List[str],
        name: str,
        path: Optional[Path] = None,
        au_zones: Optional[Dict[int, int]] = None,
        upsell: Optional[PriceTable] = None,
    ) -> None:
        # SKU keys are stored normalised (see _norm_key); lookups resolve
        # through _resolve so ASCII/full-width, case, spacing and one-letter
        # typos in either file still match.
        self._tables = tables      # standard table(s) first, special last
        self._upsell = upsell      # 'Upsell' sheet: per-unit rate for 2nd+ units
        self.countries = countries
        self.name = name           # display name of the source file
        self.path = path           # local path when file-backed (None for Drive)
        self.au_zones = au_zones or {}  # AU postcode -> zone number (1..4)
        self._all_skus = set()
        for t in tables:
            self._all_skus.update(t.lookup.keys())
        self._resolve_cache: Dict[str, Optional[str]] = {}

    @property
    def sku_count(self) -> int:
        return len(self._all_skus)

    def _resolve(self, sku: str) -> Optional[str]:
        """Map a requested product code onto a stored quotation key."""
        query = _norm_key(sku)
        if query in self._all_skus:
            return query
        if query in self._resolve_cache:
            return self._resolve_cache[query]
        # Fuzzy fallback: accept a single-edit difference (supplier typo like
        # 'poket' vs 'pocket') only when exactly ONE stored key qualifies.
        candidates = [k for k in self._all_skus if _within_one_edit(query, k)]
        resolved = candidates[0] if len(candidates) == 1 else None
        if resolved:
            logger.info("Quotation key %r fuzzily matched to %r.", sku, resolved)
        self._resolve_cache[query] = resolved
        return resolved

    def _country_plan(self, country: str) -> Tuple[List[str], List[PriceTable]]:
        """Candidate labels + table order for an invoice country value.

        'NEW LINE TO US' first tries a literal 'NEW LINE TO US' column (newer
        files put the special line in the main sheet), then falls back to the
        bare country in the special-line table. Everything else prefers the
        standard table, falling back to the special one for products that only
        ship on the special line (liquids/creams).
        """
        c = " ".join(str(country or "").split()).upper()
        if not c:
            return [], self._tables
        special_first = c.startswith("NEW LINE TO ")
        candidates = [c]
        if special_first:
            candidates.append(c.rsplit(" ", 1)[-1])
        tables = sorted(self._tables, key=lambda t: t.special != special_first)
        return candidates, tables

    def price(self, sku: str, qty: int, country: str) -> Optional[float]:
        key = self._resolve(sku)
        if key is None:
            return None
        candidates, tables = self._country_plan(country)
        for cand in candidates:
            for t in tables:
                col = t.resolve_label(cand)
                if col is None:
                    continue
                value = t.lookup.get(key, {}).get(qty, {}).get(col)
                if value is not None:
                    return value
        return None

    def _max_tier(self, sku: str) -> int:
        key = self._resolve(sku)
        if key is None:
            return 0
        best = 0
        for t in self._tables:
            tiers = t.lookup.get(key)
            if tiers:
                best = max(best, max(tiers))
        return best

    def upsell_rate(self, sku: str, country: str) -> Optional[float]:
        """Per-unit price for 2nd+ units, from the quotation's 'Upsell' sheet."""
        if self._upsell is None:
            return None
        key = self._resolve(sku)
        if key is None:
            return None
        candidates, _ = self._country_plan(country)
        for cand in candidates:
            col = self._upsell.resolve_label(cand)
            if col is None:
                continue
            value = self._upsell.lookup.get(key, {}).get(1, {}).get(col)
            if value is not None:
                return value
        return None

    def marginal(self, sku: str, country: str) -> Optional[float]:
        key = self._resolve(sku)
        if key is None:
            return None
        candidates, tables = self._country_plan(country)
        for cand in candidates:
            for t in tables:
                col = t.resolve_label(cand)
                if col is None:
                    continue
                value = t.marginal.get(key, {}).get(col)
                if value is not None:
                    return value
        return None

    def _resolve_au_column(
        self, items: List[InvoiceLineItem]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Map an AU order to its zone price column ('AU-1'..'AU-4').

        The postcode comes from the invoice's Zip column, falling back to the
        free-text address, and is looked up in the AU Zone sheet.
        """
        if not self.au_zones:
            return None, "quotation has no AU Zone sheet to resolve zones"

        zipcode = None
        for it in items:
            zipcode = _zip_digits(getattr(it, "zip_code", None))
            if zipcode is not None:
                break
        if zipcode is None:
            for it in items:
                zipcode = _zip_from_address(
                    getattr(it, "address", None), getattr(it, "city", None)
                )
                if zipcode is not None:
                    break
        if zipcode is None:
            return None, "AU order has no postcode to resolve a shipping zone"

        zone = self.au_zones.get(zipcode)
        if zone is None:
            return None, f"AU postcode {zipcode} is not in the AU Zone table"
        return f"AU-{zone}", None

    def expected_order_price(
        self, items: List[InvoiceLineItem], country: Optional[str]
    ) -> Tuple[Optional[float], Optional[str]]:
        """Return (expected_pre_tax_price, error_reason) for one order.

        Combined-shipment model: the most expensive product carries its full
        quotation price at its quantity; every other product's units add the
        quotation's per-unit increment (tier-2 minus tier-1).
        """
        cc = (country or "").strip().upper()
        if not cc:
            return None, "no destination country on the invoice line"

        # Australia is priced per zone (AU-1..AU-4): resolve the order's
        # postcode against the quotation's "AU Zone" sheet.
        if cc == "AU":
            cc, reason = self._resolve_au_column(items)
            if cc is None:
                return None, reason

        by_base: Dict[str, int] = defaultdict(int)
        for it in items:
            base = (it.product or "").strip()
            if base:
                by_base[base] += int(round(it.quantity or 0))
        if not by_base:
            return None, "no product code on the invoice lines"

        # Primary = base product with the highest single-unit price.
        def unit_price(base: str) -> float:
            v = self.price(base, 1, cc)
            return v if v is not None else -1.0

        primary = max(by_base, key=unit_price)
        pq = by_base[primary]

        # Newer quotations carry an 'Upsell' sheet: the supplier bills the
        # first unit at the regular qty-1 price and EVERY additional unit
        # (extra units of the primary included) at that product's upsell rate.
        if self._upsell is not None:
            first = self.price(primary, 1, cc)
            if first is None:
                return None, f"no quotation for {primary} to {cc}"
            total = first
            for base, qty in by_base.items():
                extra = qty - 1 if base == primary else qty
                if extra <= 0:
                    continue
                rate = self.upsell_rate(base, cc)
                if rate is None:
                    # SKU absent from the Upsell sheet — old marginal fallback.
                    marg = self.marginal(base, cc)
                    if marg is None:
                        return None, f"no upsell rate for {base} to {cc}"
                    rate = marg + self._variant_premium(base, cc)
                total += rate * extra
            return round(total, 2), None

        base_price = self.price(primary, pq, cc)
        if base_price is None:
            # Quantity beyond the tier table → extrapolate from the top tier.
            top = self._max_tier(primary)
            top_price = self.price(primary, top, cc) if top else None
            marg = self.marginal(primary, cc)
            if top_price is not None and marg is not None and pq > top:
                base_price = round(top_price + marg * (pq - top), 2)
        if base_price is None:
            return None, f"no quotation for {primary} ×{pq} to {cc}"

        total = base_price
        for base, qty in by_base.items():
            if base == primary:
                continue
            marg = self.marginal(base, cc)
            if marg is None:
                return None, f"no add-on rate for {base} to {cc}"
            # Variant premiums (e.g. '(Pocket)') are baked into base tiers but
            # NOT into tier-to-tier marginals; the supplier still bills them
            # per add-on unit, so add the qty-1 premium over the plain sibling.
            total += (marg + self._variant_premium(base, cc)) * qty
        return round(total, 2), None

    def _variant_premium(self, base: str, cc: str) -> float:
        """Per-unit premium of a qualified variant over its plain sibling.

        'CE001(Pocket)' -> price('CE001(Pocket)',1) - price('CE001',1) when the
        plain sibling exists and the difference is positive; otherwise 0.
        """
        key = _norm_key(base)
        if "(" not in key:
            return 0.0
        sibling = key.split("(", 1)[0]
        if sibling not in self._all_skus:
            return 0.0
        variant_p1 = self.price(base, 1, cc)
        sibling_p1 = self.price(sibling, 1, cc)
        if variant_p1 is None or sibling_p1 is None:
            return 0.0
        premium = variant_p1 - sibling_p1
        return premium if premium > 0 else 0.0


def _extract_au_zones(wb) -> Dict[int, int]:
    """Read the 'AU Zone' sheet (ZIP -> Zone) if the workbook has one."""
    sheet = None
    for name in wb.sheetnames:
        n = str(name).strip().lower()
        if "au" in n and "zone" in n:
            sheet = name
            break
    if sheet is None:
        return {}

    zones: Dict[int, int] = {}
    for row in wb[sheet].iter_rows(values_only=True):
        if not row or row[0] is None or len(row) < 2:
            continue
        try:
            zipcode = int(float(row[0]))
        except (TypeError, ValueError):
            continue  # header row ('ZIP') and stray text
        # Zone may be numeric (1) or text ('Zone 1') depending on file version.
        zone_digits = re.sub(r"[^0-9]", "", str(row[1]))
        if not zone_digits:
            continue
        zones[zipcode] = int(zone_digits)
    return zones


def _parse(path: Path) -> Quotation:
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise QuotationError(f"Could not open quotation file {path}: {exc}") from exc
    return _parse_workbook(wb, name=path.name, path=path)


def parse_quotation_bytes(content: bytes, name: str) -> Quotation:
    """Parse a quotation directly from bytes (e.g. downloaded from Drive)."""
    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(content), data_only=True, read_only=True
        )
    except Exception as exc:  # noqa: BLE001
        raise QuotationError(f"Could not open quotation '{name}': {exc}") from exc
    return _parse_workbook(wb, name=name, path=None)


def _pick_pricing_sheet(sheetnames: List[str]) -> str:
    """Choose the tiered pricing sheet across file versions.

    Preference: configured name -> any sheet mentioning 'qty' (the supplier
    renamed 'QTY=1-5' to 'Total QTY' in July 2026) -> the first sheet.
    """
    if _QUOTATION_SHEET in sheetnames:
        return _QUOTATION_SHEET
    for s in sheetnames:
        if "qty" in str(s).lower():
            return s
    candidates = [s for s in sheetnames if "upsell" not in str(s).lower()]
    return candidates[0] if candidates else sheetnames[0]


def _parse_price_sheet(rows, special: bool) -> Optional[PriceTable]:
    """Parse one pricing sheet into a PriceTable (None if not a price sheet).

    All pricing sheets share the layout: row 2 holds 'Total to <country>'
    labels, a QTY column, and SKU rows with per-quantity tiers.
    """
    header = rows[1]
    country_col: Dict[str, int] = {}
    for idx, val in enumerate(header):
        label = _extract_country_label(val)
        if label and label not in country_col:
            country_col[label] = idx
    if not country_col:
        return None

    # Locate the QTY column from the header row (fallback to index 4).
    qty_idx = 4
    for idx, val in enumerate(header):
        if val and str(val).strip().upper() in ("QTY", "QUANTITY"):
            qty_idx = idx
            break

    lookup: Dict[str, Dict[int, Dict[str, float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    current: Optional[str] = None
    for row in rows[2:]:
        sku = row[0] if row else None
        if sku is not None and str(sku).strip():
            current = _norm_key(sku)  # normalised storage key
        if current is None or qty_idx >= len(row):
            continue
        qval = row[qty_idx]
        if qval is None:
            continue
        try:
            qty = int(float(qval))
        except (TypeError, ValueError):
            continue
        for label, idx in country_col.items():
            if idx < len(row) and isinstance(row[idx], (int, float)):
                lookup[current][qty][label] = round(float(row[idx]), 4)

    if not lookup:
        return None

    # Per-unit add-on = tier-2 minus tier-1 (falls back to tier-1 if only one).
    marginal: Dict[str, Dict[str, float]] = defaultdict(dict)
    for sku, tiers in lookup.items():
        for label in country_col:
            t1 = tiers.get(1, {}).get(label)
            t2 = tiers.get(2, {}).get(label)
            if t1 is not None and t2 is not None:
                marginal[sku][label] = round(t2 - t1, 4)
            elif t1 is not None:
                marginal[sku][label] = round(t1, 4)

    # Aliases: full label; ' AREA'-less form ('GB-REMOTE AREA' -> 'GB-REMOTE');
    # and the bare country for carrier-suffixed labels ('US 4PX' -> 'US').
    alias: Dict[str, str] = {}
    for label in sorted(country_col):
        alias.setdefault(label, label)
        if label.endswith(" AREA"):
            alias.setdefault(label[: -len(" AREA")].strip(), label)
        tokens = label.split()
        if len(tokens) > 1 and not any(
            t in ("AREA", "TAX", "INCLUDED", "LINE") for t in tokens
        ):
            alias.setdefault(tokens[0], label)

    return PriceTable(lookup=lookup, marginal=marginal, alias=alias, special=special)


def _parse_workbook(wb, name: str, path: Optional[Path]) -> Quotation:
    main = _pick_pricing_sheet(wb.sheetnames)
    # Additional pricing tables — the 'Special shipping line' sheet prices the
    # alternative line (liquids/creams and 'NEW LINE TO <CC>' shipments).
    extra = [
        s for s in wb.sheetnames if s != main and "special" in str(s).lower()
    ]

    tables: List[PriceTable] = []
    used_sheets: List[str] = []
    countries: set = set()
    for sname, special in [(main, False)] + [(s, True) for s in extra]:
        rows = [r for r in wb[sname].iter_rows(values_only=True)]
        if len(rows) < 3:
            continue
        table = _parse_price_sheet(rows, special=special)
        if table:
            tables.append(table)
            used_sheets.append(sname)
            countries.update(
                lbl for sku in table.lookup.values() for t in sku.values() for lbl in t
            )

    if not tables:
        raise QuotationError(
            f"No pricing rows parsed from quotation sheets {wb.sheetnames}."
        )

    # 'Upsell' sheet (same layout, single QTY=1 row per SKU): per-unit rate
    # billed for every unit after the first in a package.
    upsell_table: Optional[PriceTable] = None
    for sname in wb.sheetnames:
        if "upsell" not in str(sname).lower():
            continue
        rows = [r for r in wb[sname].iter_rows(values_only=True)]
        if len(rows) >= 3:
            upsell_table = _parse_price_sheet(rows, special=False)
        if upsell_table:
            used_sheets.append(sname)
            break

    au_zones = _extract_au_zones(wb)

    quotation = Quotation(
        tables=tables,
        countries=sorted(countries),
        name=name,
        path=path,
        au_zones=au_zones,
        upsell=upsell_table,
    )
    logger.info(
        "Loaded quotation from %s: %d SKUs, %d countries, %d AU zone postcodes "
        "(sheets %s).",
        name,
        quotation.sku_count,
        len(countries),
        len(au_zones),
        used_sheets,
    )
    return quotation


# In-memory cache of quotations fetched from Google Drive by file id. Keeps
# serverless instances stateless: /compare can name its quotation explicitly
# and any instance can serve it. TTL keeps supplier edits flowing through.
_DRIVE_QUOTE_TTL_SECONDS = 600
_drive_quotes: Dict[str, Tuple[float, Quotation]] = {}


def get_quotation_by_drive_id(file_id: str) -> Quotation:
    """Fetch + parse a quotation from Google Drive, with a short-TTL cache.

    Raises QuotationError (parse) or gdrive.DriveError (download/config).
    """
    now = time.time()
    hit = _drive_quotes.get(file_id)
    if hit and now - hit[0] < _DRIVE_QUOTE_TTL_SECONDS:
        return hit[1]

    from . import gdrive  # imported here to avoid a module cycle

    name, content = gdrive.download_file(file_id)
    quotation = parse_quotation_bytes(content, name)
    _drive_quotes[file_id] = (now, quotation)
    return quotation


@lru_cache(maxsize=1)
def get_quotation() -> Optional[Quotation]:
    """Return the active parsed quotation, or None if none is configured."""
    path = _resolve_path()
    if path is None:
        logger.warning(
            "No quotation file found (set QUOTATION_FILE or place a "
            "*quotation*.xlsx in the backend directory)."
        )
        return None
    try:
        return _parse(path)
    except QuotationError as exc:
        logger.error("Failed to load quotation: %s", exc)
        return None
