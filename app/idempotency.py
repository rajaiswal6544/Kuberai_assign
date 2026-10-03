"""Idempotency-Key handling for POST endpoints.

The key is reserved with an INSERT before the work runs, so the primary key - not a
check-then-insert in Python - decides which request does the work:
  same key + same body  -> stored response replayed
  same key + other body -> 409
  same key while the first request is still running -> 409 (retry shortly)
5xx outcomes are not stored, so a retry after e.g. a price outage does real work.
"""

import hashlib
import json
from collections.abc import Callable

from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.clock import Clock
from app.errors import AppError
from app.logging import get_logger
from app.models import IdempotencyKey

log = get_logger()

MAX_KEY_LENGTH = 255


def request_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def run_idempotent(
    session: Session,
    *,
    key: str | None,
    user_id: str,
    endpoint: str,
    body: dict,
    clock: Clock,
    handler: Callable[[], tuple[int, dict]],
) -> JSONResponse:
    if key is None:
        status, payload = _call(handler)
        return JSONResponse(payload, status_code=status)

    if not key or len(key) > MAX_KEY_LENGTH:
        raise AppError(400, "invalid_idempotency_key", "Idempotency-Key must be 1-255 characters.")

    digest = request_hash(body)
    reserved = session.execute(
        insert(IdempotencyKey)
        .values(
            key=key, user_id=user_id, endpoint=endpoint, request_hash=digest, created_at=clock.now()
        )
        .on_conflict_do_nothing()
        .returning(IdempotencyKey.key)
    ).first()
    session.commit()

    if reserved is None:
        row = session.scalars(
            select(IdempotencyKey).where(
                IdempotencyKey.key == key,
                IdempotencyKey.user_id == user_id,
                IdempotencyKey.endpoint == endpoint,
            )
        ).one()
        session.commit()
        if row.request_hash != digest:
            raise AppError(
                409,
                "idempotency_key_reused",
                "This Idempotency-Key was already used with a different request body.",
            )
        if row.status_code is None:
            raise AppError(
                409, "idempotency_in_progress", "The original request is still being processed."
            )
        log.info("idempotency_replay", endpoint=endpoint, idempotency_key=key)
        return JSONResponse(
            row.response_json, status_code=row.status_code, headers={"Idempotent-Replay": "true"}
        )

    try:
        status, payload = _call(handler)
    except Exception:
        _release(session, key, user_id, endpoint)
        raise

    if status >= 500:
        _release(session, key, user_id, endpoint)
    else:
        row = session.get(IdempotencyKey, (key, user_id, endpoint))
        assert row is not None
        row.status_code = status
        row.response_json = payload
        session.commit()
    return JSONResponse(payload, status_code=status)


def _call(handler: Callable[[], tuple[int, dict]]) -> tuple[int, dict]:
    try:
        return handler()
    except AppError as exc:
        return exc.status, exc.to_body()


def _release(session: Session, key: str, user_id: str, endpoint: str) -> None:
    session.rollback()
    session.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.key == key,
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.endpoint == endpoint,
        )
    )
    session.commit()
