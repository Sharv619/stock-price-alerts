from dataclasses import fields, replace
from datetime import date
from decimal import Decimal

import pytest

from app.straddle import (
    OptionLeg,
    OptionType,
    PositionSide,
    STRADDLE_ENGINE_VERSION,
    Strategy,
    calculate_long_straddle,
)


@pytest.fixture
def acceptance_strategy():
    common = {
        "underlying": "NIFTY",
        "side": PositionSide.BUY,
        "strike": Decimal("24300"),
        "expiry": date(2026, 8, 27),
        "quantity": 75,
    }
    return Strategy(
        call_leg=OptionLeg(
            option_type=OptionType.CALL,
            premium=Decimal("180"),
            **common,
        ),
        put_leg=OptionLeg(
            option_type=OptionType.PUT,
            premium=Decimal("160"),
            **common,
        ),
    )


def calculate(strategy, **overrides):
    inputs = {
        "spot_price": Decimal("24300"),
        "expiry_spot": Decimal("25500"),
        "fees": Decimal("0"),
        "slippage": Decimal("0"),
    }
    inputs.update(overrides)
    return calculate_long_straddle(strategy, **inputs)


def test_mandatory_acceptance_calculation(acceptance_strategy):
    result = calculate(acceptance_strategy)

    assert result.engine_version == "1.0.0"
    assert result.combined_premium == Decimal("340")
    assert result.total_cost == Decimal("25500")
    assert result.upper_break_even == Decimal("24640")
    assert result.lower_break_even == Decimal("23960")
    assert result.maximum_loss == Decimal("25500")
    assert result.required_move_points == Decimal("340")
    assert result.required_move_percent == Decimal("1.399176954732510288065843621")
    assert result.required_move_percent.quantize(Decimal("0.0000000001")) == Decimal("1.3991769547")
    assert result.call_payoff == Decimal("90000")
    assert result.put_payoff == Decimal("0")
    assert result.net_pnl == Decimal("64500")


def test_put_payoff_and_call_payoff_below_strike(acceptance_strategy):
    result = calculate(acceptance_strategy, expiry_spot=Decimal("23500"))

    assert result.call_payoff == Decimal("0")
    assert result.put_payoff == Decimal("60000")
    assert result.net_pnl == Decimal("34500")


def test_at_the_money_expiry_has_zero_intrinsic_payoff(acceptance_strategy):
    result = calculate(acceptance_strategy, expiry_spot=Decimal("24300"))

    assert result.call_payoff == Decimal("0")
    assert result.put_payoff == Decimal("0")
    assert result.net_pnl == Decimal("-25500")


def test_quantity_scaling(acceptance_strategy):
    doubled = Strategy(
        call_leg=replace(acceptance_strategy.call_leg, quantity=150),
        put_leg=replace(acceptance_strategy.put_leg, quantity=150),
    )

    base_result = calculate(acceptance_strategy)
    doubled_result = calculate(doubled)

    assert doubled_result.total_cost == base_result.total_cost * 2
    assert doubled_result.call_payoff == base_result.call_payoff * 2
    assert doubled_result.net_pnl == base_result.net_pnl * 2
    assert doubled_result.combined_premium == base_result.combined_premium
    assert doubled_result.required_move_points == base_result.required_move_points


def test_fees_reduce_pnl_and_increase_maximum_loss(acceptance_strategy):
    result = calculate(acceptance_strategy, fees=Decimal("125.50"))

    assert result.net_pnl == Decimal("64374.50")
    assert result.maximum_loss == Decimal("25625.50")


def test_slippage_reduces_pnl_and_increases_maximum_loss(acceptance_strategy):
    result = calculate(acceptance_strategy, slippage=Decimal("75.25"))

    assert result.net_pnl == Decimal("64424.75")
    assert result.maximum_loss == Decimal("25575.25")


def test_zero_fees_and_slippage_leave_base_values(acceptance_strategy):
    implicit = calculate_long_straddle(
        acceptance_strategy,
        spot_price=Decimal("24300"),
        expiry_spot=Decimal("25500"),
    )
    explicit = calculate(acceptance_strategy)

    assert implicit == explicit
    assert explicit.maximum_loss == explicit.total_cost


def test_identical_inputs_produce_identical_results(acceptance_strategy):
    assert calculate(acceptance_strategy) == calculate(acceptance_strategy)


def test_all_calculated_numeric_values_remain_decimal(acceptance_strategy):
    result = calculate(acceptance_strategy)
    numeric_fields = [field.name for field in fields(result) if field.name != "engine_version"]

    assert all(isinstance(getattr(result, name), Decimal) for name in numeric_fields)


def test_binary_float_money_is_rejected(acceptance_strategy):
    with pytest.raises(TypeError, match="spot_price must be a Decimal"):
        calculate_long_straddle(
            acceptance_strategy,
            spot_price=24300.0,
            expiry_spot=Decimal("25500"),
        )


def test_engine_version_comes_from_central_constant(acceptance_strategy):
    assert calculate(acceptance_strategy).engine_version == STRADDLE_ENGINE_VERSION


@pytest.mark.parametrize(
    "changed_leg, message",
    [
        ({"underlying": "BANKNIFTY"}, "same underlying"),
        ({"strike": Decimal("24400")}, "same strike"),
        ({"expiry": date(2026, 9, 3)}, "same expiry"),
        ({"quantity": 50}, "same quantity"),
    ],
)
def test_minimum_safe_straddle_shape_is_enforced(
    acceptance_strategy, changed_leg, message
):
    invalid = replace(
        acceptance_strategy,
        put_leg=replace(acceptance_strategy.put_leg, **changed_leg),
    )

    with pytest.raises(ValueError, match=message):
        calculate(invalid)
