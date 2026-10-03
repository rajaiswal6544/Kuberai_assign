"""Read side of the append-only ledger."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import GRAM, RUPEE
from app.models import LedgerEntry, Purchase, SipInstalment


def get_ledger(session: Session, user_id: str) -> dict:
    rows = session.execute(
        select(LedgerEntry, SipInstalment.is_catch_up, SipInstalment.due_date)
        .join(Purchase, Purchase.id == LedgerEntry.purchase_id)
        .outerjoin(SipInstalment, SipInstalment.id == Purchase.sip_instalment_id)
        .where(LedgerEntry.user_id == user_id)
        # Entries written in one run-due share a timestamp; due date keeps them in schedule order.
        .order_by(LedgerEntry.created_at, SipInstalment.due_date.nulls_first(), LedgerEntry.id)
    ).all()
    entries = [row[0] for row in rows]
    total_grams = sum((e.grams for e in entries), Decimal("0")).quantize(GRAM)
    total_charged = sum((e.total_charged for e in entries), Decimal("0")).quantize(RUPEE)
    return {
        "user_id": user_id,
        "entries": [
            {
                "entry_id": str(e.id),
                "entry_type": e.entry_type,
                "purchase_id": str(e.purchase_id),
                "grams": str(e.grams),
                "total_charged": str(e.total_charged),
                "created_at": e.created_at.isoformat(),
                # SIP entries only: which due date it paid for, and whether it was a late catch-up.
                "due_date": due.isoformat() if due else None,
                "is_catch_up": bool(catch_up) if e.entry_type == "sip_instalment" else False,
            }
            for e, catch_up, due in rows
        ],
        "total_grams": str(total_grams),
        "total_charged": str(total_charged),
    }
