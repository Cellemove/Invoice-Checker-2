"""Google Drive endpoints: list files, import an invoice, set the quotation."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import AuthUser, get_optional_user
from ..gdrive import (
    DriveError,
    download_file,
    folder_id,
    is_configured,
    list_files,
    service_account_email,
)
from ..models import CompareRequest, ParsedInvoice
from ..parsers import InvoiceParseError, parse_invoice
from ..quotation import active_source, get_quotation, set_active_quotation

logger = logging.getLogger("invoice.drive")
router = APIRouter(tags=["drive"], prefix="/drive")


@router.get("/status")
async def drive_status(user: AuthUser = Depends(get_optional_user)) -> dict:
    configured = is_configured()
    q = get_quotation()
    return {
        "configured": configured,
        "folder_id": folder_id(),
        "service_account_email": service_account_email() if configured else None,
        "active_quotation": q.name if q else None,
        "active_quotation_source": active_source(),
    }


@router.get("/files")
async def drive_files(
    folder: str | None = Query(None, description="Drive folder id to list."),
    user: AuthUser = Depends(get_optional_user),
) -> dict:
    try:
        files = list_files(folder)
    except DriveError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"files": files}


@router.post("/import-invoice", response_model=ParsedInvoice)
async def import_invoice(
    file_id: str = Query(..., description="Drive file id of the invoice."),
    user: AuthUser = Depends(get_optional_user),
) -> ParsedInvoice:
    try:
        name, content = download_file(file_id)
    except DriveError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    try:
        return parse_invoice(name, content)
    except InvoiceParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post("/check")
async def check_drive_invoice(
    file_id: str = Query(..., description="Drive file id of the invoice."),
    quotation_file_id: str | None = Query(
        None, description="Drive file id of the quotation to price against."
    ),
    user: AuthUser = Depends(get_optional_user),
) -> dict:
    """Download, parse and reconcile a Drive invoice in ONE request.

    The browser sends only the two file ids and receives per-order results —
    line items never cross the wire, so serverless payload limits (4.5 MB on
    Vercel) no longer cap the invoice size.
    """
    from .compare import run_compare  # imported here to avoid a module cycle

    try:
        name, content = download_file(file_id)
    except DriveError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    try:
        parsed = parse_invoice(name, content)
    except InvoiceParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    payload = CompareRequest(
        filename=parsed.filename,
        line_items=parsed.line_items,
        invoice_total=parsed.invoice_total,
        quotation_file_id=quotation_file_id,
        persist=True,
    )
    result = await run_compare(payload, user)

    return {
        "invoice": {
            "filename": parsed.filename,
            "row_count": parsed.row_count,
            "sheets_used": parsed.sheets_used,
            "sheets_skipped": parsed.sheets_skipped,
        },
        "result": result,
    }


@router.post("/set-quotation")
async def set_quotation(
    file_id: str = Query(..., description="Drive file id of the quotation."),
    user: AuthUser = Depends(get_optional_user),
) -> dict:
    try:
        name, content = download_file(file_id)
    except DriveError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    try:
        q = set_active_quotation(content, source=name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Could not parse the quotation '{name}': {exc}",
        ) from exc
    return {
        "quotation": name,
        "file_id": file_id,
        "sku_count": q.sku_count,
        "countries": len(q.countries),
    }
