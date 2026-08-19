"""Pure provider-neutral valuation of open paper straddle positions."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from enum import Enum

from app.straddle.paper_trading import (
    PaperPosition,
    PaperPositionStatus,
    TradeEvent,
    TradeEventType,
)
from app.straddle.rules import (
    DATA_001,
    DATA_002,
    DATA_004,
    MarketDataSnapshot,
    PositionAction,
    PositionActionInputs,
    PositionStatus,
    RuleResult,
    RulesConfig,
    evaluate_market_data,
    evaluate_position_action,
)

ZERO = Decimal("0")
MONITORING_DECIMAL_PRECISION = 28


class PositionDataStatus(str, Enum):
    FRESH = "FRESH"
    WARNING_STALE = "WARNING_STALE"
    LAST_KNOWN_GOOD = "LAST_KNOWN_GOOD"


@dataclass(frozen=True, slots=True)
class PositionMarketSnapshot:
    call_price: Decimal | None
    put_price: Decimal | None
    spot_price: Decimal | None
    provider_timestamp: datetime | None
    received_at: datetime
    call_delta: Decimal | None = None
    put_delta: Decimal | None = None
    call_gamma: Decimal | None = None
    put_gamma: Decimal | None = None
    call_theta: Decimal | None = None
    put_theta: Decimal | None = None
    call_vega: Decimal | None = None
    put_vega: Decimal | None = None
    implied_volatility: Decimal | None = None

    def __post_init__(self):
        for name in (
            "call_price",
            "put_price",
            "spot_price",
            "call_delta",
            "put_delta",
            "call_gamma",
            "put_gamma",
            "call_theta",
            "put_theta",
            "call_vega",
            "put_vega",
            "implied_volatility",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal or None")


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    position_id: str
    captured_at: datetime
    provider_timestamp: datetime | None
    received_at: datetime
    call_price: Decimal
    put_price: Decimal
    call_current_value: Decimal
    put_current_value: Decimal
    combined_value: Decimal
    unrealised_pnl: Decimal
    realised_pnl: Decimal
    total_pnl: Decimal
    remaining_call_quantity: int
    remaining_put_quantity: int
    spot_price: Decimal
    upper_break_even: Decimal
    lower_break_even: Decimal
    distance_to_upper_break_even: Decimal
    distance_to_lower_break_even: Decimal
    net_delta: Decimal | None
    net_gamma: Decimal | None
    net_theta: Decimal | None
    net_vega: Decimal | None
    implied_volatility: Decimal | None
    data_status: PositionDataStatus
    rule_results: tuple[RuleResult, ...]
    used_last_known_good: bool


@dataclass(frozen=True, slots=True)
class PositionMonitoringResult:
    successful: bool
    snapshot: PositionSnapshot | None
    rule_results: tuple[RuleResult, ...]
    events: tuple[TradeEvent, ...]


def _result_ids(results: tuple[RuleResult, ...]) -> set[str]:
    return {result.rule_id for result in results}


def _greeks_available(snapshot: PositionMarketSnapshot) -> bool:
    return all(
        value is not None
        for value in (
            snapshot.call_delta,
            snapshot.put_delta,
            snapshot.call_gamma,
            snapshot.put_gamma,
            snapshot.call_theta,
            snapshot.put_theta,
            snapshot.call_vega,
            snapshot.put_vega,
        )
    )


def _aggregate(
    call_value: Decimal | None,
    put_value: Decimal | None,
    position: PaperPosition,
) -> Decimal | None:
    if call_value is None or put_value is None:
        return None
    with localcontext() as context:
        context.prec = MONITORING_DECIMAL_PRECISION
        return (
            call_value * position.remaining_call_quantity
            + put_value * position.remaining_put_quantity
        )


def _snapshot_age(snapshot: PositionMarketSnapshot, captured_at: datetime):
    return captured_at - (snapshot.provider_timestamp or snapshot.received_at)


def _fallback_matches_position(
    fallback: PositionSnapshot,
    position: PaperPosition,
) -> bool:
    return (
        fallback.position_id == position.position_id
        and fallback.remaining_call_quantity == position.remaining_call_quantity
        and fallback.remaining_put_quantity == position.remaining_put_quantity
    )


def _event(
    position_id: str,
    captured_at: datetime,
    status: PositionDataStatus,
    used_last_known_good: bool,
) -> TradeEvent:
    return TradeEvent(
        event_type=TradeEventType.MARKET_SNAPSHOT_APPLIED,
        occurred_at=captured_at,
        payload=(
            ("position_id", position_id),
            ("data_status", status.value),
            ("used_last_known_good", used_last_known_good),
        ),
    )


def monitor_paper_position(
    position: PaperPosition,
    market_snapshot: PositionMarketSnapshot,
    *,
    captured_at: datetime,
    rules_config: RulesConfig,
    last_known_good: PositionSnapshot | None = None,
) -> PositionMonitoringResult:
    """Value remaining units, or explicitly preserve a supplied good snapshot."""
    if position.status is PaperPositionStatus.CLOSED:
        closed_rules = evaluate_position_action(
            PositionActionInputs(
                action=PositionAction.MUTATE,
                status=PositionStatus.CLOSED,
            ),
            config=rules_config,
        )
        return PositionMonitoringResult(False, None, closed_rules, ())

    market_rules = evaluate_market_data(
        MarketDataSnapshot(
            spot_price=market_snapshot.spot_price,
            call_premium=market_snapshot.call_price,
            put_premium=market_snapshot.put_price,
            provider_timestamp=market_snapshot.provider_timestamp,
            received_at=market_snapshot.received_at,
            greeks_available=_greeks_available(market_snapshot),
        ),
        evaluation_time=captured_at,
        config=rules_config,
    )
    position_rules = evaluate_position_action(
        PositionActionInputs(
            action=PositionAction.REFRESH,
            status=PositionStatus.OPEN,
            snapshot_age=_snapshot_age(market_snapshot, captured_at),
        ),
        config=rules_config,
    )
    rule_results = (*market_rules, *position_rules)
    result_ids = _result_ids(rule_results)
    unusable = DATA_001 in result_ids or DATA_004 in result_ids

    if unusable:
        if last_known_good is None or not _fallback_matches_position(
            last_known_good, position
        ):
            return PositionMonitoringResult(False, None, rule_results, ())
        with localcontext() as context:
            context.prec = MONITORING_DECIMAL_PRECISION
            fallback_total_pnl = (
                position.realised_pnl + last_known_good.unrealised_pnl
            )
        fallback = replace(
            last_known_good,
            captured_at=captured_at,
            realised_pnl=position.realised_pnl,
            total_pnl=fallback_total_pnl,
            data_status=PositionDataStatus.LAST_KNOWN_GOOD,
            rule_results=rule_results,
            used_last_known_good=True,
        )
        event = _event(
            position.position_id,
            captured_at,
            PositionDataStatus.LAST_KNOWN_GOOD,
            True,
        )
        return PositionMonitoringResult(True, fallback, rule_results, (event,))

    call_price = market_snapshot.call_price
    put_price = market_snapshot.put_price
    spot_price = market_snapshot.spot_price
    with localcontext() as context:
        context.prec = MONITORING_DECIMAL_PRECISION
        call_current_value = call_price * position.remaining_call_quantity
        put_current_value = put_price * position.remaining_put_quantity
        combined_value = call_current_value + put_current_value
        unrealised_call = (
            call_price - position.entry_call_premium
        ) * position.remaining_call_quantity
        unrealised_put = (
            put_price - position.entry_put_premium
        ) * position.remaining_put_quantity
        unrealised_pnl = unrealised_call + unrealised_put
        total_pnl = position.realised_pnl + unrealised_pnl
        combined_entry_premium = (
            position.entry_call_premium + position.entry_put_premium
        )
        upper_break_even = position.strike + combined_entry_premium
        lower_break_even = position.strike - combined_entry_premium
        distance_to_upper = upper_break_even - spot_price
        distance_to_lower = spot_price - lower_break_even

    data_status = (
        PositionDataStatus.WARNING_STALE
        if DATA_002 in result_ids
        else PositionDataStatus.FRESH
    )
    snapshot = PositionSnapshot(
        position_id=position.position_id,
        captured_at=captured_at,
        provider_timestamp=market_snapshot.provider_timestamp,
        received_at=market_snapshot.received_at,
        call_price=call_price,
        put_price=put_price,
        call_current_value=call_current_value,
        put_current_value=put_current_value,
        combined_value=combined_value,
        unrealised_pnl=unrealised_pnl,
        realised_pnl=position.realised_pnl,
        total_pnl=total_pnl,
        remaining_call_quantity=position.remaining_call_quantity,
        remaining_put_quantity=position.remaining_put_quantity,
        spot_price=spot_price,
        upper_break_even=upper_break_even,
        lower_break_even=lower_break_even,
        distance_to_upper_break_even=distance_to_upper,
        distance_to_lower_break_even=distance_to_lower,
        net_delta=_aggregate(
            market_snapshot.call_delta, market_snapshot.put_delta, position
        ),
        net_gamma=_aggregate(
            market_snapshot.call_gamma, market_snapshot.put_gamma, position
        ),
        net_theta=_aggregate(
            market_snapshot.call_theta, market_snapshot.put_theta, position
        ),
        net_vega=_aggregate(
            market_snapshot.call_vega, market_snapshot.put_vega, position
        ),
        implied_volatility=market_snapshot.implied_volatility,
        data_status=data_status,
        rule_results=rule_results,
        used_last_known_good=False,
    )
    event = _event(position.position_id, captured_at, data_status, False)
    return PositionMonitoringResult(True, snapshot, rule_results, (event,))
