"""POST /upload and /upload-check — parse (and reconcile) an uploaded invoice."""
from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)

from ..auth import AuthUser, get_optional_user
from ..models import CompareRequest, ParsedInvoice
from ..parsers import MAX_FILE_BYTES, InvoiceParseError, parse_invoice

logger = logging.getLogger("invoice.upload")
router = APIRouter(tags=["upload"])

_ALLOWED_SUFFIXES = (".csv", ".xlsx", ".xlsm")


async def _read_and_parse(file: UploadFile) -> ParsedInvoice:
    filename = file.filename or "upload"
    if not filename.lower().endswith(_ALLOWED_SUFFIXES):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Please upload a .csv or .xlsx file.",
        )

    try:
        content = await file.read()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not read the uploaded file: {exc}",
        ) from exc
    finally:
        await file.close()

    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit.",
        )

    try:
        return parse_invoice(filename, content)
    except InvoiceParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error parsing %s", filename)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error while parsing the file: {exc}",
        ) from exc


@router.post("/upload", response_model=ParsedInvoice)
async def upload_invoice(
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_optional_user),
) -> ParsedInvoice:
    parsed = await _read_and_parse(file)
    logger.info(
        "user=%s parsed file=%s rows=%s", user.id, parsed.filename, parsed.row_count
    )
    return parsed


@router.post("/upload-check")
async def upload_and_check(
    file: UploadFile = File(...),
    quotation_file_id: str | None = Query(
        None, description="Drive file id of the quotation to price against."
    ),
    user: AuthUser = Depends(get_optional_user),
) -> dict:
    """Upload, parse AND reconcile in one request.

    The response carries per-order results only — parsed line items never
    travel back to the browser, so a large invoice can't produce an oversized
    follow-up request on serverless hosts (Vercel caps bodies at 4.5 MB).
    """
    from .compare import run_compare  # imported here to avoid a module cycle

    parsed = await _read_and_parse(file)
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
