"""Reconcile a payments/gold transaction log and report anomalies.

Usage: python scripts/reconcile.py samples/txn_log.csv [--out reconcile_report.json] [--window 60]
Exit code: 0 if the log is clean, 1 if any anomaly was found.

Assumed log format (the real one was not specified): CSV with a header row, or JSON lines,
with fields txn_id, user_id, order_id, event, amount, ts where
  event  in charge_initiated | charge_success | gold_credited | confirmation
  amount is a decimal rupee string (parsed as Decimal, never float)
  ts     is ISO-8601 (a trailing Z is accepted)

Anomalies:
  DUPLICATE_ROW         the same txn_id appears more than once (duplicates are then ignored)
  DOUBLE_CHARGE         2+ charge_success for one order_id, or for the same user and amount
                        on different orders within --window seconds
  MISSING_CONFIRMATION  a charge_success whose order has no gold_credited and/or no confirmation
  ORPHAN_CONFIRMATION   a confirmation or gold_credited for an order with no charge_success
  AMOUNT_MISMATCH       the credited amount differs from the charged amount
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

EVENTS = {"charge_initiated", "charge_success", "gold_credited", "confirmation"}
ORDER = ["DUPLICATE_ROW", "DOUBLE_CHARGE", "MISSING_CONFIRMATION", "ORPHAN_CONFIRMATION", "AMOUNT_MISMATCH", "MALFORMED_ROW"]


@dataclass(frozen=True)
class Txn:
    txn_id: str
    user_id: str
    order_id: str
    event: str
    amount: Decimal
    ts: datetime
    line: int


def read_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".jsonl", ".json", ".ndjson"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return list(csv.DictReader(text.splitlines()))


def parse(rows: list[dict]) -> tuple[list[Txn], list[dict]]:
    txns, problems = [], []
    for i, row in enumerate(rows, start=2):  # line 1 is the CSV header
        try:
            event = str(row["event"]).strip()
            if event not in EVENTS:
                raise ValueError(f"unknown event {event!r}")
            amount = Decimal(str(row["amount"]).strip())
            if not amount.is_finite():
                raise ValueError("amount is not a finite number")
            ts = datetime.fromisoformat(str(row["ts"]).strip().replace("Z", "+00:00"))
            txns.append(
                Txn(str(row["txn_id"]).strip(), str(row["user_id"]).strip(), str(row["order_id"]).strip(),
                    event, amount, ts, i)
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            problems.append({"type": "MALFORMED_ROW", "line": i, "detail": str(exc)})
    return txns, problems


def reconcile(txns: list[Txn], window_seconds: int = 60) -> list[dict]:
    anomalies: list[dict] = []

    # Duplicate rows are reported, then ignored so they don't also look like double charges.
    seen: dict[str, Txn] = {}
    counts: dict[str, int] = defaultdict(int)
    unique: list[Txn] = []
    for t in txns:
        counts[t.txn_id] += 1
        if t.txn_id not in seen:
            seen[t.txn_id] = t
            unique.append(t)
    for txn_id, n in counts.items():
        if n > 1:
            t = seen[txn_id]
            anomalies.append({"type": "DUPLICATE_ROW", "txn_id": txn_id, "order_id": t.order_id, "count": n})

    by_order: dict[str, list[Txn]] = defaultdict(list)
    for t in unique:
        by_order[t.order_id].append(t)

    for order_id, rows in sorted(by_order.items()):
        charges = [t for t in rows if t.event == "charge_success"]
        credits = [t for t in rows if t.event == "gold_credited"]
        confirmations = [t for t in rows if t.event == "confirmation"]
        if len(charges) > 1:
            anomalies.append({
                "type": "DOUBLE_CHARGE", "reason": "same_order", "order_id": order_id,
                "user_id": charges[0].user_id, "txn_ids": [t.txn_id for t in charges],
                "amounts": [str(t.amount) for t in charges],
            })
        if charges:
            missing = [name for name, found in (("gold_credited", credits), ("confirmation", confirmations)) if not found]
            if missing:
                anomalies.append({
                    "type": "MISSING_CONFIRMATION", "order_id": order_id, "user_id": charges[0].user_id,
                    "charge_txn_id": charges[0].txn_id, "missing": missing,
                })
            charged = charges[0].amount
            for c in credits:
                if c.amount != charged:
                    anomalies.append({
                        "type": "AMOUNT_MISMATCH", "order_id": order_id, "user_id": charges[0].user_id,
                        "charged": str(charged), "credited": str(c.amount), "difference": str(charged - c.amount),
                    })
        else:
            for t in credits + confirmations:
                anomalies.append({
                    "type": "ORPHAN_CONFIRMATION", "order_id": order_id, "user_id": t.user_id,
                    "txn_id": t.txn_id, "event": t.event,
                })

    # Same user, same amount, different orders, charged within the window.
    by_user_amount: dict[tuple[str, Decimal], list[Txn]] = defaultdict(list)
    for t in unique:
        if t.event == "charge_success":
            by_user_amount[(t.user_id, t.amount)].append(t)
    for (user_id, amount), rows in sorted(by_user_amount.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        rows.sort(key=lambda t: t.ts)
        for a, b in zip(rows, rows[1:]):
            gap = (b.ts - a.ts).total_seconds()
            if a.order_id != b.order_id and gap <= window_seconds:
                anomalies.append({
                    "type": "DOUBLE_CHARGE", "reason": "same_user_amount_within_window",
                    "user_id": user_id, "amount": str(amount), "order_ids": [a.order_id, b.order_id],
                    "txn_ids": [a.txn_id, b.txn_id], "seconds_apart": gap,
                })

    anomalies.sort(key=lambda a: ORDER.index(a["type"]))
    return anomalies


def _summary(a: dict) -> str:
    keys = [k for k in ("order_id", "order_ids", "txn_id", "txn_ids", "user_id", "reason", "missing",
                        "charged", "credited", "count", "seconds_apart") if k in a]
    return "  ".join(f"{k}={a[k]}" for k in keys)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Reconcile a transaction log.")
    parser.add_argument("path")
    parser.add_argument("--out", default="reconcile_report.json")
    parser.add_argument("--window", type=int, default=60, help="double-charge window in seconds")
    args = parser.parse_args(argv[1:])

    path = Path(args.path)
    txns, problems = parse(read_rows(path))
    anomalies = reconcile(txns, args.window) + problems
    counts = {t: sum(a["type"] == t for a in anomalies) for t in ORDER if any(a["type"] == t for a in anomalies)}
    report = {"input": str(path), "rows": len(txns) + len(problems), "window_seconds": args.window,
              "anomaly_counts": counts, "anomalies": anomalies}
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"{path}: {report['rows']} rows, {len(anomalies)} anomalies")
    print(f"{'TYPE':<22} DETAILS")
    for a in anomalies:
        print(f"{a['type']:<22} {_summary(a)}")
    print(f"\nReport written to {args.out}")
    return 1 if anomalies else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
