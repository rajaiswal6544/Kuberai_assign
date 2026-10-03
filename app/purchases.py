"""Executing a quote. The quote row lock plus UNIQUE(purchases.quote_id) mean a quote is
charged at most once, however many times or however concurrently it is confirmed."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import Clock
from app.errors import AppError
from app.logging import get_logger
from app.models import LedgerEntry, Purchase, Quote

log = get_logger()


def serialize_purchase(p: Purchase, *, replay: bool = False) -> dict:
    return {
        "purchase_id": str(p.id),
        "source": p.source,
        "quote_id": str(p.quote_id) if p.quote_id else None,
        "sip_instalment_id": str(p.sip_instalment_id) if p.sip_instalment_id else None,
        "requested_amount": str(p.requested_amount),
        "price_per_gram": str(p.price_per_gram),
        "grams": str(p.grams),
        "gold_value": str(p.gold_value),
        "gst": str(p.gst),
        "total_charged": str(p.total_charged),
        "created_at": p.created_at.isoformat(),
        "idempotent_replay": replay,
    }


def _existing_purchase(session: Session, quote_id: uuid.UUID) -> Purchase | None:
    return session.scalars(select(Purchase).where(Purchase.quote_id == quote_id)).first()


def execute_quote(
    session: Session, *, user_id: str, quote_id: uuid.UUID, clock: Clock
) -> tuple[Purchase, bool]:
    """Returns (purchase, idempotent_replay)."""
    quote = session.scalars(
        select(Quote).where(Quote.id == quote_id, Quote.user_id == user_id).with_for_update()
    ).first()
    if quote is None:
        session.rollback()
        raise AppError(404, "quote_not_found", "That quote doesn't exist. Ask for a new quote.")

    if quote.status == "executed":
        purchase = _existing_purchase(session, quote.id)
        session.commit()
        assert purchase is not None
        log.info("purchase_replayed", quote_id=quote.id, purchase_id=purchase.id)
        return purchase, True

    now = clock.now()
    if quote.status == "expired" or quote.expires_at <= now:
        quote.status = "expired"
        session.commit()
        log.info("quote_expired", quote_id=quote.id)
        raise AppError(
            409,
            "quote_expired",
            "That quote has expired because prices move. Nothing was charged; ask for a fresh quote.",
            quote_id=str(quote.id),
        )

    purchase = Purchase(
        id=uuid.uuid4(),
        user_id=user_id,
        quote_id=quote.id,
        source="one_time",
        requested_amount=quote.requested_amount,
        price_per_gram=quote.price_per_gram,
        grams=quote.grams,
        gold_value=quote.gold_value,
        gst=quote.gst,
        total_charged=quote.total_charged,
        created_at=now,
    )
    session.add(purchase)
    session.flush()
    session.add(
        LedgerEntry(
            id=uuid.uuid4(),
            user_id=user_id,
            entry_type="purchase",
            purchase_id=purchase.id,
            grams=purchase.grams,
            total_charged=purchase.total_charged,
            created_at=now,
        )
    )
    quote.status = "executed"
    try:
        session.commit()
    except IntegrityError:
        # Backstop for the row lock: the unique constraint already holds a purchase.
        session.rollback()
        existing = _existing_purchase(session, quote_id)
        session.commit()
        if existing is None:
            raise
        log.info("purchase_replayed", quote_id=quote_id, purchase_id=existing.id)
        return existing, True

    log.info(
        "purchase_executed",
        quote_id=quote.id,
        purchase_id=purchase.id,
        grams=purchase.grams,
        total_charged=purchase.total_charged,
    )
    return purchase, False
