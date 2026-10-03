"""Price, quotes, purchases and the ledger."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, StrictStr
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.config import GST_RATE
from app.errors import price_unavailable
from app.idempotency import run_idempotent
from app.ledger import get_ledger
from app.pricing import PriceUnavailable
from app.purchases import execute_quote, serialize_purchase
from app.quotes import create_quote, serialize_quote

router = APIRouter()

IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A string, never a JSON number: a float must not touch a money value.
    amount: StrictStr


class PurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: UUID


@router.get("/price")
def get_price(request: Request) -> dict:
    service = request.app.state.price_service
    try:
        point = service.current()
    except PriceUnavailable:
        raise price_unavailable() from None
    age = (request.app.state.clock.now() - point.as_of).total_seconds()
    return {
        "price_per_gram": str(point.price),
        "currency": "INR",
        "excludes_gst": True,
        "gst_rate": str(GST_RATE),
        "as_of": point.as_of.isoformat(),
        "age_seconds": round(age, 1),
    }


@router.post("/quotes")
def post_quote(
    body: QuoteRequest,
    request: Request,
    idempotency_key: IdemKey = None,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
):
    state = request.app.state

    def handler() -> tuple[int, dict]:
        quote, reused = create_quote(
            session,
            user_id=user_id,
            amount=body.amount,
            price_service=state.price_service,
            clock=state.clock,
            ttl_seconds=state.settings.quote_ttl_seconds,
        )
        return (200 if reused else 201), {**serialize_quote(quote), "reused": reused}

    return run_idempotent(
        session,
        key=idempotency_key,
        user_id=user_id,
        endpoint="POST /quotes",
        body=body.model_dump(mode="json"),
        clock=state.clock,
        handler=handler,
    )


@router.post("/purchases")
def post_purchase(
    body: PurchaseRequest,
    request: Request,
    idempotency_key: IdemKey = None,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
):
    state = request.app.state

    def handler() -> tuple[int, dict]:
        purchase, replay = execute_quote(
            session, user_id=user_id, quote_id=body.quote_id, clock=state.clock
        )
        return (200 if replay else 201), serialize_purchase(purchase, replay=replay)

    return run_idempotent(
        session,
        key=idempotency_key,
        user_id=user_id,
        endpoint="POST /purchases",
        body=body.model_dump(mode="json"),
        clock=state.clock,
        handler=handler,
    )


@router.get("/users/me/ledger")
def ledger(user_id: str = Depends(get_user_id), session: Session = Depends(get_session)) -> dict:
    return get_ledger(session, user_id)
