"""App factory. Run with: uvicorn app.main:create_app --factory"""

import re
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes_admin import router as admin_router
from app.api.routes_chat import router as chat_router
from app.api.routes_health import router as health_router
from app.api.routes_money import router as money_router
from app.api.routes_sips import router as sips_router
from app.clock import Clock, OffsetClock, SystemClock
from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.errors import AppError, error_body
from app.llm.base import LLMClient, create_llm
from app.logging import configure_logging, get_logger
from app.pricing import PriceService, SimulatedPriceProvider

log = get_logger()

# Client-supplied request ids are accepted only if they are short and safe to log.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DEFAULT = object()


def _validation_message(errors: list[dict]) -> str:
    for err in errors:
        loc, kind = err.get("loc", ()), err.get("type")
        if "amount" in loc and kind == "string_type":
            return 'amount must be a JSON string such as "500.00" so money stays exact.'
        if "message" in loc and kind == "string_too_long":
            return "Your message is too long. Please keep it under 1000 characters."
        if kind == "json_invalid":
            return "The request body is not valid JSON."
    return "The request body is invalid."


def create_app(
    settings: Settings | None = None,
    clock: Clock | None = None,
    llm: LLMClient | None | object = _DEFAULT,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings)
    # Every component shares one clock; /admin/clock can move it forward (non-production).
    clock = OffsetClock(clock or SystemClock(settings.business_tz), settings.business_tz)
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            init_db(engine)
            log.info("db_initialized")
        except SQLAlchemyError as exc:
            # Keep serving: /health reports the DB as down instead of the process crashing.
            log.error("db_init_failed", error_type=type(exc).__name__)
        yield
        engine.dispose()

    app = FastAPI(title="Digital Gold Backend", lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.price_service = PriceService(
        SimulatedPriceProvider(settings.price_base, clock, settings.price_source_mode),
        clock,
        settings.price_max_age_seconds,
    )
    app.state.llm = create_llm(settings) if llm is _DEFAULT else llm

    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(exc.to_body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        log.info("request_invalid", errors=[{"loc": e.get("loc"), "type": e.get("type")} for e in errors])
        body = error_body(
            "invalid_request",
            _validation_message(errors),
            details=[{"loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in errors],
        )
        return JSONResponse(body, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(error_body(code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        # Logged by the middleware with the traceback; the client gets the same envelope.
        return JSONResponse(
            error_body("internal_error", "Something went wrong on our side. Nothing was charged twice; please retry."),
            status_code=500,
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if _REQUEST_ID_RE.match(supplied) else uuid4().hex
        user_id = request.headers.get("x-user-id") or None
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, user_id=user_id[:64] if user_id else None
        )
        request.state.request_id = request_id
        start = time.perf_counter()
        log.info("request_received", method=request.method, path=request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled_error")
            raise
        extra = {}
        if request.url.path == "/chat":
            extra["intent"] = getattr(request.state, "intent", None)
        log.info(
            "response_sent",
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
            **extra,
        )
        response.headers["X-Request-Id"] = request_id
        return response

    for router in (health_router, money_router, sips_router, chat_router, admin_router):
        app.include_router(router)
    return app
