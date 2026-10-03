"""Purchase math. Pure functions, Decimal only, no I/O.

The amount the user enters is GST-inclusive. We buy the most gold (in 0.0001 g
steps) that the amount pays for and charge only for the grams delivered; the
remainder is shown as "unspent" and never charged.
"""

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, localcontext

from app.config import GRAM, GST_RATE, MAX_PURCHASE, MIN_PURCHASE, RUPEE

UNSPENT_REASON = (
    "Gold is bought in steps of 0.0001 g, so the few paise that can't buy another "
    "step are not charged."
)

_AMOUNT_RE = re.compile(r"^-?\d+(\.\d+)?$")


class AmountError(ValueError):
    """A user-facing validation error. `code` is stable; `message` is shown to the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PurchaseBreakdown:
    requested_amount: Decimal
    price_per_gram: Decimal
    grams: Decimal
    gold_value: Decimal
    gst: Decimal
    total_charged: Decimal
    unspent: Decimal


def format_inr(value: Decimal) -> str:
    """₹ with Indian digit grouping, e.g. ₹1,00,000.00."""
    q = value.quantize(RUPEE, rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    whole, frac = f"{abs(q):f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{frac}"


def _limits() -> str:
    return f"{format_inr(MIN_PURCHASE)} to {format_inr(MAX_PURCHASE)}"


def validate_amount(raw: str | Decimal | int) -> Decimal:
    """Parse and validate a rupee amount. Returns it as a 2 dp Decimal or raises AmountError."""
    if isinstance(raw, (float, bool)):
        raise TypeError("money amounts must be str, int or Decimal, never float")

    if isinstance(raw, Decimal):
        if not raw.is_finite():
            raise AmountError("not_a_number", "Please enter the amount as a number, like 500 or 250.50.")
        amount = raw
    else:
        text = str(raw).strip()
        if not _AMOUNT_RE.match(text):
            raise AmountError("not_a_number", "Please enter the amount as a number, like 500 or 250.50.")
        amount = Decimal(text)

    if amount < 0:
        raise AmountError(
            "negative", f"The amount can't be negative. Please enter an amount from {_limits()}."
        )
    if amount == 0:
        raise AmountError(
            "zero", f"The amount has to be more than zero. Please enter an amount from {_limits()}."
        )
    # Checked before decimals: quantize() on a huge number overflows Decimal precision.
    if amount > MAX_PURCHASE:
        entered = f"; you entered {format_inr(amount)}" if amount < Decimal(10) ** 15 else ""
        raise AmountError(
            "above_maximum",
            f"The maximum per purchase is {format_inr(MAX_PURCHASE)}{entered}.",
        )
    # Trailing zeros are fine ("10.000" is 10.00); real fractions of a paisa are not.
    if amount != amount.quantize(RUPEE, rounding=ROUND_DOWN):
        raise AmountError(
            "too_many_decimals",
            "Amounts can have at most 2 decimal places (rupees and paise), like 333.33.",
        )
    if amount < MIN_PURCHASE:
        raise AmountError(
            "below_minimum",
            f"The minimum is {format_inr(MIN_PURCHASE)}; you entered {format_inr(amount)}.",
        )
    return amount.quantize(RUPEE)


def _check_price(price: Decimal) -> None:
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise ValueError(f"price must be a positive Decimal, got {price!r}")
    if price != price.quantize(RUPEE, rounding=ROUND_DOWN):
        raise ValueError(f"price must have at most 2 decimal places, got {price}")


def calculate_purchase(amount: str | Decimal | int, price_per_gram: Decimal) -> PurchaseBreakdown:
    """Spec section 5, steps 1-9. Raises AmountError for anything the user should fix."""
    amount = validate_amount(amount)
    _check_price(price_per_gram)
    price = price_per_gram.quantize(RUPEE)

    # Pin precision so a caller changing the global Decimal context can't change results.
    with localcontext() as ctx:
        ctx.prec = 28
        budget = amount / (1 + GST_RATE)
        grams = (budget / price).quantize(GRAM, rounding=ROUND_DOWN)

        while grams > 0:
            gold_value = (grams * price).quantize(RUPEE, rounding=ROUND_HALF_UP)
            gst = (gold_value * GST_RATE).quantize(RUPEE, rounding=ROUND_HALF_UP)
            total_charged = gold_value + gst
            if total_charged <= amount:
                break
            # Rounding pushed the charge over what the user entered: deliver one step less.
            grams -= GRAM

    if grams <= 0:
        raise AmountError(
            "too_small_for_gold",
            f"{format_inr(amount)} is too small to buy the smallest unit of gold (0.0001 g) "
            f"at {format_inr(price)}/g.",
        )

    return PurchaseBreakdown(
        requested_amount=amount,
        price_per_gram=price,
        grams=grams,
        gold_value=gold_value,
        gst=gst,
        total_charged=total_charged,
        unspent=amount - total_charged,
    )


def amount_for_grams(grams: Decimal, price_per_gram: Decimal, max_steps: int = 100_000) -> PurchaseBreakdown:
    """The smallest rupee amount whose purchase delivers at least `grams`.

    Purchases are by rupee amount, so a gram request is converted: start at
    grams * price * 1.03 rounded up to the paisa, then add one paisa at a time until the real
    purchase calculation returns at least that many grams. Raises AmountError if the amount
    is outside the purchase limits.
    """
    if not isinstance(grams, Decimal) or not grams.is_finite() or grams <= 0:
        raise AmountError("invalid_grams", "The gold quantity must be more than 0 g.")
    _check_price(price_per_gram)
    target = grams.quantize(GRAM, rounding=ROUND_UP)  # we deliver in 0.0001 g steps
    with localcontext() as ctx:
        ctx.prec = 28
        amount = (target * price_per_gram * (1 + GST_RATE)).quantize(RUPEE, rounding=ROUND_UP)
    amount = max(amount, MIN_PURCHASE)
    for _ in range(max_steps):
        b = calculate_purchase(amount, price_per_gram)  # raises above_maximum past the limit
        if b.grams >= target:
            return b
        amount += RUPEE
    raise AmountError("invalid_grams", "Could not price that quantity of gold.")  # pragma: no cover
