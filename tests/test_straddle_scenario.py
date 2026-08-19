from dataclasses import fields, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.straddle.domain import OptionLeg, OptionType, PositionSide, Strategy
from app.straddle.scenario import (
    ScenarioConfig,
    ScenarioLabel,
    build_scenario_presets,
    simulate_long_straddle,
)


@pytest.fixture
def strategy():
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


def simulate(strategy, prices, **overrides):
    inputs = {
        "spot_price": Decimal("24300"),
        "scenario_prices": tuple(Decimal(str(price)) for price in prices),
        "fees": Decimal("0"),
        "slippage": Decimal("0"),
    }
    inputs.update(overrides)
    return simulate_long_straddle(strategy, **inputs)


def points_by_price(simulation):
    return {point.underlying_price: point for point in simulation.points}


def test_mandatory_acceptance_example(strategy):
    simulation = simulate(strategy, (23000, 23960, 24300, 24640, 25500))
    points = points_by_price(simulation)

    assert points[Decimal("23000")].call_payoff == Decimal("0")
    assert points[Decimal("23000")].put_payoff == Decimal("97500")
    assert points[Decimal("23000")].net_pnl == Decimal("72000")
    assert points[Decimal("23960")].net_pnl == Decimal("0")
    assert points[Decimal("24300")].net_pnl == Decimal("-25500")
    assert points[Decimal("24640")].net_pnl == Decimal("0")
    assert points[Decimal("25500")].call_payoff == Decimal("90000")
    assert points[Decimal("25500")].put_payoff == Decimal("0")
    assert points[Decimal("25500")].net_pnl == Decimal("64500")


def test_simulation_summary_matches_phase_1_values(strategy):
    simulation = simulate(strategy, (24300,))

    assert simulation.engine_version == "1.0.0"
    assert simulation.strike == Decimal("24300")
    assert simulation.combined_premium == Decimal("340")
    assert simulation.total_cost == Decimal("25500")
    assert simulation.fees == Decimal("0")
    assert simulation.slippage == Decimal("0")


def test_break_even_invariants(strategy):
    simulation = simulate(strategy, (23960, 24640))

    assert tuple(point.net_pnl for point in simulation.points) == (
        Decimal("0"),
        Decimal("0"),
    )


def test_strike_is_maximum_loss_for_sampled_expiry_surface(strategy):
    simulation = simulate(strategy, (23000, 23960, 24300, 24640, 25500))
    worst = min(simulation.points, key=lambda point: point.net_pnl)

    assert worst.underlying_price == Decimal("24300")
    assert worst.net_pnl == -simulation.total_cost


def test_equal_moves_are_symmetric_around_strike(strategy):
    simulation = simulate(strategy, (24000, 24600))
    lower, upper = simulation.points

    assert lower.put_payoff == upper.call_payoff == Decimal("22500")
    assert lower.gross_pnl == upper.gross_pnl
    assert lower.net_pnl == upper.net_pnl


def test_payoff_is_positive_beyond_break_evens(strategy):
    simulation = simulate(strategy, (23959, 24641))

    assert all(point.net_pnl == Decimal("75") for point in simulation.points)


def test_quantity_scaling_is_linear(strategy):
    doubled = Strategy(
        call_leg=replace(strategy.call_leg, quantity=150),
        put_leg=replace(strategy.put_leg, quantity=150),
    )
    base = simulate(strategy, (23000, 24300, 25500))
    scaled = simulate(doubled, (23000, 24300, 25500))

    assert scaled.total_cost == base.total_cost * 2
    for base_point, scaled_point in zip(base.points, scaled.points):
        assert scaled_point.call_payoff == base_point.call_payoff * 2
        assert scaled_point.put_payoff == base_point.put_payoff * 2
        assert scaled_point.gross_pnl == base_point.gross_pnl * 2
        assert scaled_point.net_pnl == base_point.net_pnl * 2


def test_fees_and_slippage_reduce_every_scenario_equally(strategy):
    base = simulate(strategy, (23000, 24300, 25500))
    charged = simulate(
        strategy,
        (23000, 24300, 25500),
        fees=Decimal("125.50"),
        slippage=Decimal("74.50"),
    )

    for base_point, charged_point in zip(base.points, charged.points):
        assert charged_point.gross_pnl == base_point.gross_pnl
        assert charged_point.net_pnl == base_point.net_pnl - Decimal("200")


def test_identical_inputs_produce_identical_simulations(strategy):
    prices = (Decimal("25500"), Decimal("23000"), Decimal("24300"))
    first = simulate_long_straddle(strategy, spot_price=Decimal("24300"), scenario_prices=prices)
    second = simulate_long_straddle(strategy, spot_price=Decimal("24300"), scenario_prices=prices)

    assert first == second


def test_output_order_and_duplicate_prices_are_preserved(strategy):
    prices = (Decimal("25500"), Decimal("23000"), Decimal("25500"), Decimal("24300"))
    simulation = simulate_long_straddle(
        strategy,
        spot_price=Decimal("24300"),
        scenario_prices=prices,
    )

    assert tuple(point.underlying_price for point in simulation.points) == prices
    assert simulation.points[0] == simulation.points[2]


def test_all_financial_outputs_are_decimal(strategy):
    simulation = simulate(strategy, (23000, 24300, 25500))

    for name in ("strike", "combined_premium", "total_cost", "fees", "slippage"):
        assert isinstance(getattr(simulation, name), Decimal)
    for point in simulation.points:
        assert all(isinstance(getattr(point, field.name), Decimal) for field in fields(point))


def test_presets_use_explicit_percentages_and_stable_order():
    presets = build_scenario_presets(
        Decimal("100"),
        config=ScenarioConfig(
            large_move_percent=Decimal("10"),
            small_move_percent=Decimal("5"),
        ),
    )

    assert tuple(preset.label for preset in presets) == (
        ScenarioLabel.LARGE_FALL,
        ScenarioLabel.SMALL_FALL,
        ScenarioLabel.FLAT,
        ScenarioLabel.SMALL_RALLY,
        ScenarioLabel.LARGE_RALLY,
    )
    assert tuple(preset.underlying_price for preset in presets) == (
        Decimal("90.0"),
        Decimal("95.00"),
        Decimal("100"),
        Decimal("105.00"),
        Decimal("110.0"),
    )


def test_presets_do_not_round_generated_prices():
    presets = build_scenario_presets(
        Decimal("24342"),
        config=ScenarioConfig(
            large_move_percent=Decimal("7.5"),
            small_move_percent=Decimal("2.5"),
        ),
    )

    assert presets[0].underlying_price == Decimal("22516.350")
    assert presets[1].underlying_price == Decimal("23733.450")
    assert presets[3].underlying_price == Decimal("24950.550")
    assert presets[4].underlying_price == Decimal("26167.650")


def test_different_explicit_configs_produce_different_presets():
    narrow = ScenarioConfig(Decimal("5"), Decimal("2"))
    wide = ScenarioConfig(Decimal("10"), Decimal("4"))

    assert build_scenario_presets(Decimal("100"), config=narrow) != build_scenario_presets(
        Decimal("100"), config=wide
    )


@pytest.mark.parametrize("bad_price", [1.0, "24300", None])
def test_binary_float_and_non_decimal_scenario_prices_are_rejected(strategy, bad_price):
    with pytest.raises(TypeError, match="scenario prices must be Decimals"):
        simulate_long_straddle(
            strategy,
            spot_price=Decimal("24300"),
            scenario_prices=(bad_price,),
        )


def test_binary_float_spot_is_rejected(strategy):
    with pytest.raises(TypeError, match="spot_price must be a Decimal"):
        simulate_long_straddle(strategy, spot_price=24300.0, scenario_prices=(Decimal("24300"),))


def test_negative_scenario_price_is_rejected(strategy):
    with pytest.raises(ValueError, match="non-negative"):
        simulate_long_straddle(
            strategy,
            spot_price=Decimal("24300"),
            scenario_prices=(Decimal("-1"),),
        )


def test_empty_scenario_list_is_rejected(strategy):
    with pytest.raises(ValueError, match="must not be empty"):
        simulate_long_straddle(
            strategy,
            spot_price=Decimal("24300"),
            scenario_prices=(),
        )


def test_scenario_config_rejects_binary_floats():
    with pytest.raises(TypeError, match="large_move_percent must be a Decimal"):
        ScenarioConfig(large_move_percent=10.0, small_move_percent=Decimal("5"))


def test_scenario_config_rejects_invalid_ordering():
    with pytest.raises(ValueError, match="small_move_percent"):
        ScenarioConfig(large_move_percent=Decimal("5"), small_move_percent=Decimal("10"))


def test_scenario_module_is_expiry_payoff_only_and_dependency_free():
    source = Path(__file__).resolve().parents[1].joinpath("app/straddle/scenario.py").read_text().lower()
    forbidden = (
        "fastapi",
        "streamlit",
        "sqlalchemy",
        "market_feed",
        "scheduler",
        "notifier",
        "dhan",
        "yfinance",
        "httpx",
        "llm",
        "theta",
        "volatility",
        "black-scholes",
        "monte carlo",
        "probability",
        "datetime.now",
        "random",
    )
    assert all(term not in source for term in forbidden)
