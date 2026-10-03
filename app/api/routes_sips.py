from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.idempotency import run_idempotent
from app.sips import (
    cancel_sip,
    confirm_sip,
    instalments_for,
    list_sips,
    pause_sip,
    propose_sip,
    resume_sip,
    serialize_sip,
)

router = APIRouter()

IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class SipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: StrictStr
    frequency: Literal["daily", "weekly", "monthly"]
    day_of_month: StrictInt | None = None
    start_date: date | None = None


@router.post("/sips")
def create_sip(
    body: SipRequest,
    request: Request,
    idempotency_key: IdemKey = None,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
):
    state = request.app.state

    def handler() -> tuple[int, dict]:
        sip, existed, notes = propose_sip(
            session,
            user_id=user_id,
            amount=body.amount,
            frequency=body.frequency,
            day_of_month=body.day_of_month,
            start_date=body.start_date,
            clock=state.clock,
        )
        return (200 if existed else 201), serialize_sip(sip, existing=existed, notes=notes)

    return run_idempotent(
        session,
        key=idempotency_key,
        user_id=user_id,
        endpoint="POST /sips",
        body=body.model_dump(mode="json"),
        clock=state.clock,
        handler=handler,
    )


def _transition(name: str, fn):
    def endpoint(
        sip_id: UUID,
        request: Request,
        idempotency_key: IdemKey = None,
        user_id: str = Depends(get_user_id),
        session: Session = Depends(get_session),
    ):
        state = request.app.state

        def handler() -> tuple[int, dict]:
            result = fn(session, user_id=user_id, sip_id=sip_id, clock=state.clock)
            if name == "confirm":
                sip, replay = result
                return 200, serialize_sip(sip, idempotent_replay=replay)
            return 200, serialize_sip(result)

        return run_idempotent(
            session,
            key=idempotency_key,
            user_id=user_id,
            endpoint=f"POST /sips/{{id}}/{name}",
            body={"sip_id": str(sip_id)},
            clock=state.clock,
            handler=handler,
        )

    endpoint.__name__ = f"{name}_sip"
    return endpoint


for _name, _fn in (
    ("confirm", confirm_sip),
    ("pause", pause_sip),
    ("resume", resume_sip),
    ("cancel", cancel_sip),
):
    router.add_api_route(f"/sips/{{sip_id}}/{_name}", _transition(_name, _fn), methods=["POST"])


@router.get("/sips")
def get_sips(user_id: str = Depends(get_user_id), session: Session = Depends(get_session)) -> dict:
    return {
        "sips": [serialize_sip(s, instalments_for(session, s.id)) for s in list_sips(session, user_id)]
    }
