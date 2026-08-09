"""Google Drive integration via a service account (headless / internal use).

The app reads invoice and quotation files straight from a shared Google Drive
folder. Authentication uses a service-account key — no interactive OAuth — so it
works on a server with no user present. Share the Drive folder with the service
account's email and the app can list and download the files.

Configuration (env):
  * GOOGLE_SERVICE_ACCOUNT_FILE  — path to the service-account JSON key, or
  * GOOGLE_SERVICE_ACCOUNT_JSON  — the key JSON inline
  * GOOGLE_DRIVE_FOLDER_ID       — (optional) restrict listing to this folder
"""
from __future__ import annotations

import io
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("invoice.gdrive")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Auto-detected key if neither env var is set (drop the JSON in the backend dir).
_DEFAULT_KEY = Path(__file__).resolve().parents[1] / "service-account.json"


def _key_path() -> Optional[Path]:
    env = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if env and os.path.exists(env):
        return Path(env)
    if _DEFAULT_KEY.exists():
        return _DEFAULT_KEY
    return None

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSM_MIME = "application/vnd.ms-excel.sheet.macroEnabled.12"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
CSV_MIME = "text/csv"
_SPREADSHEET_MIMES = (XLSX_MIME, XLSM_MIME, GSHEET_MIME, CSV_MIME)


class DriveError(RuntimeError):
    """Raised for Google Drive configuration / API failures."""


def _credentials():
    """Load service-account credentials from a file path or inline JSON."""
    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover
        raise DriveError(
            "google-auth is not installed. Run: pip install -r requirements.txt"
        ) from exc

    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DriveError(
                f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}"
            ) from exc
        return service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
    path = _key_path()
    if path is not None:
        return service_account.Credentials.from_service_account_file(
            str(path), scopes=SCOPES
        )
    return None


@lru_cache(maxsize=1)
def _service():
    creds = _credentials()
    if creds is None:
        return None
    try:
        from googleapiclient.discovery import build

        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to initialise Google Drive client: %s", exc)
        return None


def folder_id() -> Optional[str]:
    fid = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    return fid or None


def is_configured() -> bool:
    return _service() is not None


def service_account_email() -> Optional[str]:
    """Return the service account email (for the 'share the folder' hint)."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    try:
        if raw:
            return json.loads(raw).get("client_email")
        path = _key_path()
        if path is not None:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh).get("client_email")
    except (OSError, json.JSONDecodeError):
        return None
    return None


def list_files(in_folder: Optional[str] = None) -> List[dict]:
    """List spreadsheet/CSV files (optionally within a folder)."""
    svc = _service()
    if svc is None:
        raise DriveError("Google Drive is not configured on the server.")

    mime_q = " or ".join(f"mimeType='{m}'" for m in _SPREADSHEET_MIMES)
    parts = ["trashed = false", f"({mime_q})"]
    target = in_folder or folder_id()
    if target:
        parts.append(f"'{target}' in parents")
    query = " and ".join(parts)

    files: List[dict] = []
    page_token: Optional[str] = None
    try:
        while True:
            resp = (
                svc.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id,name,mimeType,modifiedTime,size)",
                    pageSize=200,
                    orderBy="modifiedTime desc",
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token or len(files) >= 1000:
                break
    except Exception as exc:  # noqa: BLE001
        raise DriveError(f"Failed to list Google Drive files: {exc}") from exc
    return files


def download_file(file_id: str) -> Tuple[str, bytes]:
    """Download a file by id, exporting Google Sheets to .xlsx. Returns (name, bytes)."""
    svc = _service()
    if svc is None:
        raise DriveError("Google Drive is not configured on the server.")

    from googleapiclient.http import MediaIoBaseDownload

    try:
        meta = (
            svc.files()
            .get(fileId=file_id, fields="id,name,mimeType", supportsAllDrives=True)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        raise DriveError(f"Could not read Drive file {file_id}: {exc}") from exc

    name = meta.get("name", file_id)
    mime = meta.get("mimeType", "")

    if mime == GSHEET_MIME:
        request = svc.files().export_media(fileId=file_id, mimeType=XLSX_MIME)
        if not name.lower().endswith(".xlsx"):
            name = f"{name}.xlsx"
    else:
        request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    try:
        done = False
        while not done:
            _, done = downloader.next_chunk()
    except Exception as exc:  # noqa: BLE001
        raise DriveError(f"Failed to download Drive file '{name}': {exc}") from exc
    return name, buffer.getvalue()
