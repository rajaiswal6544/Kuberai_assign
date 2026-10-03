from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.users import ensure_user, validate_user_id


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def get_user_id(request: Request, session: Session = Depends(get_session)) -> str:
    user_id = validate_user_id(request.headers.get("x-user-id"))
    ensure_user(session, user_id, request.app.state.clock)
    return user_id
