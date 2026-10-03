"""Interactive chat client for manual testing of /chat.

Run (PowerShell or any terminal):
    docker compose exec api python scripts/chat_cli.py
    docker compose exec api python scripts/chat_cli.py --user raj

Type messages as a user would. Extra commands:
    #1, #2 ...   send suggested chip 1, 2 ...
    /ledger      show this user's ledger
    /sips        show this user's SIPs
    /new         start over as a brand-new user
    /quit        exit
"""

import argparse
import time

import httpx

BASE = "http://localhost:8000"


def show_reply(body: dict) -> list[str]:
    if "reply" not in body:  # error envelope
        print(f"  ! {body.get('error')}: {body.get('message')}")
        return []
    tags = [body.get("intent", "?"), body.get("language", "?")]
    if body.get("error"):
        tags.append(f"error={body['error']}")
    print(f"  [{' | '.join(tags)}]")
    for line in body["reply"].splitlines():
        print(f"  {line}")
    if body.get("quote"):
        q = body["quote"]
        print(f"  (quote {q['quote_id'][:8]}: {q['grams']} g, total ₹{q['total_charged']}, expires {q['expires_at'][11:19]} UTC)")
    if body.get("purchase"):
        p = body["purchase"]
        print(f"  (purchase {p['purchase_id'][:8]}: {p['grams']} g for ₹{p['total_charged']})")
    if body.get("sip_proposal") or body.get("sip"):
        s = body.get("sip_proposal") or body.get("sip")
        print(f"  (SIP {s['sip_id'][:8]}: {s['status']}, next {s['next_run_date']})")
    chips = body.get("suggested_actions") or []
    for i, chip in enumerate(chips, 1):
        print(f"  #{i} {chip}")
    return chips


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive /chat tester")
    parser.add_argument("--user", default=None, help="X-User-Id (default: a fresh id)")
    parser.add_argument("--base", default=BASE)
    args = parser.parse_args()
    user = args.user or f"cli-{int(time.time())}"
    client = httpx.Client(base_url=args.base, timeout=30)

    health = client.get("/health").json()
    print(f"Connected to {args.base} as user '{user}'. LLM: {health['llm'].get('status')}, price: {health['price_source']['status']}")
    print("Type a message, #N for a chip, /ledger, /sips, /new or /quit.\n")
    chips: list[str] = []
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text in ("/quit", "/exit"):
            break
        if text == "/new":
            user = f"cli-{int(time.time())}"
            print(f"  now chatting as '{user}'")
            continue
        if text == "/ledger":
            led = client.get("/users/me/ledger", headers={"X-User-Id": user}).json()
            for e in led.get("entries", []):
                print(f"  {e['entry_type']:<15} {e['grams']} g  ₹{e['total_charged']}")
            print(f"  total: {led.get('total_grams')} g, ₹{led.get('total_charged')}")
            continue
        if text == "/sips":
            for s in client.get("/sips", headers={"X-User-Id": user}).json().get("sips", []):
                print(f"  {s['schedule']} [{s['status']}] next {s['next_run_date']}, {len(s['instalments'])} instalment(s)")
            continue
        if text.startswith("#") and text[1:].isdigit():
            idx = int(text[1:]) - 1
            if not 0 <= idx < len(chips):
                print("  no such chip")
                continue
            text = chips[idx]
            print(f"  > {text}")
        start = time.perf_counter()
        resp = client.post("/chat", json={"message": text}, headers={"X-User-Id": user})
        chips = show_reply(resp.json())
        print(f"  ({(time.perf_counter() - start) * 1000:.0f} ms)\n")


if __name__ == "__main__":
    main()
