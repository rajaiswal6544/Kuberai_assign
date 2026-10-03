COMPOSE = docker compose

.PHONY: up down test verify trace mutation-check live-llm-check

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

test:
	$(COMPOSE) run --rm api pytest

# End-to-end run against the live API (starts it if needed).
verify: up
	$(COMPOSE) exec -T api bash scripts/verify.sh

# USER must be given explicitly so it never falls back to your login name.
trace:
ifneq ($(origin USER),command line)
	$(error usage: make trace USER=<user_id>)
endif
	$(COMPOSE) run --rm --no-deps api python scripts/trace.py --user $(USER)

mutation-check:
	$(COMPOSE) exec -T api bash scripts/mutation_check.sh

# Sends the spec inputs to the real LLM provider (needs a key in .env).
live-llm-check:
	$(COMPOSE) run --rm --no-deps api python scripts/live_llm_check.py
