"""Immutable domain objects for the Phase 1 long-straddle foundation."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum


class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class PositionSide(str, Enum):
    BUY = "BUY"


@dataclass(frozen=True, slots=True)
class OptionLeg:
    underlying: str
    option_type: OptionType
    side: PositionSide
    strike: Decimal
    expiry: date
    premium: Decimal
    quantity: int


@dataclass(frozen=True, slots=True)
class Strategy:
    call_leg: OptionLeg
    put_leg: OptionLeg


@dataclass(frozen=True, slots=True)
class StrategyCalculation:
    engine_version: str
    combined_premium: Decimal
    total_cost: Decimal
    upper_break_even: Decimal
    lower_break_even: Decimal
    call_payoff: Decimal
    put_payoff: Decimal
    net_pnl: Decimal
    maximum_loss: Decimal
    required_move_points: Decimal
    required_move_percent: Decimal
