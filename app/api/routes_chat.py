import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.chat.orchestrator import ChatDeps, handle_chat
from app.idempotency import run_idempotent

router = APIRouter()

MAX_MESSAGE_CHARS = 1000


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Empty is allowed (gets a friendly prompt); too long is a 422, never silently truncated.
    message: Annotated[StrictStr, Field(max_length=MAX_MESSAGE_CHARS)]


@router.post("/chat")
def chat(
    body: ChatRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
):
    state = request.app.state
    deps = ChatDeps(
        session_factory=state.session_factory,
        clock=state.clock,
        settings=state.settings,
        price_service=state.price_service,
        llm=state.llm,
    )
    response = run_idempotent(
        session,
        key=idempotency_key,
        user_id=user_id,
        endpoint="POST /chat",
        body=body.model_dump(),
        clock=state.clock,
        handler=lambda: (200, handle_chat(deps, user_id, body.message)),
    )
    try:
        request.state.intent = json.loads(response.body).get("intent")  # for response_sent
    except (ValueError, AttributeError):
        request.state.intent = None
    return response
