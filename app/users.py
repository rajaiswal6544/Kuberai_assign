"""Users are created on first sight from the X-User-Id header. There is no auth."""

import re

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.clock import Clock
from app.errors import AppError
from app.models import User

USER_ID_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def validate_user_id(raw: str | None) -> str:
    if not raw:
        raise AppError(400, "missing_user_id", "Send your user id in the X-User-Id header.")
    if not USER_ID_RE.match(raw):
        raise AppError(
            400,
            "invalid_user_id",
            "X-User-Id may only contain letters, digits and . _ @ - (max 64 characters).",
        )
    return raw


def ensure_user(session: Session, user_id: str, clock: Clock) -> None:
    session.execute(
        insert(User).values(id=user_id, created_at=clock.now()).on_conflict_do_nothing()
    )
    session.commit()
