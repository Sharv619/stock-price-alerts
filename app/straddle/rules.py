"""Deterministic validation and risk rules for StraddleLab Phase 2."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from app.straddle.domain import OptionLeg, PositionSide, Strategy


class RuleSeverity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class PositionAction(str, Enum):
    MUTATE = "MUTATE"
    EXIT = "EXIT"
    ONE_LEG_EXIT = "ONE_LEG_EXIT"
    REFRESH = "REFRESH"
    NEW_SHORT_STRADDLE = "NEW_SHORT_STRADDLE"


@dataclass(frozen=True, slots=True)
class RuleDefinition:
    rule_id: str
    default_severity: RuleSeverity
    blocking: bool
    requires_acknowledgement: bool
    description: str


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    severity: RuleSeverity
    blocking: bool
    requires_acknowledgement: bool
    message: str
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RulesConfig:
    warning_stale_after: timedelta
    hard_stale_after: timedelta
    high_iv_ratio: Decimal
    high_theta_share: Decimal
    required_move_threshold: Decimal
    position_stale_after: timedelta

    def __post_init__(self):
        if self.warning_stale_after < timedelta(0):
            raise ValueError("warning_stale_after must be non-negative")
        if self.hard_stale_after < self.warning_stale_after:
            raise ValueError("hard_stale_after must be >= warning_stale_after")
        if self.position_stale_after < timedelta(0):
            raise ValueError("position_stale_after must be non-negative")
        for name in ("high_iv_ratio", "high_theta_share", "required_move_threshold"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal")
            if value < Decimal("0"):
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class StrategyValidationInput:
    call_leg: OptionLeg | None
    put_leg: OptionLeg | None


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    spot_price: Decimal | None
    call_premium: Decimal | None
    put_premium: Decimal | None
    provider_timestamp: datetime | None
    received_at: datetime
    greeks_available: bool | None = None


@dataclass(frozen=True, slots=True)
class RiskInputs:
    current_iv: Decimal | None = None
    historical_iv_baseline: Decimal | None = None
    estimated_daily_theta: Decimal | None = None
    combined_premium: Decimal | None = None
    required_move_percent: Decimal | None = None
    event_tag: str | None = None
    event_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class PositionActionInputs:
    action: PositionAction
    status: PositionStatus
    open_quantity: int | None = None
    requested_quantity: int | None = None
    snapshot_age: timedelta | None = None


STR_001 = "STR-001"
STR_002 = "STR-002"
STR_003 = "STR-003"
STR_004 = "STR-004"
STR_005 = "STR-005"
STR_006 = "STR-006"
STR_007 = "STR-007"
STR_008 = "STR-008"
DATA_001 = "DATA-001"
DATA_002 = "DATA-002"
DATA_003 = "DATA-003"
DATA_004 = "DATA-004"
DATA_005 = "DATA-005"
RISK_IV_001 = "RISK-IV-001"
RISK_IV_002 = "RISK-IV-002"
RISK_THETA_001 = "RISK-THETA-001"
RISK_MOVE_001 = "RISK-MOVE-001"
POS_001 = "POS-001"
POS_002 = "POS-002"
POS_003 = "POS-003"
POS_004 = "POS-004"
POS_005 = "POS-005"


_DEFINITIONS = (
    RuleDefinition(STR_001, RuleSeverity.ERROR, True, False, "Call and put underlyings differ."),
    RuleDefinition(STR_002, RuleSeverity.ERROR, True, False, "Call and put strikes differ."),
    RuleDefinition(STR_003, RuleSeverity.ERROR, True, False, "Call and put expiries differ."),
    RuleDefinition(STR_004, RuleSeverity.ERROR, True, False, "Call and put quantities differ."),
    RuleDefinition(STR_005, RuleSeverity.ERROR, True, False, "A long-straddle leg action is not BUY."),
    RuleDefinition(STR_006, RuleSeverity.ERROR, True, False, "An option premium is negative."),
    RuleDefinition(STR_007, RuleSeverity.ERROR, True, False, "An option expiry is in the past."),
    RuleDefinition(STR_008, RuleSeverity.ERROR, True, False, "A required call or put leg is missing."),
    RuleDefinition(DATA_001, RuleSeverity.ERROR, True, False, "Market-data snapshot exceeds the hard freshness limit."),
    RuleDefinition(DATA_002, RuleSeverity.WARNING, False, False, "Market-data snapshot exceeds the warning freshness limit."),
    RuleDefinition(DATA_003, RuleSeverity.WARNING, False, False, "Provider timestamp is missing."),
    RuleDefinition(DATA_004, RuleSeverity.ERROR, True, False, "Spot or a required option premium is missing."),
    RuleDefinition(DATA_005, RuleSeverity.INFO, False, False, "Option Greeks are unavailable."),
    RuleDefinition(RISK_IV_001, RuleSeverity.WARNING, False, False, "Current IV is materially above its configured historical baseline."),
    RuleDefinition(RISK_IV_002, RuleSeverity.WARNING, False, True, "A tagged event has occurred and IV contraction may follow."),
    RuleDefinition(RISK_THETA_001, RuleSeverity.WARNING, False, False, "Estimated daily theta exceeds the configured share of premium."),
    RuleDefinition(RISK_MOVE_001, RuleSeverity.INFO, False, False, "Required move exceeds the configured threshold."),
    RuleDefinition(POS_001, RuleSeverity.WARNING, False, True, "A one-leg exit was requested."),
    RuleDefinition(POS_002, RuleSeverity.WARNING, False, False, "Position refresh is using stale data."),
    RuleDefinition(POS_003, RuleSeverity.ERROR, True, False, "A mutation was requested for a closed position."),
    RuleDefinition(POS_004, RuleSeverity.ERROR, True, False, "Requested exit quantity exceeds open quantity."),
    RuleDefinition(POS_005, RuleSeverity.ERROR, True, False, "New short-straddle execution is analysis-only in the MVP."),
)

RULE_REGISTRY: Mapping[str, RuleDefinition] = MappingProxyType(
    {definition.rule_id: definition for definition in _DEFINITIONS}
)

RULES_DECIMAL_PRECISION = 28


def _result(rule_id: str, **evidence: object) -> RuleResult:
    definition = RULE_REGISTRY[rule_id]
    return RuleResult(
        rule_id=definition.rule_id,
        severity=definition.default_severity,
        blocking=definition.blocking,
        requires_acknowledgement=definition.requires_acknowledgement,
        message=definition.description,
        evidence=MappingProxyType(dict(evidence)),
    )


def _optional_decimal(name: str, value: Decimal | None) -> None:
    if value is not None and not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal or None")


def validate_strategy(
    strategy: Strategy | StrategyValidationInput,
    *,
    evaluation_date: date,
) -> tuple[RuleResult, ...]:
    """Evaluate structural long-straddle rules in stable registry order."""
    results: list[RuleResult] = []
    call: OptionLeg | None = strategy.call_leg
    put: OptionLeg | None = strategy.put_leg

    missing = tuple(name for name, leg in (("call", call), ("put", put)) if leg is None)
    if missing:
        results.append(_result(STR_008, missing_legs=missing))

    if call is not None and put is not None:
        if call.underlying != put.underlying:
            results.append(_result(STR_001, call_underlying=call.underlying, put_underlying=put.underlying))
        if call.strike != put.strike:
            results.append(_result(STR_002, call_strike=call.strike, put_strike=put.strike))
        if call.expiry != put.expiry:
            results.append(_result(STR_003, call_expiry=call.expiry, put_expiry=put.expiry))
        if call.quantity != put.quantity:
            results.append(_result(STR_004, call_quantity=call.quantity, put_quantity=put.quantity))

    legs = tuple((name, leg) for name, leg in (("call", call), ("put", put)) if leg is not None)
    non_buy = tuple(name for name, leg in legs if leg.side is not PositionSide.BUY)
    if non_buy:
        results.append(_result(STR_005, non_buy_legs=non_buy))
    negative = tuple(name for name, leg in legs if leg.premium < Decimal("0"))
    if negative:
        results.append(_result(STR_006, negative_premium_legs=negative))
    expired = tuple(name for name, leg in legs if leg.expiry < evaluation_date)
    if expired:
        results.append(_result(STR_007, expired_legs=expired, evaluation_date=evaluation_date))

    return tuple(results)


def evaluate_market_data(
    snapshot: MarketDataSnapshot,
    *,
    evaluation_time: datetime,
    config: RulesConfig,
) -> tuple[RuleResult, ...]:
    """Evaluate supplied snapshot fields without fetching market data."""
    results: list[RuleResult] = []
    snapshot_time = snapshot.provider_timestamp or snapshot.received_at
    age = evaluation_time - snapshot_time

    if age > config.hard_stale_after:
        results.append(_result(DATA_001, snapshot_age=age, hard_stale_after=config.hard_stale_after, timestamp_source="provider" if snapshot.provider_timestamp else "received"))
    elif age > config.warning_stale_after:
        results.append(_result(DATA_002, snapshot_age=age, warning_stale_after=config.warning_stale_after, timestamp_source="provider" if snapshot.provider_timestamp else "received"))
    if snapshot.provider_timestamp is None:
        results.append(_result(DATA_003, received_at=snapshot.received_at))

    missing = tuple(
        name
        for name, value in (
            ("spot_price", snapshot.spot_price),
            ("call_premium", snapshot.call_premium),
            ("put_premium", snapshot.put_premium),
        )
        if value is None
    )
    if missing:
        results.append(_result(DATA_004, missing_fields=missing))
    if not snapshot.greeks_available:
        results.append(_result(DATA_005, greeks_available=snapshot.greeks_available))
    return tuple(results)


def evaluate_risk(
    inputs: RiskInputs,
    *,
    evaluation_time: datetime,
    config: RulesConfig,
) -> tuple[RuleResult, ...]:
    """Compare explicit IV, event, theta, and move inputs to explicit thresholds."""
    for name in (
        "current_iv",
        "historical_iv_baseline",
        "estimated_daily_theta",
        "combined_premium",
        "required_move_percent",
    ):
        _optional_decimal(name, getattr(inputs, name))

    results: list[RuleResult] = []
    if (
        inputs.current_iv is not None
        and inputs.historical_iv_baseline is not None
        and inputs.historical_iv_baseline > Decimal("0")
    ):
        with localcontext() as context:
            context.prec = RULES_DECIMAL_PRECISION
            iv_ratio = inputs.current_iv / inputs.historical_iv_baseline
        if iv_ratio > config.high_iv_ratio:
            results.append(_result(RISK_IV_001, current_iv=inputs.current_iv, historical_iv_baseline=inputs.historical_iv_baseline, iv_ratio=iv_ratio, threshold=config.high_iv_ratio))

    if inputs.event_tag and inputs.event_time is not None and inputs.event_time <= evaluation_time:
        results.append(_result(RISK_IV_002, event_tag=inputs.event_tag, event_time=inputs.event_time, evaluation_time=evaluation_time))

    if (
        inputs.estimated_daily_theta is not None
        and inputs.combined_premium is not None
        and inputs.combined_premium > Decimal("0")
    ):
        with localcontext() as context:
            context.prec = RULES_DECIMAL_PRECISION
            theta_share = abs(inputs.estimated_daily_theta) / inputs.combined_premium
        if theta_share > config.high_theta_share:
            results.append(_result(RISK_THETA_001, estimated_daily_theta=inputs.estimated_daily_theta, combined_premium=inputs.combined_premium, theta_share=theta_share, threshold=config.high_theta_share))

    if inputs.required_move_percent is not None and inputs.required_move_percent > config.required_move_threshold:
        results.append(_result(RISK_MOVE_001, required_move_percent=inputs.required_move_percent, threshold=config.required_move_threshold))
    return tuple(results)


def evaluate_position_action(
    inputs: PositionActionInputs,
    *,
    config: RulesConfig,
) -> tuple[RuleResult, ...]:
    """Evaluate an explicit position action without mutating position state."""
    results: list[RuleResult] = []
    exit_actions = (PositionAction.EXIT, PositionAction.ONE_LEG_EXIT)
    mutation_actions = (*exit_actions, PositionAction.MUTATE, PositionAction.NEW_SHORT_STRADDLE)

    if inputs.action is PositionAction.ONE_LEG_EXIT:
        results.append(_result(POS_001, action=inputs.action.value))
    if (
        inputs.action is PositionAction.REFRESH
        and inputs.snapshot_age is not None
        and inputs.snapshot_age > config.position_stale_after
    ):
        results.append(_result(POS_002, snapshot_age=inputs.snapshot_age, threshold=config.position_stale_after))
    if inputs.status is PositionStatus.CLOSED and inputs.action in mutation_actions:
        results.append(_result(POS_003, status=inputs.status.value, action=inputs.action.value))
    if (
        inputs.action in exit_actions
        and inputs.requested_quantity is not None
        and inputs.open_quantity is not None
        and inputs.requested_quantity > inputs.open_quantity
    ):
        results.append(_result(POS_004, requested_quantity=inputs.requested_quantity, open_quantity=inputs.open_quantity))
    if inputs.action is PositionAction.NEW_SHORT_STRADDLE:
        results.append(_result(POS_005, action=inputs.action.value, execution_allowed=False))
    return tuple(results)
