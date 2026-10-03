"""SIPs: proposal -> active <-> paused -> cancelled, and execution of due instalments.

Double-charging is prevented by UNIQUE(sip_id, due_date) on instalments, UNIQUE
purchases.sip_instalment_id, a SIP row lock during execution, and a partial unique
index that forbids two identical live SIPs.
"""

import calendar
import uuid
from datetime import date, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.errors import AppError
from app.logging import get_logger
from app.models import LedgerEntry, Purchase, Sip, SipInstalment
from app.money import AmountError, calculate_purchase, format_inr, validate_amount
from app.pricing import PriceService, PriceUnavailable

log = get_logger()

FREQUENCIES = ("daily", "weekly", "monthly")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# --- Schedule arithmetic ---------------------------------------------------------

def _monthly_date(year: int, month: int, day_of_month: int) -> date:
    """Day 29/30/31 in a shorter month runs on that month's last day."""
    return date(year, month, min(day_of_month, calendar.monthrange(year, month)[1]))


def next_occurrence(
    frequency: str, day_of_month: int | None, weekday: int | None, on_or_after: date
) -> date:
    if frequency == "daily":
        return on_or_after
    if frequency == "weekly":
        assert weekday is not None
        return on_or_after + timedelta(days=(weekday - on_or_after.weekday()) % 7)
    assert day_of_month is not None
    candidate = _monthly_date(on_or_after.year, on_or_after.month, day_of_month)
    if candidate >= on_or_after:
        return candidate
    year, month = (on_or_after.year + 1, 1) if on_or_after.month == 12 else (
        on_or_after.year,
        on_or_after.month + 1,
    )
    return _monthly_date(year, month, day_of_month)


def _following(sip: Sip, due: date) -> date:
    return next_occurrence(sip.frequency, sip.day_of_month, sip.weekday, due + timedelta(days=1))


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def fmt_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


def describe_schedule(sip: Sip) -> str:
    amount = format_inr(sip.amount)
    if sip.frequency == "daily":
        return f"{amount} every day"
    if sip.frequency == "weekly":
        return f"{amount} every {WEEKDAYS[sip.weekday]}"  # type: ignore[index]
    return f"{amount} every month on the {ordinal(sip.day_of_month)}"  # type: ignore[arg-type]


def serialize_sip(sip: Sip, instalments: list[SipInstalment] | None = None, **extra) -> dict:
    body = {
        "sip_id": str(sip.id),
        "status": sip.status,
        "amount": str(sip.amount),
        "frequency": sip.frequency,
        "day_of_month": sip.day_of_month,
        "weekday": sip.weekday,
        "start_date": sip.start_date.isoformat(),
        "next_run_date": sip.next_run_date.isoformat(),
        "schedule": describe_schedule(sip),
        **extra,
    }
    if instalments is not None:
        body["instalments"] = [
            {
                "instalment_id": str(i.id),
                "due_date": i.due_date.isoformat(),
                "status": i.status,
                "purchase_id": str(i.purchase_id) if i.purchase_id else None,
                "attempts": i.attempts,
                "last_error": i.last_error,
                "is_catch_up": bool(i.is_catch_up),
            }
            for i in instalments
        ]
    return body


# --- Lifecycle -------------------------------------------------------------------

def _find_identical(session: Session, user_id: str, amount, frequency, dom, weekday) -> Sip | None:
    return session.scalars(
        select(Sip).where(
            Sip.user_id == user_id,
            Sip.amount == amount,
            Sip.frequency == frequency,
            Sip.day_of_month.is_(None) if dom is None else Sip.day_of_month == dom,
            Sip.weekday.is_(None) if weekday is None else Sip.weekday == weekday,
            Sip.status.in_(("proposed", "active")),
        )
    ).first()


def propose_sip(
    session: Session,
    *,
    user_id: str,
    amount: object,
    frequency: str,
    day_of_month: int | None,
    start_date: date | None,
    clock: Clock,
) -> tuple[Sip, bool, list[str]]:
    """Returns (sip, already_existed, notes). Notes explain any adjustment we made."""
    try:
        amount_d = validate_amount(amount)  # type: ignore[arg-type]
    except AmountError as exc:
        raise AppError(422, exc.code, exc.message) from None
    if frequency not in FREQUENCIES:
        raise AppError(422, "invalid_frequency", "Frequency must be daily, weekly or monthly.")
    if day_of_month is not None and frequency != "monthly":
        raise AppError(422, "invalid_day_of_month", "day_of_month only applies to monthly SIPs.")
    if day_of_month is not None and not 1 <= day_of_month <= 31:
        raise AppError(422, "invalid_day_of_month", "day_of_month must be between 1 and 31.")

    today = clock.today()
    notes: list[str] = []
    start = start_date or today
    if start < today:
        notes.append(
            f"{fmt_date(start)} has already passed, so this SIP starts today ({fmt_date(today)}). "
            "Past instalments are never backfilled."
        )
        start = today

    weekday = start.weekday() if frequency == "weekly" else None
    dom = (day_of_month or start.day) if frequency == "monthly" else None
    if dom is not None and dom > 28:
        notes.append(
            f"In months that have no {ordinal(dom)}, it runs on the last day of the month."
        )

    existing = _find_identical(session, user_id, amount_d, frequency, dom, weekday)
    if existing is not None:
        session.commit()
        log.info("sip_duplicate_detected", sip_id=existing.id, status=existing.status)
        return existing, True, []

    now = clock.now()
    sip = Sip(
        id=uuid.uuid4(),
        user_id=user_id,
        amount=amount_d,
        frequency=frequency,
        day_of_month=dom,
        weekday=weekday,
        start_date=start,
        next_run_date=next_occurrence(frequency, dom, weekday, start),
        status="proposed",
        created_at=now,
        updated_at=now,
    )
    session.add(sip)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent identical request won the partial unique index.
        session.rollback()
        existing = _find_identical(session, user_id, amount_d, frequency, dom, weekday)
        session.commit()
        if existing is None:
            raise
        return existing, True, []
    log.info(
        "sip_proposed",
        sip_id=sip.id,
        amount=sip.amount,
        frequency=frequency,
        day_of_month=dom,
        weekday=weekday,
        first_run_date=sip.next_run_date,
    )
    return sip, False, notes


def _lock_sip(session: Session, user_id: str, sip_id: uuid.UUID) -> Sip:
    sip = session.scalars(
        select(Sip).where(Sip.id == sip_id, Sip.user_id == user_id).with_for_update()
    ).first()
    if sip is None:
        session.rollback()
        raise AppError(404, "sip_not_found", "That SIP doesn't exist.")
    return sip


def _invalid(session: Session, sip: Sip, action: str, reason: str) -> AppError:
    session.rollback()
    log.info("sip_transition_rejected", sip_id=sip.id, action=action, status=sip.status)
    return AppError(409, "invalid_transition", reason, sip_id=str(sip.id), sip_status=sip.status)


def confirm_sip(session: Session, *, user_id: str, sip_id: uuid.UUID, clock: Clock) -> tuple[Sip, bool]:
    """Returns (sip, idempotent_replay). Confirming does not charge; run-due does."""
    sip = _lock_sip(session, user_id, sip_id)
    if sip.status == "active":
        session.commit()
        return sip, True
    if sip.status != "proposed":
        raise _invalid(session, sip, "confirm", f"This SIP is {sip.status}; only a proposed SIP can be confirmed.")
    today = clock.today()
    if sip.next_run_date < today:
        # Proposal confirmed late: start from today, never backfill.
        sip.next_run_date = next_occurrence(sip.frequency, sip.day_of_month, sip.weekday, today)
    sip.status = "active"
    sip.updated_at = clock.now()
    session.commit()
    log.info("sip_confirmed", sip_id=sip.id, next_run_date=sip.next_run_date)
    return sip, False


def pause_sip(session: Session, *, user_id: str, sip_id: uuid.UUID, clock: Clock) -> Sip:
    sip = _lock_sip(session, user_id, sip_id)
    if sip.status != "active":
        reason = {
            "paused": "This SIP is already paused.",
            "proposed": "This SIP hasn't been confirmed yet, so there is nothing to pause.",
            "cancelled": "This SIP is cancelled and can't be paused.",
        }[sip.status]
        raise _invalid(session, sip, "pause", reason)
    sip.status = "paused"
    sip.updated_at = clock.now()
    session.commit()
    log.info("sip_paused", sip_id=sip.id)
    return sip


def resume_sip(session: Session, *, user_id: str, sip_id: uuid.UUID, clock: Clock) -> Sip:
    sip = _lock_sip(session, user_id, sip_id)
    if sip.status != "paused":
        reason = {
            "active": "This SIP is already active.",
            "proposed": "This SIP hasn't been confirmed yet; confirm it instead.",
            "cancelled": "This SIP is cancelled and can't be resumed. You can start a new one.",
        }[sip.status]
        raise _invalid(session, sip, "resume", reason)
    # Paused periods are not backfilled: the next run is the first schedule date from today.
    sip.next_run_date = next_occurrence(sip.frequency, sip.day_of_month, sip.weekday, clock.today())
    sip.status = "active"
    sip.updated_at = clock.now()
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise AppError(
            409,
            "duplicate_sip",
            "You already have an identical SIP running, so this one can't be resumed.",
            sip_id=str(sip_id),
        ) from None
    log.info("sip_resumed", sip_id=sip.id, next_run_date=sip.next_run_date)
    return sip


def cancel_sip(session: Session, *, user_id: str, sip_id: uuid.UUID, clock: Clock) -> Sip:
    sip = _lock_sip(session, user_id, sip_id)
    if sip.status == "cancelled":
        raise _invalid(session, sip, "cancel", "This SIP is already cancelled.")
    now = clock.now()
    sip.status = "cancelled"
    sip.updated_at = now
    for inst in session.scalars(
        select(SipInstalment).where(
            SipInstalment.sip_id == sip.id, SipInstalment.status == "pending_retry"
        )
    ):
        inst.status = "failed"
        inst.last_error = "sip_cancelled"
        inst.updated_at = now
    session.commit()
    log.info("sip_cancelled", sip_id=sip.id)
    return sip


def list_sips(session: Session, user_id: str, statuses: tuple[str, ...] | None = None) -> list[Sip]:
    query = select(Sip).where(Sip.user_id == user_id)
    if statuses:
        query = query.where(Sip.status.in_(statuses))
    return list(session.scalars(query.order_by(Sip.created_at)).all())


def instalments_for(session: Session, sip_id: uuid.UUID) -> list[SipInstalment]:
    return list(
        session.scalars(
            select(SipInstalment)
            .where(SipInstalment.sip_id == sip_id)
            .order_by(SipInstalment.due_date)
        ).all()
    )


# --- Execution -------------------------------------------------------------------

def _attempt(
    session: Session,
    sip: Sip,
    inst: SipInstalment,
    price_service: PriceService,
    clock: Clock,
    max_attempts: int = 5,
) -> str:
    """Try to charge one instalment. Returns its resulting status."""
    now = clock.now()
    inst.attempts += 1
    inst.updated_at = now
    try:
        point = price_service.current()
        b = calculate_purchase(sip.amount, point.price)
    except PriceUnavailable:
        # Never charged at a stale price. Retried on the next run, up to max_attempts;
        # then marked failed (and logged) rather than silently dropped.
        if inst.attempts >= max_attempts:
            inst.status = "failed"
            inst.last_error = "price_unavailable_max_retries"
            session.flush()
            log.error(
                "instalment_failed",
                sip_id=sip.id,
                due_date=inst.due_date,
                reason="max_retries",
                attempts=inst.attempts,
            )
            return inst.status
        inst.status = "pending_retry"
        inst.last_error = "price_unavailable"
        session.flush()
        log.warning("instalment_pending_retry", sip_id=sip.id, due_date=inst.due_date, attempts=inst.attempts)
        return inst.status
    except AmountError as exc:
        inst.status = "failed"
        inst.last_error = exc.code
        session.flush()
        log.error("instalment_failed", sip_id=sip.id, due_date=inst.due_date, reason=exc.code)
        return inst.status

    purchase_id = uuid.uuid4()
    inst.status = "executed"
    inst.purchase_id = purchase_id
    inst.last_error = None
    session.flush()
    session.add(
        Purchase(
            id=purchase_id,
            user_id=sip.user_id,
            source="sip",
            sip_instalment_id=inst.id,
            requested_amount=b.requested_amount,
            price_per_gram=b.price_per_gram,
            grams=b.grams,
            gold_value=b.gold_value,
            gst=b.gst,
            total_charged=b.total_charged,
            created_at=now,
        )
    )
    session.flush()
    session.add(
        LedgerEntry(
            id=uuid.uuid4(),
            user_id=sip.user_id,
            entry_type="sip_instalment",
            purchase_id=purchase_id,
            grams=b.grams,
            total_charged=b.total_charged,
            created_at=now,
        )
    )
    session.flush()
    log.info(
        "instalment_executed",
        sip_id=sip.id,
        instalment_id=inst.id,
        due_date=inst.due_date,
        purchase_id=purchase_id,
        price_per_gram=b.price_per_gram,
        grams=b.grams,
        total_charged=b.total_charged,
    )
    return inst.status


def _new_instalment(
    session: Session, sip: Sip, due: date, status: str, clock: Clock, **fields
) -> SipInstalment | None:
    """Insert one instalment row; None if that date already has one (UNIQUE(sip_id, due_date))."""
    now = clock.now()
    inst = SipInstalment(
        id=uuid.uuid4(),
        sip_id=sip.id,
        due_date=due,
        status=status,
        attempts=0,
        created_at=now,
        updated_at=now,
        **fields,
    )
    try:
        with session.begin_nested():
            session.add(inst)
            session.flush()
    except IntegrityError:
        # e.g. paused and resumed the same day: the constraint means we never charge twice.
        log.info("instalment_already_exists", sip_id=sip.id, due_date=due)
        return None
    return inst


def run_due(
    session_factory: sessionmaker[Session],
    *,
    as_of: date,
    price_service: PriceService,
    clock: Clock,
    max_catch_up: int = 7,
    max_attempts: int = 5,
) -> dict:
    """Execute instalments due on or before as_of (an IST date). Re-running is a no-op.

    - pending_retry instalments are retried first, in date order, at the current price;
    - missed dates (the scheduler did not run) are executed in date order and flagged
      is_catch_up; if more than max_catch_up are due for one SIP, only the most recent
      max_catch_up are charged and the older ones are recorded as skipped_missed (no charge);
    - nothing is ever created for a date before the SIP's start date.
    """
    max_catch_up = max(1, max_catch_up)
    with session_factory() as session:
        retry_sip_ids = select(SipInstalment.sip_id).where(
            SipInstalment.status == "pending_retry", SipInstalment.due_date <= as_of
        )
        sip_ids = session.scalars(
            select(Sip.id)
            .where(Sip.status == "active")
            .where((Sip.next_run_date <= as_of) | Sip.id.in_(retry_sip_ids))
            .order_by(Sip.next_run_date, Sip.id)
        ).all()

    results: list[dict] = []
    for sip_id in sip_ids:
        with session_factory() as session:
            sip = session.scalars(select(Sip).where(Sip.id == sip_id).with_for_update()).one()
            if sip.status != "active":
                session.rollback()
                continue
            with structlog.contextvars.bound_contextvars(user_id=sip.user_id):
                # Older failed dates first, so instalments are executed in date order.
                for inst in session.scalars(
                    select(SipInstalment)
                    .where(
                        SipInstalment.sip_id == sip.id,
                        SipInstalment.status == "pending_retry",
                        SipInstalment.due_date <= as_of,
                    )
                    .order_by(SipInstalment.due_date)
                ).all():
                    status = _attempt(session, sip, inst, price_service, clock, max_attempts)
                    results.append(_result(sip, inst, status))

                dues: list[date] = []
                due = sip.next_run_date
                while due <= as_of:
                    if due >= sip.start_date:  # never before the SIP started
                        dues.append(due)
                    due = _following(sip, due)
                sip.next_run_date = due

                skipped, to_run = dues[:-max_catch_up], dues[-max_catch_up:]
                for missed in skipped:
                    inst = _new_instalment(
                        session,
                        sip,
                        missed,
                        "skipped_missed",
                        clock,
                        last_error="missed_beyond_catch_up_cap",
                        is_catch_up=True,
                    )
                    if inst is not None:
                        results.append(_result(sip, inst, inst.status))
                if skipped:
                    log.warning(
                        "instalments_skipped_missed",
                        sip_id=sip.id,
                        count=len(skipped),
                        first_due=skipped[0],
                        last_due=skipped[-1],
                        cap=max_catch_up,
                    )

                for d in to_run:
                    catch_up = d < as_of
                    inst = _new_instalment(session, sip, d, "pending_retry", clock, is_catch_up=catch_up)
                    if inst is None:
                        continue
                    if catch_up:
                        log.info("instalment_catch_up", sip_id=sip.id, due_date=d, days_late=(as_of - d).days)
                    status = _attempt(session, sip, inst, price_service, clock, max_attempts)
                    results.append(_result(sip, inst, status))
                session.commit()

    summary = {
        "as_of": as_of.isoformat(),
        "executed": sum(r["status"] == "executed" for r in results),
        "pending_retry": sum(r["status"] == "pending_retry" for r in results),
        "failed": sum(r["status"] == "failed" for r in results),
        "skipped_missed": sum(r["status"] == "skipped_missed" for r in results),
        "catch_up": sum(r["is_catch_up"] and r["status"] == "executed" for r in results),
        "instalments": results,
    }
    log.info("run_due_completed", **{k: v for k, v in summary.items() if k != "instalments"})
    return summary


def _result(sip: Sip, inst: SipInstalment, status: str) -> dict:
    return {
        "sip_id": str(sip.id),
        "user_id": sip.user_id,
        "instalment_id": str(inst.id),
        "due_date": inst.due_date.isoformat(),
        "status": status,
        "is_catch_up": bool(inst.is_catch_up),
        "purchase_id": str(inst.purchase_id) if inst.purchase_id else None,
    }
