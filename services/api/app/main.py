"""FastAPI application. Runs on Lambda (via Mangum) or ECS Fargate (via uvicorn)."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import billing, vaults
from app.core.errors import VaultlaError

log = logging.getLogger("vaultla.api")

app = FastAPI(title="Vaultla.io API", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(vaults.router)
app.include_router(billing.router)


@app.exception_handler(VaultlaError)
async def _domain_error(_: Request, exc: VaultlaError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "detail": exc.detail}, status_code=exc.http_status)


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")               # details stay in logs, never in the response
    return JSONResponse({"error": "internal_error", "detail": "An internal error occurred."}, status_code=500)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    return resp


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


try:  # Lambda entrypoint (ignored on Fargate)
    from mangum import Mangum
    lambda_handler = Mangum(app, lifespan="off")
except ImportError:  # pragma: no cover
    pass
