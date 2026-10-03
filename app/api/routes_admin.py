"""Operator endpoints. Protected by a shared X-Admin-Token (a stopgap, not real auth)."""

import hmac
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.config import DEFAULT_ADMIN_TOKEN
from app.errors import AppError
from app.logging import get_logger
from app.sips import run_due

log = get_logger()

APP_TABLES = (
    "users, quotes, purchases, sips, sip_instalments, ledger_entries, pending_actions, idempotency_keys"
)


def require_admin(
    request: Request, x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None
) -> None:
    settings = request.app.state.settings
    expected = settings.admin_token.get_secret_value() if settings.admin_token else ""
    usable = bool(expected) and not (settings.env == "production" and expected == DEFAULT_ADMIN_TOKEN)
    if not usable or not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        log.warning("security_event", kind="admin_auth_failed", path=request.url.path)
        raise AppError(401, "unauthorized", "This admin endpoint needs a valid X-Admin-Token header.")


def require_non_production(request: Request) -> None:
    if request.app.state.settings.env == "production":
        raise AppError(403, "not_allowed_in_production", "This demo helper is disabled when ENV=production.")


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class PriceSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["up", "down"]
    # Simulate an outage that has already outlasted PRICE_MAX_AGE_SECONDS, so the cached
    # last-good price can't be used either. Set false to test the cache window.
    expire_cache: bool = True


class ClockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    advance_seconds: int = Field(gt=0, le=366 * 24 * 3600)


@router.post("/run-due")
def post_run_due(request: Request, as_of: date | None = None) -> dict:
    """as_of is an IST calendar date; default is today in IST."""
    state = request.app.state
    return run_due(
        state.session_factory,
        as_of=as_of or state.clock.today(),
        price_service=state.price_service,
        clock=state.clock,
        max_catch_up=state.settings.max_catch_up_instalments,
        max_attempts=state.settings.max_retry_attempts,
    )


@router.post("/price-source")
def post_price_source(body: PriceSourceRequest, request: Request) -> dict:
    service = request.app.state.price_service
    service.provider.mode = body.mode
    if body.mode == "down" and body.expire_cache:
        service.expire_cache()
    log.warning("price_source_changed", mode=body.mode, expire_cache=body.expire_cache)
    return {"mode": body.mode, "price_source": service.status()}


@router.post("/clock", dependencies=[Depends(require_non_production)])
def post_clock(body: ClockRequest, request: Request) -> dict:
    clock = request.app.state.clock
    clock.advance(body.advance_seconds)
    offset = int(clock.offset.total_seconds())
    log.warning("clock_advanced", advance_seconds=body.advance_seconds, offset_seconds=offset, now=clock.now())
    return {"offset_seconds": offset, "now": clock.now().isoformat(), "today": clock.today().isoformat()}


@router.post("/reset", dependencies=[Depends(require_non_production)])
def post_reset(request: Request) -> dict:
    state = request.app.state
    with state.engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {APP_TABLES} CASCADE"))
    state.price_service.expire_cache()
    state.price_service.provider.mode = "up"
    state.clock.reset()
    log.warning("demo_reset", tables=APP_TABLES)
    return {"reset": True, "tables": APP_TABLES.split(", ")}
