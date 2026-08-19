"""Deterministic, side-effect-free long-straddle calculations."""

from decimal import Decimal, localcontext

from app.straddle.domain import (
    OptionType,
    PositionSide,
    Strategy,
    StrategyCalculation,
)
from app.straddle.version import STRADDLE_ENGINE_VERSION

DECIMAL_PRECISION = 28
ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


def _require_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")


def _validate_calculation_inputs(
    strategy: Strategy,
    spot_price: Decimal,
    expiry_spot: Decimal,
    fees: Decimal,
    slippage: Decimal,
) -> None:
    """Enforce only the shape and numeric guarantees needed by the engine."""
    call = strategy.call_leg
    put = strategy.put_leg

    if call.option_type is not OptionType.CALL or call.side is not PositionSide.BUY:
        raise ValueError("call_leg must be a BUY call")
    if put.option_type is not OptionType.PUT or put.side is not PositionSide.BUY:
        raise ValueError("put_leg must be a BUY put")
    if call.underlying != put.underlying:
        raise ValueError("long-straddle legs must have the same underlying")
    if call.strike != put.strike:
        raise ValueError("long-straddle legs must have the same strike")
    if call.expiry != put.expiry:
        raise ValueError("long-straddle legs must have the same expiry")
    if call.quantity != put.quantity:
        raise ValueError("long-straddle legs must have the same quantity")
    if not isinstance(call.quantity, int) or isinstance(call.quantity, bool) or call.quantity <= 0:
        raise ValueError("long-straddle quantity must be a positive integer")

    for name, value in (
        ("call strike", call.strike),
        ("put strike", put.strike),
        ("call premium", call.premium),
        ("put premium", put.premium),
        ("spot_price", spot_price),
        ("expiry_spot", expiry_spot),
        ("fees", fees),
        ("slippage", slippage),
    ):
        _require_decimal(name, value)
    if spot_price == ZERO:
        raise ValueError("spot_price must be non-zero")


def calculate_long_straddle(
    strategy: Strategy,
    *,
    spot_price: Decimal,
    expiry_spot: Decimal,
    fees: Decimal = ZERO,
    slippage: Decimal = ZERO,
) -> StrategyCalculation:
    """Calculate expiry payoff and risk metrics for one long straddle."""
    _validate_calculation_inputs(strategy, spot_price, expiry_spot, fees, slippage)
    call = strategy.call_leg
    put = strategy.put_leg

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        combined_premium = call.premium + put.premium
        total_cost = combined_premium * call.quantity
        upper_break_even = call.strike + combined_premium
        lower_break_even = call.strike - combined_premium
        call_payoff = max(expiry_spot - call.strike, ZERO) * call.quantity
        put_payoff = max(call.strike - expiry_spot, ZERO) * call.quantity
        net_pnl = call_payoff + put_payoff - total_cost - fees - slippage
        maximum_loss = total_cost + fees + slippage
        required_move_points = combined_premium
        required_move_percent = combined_premium / spot_price * ONE_HUNDRED

    return StrategyCalculation(
        engine_version=STRADDLE_ENGINE_VERSION,
        combined_premium=combined_premium,
        total_cost=total_cost,
        upper_break_even=upper_break_even,
        lower_break_even=lower_break_even,
        call_payoff=call_payoff,
        put_payoff=put_payoff,
        net_pnl=net_pnl,
        maximum_loss=maximum_loss,
        required_move_points=required_move_points,
        required_move_percent=required_move_percent,
    )
