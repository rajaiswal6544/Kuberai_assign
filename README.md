# Digital Gold Backend

A Python backend that reproduces Kuber.AI's digital-gold journey: a chat assistant that
answers gold questions and moves users toward buying, one-time purchases (quote → confirm),
and gold SIPs (create, run instalments as they fall due, pause, resume, cancel). Every
purchase and instalment lands in an append-only ledger.

## Setup (under five minutes)

Requirements: Docker Desktop (with Compose v2). Nothing else on the host.

```bash
cp .env.example .env          # optional: add GROQ_API_KEY for real LLM answers
docker compose up -d --build  # API on http://localhost:8000, Postgres 16 alongside
curl -H "X-User-Id: demo" localhost:8000/health
```

Without an LLM key everything still works: chat runs on the rule-based classifier and
advice questions get an honest "advice is temporarily unavailable" reply.

### The one command

```bash
make verify
```

Brings the stack up and runs `scripts/verify.sh`: health → advice question → quote →
confirm → duplicate confirm (no second charge) → SIP via chat → confirm → `run-due` for
three dates → pause → `run-due` (no instalment) → price outage → quote refused → ledger →
admin call without token (401) → chat-level duplicate purchase and SIP → expired quote via
`/admin/clock` → SIP instalment during an outage (pending_retry, then executed once) → reply
language → injection and invalid amounts (ledger unchanged) → the user's log timeline.
Ends with `VERIFY OK`. Without make: `docker compose exec -T api bash scripts/verify.sh`.

### Commands (no make)

Every command works without `make` and is safe to paste into Windows PowerShell. Start the
stack first with `docker compose up -d --build`. Replace `demo-user` with the user id you
want (PowerShell treats `<...>` as redirection, so there are no angle-bracket placeholders).

| Task | Command |
| --- | --- |
| Start / stop | `docker compose up -d --build` / `docker compose down` |
| Tests | `docker compose run --rm api pytest -rs` |
| Verify (end to end) | `docker compose exec -T api bash scripts/verify.sh` |
| Trace one user | `docker compose exec -T api python scripts/trace.py --user demo-user` |
| Mutation check | `docker compose exec -T api bash scripts/mutation_check.sh` |
| Live LLM check | `docker compose exec -T api python scripts/live_llm_check.py` |
| Chat from the terminal | `docker compose exec -T api bash scripts/chat.sh demo-user "Buy gold worth 500"` |
| Admin helper | `docker compose exec -T api bash scripts/admin.sh run-due 2026-10-02` (also `price down`, `price up`, `clock 301`, `reset`) |
| No-key demo | `docker compose exec -T api bash scripts/nokey_check.sh` |
| Reset demo data | `docker compose exec -T api bash scripts/reset.sh` (refused when `ENV=production`) |
| Reconcile a transaction log | `docker compose exec -T api python scripts/reconcile.py samples/txn_log.csv` |
| Wipe everything incl. schema | `docker compose down -v` (removes the database volume) |

The `Makefile` wraps the same commands for convenience (`make test`, `make verify`,
`make trace USER=demo-user`, ...); nothing requires it.

### Schema changes

Tables are created on startup (`create_all`); there are no migrations. A fresh clone gets the
full schema automatically. After pulling a change that alters the schema (a new column,
status value or constraint), recreate the database once with `docker compose down -v`,
then `docker compose up -d --build`. Tests always rebuild their own `gold_test` schema.

### Commit messages

`type(scope): summary`, where type is one of `feat`, `fix`, `test`, `docs`, `chore`,
e.g. `fix(chat): decide reply language on the server`.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `groq` | `groq` or `gemini` |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Model id for the chosen provider |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | empty | No key → rule-based classification only |
| `LLM_TIMEOUT_SECONDS` | `8` | Per LLM call; on timeout the fallback answers |
| `PRICE_BASE` | `15131.23` | Simulated gold price, INR per gram, ex-GST |
| `PRICE_SOURCE_MODE` | `up` | Start with the price source `up` or `down` |
| `PRICE_MAX_AGE_SECONDS` | `300` | Older prices are stale and never sold on |
| `QUOTE_TTL_SECONDS` | `300` | How long a quote can be confirmed |
| `PENDING_ACTION_TTL_SECONDS` | `300` | How long a SIP proposal waits for "yes" in chat |
| `BUSINESS_TZ` | `Asia/Kolkata` | Zone for calendar dates (SIP due dates) |
| `ADMIN_TOKEN` | `dev-admin-token` | Required in `X-Admin-Token` for `/admin/*` (a shared-secret stopgap, not real auth) |
| `ENV` | `development` | `production` disables `/admin/clock` and `/admin/reset` and refuses the default admin token |
| `LOG_LEVEL` / `LOG_FILE` | `INFO` / `logs/app.jsonl` | JSON logs to stdout and this file |

`DATABASE_URL` is set by `docker-compose.yml`.

## Tests, verification and tracing

- Tests (`docker compose run --rm api pytest -rs`): 280+ tests, zero skips, against a real Postgres (`gold_test`, created automatically; your
  dev data is never touched). Includes golden money values, Hypothesis properties,
  concurrent double-confirm and double-`run-due`, every SIP rule, and all spec chat inputs
  run twice (with a scripted fake LLM, and with the rule-based fallback alone).
- Mutation check: changes each rounding mode in the purchase math (and removes the
  never-exceed-the-amount step) on a throwaway copy, and asserts the suite fails every time.
- Trace (`scripts/trace.py --user demo-user`): prints that user's story from `logs/app.jsonl`, grouped by request.
- Live LLM check: sends the spec's chat inputs to the real provider and prints what
  was extracted (not part of the test suite).

## API

All requests that act for a user send `X-User-Id` (created on first sight; there is no auth).
Money is always a JSON **string** (`"500.00"`), never a number. All `POST`s except `/admin/*`
accept `Idempotency-Key`: same key + same body replays the stored response
(`Idempotent-Replay: true`); same key + different body → 409.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | DB, price source and LLM status |
| POST | `/chat` | `{message}` → `{reply, intent, language, quote, sip_proposal, suggested_actions, ...}` |
| GET | `/price` | Current price and its age; 503 if unavailable |
| POST | `/quotes` | `{amount}` → quote (201, or 200 when an identical open quote is reused) |
| POST | `/purchases` | `{quote_id}` → purchase (201; a repeat returns 200 with `idempotent_replay: true`) |
| POST | `/sips` | `{amount, frequency, day_of_month?, start_date?}` → proposal |
| POST | `/sips/{id}/confirm` | proposed → active (confirming never charges) |
| POST | `/sips/{id}/pause`, `/resume`, `/cancel` | Lifecycle; invalid transitions → 409 with a reason |
| GET | `/sips` | The user's SIPs with their instalments |
| GET | `/users/me/ledger` | Ledger entries, total grams, total charged |
| POST | `/admin/run-due?as_of=YYYY-MM-DD` | Execute due instalments up to an IST date (default: today in IST); missed dates are caught up (max 7 per SIP per run) |
| POST | `/admin/price-source` | `{mode: "up" \| "down", expire_cache?: bool}` to simulate an outage |
| POST | `/admin/clock` | `{advance_seconds: N}` moves the app clock forward (in memory; not in production) |
| POST | `/admin/reset` | Truncates all app tables, clears the price cache and clock offset (not in production) |

Every `/admin/*` call needs the `X-Admin-Token` header (`ADMIN_TOKEN`); missing or wrong → 401.

Every error response has one shape: `{"error": "<code>", "message": "<user-facing text>", "request_id": "..."}`
(validation errors, unknown routes and unexpected failures included). The `request_id` matches the
`X-Request-Id` response header and every log line for that request.

Chat input rules: `message` is a string of at most 1000 characters (longer → 422, never truncated);
an empty message gets a friendly prompt. Amounts in chat may be written `500`, `₹500`, `Rs. 500`,
`1,00,000`, `5k`, `2 hazaar`, `5 sau`, `1 lakh`; written-out numbers are not guessed. Gram requests
("buy 2 grams") are answered with the rupee amount that buys at least that much.

## Architecture

**The LLM understands; the backend decides.** The LLM only (a) turns a message into intent
JSON and (b) writes short advice text. Everything else is backend code:

```
message ─► injection rules ─► LLM extraction ──(fails/invalid)──► rule-based fallback
                                   │
                    server checks: amount must appear in the message,
                    dates resolved by the server clock
                                   ▼
                     intent handler (orchestrator.py)
          ┌──────────────┬───────────────┬──────────────┬─────────────┐
       advice/compare   quote          SIP proposal   lifecycle     templates
       (LLM text,       (money.py,     (sips.py)      pause/resume  (out of scope,
        FACTS only,      pricing.py)                  /cancel        injection)
        post-checked)        │               │
                             ▼               ▼
                     pending_actions: what "yes" confirms (server-issued id)
                             │
                             ▼
              purchases / sip_instalments ─► ledger_entries (append-only)
```

- **Money is exact**: `Decimal` end to end, `NUMERIC(14,2)` rupees and `NUMERIC(14,4)` grams,
  string amounts in JSON, LLM JSON parsed with `parse_float=Decimal`.
- **Nothing charges twice**, enforced by the database: `UNIQUE(purchases.quote_id)` plus a
  `SELECT ... FOR UPDATE` on the quote, `UNIQUE(sip_instalments.sip_id, due_date)`,
  `UNIQUE(purchases.sip_instalment_id)`, a partial unique index against identical live SIPs,
  and the idempotency key's primary key.
- **Dependencies fail deliberately**: no fresh price → 503 / `pending_retry`, never a stale
  sale; LLM down, slow or invalid → rule-based fallback, logged as `llm_fallback_used`.
- **Traceable**: every log line is JSON with `ts, level, event, user_id, request_id`.

Code map: `app/money.py` (purchase math) · `pricing.py` · `quotes.py` · `purchases.py` ·
`sips.py` · `ledger.py` · `idempotency.py` · `chat/` (orchestrator, intents, fallback,
prompts, templates) · `llm/` (Groq, Gemini, fake) · `api/` (routes).

## Transaction log reconciliation (bonus)

`scripts/reconcile.py <file>` reads a transaction log and reports anomalies as a table and as
`reconcile_report.json`. The real log format was not specified, so this format is **assumed**:
CSV with a header (or JSON lines) and fields `txn_id, user_id, order_id, event, amount, ts`, where
`event` is `charge_initiated | charge_success | gold_credited | confirmation`, `amount` is a decimal
rupee string and `ts` is ISO-8601. Detected: `DUPLICATE_ROW`, `DOUBLE_CHARGE` (same order, or same
user and amount within 60 s, `--window` to change), `MISSING_CONFIRMATION`, `ORPHAN_CONFIRMATION`,
`AMOUNT_MISMATCH`, plus `MALFORMED_ROW` for unparseable lines. `samples/txn_log.csv` has one planted
example of each.

## Decision log

Each entry: what was decided, why, and the main alternative that was rejected.

### Money

- **The amount the user enters is GST-inclusive, and only delivered grams are charged.** ₹10 →
  0.0006 g → ₹9.08 gold + ₹0.27 GST = ₹9.35 charged; the ₹0.65 that can't buy another 0.0001 g is
  shown as "not charged" with the reason. This matches Kuber.AI's card and never overcharges.
  *Rejected:* charging the full amount (the user would pay for gold they don't get).
- **Grams are rounded down to 4 dp, rupees half-up to 2 dp, and the total may never exceed the
  amount.** If rounding ever pushes it over, one 0.0001 g step is removed. Checked exhaustively for
  all 9,999,001 amounts from ₹10 to ₹1,00,000 at the default price: zero overcharges, worst unspent
  ₹1.56. *Rejected:* delivering the extra step when a rounded-down gold value happens to fit (e.g. at
  ₹12.46): it would charge less than that gold is worth.
- **`Decimal` everywhere, `NUMERIC` in Postgres, amounts as JSON strings.** A JSON number for
  `amount` is rejected with a 422; LLM JSON is parsed with `parse_float=Decimal`; a float passed to
  the money code raises. *Rejected:* accepting numbers and converting them (the float has already
  happened by then).
- **Limits: ₹10 minimum, ₹1,00,000 maximum per purchase**, both inclusive.
- **The test suite is checked by mutation, not trusted.** The three golden rows in the brief did not
  catch a change to the grams rounding (the never-exceed step silently undid it), so rows were added
  that do. `scripts/mutation_check.sh` applies 14 mutations (rounding modes, GST rate, both limits,
  removing the never-exceed step, 3-dp grams) and all must fail the money tests.

### Never charging twice

- **Enforced by the database, not by Python checks.** `UNIQUE(purchases.quote_id)` plus
  `SELECT … FOR UPDATE` on the quote, `UNIQUE(sip_instalments.sip_id, due_date)`,
  `UNIQUE(purchases.sip_instalment_id)`, a partial unique index that forbids two identical live SIPs,
  an append-only ledger (a trigger rejects `UPDATE`/`DELETE`), and an idempotency key reserved by
  `INSERT` before the work runs. Tested with 8 concurrent confirms and concurrent `run-due` calls.
  *Rejected:* check-then-insert in application code (races under concurrency).
- **A repeated confirm returns the original purchase with `idempotent_replay: true` and HTTP 200,**
  and confirming an already-active SIP does the same, so client retries are always safe. 409 is used
  only for transitions that are genuinely invalid (e.g. resuming a cancelled SIP).
- **A quote locks its price for 5 minutes.** Executing it during a price outage still succeeds at
  the quoted price; an expired quote is never executed, and "yes" after expiry offers a fresh quote.

### LLM: it understands, the backend decides

- **The LLM only classifies intent and writes short advice text.** Prices, grams, dates and every
  money action come from backend code; money moves only when a server-issued quote or SIP proposal
  is confirmed. Prompt injection therefore has nothing to act on.
- **A server-side parser is the source of truth for amounts.** If the LLM's amount disagrees with
  the parser, the parser wins (`amount_mismatch` is logged); if the message contains no amount, the
  LLM's is dropped. It accepts `5k`, `2 hazaar`, `5 sau` and `1 lakh` only when attached to a
  number, never guesses written-out numbers, and asks "which one?" when a message has two amounts.
  Dates such as "from the 31st" are removed before amounts are extracted.
- **Rules run before the LLM where they can decide safely:** prompt-injection patterns, exact
  confirm/decline words ("yes", "haan", "no", "nahi"…), and empty or emoji-only messages never call
  the LLM. A confirmation that contains an amount ("yes, make it 5000") is treated as a new request
  that supersedes the pending one, never as a confirmation of the old one.
- **Reply language is decided on the server** from Devanagari or unambiguous Hinglish words; the
  LLM's own language label is only a tie-breaker. *Rejected:* trusting the LLM's label, which once
  answered an English request in Hinglish.
- **Every LLM-written reply passes an output guard** or is replaced by a template built only from
  known facts. The guard rejects figures not in the facts given to the model, claims that money
  moved ("I've bought…"), banned claims ("compounds", "regulated", "guaranteed", "will rise"…), more
  than one question, and replies still over 140 words after trimming at 120.
- **If the LLM is down, slow or returns invalid JSON, a rule-based classifier takes over**
  (`llm_fallback_used`). Every assignment input works with no API key at all; that is tested, and
  `scripts/nokey_check.sh` demonstrates it. Groq (`openai/gpt-oss-120b`) is the default provider,
  called with temperature 0, low reasoning effort and JSON mode; Gemini is available as an option.

### Conversation

- **Ambiguous requests ask one question and never create a quote** ("I want to invest 5000", "gold").
- **Comparison and trust questions (gold vs FD, "my mom says…") get a balanced answer with no sales
  nudge** and no purchase chips.
- **One pending action per user; the newest replaces the older one**, and the replaced quote is
  marked `superseded`.
- **Cancelling a SIP asks for "yes" first,** because it is final. Pause and resume act immediately.
- **Gram requests are converted to rupees**: "buy 1 gram" answers with ₹15,585.17, the smallest
  amount that delivers at least 1 g, as a chip; no quote is created until that amount is confirmed.
- **Selling is not offered**; the assistant says so and shows the user's holdings.

### SIPs

- **Confirming a SIP never charges; only `run-due` does,** so there is one code path for
  instalments.
- **A start date in the past starts today and says so; nothing is ever backfilled.** A monthly SIP
  on the 29th–31st runs on the last day of shorter months, and the proposal says so.
- **Missed dates are caught up, capped.** If the scheduler missed days, `run-due` executes each
  missed date in order at the current price and flags it `is_catch_up`. More than 7 due for one SIP
  in a single run → only the most recent 7 are charged and older ones are recorded as
  `skipped_missed`, so a user is never charged for more than a week at once without notice.
- **No price → `pending_retry`, never a stale price, never silently skipped.** Retried before new
  dates on the next run; after 5 attempts it is marked `failed` and logged.
- **Paused periods are skipped, not backfilled;** resume schedules from the resume date.

### Operations

- **Calendar dates are IST (Asia/Kolkata); timestamps are stored in UTC.** At 23:30 UTC on 2 Oct it
  is already 3 Oct for the user. *Rejected:* UTC dates, which would put an Indian user's 1 a.m.
  instalment on the previous day.
- **Every log line is JSON with `user_id` and `request_id`,** written to stdout and
  `logs/app.jsonl`. SIP instalments executed by the admin `run-due` call are logged against the SIP
  owner, so `scripts/trace.py --user <id>` shows the whole journey. API keys and the system prompt
  are never logged.
- **Admin endpoints need an `X-Admin-Token`.** It is a shared-secret stopgap, not real
  authentication; `ENV=production` refuses the default token and disables the demo-only clock and
  reset endpoints.
- **Tests run against a real Postgres, never SQLite,** so `NUMERIC`, row locks and unique
  constraints are exercised. They use a separate `gold_test` database.
- **No Alembic.** Tables are created on startup; a schema change needs `docker compose down -v`.
  *Rejected:* migrations, which are overhead for a single-version take-home.

## AI disclosure

This project was built with **Claude Code** (Anthropic's coding agent, model Claude Opus 5.5)
working in my terminal.

**How the work was split**

- **Me:** I wrote the specification: the requirements, money rules, data model, API, conversation
  behaviour and a phased build plan, and later a second hardening specification after running the
  system myself on Windows and finding gaps. I made the product decisions when asked: the
  ₹1,00,000 maximum, charging only for delivered grams, which amount formats to accept, keeping
  HTTP 200 for repeated SIP confirms, the advice length limits, the catch-up policy, the admin token
  and the LLM provider and model. I supplied the API key and ran the system and its scripts myself.
- **Claude Code:** wrote the application code, the tests, the scripts (verify, trace, mutation
  check, no-key check, reconciliation) and the rest of this README. Where the spec was ambiguous it
  asked rather than guessing, and it recorded every decision it made, with the rejected
  alternative, in `NOTES_FOR_HUMAN.md`. It also found problems in my spec, for example that my three
  golden values could not detect a grams-rounding bug.

**What was verified rather than taken on trust**

- The golden money values were computed by hand (mine in the original table; the additional ones
  were shown to me with the arithmetic).
- An exhaustive run over every amount from ₹10 to ₹1,00,000 at the default price.
- The mutation check, which proves the tests fail when the money rounding changes.
- End-to-end runs of `verify.sh` against the real Groq model, plus live runs of all the assignment
  inputs. These live runs caught real bugs that the unit tests had missed, for example an ambiguous
  "I want to invest 5000" being turned into a quote while another quote was pending, and a
  comparison reply that ended with a sales nudge. Both were fixed and now have regression tests.

**AI inside the product:** at runtime an LLM (Groq, `openai/gpt-oss-120b`) is used only to
classify the user's message and to write short advice text. It never sets prices, computes
amounts or moves money, and everything works without it.

## What's unfinished

- **No real authentication.** `X-User-Id` is trusted as sent. Admin endpoints are protected only by
  a shared token.
- **Simulated price feed.** There is no real gold price provider. The price cache, the simulated
  outage switch and the admin clock offset are in-process memory, so the app assumes a single
  worker; several workers would each have their own.
- **No scheduler.** Instalments run only when `/admin/run-due` is called; production would need a
  daily job (and the catch-up cap exists for when that job misses days).
- **No migrations.** Schema changes need `docker compose down -v`, which deletes data.
- **No retry or backoff on LLM rate limits.** Groq returned HTTP 429 during testing; the rule-based
  fallback answered correctly, but the user loses the LLM's understanding for those messages.
- **The output guard can reject a correct answer.** A comparison that uses a banned phrase (e.g. a
  correct FD answer that says "guaranteed") falls back to a short neutral template.
- **Language coverage is limited.** Hinglish templates have not been reviewed by a native speaker,
  and Devanagari input gets Hinglish (Latin script) replies rather than Hindi.
- **Narrow date understanding.** Only "yesterday", "today", "tomorrow" and "from the Nth" are
  understood as start dates; anything else ("next Monday", "5 November") starts today.
