"""Pure deterministic expiry-payoff scenarios for long straddles."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from typing import Iterable

from app.straddle.domain import Strategy
from app.straddle.engine import calculate_long_straddle
from app.straddle.version import STRADDLE_ENGINE_VERSION

ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


class ScenarioLabel(str, Enum):
    LARGE_FALL = "large_fall"
    SMALL_FALL = "small_fall"
    FLAT = "flat"
    SMALL_RALLY = "small_rally"
    LARGE_RALLY = "large_rally"


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    large_move_percent: Decimal
    small_move_percent: Decimal

    def __post_init__(self):
        for name in ("large_move_percent", "small_move_percent"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be a Decimal")
        if self.small_move_percent < ZERO or self.large_move_percent < ZERO:
            raise ValueError("scenario move percentages must be non-negative")
        if self.small_move_percent > self.large_move_percent:
            raise ValueError("small_move_percent must be <= large_move_percent")
        if self.large_move_percent > ONE_HUNDRED:
            raise ValueError("large_move_percent must be <= 100")


@dataclass(frozen=True, slots=True)
class ScenarioPreset:
    label: ScenarioLabel
    underlying_price: Decimal


@dataclass(frozen=True, slots=True)
class ScenarioPoint:
    underlying_price: Decimal
    call_payoff: Decimal
    put_payoff: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class ScenarioSimulation:
    engine_version: str
    strike: Decimal
    combined_premium: Decimal
    total_cost: Decimal
    points: tuple[ScenarioPoint, ...]
    fees: Decimal
    slippage: Decimal


def build_scenario_presets(
    spot_price: Decimal,
    *,
    config: ScenarioConfig,
) -> tuple[ScenarioPreset, ...]:
    """Build five ordered, unrounded prices from explicit percentage moves."""
    if not isinstance(spot_price, Decimal):
        raise TypeError("spot_price must be a Decimal")
    if spot_price < ZERO:
        raise ValueError("spot_price must be non-negative")

    precision = max(
        28,
        len(spot_price.as_tuple().digits)
        + len(config.large_move_percent.as_tuple().digits)
        + len(config.small_move_percent.as_tuple().digits)
        + 10,
    )
    with localcontext() as context:
        context.prec = precision
        large_move = spot_price * config.large_move_percent / ONE_HUNDRED
        small_move = spot_price * config.small_move_percent / ONE_HUNDRED
        large_fall = spot_price - large_move
        small_fall = spot_price - small_move
        small_rally = spot_price + small_move
        large_rally = spot_price + large_move
    return (
        ScenarioPreset(ScenarioLabel.LARGE_FALL, large_fall),
        ScenarioPreset(ScenarioLabel.SMALL_FALL, small_fall),
        ScenarioPreset(ScenarioLabel.FLAT, spot_price),
        ScenarioPreset(ScenarioLabel.SMALL_RALLY, small_rally),
        ScenarioPreset(ScenarioLabel.LARGE_RALLY, large_rally),
    )


def simulate_long_straddle(
    strategy: Strategy,
    *,
    spot_price: Decimal,
    scenario_prices: Iterable[Decimal],
    fees: Decimal = ZERO,
    slippage: Decimal = ZERO,
) -> ScenarioSimulation:
    """Evaluate the Phase 1 calculation engine at each supplied expiry price."""
    if not isinstance(spot_price, Decimal):
        raise TypeError("spot_price must be a Decimal")
    if not isinstance(fees, Decimal):
        raise TypeError("fees must be a Decimal")
    if not isinstance(slippage, Decimal):
        raise TypeError("slippage must be a Decimal")

    prices = tuple(scenario_prices)
    if not prices:
        raise ValueError("scenario_prices must not be empty")
    for price in prices:
        if not isinstance(price, Decimal):
            raise TypeError("scenario prices must be Decimals")
        if price < ZERO:
            raise ValueError("scenario prices must be non-negative")

    points: list[ScenarioPoint] = []
    first_calculation = None
    for price in prices:
        calculation = calculate_long_straddle(
            strategy,
            spot_price=spot_price,
            expiry_spot=price,
            fees=fees,
            slippage=slippage,
        )
        if first_calculation is None:
            first_calculation = calculation
        with localcontext() as context:
            context.prec = 28
            gross_pnl = (
                calculation.call_payoff
                + calculation.put_payoff
                - calculation.total_cost
            )
        points.append(
            ScenarioPoint(
                underlying_price=price,
                call_payoff=calculation.call_payoff,
                put_payoff=calculation.put_payoff,
                gross_pnl=gross_pnl,
                net_pnl=calculation.net_pnl,
            )
        )

    return ScenarioSimulation(
        engine_version=STRADDLE_ENGINE_VERSION,
        strike=strategy.call_leg.strike,
        combined_premium=first_calculation.combined_premium,
        total_cost=first_calculation.total_cost,
        points=tuple(points),
        fees=fees,
        slippage=slippage,
    )
