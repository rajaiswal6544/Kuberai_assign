"""Quotes: the backend prices a purchase and holds it for QUOTE_TTL_SECONDS. No money moves here."""

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.errors import AppError, price_unavailable
from app.logging import get_logger
from app.models import Quote
from app.money import UNSPENT_REASON, AmountError, calculate_purchase, validate_amount
from app.pricing import PriceService, PriceUnavailable

log = get_logger()


def serialize_quote(q: Quote) -> dict:
    return {
        "quote_id": str(q.id),
        "status": q.status,
        "requested_amount": str(q.requested_amount),
        "price_per_gram": str(q.price_per_gram),
        "grams": str(q.grams),
        "gold_value": str(q.gold_value),
        "gst": str(q.gst),
        "total_charged": str(q.total_charged),
        "unspent": str(q.requested_amount - q.total_charged),
        "unspent_reason": UNSPENT_REASON,
        "expires_at": q.expires_at.isoformat(),
    }


def amount_error(exc: AmountError, raw: object) -> AppError:
    log.info("quote_rejected", reason=exc.code, amount_input=str(raw)[:50])
    return AppError(422, exc.code, exc.message)


def create_quote(
    session: Session,
    *,
    user_id: str,
    amount: object,
    price_service: PriceService,
    clock: Clock,
    ttl_seconds: int,
) -> tuple[Quote, bool]:
    """Returns (quote, reused). An identical open quote (same amount, same price) is reused."""
    try:
        requested = validate_amount(amount)  # type: ignore[arg-type]
    except AmountError as exc:
        raise amount_error(exc, amount) from None

    try:
        point = price_service.current()
    except PriceUnavailable:
        log.warning("quote_rejected", reason="price_unavailable", amount=requested)
        raise price_unavailable() from None

    try:
        b = calculate_purchase(requested, point.price)
    except AmountError as exc:
        raise amount_error(exc, amount) from None

    now = clock.now()
    existing = session.scalars(
        select(Quote)
        .where(
            Quote.user_id == user_id,
            Quote.requested_amount == b.requested_amount,
            Quote.price_per_gram == b.price_per_gram,
            Quote.status == "open",
            Quote.expires_at > now,
        )
        .order_by(Quote.created_at.desc())
        .limit(1)
    ).first()
    if existing is not None:
        log.info("quote_reused", quote_id=existing.id, amount=existing.requested_amount)
        return existing, True

    quote = Quote(
        id=uuid.uuid4(),
        user_id=user_id,
        requested_amount=b.requested_amount,
        price_per_gram=b.price_per_gram,
        grams=b.grams,
        gold_value=b.gold_value,
        gst=b.gst,
        total_charged=b.total_charged,
        status="open",
        expires_at=now + timedelta(seconds=ttl_seconds),
        created_at=now,
    )
    session.add(quote)
    session.commit()
    log.info(
        "quote_created",
        quote_id=quote.id,
        amount=quote.requested_amount,
        price_per_gram=quote.price_per_gram,
        grams=quote.grams,
        total_charged=quote.total_charged,
    )
    return quote, False


def expire_quote(session: Session, user_id: str, quote_id: uuid.UUID) -> None:
    quote = session.scalars(
        select(Quote).where(Quote.id == quote_id, Quote.user_id == user_id).with_for_update()
    ).first()
    if quote is not None and quote.status == "open":
        quote.status = "expired"
    session.commit()
