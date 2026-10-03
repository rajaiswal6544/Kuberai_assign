from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import GRAM, MAX_PURCHASE, MIN_PURCHASE, RUPEE
from app.money import AmountError, calculate_purchase, format_inr, validate_amount

D = Decimal
P = D("15131.23")


# --- Golden values -------------------------------------------------------------

GOLDEN = [
    # A, grams, gold_value, gst, total_charged
    # From the spec (hand-verified):
    ("10.00", "0.0006", "9.08", "0.27", "9.35"),
    ("333.33", "0.0213", "322.30", "9.67", "331.97"),
    ("500.00", "0.0320", "484.20", "14.53", "498.73"),
    # Added because the three rows above do not detect every rounding change
    # (scripts/mutation_check.sh). Each verified by hand at P = 15131.23:
    # 12.46: budget 12.0970 / P = 0.00079948 -> 0.0007 g. Rounding grams UP gives 0.0008 g.
    ("12.46", "0.0007", "10.59", "0.32", "10.91"),
    # 10.91: total equals A exactly, nothing unspent. Rounding gold_value UP overshoots A.
    ("10.91", "0.0007", "10.59", "0.32", "10.91"),
    # 23377.76: 1.5 g * P = 22696.845 exactly, a half-paisa tie (HALF_UP != HALF_EVEN).
    ("23377.76", "1.5000", "22696.85", "680.91", "23377.76"),
    # 59.23: GST = 57.50 * 0.03 = 1.725 exactly, a half-paisa tie (HALF_UP != HALF_EVEN).
    ("59.23", "0.0038", "57.50", "1.73", "59.23"),
    # 10212.96: 0.6553 g would total 10212.97 (> A), so step 8 drops to 0.6552 g.
    ("10212.96", "0.6552", "9913.98", "297.42", "10211.40"),
]


@pytest.mark.parametrize("amount,grams,gold_value,gst,total", GOLDEN)
def test_golden_values(amount, grams, gold_value, gst, total):
    q = calculate_purchase(amount, P)
    assert q.requested_amount == D(amount)
    assert q.price_per_gram == P
    assert q.grams == D(grams)
    assert q.gold_value == D(gold_value)
    assert q.gst == D(gst)
    assert q.total_charged == D(total)
    assert q.unspent == D(amount) - D(total)
    # Exact representation, not just numeric equality: grams always 4 dp, rupees 2 dp.
    assert str(q.grams) == grams
    assert str(q.total_charged) == total


def test_spec_card_example_ten_rupees():
    # Kuber.AI card: Rs 10 = Rs 9.71 gold value + Rs 0.29 GST before 4-dp gold rounding.
    q = calculate_purchase("10", P)
    assert q.total_charged <= D("10.00")
    assert q.unspent == D("0.65")


# --- Validation ------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,code",
    [
        ("-5000", "negative"),
        ("-0.50", "negative"),
        ("0", "zero"),
        ("0.00", "zero"),
        ("0.5", "below_minimum"),
        ("5", "below_minimum"),
        ("9.99", "below_minimum"),
        ("100000.01", "above_maximum"),
        ("100000000000", "above_maximum"),
        ("1" + "0" * 40, "above_maximum"),
        ("1" + "0" * 40 + ".123", "above_maximum"),
        ("333.333", "too_many_decimals"),
        ("10.001", "too_many_decimals"),
        ("0.001", "too_many_decimals"),
        ("", "not_a_number"),
        ("abc", "not_a_number"),
        ("1e3", "not_a_number"),
        ("NaN", "not_a_number"),
        ("Infinity", "not_a_number"),
        ("1,000", "not_a_number"),  # commas are normalised by the chat layer, not here
        ("₹500", "not_a_number"),
        ("+500", "not_a_number"),
        ("12.", "not_a_number"),
        (D("NaN"), "not_a_number"),
        (D("-1"), "negative"),
    ],
)
def test_invalid_amounts_rejected_with_specific_code(raw, code):
    with pytest.raises(AmountError) as exc:
        calculate_purchase(raw, P)
    assert exc.value.code == code


def test_each_error_kind_has_a_distinct_helpful_message():
    messages = {}
    for raw in ["-5000", "0.5", "100000000000", "333.333", "abc"]:
        with pytest.raises(AmountError) as exc:
            validate_amount(raw)
        messages[exc.value.code] = exc.value.message
    assert len(set(messages.values())) == 5
    assert "negative" in messages["negative"]
    assert "₹10.00" in messages["below_minimum"] and "₹0.50" in messages["below_minimum"]
    assert "₹1,00,000.00" in messages["above_maximum"]
    assert "₹1,00,00,00,00,000.00" in messages["above_maximum"]
    assert "2 decimal places" in messages["too_many_decimals"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10", "10.00"),
        ("10.000", "10.00"),  # trailing zeros are not extra precision
        (" 500 ", "500.00"),
        ("100000", "100000.00"),
        ("100000.00", "100000.00"),
        (250, "250.00"),
        (D("333.33"), "333.33"),
    ],
)
def test_valid_amounts_normalised_to_two_dp(raw, expected):
    assert str(validate_amount(raw)) == expected


def test_boundaries_are_inclusive():
    assert calculate_purchase(str(MIN_PURCHASE), P).total_charged <= MIN_PURCHASE
    assert calculate_purchase(str(MAX_PURCHASE), P).total_charged <= MAX_PURCHASE


def test_float_is_refused_outright():
    with pytest.raises(TypeError):
        validate_amount(10.0)
    with pytest.raises(TypeError):
        calculate_purchase(500.0, P)


@pytest.mark.parametrize("price", [D("0"), D("-1.00"), D("15131.234"), D("NaN"), 15131.23])
def test_bad_price_is_a_programming_error(price):
    with pytest.raises(ValueError):
        calculate_purchase("500", price)


def test_amount_too_small_for_one_step_of_gold():
    with pytest.raises(AmountError) as exc:
        calculate_purchase("10", D("99999999.99"))
    assert exc.value.code == "too_small_for_gold"


@pytest.mark.parametrize(
    "value,expected",
    [
        (D("0"), "₹0.00"),
        (D("9.35"), "₹9.35"),
        (D("999.5"), "₹999.50"),
        (D("1000"), "₹1,000.00"),
        (D("100000"), "₹1,00,000.00"),
        (D("12345678.9"), "₹1,23,45,678.90"),
        (D("-5000"), "-₹5,000.00"),
    ],
)
def test_format_inr(value, expected):
    assert format_inr(value) == expected


# --- Properties ------------------------------------------------------------------

amounts = st.decimals(min_value=MIN_PURCHASE, max_value=MAX_PURCHASE, places=2)
prices = st.decimals(min_value=D("100.00"), max_value=D("50000.00"), places=2)


@settings(max_examples=2000, deadline=None)
@given(amount=amounts, price=prices)
def test_purchase_properties(amount, price):
    q = calculate_purchase(amount, price)

    # Never charge more than the user entered.
    assert q.total_charged <= amount
    assert q.unspent >= 0
    assert q.unspent == amount - q.total_charged

    # Precision: grams at most 4 dp, every rupee figure at most 2 dp.
    assert q.grams == q.grams.quantize(GRAM)
    assert q.grams > 0
    for rupees in (q.requested_amount, q.price_per_gram, q.gold_value, q.gst, q.total_charged, q.unspent):
        assert rupees == rupees.quantize(RUPEE)

    # Internal consistency of the breakdown.
    assert q.gold_value == (q.grams * price).quantize(RUPEE, rounding=ROUND_HALF_UP)
    assert q.gst == (q.gold_value * D("0.03")).quantize(RUPEE, rounding=ROUND_HALF_UP)
    assert q.total_charged == q.gold_value + q.gst

    # Grams never exceed what the GST-exclusive budget buys, and are at most one
    # 0.0001 g step below it (the step-8 adjustment).
    budget = amount / D("1.03")
    assert q.grams * price <= budget
    assert q.grams >= (budget / price).quantize(GRAM, rounding=ROUND_DOWN) - GRAM

    # Unspent is bounded by one gold step plus rounding (spec section 12, Phase 2).
    assert amount - q.total_charged < D("0.0001") * price * D("1.03") + D("0.02")


@settings(max_examples=500, deadline=None)
@given(amount=amounts, price=prices)
def test_calculation_is_deterministic_and_input_form_independent(amount, price):
    assert calculate_purchase(amount, price) == calculate_purchase(str(amount), price)


# --- H7 additions (hand-computed, hardcoded; never recomputed with the code under test) ---

def test_maximum_boundary_golden():
    # A = 1,00,000.00 at P = 15131.23 (hand computation, shown for approval):
    #   budget = 100000 / 1.03 = 97087.3786...
    #   0.0001 g steps: 6.4163 g * P = 97086.511049 <= budget < 6.4164 g * P = 97088.024172
    #   gold_value = 97086.51, gst = 97086.51 * 0.03 = 2912.5953 -> 2912.60
    #   total = 99999.11, unspent = 0.89
    q = calculate_purchase("100000.00", P)
    assert (q.grams, q.gold_value, q.gst, q.total_charged, q.unspent) == (
        D("6.4163"), D("97086.51"), D("2912.60"), D("99999.11"), D("0.89"),
    )
    with pytest.raises(AmountError) as exc:
        calculate_purchase("100000.01", P)
    assert exc.value.code == "above_maximum"


def test_one_gram_costs_15585_17():
    # 1 g * P = 15131.23; GST 453.9369 -> 453.94; total 15585.17. At 15585.16 the floor of
    # budget / P is 0.9999 g, so 15585.17 is the smallest amount that delivers 1.0000 g.
    from app.money import amount_for_grams

    b = amount_for_grams(D("1"), P)
    assert (b.requested_amount, b.grams, b.gold_value, b.gst, b.total_charged) == (
        D("15585.17"), D("1.0000"), D("15131.23"), D("453.94"), D("15585.17"),
    )
    assert calculate_purchase("15585.16", P).grams == D("0.9999")


def test_minimum_boundary_is_exactly_ten_rupees():
    assert calculate_purchase("10.00", P).total_charged == D("9.35")
    with pytest.raises(AmountError) as exc:
        calculate_purchase("9.99", P)
    assert exc.value.code == "below_minimum"
