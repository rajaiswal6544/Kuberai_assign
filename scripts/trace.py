"""Print one user's story from logs/app.jsonl as a time-ordered timeline.

Usage: python scripts/trace.py --user <user_id> [--file path/to/app.jsonl]
       python scripts/trace.py <user_id>            (positional form still accepted)
"""

import argparse
import json
import sys
from pathlib import Path

HEAD = ("ts", "level", "event", "user_id", "request_id")
NOISY = {"exception"}
MAX_VALUE = 90


def fmt_value(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= MAX_VALUE else text[: MAX_VALUE - 3] + "..."


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print one user's log timeline.")
    parser.add_argument("user_positional", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--user", help="user id (the X-User-Id value)")
    parser.add_argument("--file", default="logs/app.jsonl", help="JSONL log file")
    args = parser.parse_args(argv[1:])
    user = args.user or args.user_positional
    if not user:
        parser.print_usage()
        print("error: --user is required")
        return 2
    path = Path(args.file)
    if not path.exists():
        print(f"No log file at {path}")
        return 1

    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("user_id") == user:
                rows.append(entry)
    if not rows:
        print(f"No log lines for user {user!r} in {path}")
        return 1
    rows.sort(key=lambda e: e.get("ts") or "")

    print(f"Timeline for user {user}: {len(rows)} events, {rows[0]['ts']} -> {rows[-1]['ts']}")
    last_request = None
    for e in rows:
        request = (e.get("request_id") or "-")[:8]
        if request != last_request:
            print(f"\n  request {request}")
            last_request = request
        ts = (e.get("ts") or "")[:23].replace("T", " ")
        level = (e.get("level") or "").upper()[:4]
        details = "  ".join(
            f"{k}={fmt_value(v)}" for k, v in e.items() if k not in HEAD and k not in NOISY and v is not None
        )
        print(f"  {ts}  {level:<4}  {e.get('event', ''):<26} {details}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
