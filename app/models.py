"""Postgres schema. Money is NUMERIC; double-charging is prevented by constraints here."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

Rupees = Numeric(14, 2, asdecimal=True)
Grams = Numeric(14, 4, asdecimal=True)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'executed', 'expired', 'superseded')", name="quote_status"),
        CheckConstraint("grams > 0 AND total_charged <= requested_amount", name="quote_money"),
        Index("ix_quotes_user_status", "user_id", "status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    requested_amount: Mapped[Decimal] = mapped_column(Rupees)
    price_per_gram: Mapped[Decimal] = mapped_column(Rupees)
    grams: Mapped[Decimal] = mapped_column(Grams)
    gold_value: Mapped[Decimal] = mapped_column(Rupees)
    gst: Mapped[Decimal] = mapped_column(Rupees)
    total_charged: Mapped[Decimal] = mapped_column(Rupees)
    status: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Sip(Base):
    __tablename__ = "sips"
    __table_args__ = (
        CheckConstraint("frequency IN ('daily', 'weekly', 'monthly')", name="sip_frequency"),
        CheckConstraint(
            "status IN ('proposed', 'active', 'paused', 'cancelled')", name="sip_status"
        ),
        CheckConstraint("day_of_month IS NULL OR day_of_month BETWEEN 1 AND 31", name="sip_dom"),
        CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name="sip_weekday"),
        CheckConstraint(
            "(frequency = 'monthly') = (day_of_month IS NOT NULL)"
            " AND (frequency = 'weekly') = (weekday IS NOT NULL)",
            name="sip_anchor",
        ),
        CheckConstraint("amount > 0", name="sip_amount"),
        # One live SIP per identical schedule: a duplicate would charge the user twice.
        Index(
            "uq_sips_identical_live",
            "user_id",
            "amount",
            "frequency",
            text("coalesce(day_of_month, 0)"),
            text("coalesce(weekday, -1)"),
            unique=True,
            postgresql_where=text("status IN ('proposed', 'active')"),
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Rupees)
    frequency: Mapped[str] = mapped_column(String(16))
    day_of_month: Mapped[int | None] = mapped_column(Integer)
    weekday: Mapped[int | None] = mapped_column(Integer)
    start_date: Mapped[date] = mapped_column(Date)
    next_run_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SipInstalment(Base):
    __tablename__ = "sip_instalments"
    __table_args__ = (
        UniqueConstraint("sip_id", "due_date", name="uq_instalment_sip_due"),
        CheckConstraint(
            "status IN ('executed', 'pending_retry', 'failed', 'skipped_missed')", name="instalment_status"
        ),
        CheckConstraint(
            "(status = 'executed') = (purchase_id IS NOT NULL)", name="instalment_purchase"
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    sip_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sips.id"), index=True)
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    purchase_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "purchases.id",
            use_alter=True,
            name="fk_instalment_purchase",
            deferrable=True,
            initially="DEFERRED",
        )
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # True when executed for a due date earlier than the run's as_of (the scheduler was late).
    is_catch_up: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Purchase(Base):
    __tablename__ = "purchases"
    __table_args__ = (
        CheckConstraint("source IN ('one_time', 'sip')", name="purchase_source"),
        CheckConstraint(
            "(source = 'one_time' AND quote_id IS NOT NULL AND sip_instalment_id IS NULL)"
            " OR (source = 'sip' AND sip_instalment_id IS NOT NULL AND quote_id IS NULL)",
            name="purchase_origin",
        ),
        CheckConstraint("grams > 0 AND total_charged <= requested_amount", name="purchase_money"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # UNIQUE: a quote can be executed at most once, an instalment charged at most once.
    quote_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("quotes.id"), unique=True)
    source: Mapped[str] = mapped_column(String(16))
    sip_instalment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sip_instalments.id"), unique=True
    )
    requested_amount: Mapped[Decimal] = mapped_column(Rupees)
    price_per_gram: Mapped[Decimal] = mapped_column(Rupees)
    grams: Mapped[Decimal] = mapped_column(Grams)
    gold_value: Mapped[Decimal] = mapped_column(Rupees)
    gst: Mapped[Decimal] = mapped_column(Rupees)
    total_charged: Mapped[Decimal] = mapped_column(Rupees)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("entry_type IN ('purchase', 'sip_instalment')", name="ledger_type"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    entry_type: Mapped[str] = mapped_column(String(16))
    purchase_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchases.id"), unique=True)
    grams: Mapped[Decimal] = mapped_column(Grams)
    total_charged: Mapped[Decimal] = mapped_column(Rupees)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# The ledger is append-only, enforced by the database rather than by convention.
event.listen(
    LedgerEntry.__table__,
    "after_create",
    DDL(
        "CREATE OR REPLACE FUNCTION ledger_append_only() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'ledger_entries is append-only'; END $$ LANGUAGE plpgsql"
    ),
)
event.listen(
    LedgerEntry.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER ledger_no_update_delete BEFORE UPDATE OR DELETE ON ledger_entries "
        "FOR EACH ROW EXECUTE FUNCTION ledger_append_only()"
    ),
)


class PendingAction(Base):
    """What a bare "yes" in chat confirms. One per user; the newest replaces the old."""

    __tablename__ = "pending_actions"
    __table_args__ = (
        CheckConstraint("kind IN ('quote', 'sip_proposal', 'sip_cancel')", name="pending_kind"),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    # NULL until the first request finishes; a concurrent duplicate sees "in progress".
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
