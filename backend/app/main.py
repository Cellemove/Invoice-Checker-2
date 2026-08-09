"""FastAPI application entry point for the Invoice Checker backend."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .routers import compare, drive, shopify, upload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("invoice")

app = FastAPI(
    title="Invoice Checker API",
    description="Reconciles uploaded invoices against live Shopify order data.",
    version="1.0.0",
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload.router)
app.include_router(shopify.router)
app.include_router(compare.router)
app.include_router(drive.router)

# Dual-mount everything under /api as well: the Vercel rewrite forwards
# "/api/..." paths to this service unchanged, while local dev keeps the bare
# paths. Same handlers, two prefixes.
app.include_router(upload.router, prefix="/api")
app.include_router(shopify.router, prefix="/api")
app.include_router(compare.router, prefix="/api")
app.include_router(drive.router, prefix="/api")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort handler so the client always receives clean JSON."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"Internal server error: {exc}"},
    )


# Bumped on payload-limit fixes; open /api/health on the deployment to verify
# which build is actually live.
APP_VERSION = "2026-07-18.2"


@app.get("/health", tags=["meta"])
@app.get("/api/health", tags=["meta"], include_in_schema=False)
async def health() -> dict:
    """Liveness probe + configuration sanity report (no secrets revealed)."""
    return {
        "status": "ok",
        "version": APP_VERSION,
        "supabase_configured": bool(
            settings.supabase_url and settings.supabase_jwt_secret
        ),
        "shopify_stores": settings.list_store_keys(),
    }


def run() -> None:  # pragma: no cover - convenience entrypoint
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=True,
    )


if __name__ == "__main__":  # pragma: no cover
    run()
