"""/chat: classify (rules, LLM, or fallback), then the backend decides and acts.

Money can only move through a pending action that the backend itself created: a quote, a SIP
proposal, or a SIP cancellation. "yes" confirms that server-issued id; nothing the user or the
LLM says can name a price, an amount to charge, or a quote id.

Order of decisions for a message:
1. empty / no letters or digits      -> deterministic reply, no LLM
2. prompt-injection patterns          -> decline + security_event, no LLM
3. exact confirm / decline phrase     -> confirm / decline, no LLM
4. LLM extraction (or rule fallback)  -> intent; amounts always come from the server parser
5. something pending + a new amount   -> the new amount supersedes the pending action
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.chat import fallback
from app.chat.amounts import ParsedAmounts, parse_amounts
from app.chat.guard import guard_reply
from app.chat.intents import Extraction, amounts_disagree, ground_amount, ground_day_of_month
from app.chat.language import detect_language, resolve_language
from app.chat.prompts import (
    ADVICE_SYSTEM,
    COMPARE_SYSTEM,
    EXTRACTION_SYSTEM,
    facts_block,
    wrap_user_message,
)
from app.chat.templates import render
from app.clock import Clock
from app.config import MAX_PURCHASE, MIN_PURCHASE, Settings
from app.errors import AppError
from app.ledger import get_ledger
from app.llm.base import LLMClient, LLMError
from app.logging import get_logger
from app.models import PendingAction, Quote, Sip
from app.money import AmountError, amount_for_grams, format_inr, validate_amount
from app.pricing import PriceService, PriceUnavailable
from app.purchases import execute_quote, serialize_purchase
from app.quotes import create_quote, expire_quote, serialize_quote
from app.sips import (
    cancel_sip,
    confirm_sip,
    describe_schedule,
    fmt_date,
    list_sips,
    pause_sip,
    propose_sip,
    resume_sip,
    serialize_sip,
)

log = get_logger()

START_ACTIONS = ["Buy ₹10 once", "Start ₹100/month SIP", "Just exploring"]
CONFIRM_ACTIONS = ["Confirm", "Not now"]
# Intents whose meaning depends on a rupee amount.
MONEY_INTENTS = {"buy_gold", "price_calc", "sip_create", "ambiguous_invest", "change_amount"}
# Intents that, while something is pending and the message has one new amount, mean
# "use this amount instead" ("yes, make it 5000").
# Deliberately not ambiguous_invest: "I want to invest 5000" must still ask once-or-SIP.
AMEND_INTENTS = {"confirm", "unknown", "change_amount"}


@dataclass
class ChatDeps:
    session_factory: sessionmaker[Session]
    clock: Clock
    settings: Settings
    price_service: PriceService
    llm: LLMClient | None


@dataclass
class Turn:
    deps: ChatDeps
    session: Session
    user_id: str
    message: str
    ext: Extraction
    source: str  # "rule" | "llm" | "fallback"
    lang: str  # decided by the server (app.chat.language), never taken raw from the LLM
    parsed: ParsedAmounts
    extra: dict = field(default_factory=dict)


# --- Classification ----------------------------------------------------------------

def _rule(intent: str, message: str) -> Extraction:
    return Extraction(intent=intent, language=detect_language(message))  # type: ignore[arg-type]


def classify_message(llm: LLMClient | None, message: str) -> tuple[Extraction, str]:
    """Returns (extraction, source) with source in rule | llm | fallback."""
    # These never depend on the LLM, and money-confirm decisions never call it.
    if fallback.is_injection(message):
        return fallback.classify(message), "rule"
    if fallback.is_confirm(message):
        return _rule("confirm", message), "rule"
    if fallback.is_decline(message):
        return _rule("decline", message), "rule"
    if not re.search(r"[^\W_]", message):  # emoji-only / punctuation-only
        return _rule("unknown", message), "rule"
    if llm is None:
        log.warning("llm_fallback_used", reason="not_configured")
        return fallback.classify(message), "fallback"
    try:
        raw = llm.complete(
            system=EXTRACTION_SYSTEM,
            user=wrap_user_message(message),
            json_mode=True,
            max_tokens=1024,
            temperature=0.0,
        )
        data = json.loads(raw, parse_float=Decimal)  # LLM numbers never become floats
        if not isinstance(data, dict):
            raise ValueError("extraction is not a JSON object")
        ext = Extraction.model_validate(data)
    except (LLMError, ValueError, ValidationError) as exc:
        log.warning("llm_fallback_used", reason=type(exc).__name__, detail=str(exc)[:200])
        return fallback.classify(message), "fallback"

    # The server parser is the source of truth for amounts and dates.
    llm_amount = ext.amount
    amount = ground_amount(llm_amount, message)
    if amounts_disagree(llm_amount, message):
        log.warning("amount_mismatch", llm_amount=str(llm_amount)[:30], parser_amount=amount)
    elif llm_amount is not None and amount is None:
        log.warning("llm_amount_dropped", llm_amount=str(llm_amount)[:30])
    dom = ground_day_of_month(ext.day_of_month, message)
    ext = ext.model_copy(update={"amount": amount, "day_of_month": dom})

    rules = fallback.classify(message)
    # "yes, buy 500" is a new request, not a bare confirmation of whatever is pending.
    if ext.intent == "confirm" and parse_amounts(message).amounts:
        return rules, "rule"
    if ext.intent == "unknown" and rules.intent != "unknown":
        return rules, "rule"
    return ext, "llm"


# --- Helpers -------------------------------------------------------------------------

def chip_inr(value: Decimal) -> str:
    s = format_inr(value)
    return s[:-3] if s.endswith(".00") else s


def _response(turn: Turn, reply: str, *, actions: list[str] | None = None, **fields) -> dict:
    body = {
        "reply": reply,
        "intent": turn.ext.intent,
        "language": turn.lang,
        "quote": None,
        "sip_proposal": None,
        "suggested_actions": actions or [],
    }
    body.update(fields)
    return body


def _supersede(session: Session, existing: PendingAction, new_kind: str, new_ref: uuid.UUID) -> None:
    """A new quote/proposal replaces whatever was waiting for "yes"."""
    if existing.kind == "quote":
        session.execute(
            update(Quote)
            .where(Quote.id == existing.ref_id, Quote.status == "open")
            .values(status="superseded")
        )
    elif existing.kind == "sip_proposal":
        session.execute(
            update(Sip)
            .where(Sip.id == existing.ref_id, Sip.status == "proposed")
            .values(status="cancelled")
        )
    log.info(
        "pending_superseded",
        kind=existing.kind,
        ref_id=existing.ref_id,
        new_kind=new_kind,
        new_ref_id=new_ref,
    )


def _set_pending(
    session: Session, user_id: str, kind: str, ref_id: uuid.UUID, expires_at: datetime
) -> None:
    existing = session.get(PendingAction, user_id, with_for_update=True)
    if existing is not None and existing.ref_id != ref_id:
        _supersede(session, existing, kind, ref_id)
    stmt = insert(PendingAction).values(user_id=user_id, kind=kind, ref_id=ref_id, expires_at=expires_at)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[PendingAction.user_id],
            set_={"kind": kind, "ref_id": ref_id, "expires_at": expires_at},
        )
    )
    session.commit()


def _clear_pending(session: Session, user_id: str, ref_id: uuid.UUID) -> None:
    # Only clear this action; a newer one created concurrently is left alone.
    session.execute(
        delete(PendingAction).where(PendingAction.user_id == user_id, PendingAction.ref_id == ref_id)
    )
    session.commit()


def _resolve_start(turn: Turn) -> datetime | None:
    phrase = f"{turn.ext.start_phrase or ''} {turn.message}".lower()
    today = turn.deps.clock.today()
    if re.search(r"\byesterday\b", phrase):
        return today - timedelta(days=1)  # type: ignore[return-value]
    if re.search(r"\btomorrow\b", phrase):
        return today + timedelta(days=1)  # type: ignore[return-value]
    return None  # "today" or nothing: propose_sip defaults to today


def _facts(turn: Turn) -> dict[str, str]:
    try:
        price = f"{format_inr(turn.deps.price_service.current().price)} per gram, before GST"
    except PriceUnavailable:
        price = "not available right now"
    return {
        "Current gold price in this app": price,
        "GST on digital gold": "3%",
        "Minimum purchase": format_inr(MIN_PURCHASE),
        "Ways to buy here": "one-time purchase, or a daily/weekly/monthly SIP",
    }


def _advice_fallback(turn: Turn) -> str:
    """Short deterministic paragraph built only from FACTS."""
    try:
        price_line = render("price_line", turn.lang, price=format_inr(turn.deps.price_service.current().price))
    except PriceUnavailable:
        price_line = ""
    return render("advice_fallback", turn.lang, price_line=price_line)


def _llm_text(turn: Turn, system: str) -> str | None:
    """Advice/compare text from the LLM, or None if unavailable or the guard rejected it."""
    llm = turn.deps.llm
    if llm is None or turn.source == "fallback":
        return None  # the LLM is already known to be down for this message
    facts = _facts(turn)
    try:
        text = llm.complete(
            system=f"{system}\n{facts_block(facts)}",
            user=wrap_user_message(turn.message),
            json_mode=False,
            max_tokens=1024,
            temperature=0.3,
        )
    except LLMError as exc:
        log.warning("llm_fallback_used", reason=type(exc).__name__, detail=str(exc)[:200], stage="text")
        return None
    result = guard_reply(text, facts)
    if result.text is None:
        log.warning("llm_reply_rejected", reason=result.reason, matched=result.detail, intent=turn.ext.intent)
        return None
    return result.text


def _active_pending(session: Session, user_id: str) -> PendingAction | None:
    pending = session.get(PendingAction, user_id)
    session.commit()
    return pending


# --- Intent handlers -------------------------------------------------------------------

def _quote_flow(turn: Turn, amount: str, prefix: str = "") -> dict:
    deps = turn.deps
    try:
        quote, _reused = create_quote(
            turn.session,
            user_id=turn.user_id,
            amount=amount,
            price_service=deps.price_service,
            clock=deps.clock,
            ttl_seconds=deps.settings.quote_ttl_seconds,
        )
    except AppError as exc:
        reply = render("price_unavailable", turn.lang) if exc.status == 503 else exc.message
        return _response(turn, reply, error=exc.code, actions=[] if exc.status == 503 else ["Buy ₹10 once"])
    _set_pending(turn.session, turn.user_id, "quote", quote.id, quote.expires_at)
    unspent = quote.requested_amount - quote.total_charged
    reply = prefix + render(
        "quote",
        turn.lang,
        amount=format_inr(quote.requested_amount),
        price=format_inr(quote.price_per_gram),
        grams=quote.grams,
        gold_value=format_inr(quote.gold_value),
        gst=format_inr(quote.gst),
        total=format_inr(quote.total_charged),
        unspent=render("unspent", turn.lang, unspent=format_inr(unspent)) if unspent > 0 else "",
        ttl=max(1, deps.settings.quote_ttl_seconds // 60),
    )
    return _response(turn, reply, quote=serialize_quote(quote), actions=CONFIRM_ACTIONS)


def h_grams(turn: Turn) -> dict:
    """Purchases are by rupee amount: convert a gram request, never create a quote here."""
    grams = turn.parsed.grams[0].value
    try:
        price = turn.deps.price_service.current().price
    except PriceUnavailable:
        return _response(turn, render("price_unavailable", turn.lang), error="price_unavailable")
    try:
        b = amount_for_grams(grams, price)
    except AmountError as exc:
        if exc.code != "above_maximum":
            return _response(turn, exc.message, error=exc.code)
        estimate = (grams * price * Decimal("1.03")).quantize(Decimal("0.01"))
        reply = render(
            "grams_too_much", turn.lang, grams=grams, amount=format_inr(estimate), max=chip_inr(MAX_PURCHASE)
        )
        return _response(turn, reply, error="above_maximum")
    amount = chip_inr(b.requested_amount)
    reply = render(
        "grams_quote",
        turn.lang,
        price=format_inr(price),
        grams=grams,
        amount=format_inr(b.requested_amount),
        gold_value=format_inr(b.gold_value),
        gst=format_inr(b.gst),
    )
    return _response(
        turn,
        reply,
        actions=[f"Buy {amount}"],
        grams_request={
            "requested_grams": format(grams, "f"),
            "amount": str(b.requested_amount),
            "grams": str(b.grams),
            "total_charged": str(b.total_charged),
        },
    )


def h_buy(turn: Turn) -> dict:
    if turn.ext.amount is None:
        if turn.parsed.grams:
            return h_grams(turn)
        if turn.ext.intent == "price_calc":
            try:
                price = turn.deps.price_service.current().price
            except PriceUnavailable:
                return _response(turn, render("price_unavailable", turn.lang))
            return _response(turn, render("price_only", turn.lang, price=format_inr(price)), actions=["Buy ₹10 once", "Buy ₹500 once"])
        return _response(
            turn,
            render("ask_amount", turn.lang, min=chip_inr(MIN_PURCHASE), max=chip_inr(MAX_PURCHASE)),
            actions=["Buy ₹10 once", "Buy ₹500 once", "Start ₹100/month SIP"],
        )
    return _quote_flow(turn, turn.ext.amount)


def h_advice(turn: Turn) -> dict:
    text = _llm_text(turn, ADVICE_SYSTEM) or _advice_fallback(turn)
    return _response(turn, text, actions=START_ACTIONS)


def h_compare(turn: Turn) -> dict:
    # No sales nudge on comparison replies: no purchase chips, and a neutral fallback text.
    text = _llm_text(turn, COMPARE_SYSTEM) or render("compare_fallback", turn.lang)
    return _response(turn, text)


def h_ambiguous(turn: Turn) -> dict:
    amount: Decimal | None = None
    if turn.ext.amount is not None:
        try:
            amount = validate_amount(turn.ext.amount)
        except AmountError:
            amount = None
    if amount is None:
        return _response(turn, render("ambiguous", turn.lang, amount_part=""), actions=START_ACTIONS)
    a = chip_inr(amount)
    return _response(
        turn,
        render("ambiguous", turn.lang, amount_part=f" with {a}" if turn.lang == "en" else f", {a}"),
        actions=[f"Buy {a} once", f"Start {a}/month SIP", "Just exploring"],
    )


def h_sip_create(turn: Turn) -> dict:
    deps, ext = turn.deps, turn.ext
    if ext.amount is None:
        return _response(
            turn,
            render("sip_need_amount", turn.lang, min=chip_inr(MIN_PURCHASE), max=chip_inr(MAX_PURCHASE)),
            actions=["Start ₹100/month SIP", "Start ₹500/month SIP"],
        )
    frequency = ext.frequency or ("monthly" if ext.day_of_month else None)
    if frequency is None:
        try:
            shown = chip_inr(validate_amount(ext.amount))
        except AmountError as exc:
            return _response(turn, exc.message, error=exc.code)
        return _response(
            turn,
            render("sip_need_frequency", turn.lang, amount=shown),
            actions=[f"{shown} daily SIP", f"{shown} weekly SIP", f"{shown} monthly SIP"],
        )
    try:
        sip, existed, notes = propose_sip(
            turn.session,
            user_id=turn.user_id,
            amount=ext.amount,
            frequency=frequency,
            day_of_month=ext.day_of_month if frequency == "monthly" else None,
            start_date=_resolve_start(turn),  # type: ignore[arg-type]
            clock=deps.clock,
        )
    except AppError as exc:
        return _response(turn, exc.message, error=exc.code)

    if existed and sip.status == "active":
        return _response(
            turn,
            render("sip_exists", turn.lang, schedule=describe_schedule(sip), next=fmt_date(sip.next_run_date)),
            sip=serialize_sip(sip, existing=True),
        )
    expires = deps.clock.now() + timedelta(seconds=deps.settings.pending_action_ttl_seconds)
    _set_pending(turn.session, turn.user_id, "sip_proposal", sip.id, expires)
    if existed:
        reply = render(
            "sip_proposal_waiting", turn.lang, schedule=describe_schedule(sip), first=fmt_date(sip.next_run_date)
        )
    else:
        reply = render(
            "sip_proposal",
            turn.lang,
            schedule=describe_schedule(sip),
            first=fmt_date(sip.next_run_date),
            notes="".join(f" {n}" for n in notes),
        )
    return _response(
        turn, reply, sip_proposal=serialize_sip(sip, notes=notes, existing=existed), actions=CONFIRM_ACTIONS
    )


_LIFECYCLE = {
    "sip_pause": ("pause", ("active",), pause_sip, "sip_paused"),
    "sip_resume": ("resume", ("paused",), resume_sip, "sip_resumed"),
    "sip_cancel": ("cancel", ("active", "paused"), cancel_sip, "sip_cancelled"),
}


def h_sip_lifecycle(turn: Turn) -> dict:
    action, statuses, fn, done_key = _LIFECYCLE[turn.ext.intent]
    candidates = list_sips(turn.session, turn.user_id, statuses)
    if turn.ext.amount is not None and len(candidates) > 1:
        try:
            wanted = Decimal(turn.ext.amount)
            matching = [s for s in candidates if s.amount == wanted]
            if len(matching) == 1:
                candidates = matching
        except InvalidOperation:
            pass
    if not candidates:
        return _response(turn, render("sip_none", turn.lang, action=action))
    if len(candidates) > 1:
        options = "\n".join(f"- {describe_schedule(s)}" for s in candidates)
        return _response(
            turn,
            render("sip_which", turn.lang, action=action, options=options),
            actions=[f"{action.capitalize()} the {chip_inr(s.amount)} {s.frequency} SIP" for s in candidates],
            sips=[serialize_sip(s) for s in candidates],
        )
    sip = candidates[0]
    if action == "cancel":
        # Cancelling is terminal, so it waits for an explicit "yes".
        expires = turn.deps.clock.now() + timedelta(seconds=turn.deps.settings.pending_action_ttl_seconds)
        _set_pending(turn.session, turn.user_id, "sip_cancel", sip.id, expires)
        return _response(
            turn,
            render("sip_cancel_confirm", turn.lang, schedule=describe_schedule(sip)),
            actions=CONFIRM_ACTIONS,
            sip=serialize_sip(sip),
        )
    try:
        sip = fn(turn.session, user_id=turn.user_id, sip_id=sip.id, clock=turn.deps.clock)
    except AppError as exc:
        return _response(turn, exc.message, error=exc.code)
    reply = render(done_key, turn.lang, schedule=describe_schedule(sip), next=fmt_date(sip.next_run_date))
    return _response(turn, reply, sip=serialize_sip(sip))


def h_sip_list(turn: Turn) -> dict:
    sips = list_sips(turn.session, turn.user_id, ("proposed", "active", "paused"))
    if not sips:
        return _response(turn, render("sip_list_empty", turn.lang, min=chip_inr(MIN_PURCHASE)), actions=["Start ₹100/month SIP"])
    lines = "\n".join(
        f"- {describe_schedule(s)} ({s.status}, next {fmt_date(s.next_run_date)})" for s in sips
    )
    return _response(turn, render("sip_list", turn.lang, lines=lines), sips=[serialize_sip(s) for s in sips])


def _holdings_text(turn: Turn) -> tuple[str, dict]:
    ledger = get_ledger(turn.session, turn.user_id)
    summary = {
        "total_grams": ledger["total_grams"],
        "total_charged": ledger["total_charged"],
        "entries": len(ledger["entries"]),
    }
    if not ledger["entries"]:
        return render("holdings_empty", turn.lang, min=chip_inr(MIN_PURCHASE)), summary
    text = render(
        "holdings",
        turn.lang,
        grams=ledger["total_grams"],
        count=len(ledger["entries"]),
        total=format_inr(Decimal(ledger["total_charged"])),
    )
    return text, summary


def h_holdings(turn: Turn) -> dict:
    text, summary = _holdings_text(turn)
    actions = ["Buy ₹10 once"] if summary["entries"] == 0 else []
    return _response(turn, text, holdings=summary, actions=actions)


def h_sell(turn: Turn) -> dict:
    text, summary = _holdings_text(turn)
    return _response(turn, render("sell_unavailable", turn.lang, holdings=text), holdings=summary)


def h_confirm(turn: Turn) -> dict:
    deps, session = turn.deps, turn.session
    pending = _active_pending(session, turn.user_id)
    resolved_by = "llm" if turn.source == "llm" else "rule"
    log.info("confirm_resolved", confirm_resolved_by=resolved_by, pending_kind=pending.kind if pending else None)
    if pending is None:
        return _response(turn, render("nothing_pending", turn.lang), actions=START_ACTIONS)

    if pending.kind == "quote":
        try:
            purchase, replay = execute_quote(session, user_id=turn.user_id, quote_id=pending.ref_id, clock=deps.clock)
        except AppError as exc:
            if exc.code == "quote_expired":
                # Never execute an expired quote: offer a fresh one at today's price.
                old = session.get(Quote, pending.ref_id)
                session.commit()
                return _quote_flow(turn, str(old.requested_amount), prefix=render("quote_refreshed", turn.lang))
            _clear_pending(session, turn.user_id, pending.ref_id)
            return _response(turn, exc.message, error=exc.code)
        _clear_pending(session, turn.user_id, pending.ref_id)
        key = "purchase_replayed" if replay else "purchase_done"
        reply = render(
            key,
            turn.lang,
            grams=purchase.grams,
            price=format_inr(purchase.price_per_gram),
            total=format_inr(purchase.total_charged),
        )
        return _response(turn, reply, purchase=serialize_purchase(purchase, replay=replay), actions=["Show my holdings"])

    if pending.expires_at <= deps.clock.now():
        _clear_pending(session, turn.user_id, pending.ref_id)
        key = "cancel_request_expired" if pending.kind == "sip_cancel" else "proposal_expired"
        return _response(turn, render(key, turn.lang), actions=["Start ₹100/month SIP"])

    if pending.kind == "sip_cancel":
        try:
            sip = cancel_sip(session, user_id=turn.user_id, sip_id=pending.ref_id, clock=deps.clock)
        except AppError as exc:
            _clear_pending(session, turn.user_id, pending.ref_id)
            return _response(turn, exc.message, error=exc.code)
        _clear_pending(session, turn.user_id, pending.ref_id)
        return _response(turn, render("sip_cancelled", turn.lang, schedule=describe_schedule(sip)), sip=serialize_sip(sip))

    try:
        sip, _replay = confirm_sip(session, user_id=turn.user_id, sip_id=pending.ref_id, clock=deps.clock)
    except AppError as exc:
        _clear_pending(session, turn.user_id, pending.ref_id)
        return _response(turn, exc.message, error=exc.code)
    _clear_pending(session, turn.user_id, pending.ref_id)
    reply = render("sip_confirmed", turn.lang, schedule=describe_schedule(sip), next=fmt_date(sip.next_run_date))
    return _response(turn, reply, sip=serialize_sip(sip))


def h_decline(turn: Turn) -> dict:
    session = turn.session
    pending = _active_pending(session, turn.user_id)
    if pending is None:
        return _response(turn, render("declined", turn.lang))
    reply = render("declined", turn.lang)
    if pending.kind == "quote":
        expire_quote(session, turn.user_id, pending.ref_id)
    elif pending.kind == "sip_proposal":
        sip = session.get(Sip, pending.ref_id)
        session.commit()
        if sip is not None and sip.status == "proposed":
            cancel_sip(session, user_id=turn.user_id, sip_id=sip.id, clock=turn.deps.clock)
    else:  # sip_cancel: keep the SIP
        sip = session.get(Sip, pending.ref_id)
        session.commit()
        if sip is not None:
            reply = render("sip_kept", turn.lang, schedule=describe_schedule(sip))
    _clear_pending(session, turn.user_id, pending.ref_id)
    log.info("pending_action_declined", kind=pending.kind, ref_id=pending.ref_id)
    return _response(turn, reply)


def h_template(key: str, actions: list[str] | None = None):
    def handler(turn: Turn) -> dict:
        return _response(turn, render(key, turn.lang, min=chip_inr(MIN_PURCHASE)), actions=actions)

    return handler


def h_injection(turn: Turn) -> dict:
    log.warning("security_event", kind="prompt_injection", detected_by=turn.source, message=turn.message[:500])
    return _response(turn, render("injection", turn.lang))


HANDLERS = {
    "gold_advice": h_advice,
    "gold_compare": h_compare,
    "price_calc": h_buy,
    "buy_gold": h_buy,
    "ambiguous_invest": h_ambiguous,
    "change_amount": h_ambiguous,  # reached only when nothing is pending
    "sip_create": h_sip_create,
    "sip_pause": h_sip_lifecycle,
    "sip_resume": h_sip_lifecycle,
    "sip_cancel": h_sip_lifecycle,
    "sip_list": h_sip_list,
    "holdings": h_holdings,
    "sell_gold": h_sell,
    "confirm": h_confirm,
    "decline": h_decline,
    "greeting": h_template("greeting", ["What's the gold price?", "Buy ₹10 once", "Start ₹100/month SIP"]),
    "out_of_scope": h_template("out_of_scope"),
    "prompt_injection": h_injection,
    "unknown": h_template("unknown", START_ACTIONS),
}


def _amend_pending(turn: Turn, pending: PendingAction) -> None:
    """A new amount while something is pending replaces it ("yes, make it 5000")."""
    amount = turn.parsed.single_text
    if pending.kind == "quote":
        turn.ext = turn.ext.model_copy(update={"intent": "buy_gold", "amount": amount})
    elif pending.kind == "sip_proposal":
        sip = turn.session.get(Sip, pending.ref_id)
        turn.session.commit()
        if sip is not None:
            turn.ext = turn.ext.model_copy(
                update={
                    "intent": "sip_create",
                    "amount": amount,
                    "frequency": sip.frequency,
                    "day_of_month": sip.day_of_month,
                }
            )
    log.info("pending_amended", kind=pending.kind, ref_id=pending.ref_id, new_amount=amount)


def handle_chat(deps: ChatDeps, user_id: str, message: str) -> dict:
    log.info("chat_message", message=message[:500])
    if not message.strip():
        lang = "en"
        log.info("intent_classified", intent="empty", source="rule", language=lang)
        return {
            "reply": render("empty_message", lang),
            "intent": "empty",
            "language": lang,
            "quote": None,
            "sip_proposal": None,
            "suggested_actions": START_ACTIONS,
        }

    ext, source = classify_message(deps.llm, message)
    lang = resolve_language(message, ext.language if source == "llm" else None)
    parsed = parse_amounts(message)
    with deps.session_factory() as session:
        turn = Turn(
            deps=deps, session=session, user_id=user_id, message=message, ext=ext, source=source,
            lang=lang, parsed=parsed,
        )
        pending = _active_pending(session, user_id)
        if pending is not None and parsed.single is not None and turn.ext.intent in AMEND_INTENTS:
            _amend_pending(turn, pending)

        log.info(
            "intent_classified",
            intent=turn.ext.intent,
            source=source,
            amount=turn.ext.amount,
            frequency=turn.ext.frequency,
            day_of_month=turn.ext.day_of_month,
            language=lang,
            llm_language=ext.language if source == "llm" else None,
        )

        if turn.ext.intent in MONEY_INTENTS:
            if parsed.ambiguous:
                shown = ", ".join(chip_inr(v) if v > 0 else str(v) for v in parsed.distinct)
                if turn.ext.intent == "sip_create":
                    freq = turn.ext.frequency or "monthly"
                    chips = [f"{chip_inr(v)} {freq} SIP" for v in parsed.distinct if v > 0]
                else:
                    chips = [f"Buy {chip_inr(v)}" for v in parsed.distinct if v > 0]
                return _response(
                    turn,
                    render("which_amount", lang, amounts=shown),
                    actions=chips[:4],
                    error="multiple_amounts",
                )
            if turn.ext.amount is None and not parsed.grams and (parsed.words_only or parsed.unparseable):
                return _response(turn, render("digits_please", lang), error="amount_not_in_digits")
        return HANDLERS[turn.ext.intent](turn)
