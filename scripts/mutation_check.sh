#!/usr/bin/env bash
# Mutation check: each mutation of the purchase math is applied to a throwaway COPY of the
# repo, the money tests are run, and the mutation must make them FAIL ("killed"). The real
# source is never edited; afterwards that is verified with git (if available) or file hashes.
#
# Exit 0: every mutation killed. Exit 1: a mutation survived. Exit 2: setup problem.
# Run: docker compose exec -T api bash scripts/mutation_check.sh
set -uo pipefail

SRC="${1:-/app}"
TESTS="${MUTATION_TESTS:-tests/test_money.py}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

snapshot() {
    if command -v git >/dev/null 2>&1 && git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        { git -C "$SRC" diff -- app/; git -C "$SRC" status --porcelain -- app/; } | sha256sum
    else
        (cd "$SRC" && find app -type f -name '*.py' -print0 | sort -z | xargs -0 sha256sum) | sha256sum
    fi
}
BEFORE="$(snapshot)"

tar -C "$SRC" --exclude=.git --exclude=logs --exclude=.hypothesis -cf - . | tar -C "$WORK" -xf -
cd "$WORK"
cp app/money.py "$WORK/money.py.orig"
cp app/config.py "$WORK/config.py.orig"

run_tests() { python -m pytest -x -q -p no:cacheprovider "$TESTS" >/dev/null 2>&1; }

echo "Baseline: unmutated money tests must pass ($TESTS)..."
if ! run_tests; then
    echo "ERROR: the money tests fail without any mutation; fix that first." >&2
    exit 2
fi

GRAMS='(budget / price).quantize(GRAM, rounding=ROUND_DOWN)'
VALUE='(grams * price).quantize(RUPEE, rounding=ROUND_HALF_UP)'
GST='(gold_value * GST_RATE).quantize(RUPEE, rounding=ROUND_HALF_UP)'

# name | file | find | replace
MUTANTS=(
    "grams ROUND_DOWN -> ROUND_HALF_UP|app/money.py|$GRAMS|${GRAMS/ROUND_DOWN/ROUND_HALF_UP}"
    "grams ROUND_DOWN -> ROUND_UP|app/money.py|$GRAMS|${GRAMS/ROUND_DOWN/\"ROUND_UP\"}"
    "grams ROUND_DOWN -> ROUND_HALF_EVEN|app/money.py|$GRAMS|${GRAMS/ROUND_DOWN/\"ROUND_HALF_EVEN\"}"
    "grams quantized to 3 dp|app/money.py|$GRAMS|${GRAMS/GRAM,/Decimal(\"0.001\"),}"
    "gold_value HALF_UP -> DOWN|app/money.py|$VALUE|${VALUE/ROUND_HALF_UP/ROUND_DOWN}"
    "gold_value HALF_UP -> UP|app/money.py|$VALUE|${VALUE/ROUND_HALF_UP/\"ROUND_UP\"}"
    "gold_value HALF_UP -> HALF_EVEN|app/money.py|$VALUE|${VALUE/ROUND_HALF_UP/\"ROUND_HALF_EVEN\"}"
    "GST HALF_UP -> DOWN|app/money.py|$GST|${GST/ROUND_HALF_UP/ROUND_DOWN}"
    "GST HALF_UP -> UP|app/money.py|$GST|${GST/ROUND_HALF_UP/\"ROUND_UP\"}"
    "GST HALF_UP -> HALF_EVEN|app/money.py|$GST|${GST/ROUND_HALF_UP/\"ROUND_HALF_EVEN\"}"
    "GST rate 0.03 -> 0.18|app/config.py|GST_RATE = Decimal(\"0.03\")|GST_RATE = Decimal(\"0.18\")"
    "minimum bound >= -> >|app/money.py|if amount < MIN_PURCHASE:|if amount <= MIN_PURCHASE:"
    "maximum bound <= -> <|app/money.py|if amount > MAX_PURCHASE:|if amount >= MAX_PURCHASE:"
    "remove total_charged <= A guard|app/money.py|if total_charged <= amount:|if True:"
)

survivors=0
printf '\n%-36s %s\n' "MUTATION" "RESULT"
printf '%-36s %s\n' "------------------------------------" "--------"
for spec in "${MUTANTS[@]}"; do
    IFS='|' read -r name file from to <<<"$spec"
    cp "$WORK/money.py.orig" app/money.py
    cp "$WORK/config.py.orig" app/config.py
    count="$(grep -cF -- "$from" "$file")"
    if [ "$count" != "1" ]; then
        echo "ERROR: expected exactly one match for '$name' in $file, found $count. Update this script." >&2
        exit 2
    fi
    python - "$file" "$from" "$to" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_text(p.read_text().replace(sys.argv[2], sys.argv[3]))
PY
    if run_tests; then
        printf '%-36s %s\n' "$name" "SURVIVED"
        survivors=$((survivors + 1))
    else
        printf '%-36s %s\n' "$name" "killed"
    fi
done
cd /

AFTER="$(snapshot)"
if [ "$BEFORE" != "$AFTER" ]; then
    echo "ERROR: app/ changed during the mutation check; the source was not restored." >&2
    exit 2
fi
echo
echo "Source check: app/ unchanged ($(command -v git >/dev/null 2>&1 && echo git || echo file hashes))."

if [ "$survivors" -ne 0 ]; then
    echo "FAIL: $survivors mutation(s) survived; add a test that kills each one." >&2
    exit 1
fi
echo "OK: all ${#MUTANTS[@]} mutations killed."
