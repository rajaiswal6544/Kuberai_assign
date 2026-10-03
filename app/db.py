"""Database engine, declarative base, sessions, and health check."""

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.logging import get_logger

log = get_logger()


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    return create_engine(
        url, pool_pre_ping=True, pool_size=10, connect_args={"connect_timeout": 3}
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create tables on startup (no Alembic; see NOTES_FOR_HUMAN.md)."""
    import app.models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(engine)


def check_db(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError as exc:
        log.warning("db_check_failed", error_type=type(exc).__name__)
        return False
