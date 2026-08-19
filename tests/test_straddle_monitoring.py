from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.straddle.domain import OptionLeg, OptionType, PositionSide, Strategy
from app.straddle.monitoring import (
    PositionDataStatus,
    PositionMarketSnapshot,
    monitor_paper_position,
)
from app.straddle.paper_trading import (
    PaperPosition,
    PaperPositionStatus,
    TradeEventType,
)
from app.straddle.rules import (
    DATA_001,
    DATA_002,
    DATA_004,
    DATA_005,
    POS_002,
    POS_003,
    RulesConfig,
)

UTC = timezone.utc
EXPIRY = date(2026, 8, 27)
OPENED_AT = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
CAPTURED_AT = datetime(2026, 8, 20, 11, 0, tzinfo=UTC)


@pytest.fixture
def rules_config():
    return RulesConfig(
        warning_stale_after=timedelta(seconds=30),
        hard_stale_after=timedelta(seconds=120),
        high_iv_ratio=Decimal("1.5"),
        high_theta_share=Decimal("0.1"),
        required_move_threshold=Decimal("2"),
        position_stale_after=timedelta(seconds=60),
    )


@pytest.fixture
def position():
    common = {
        "underlying": "NIFTY",
        "side": PositionSide.BUY,
        "strike": Decimal("24300"),
        "expiry": EXPIRY,
        "quantity": 75,
    }
    strategy = Strategy(
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
    return PaperPosition(
        position_id="paper-001",
        strategy=strategy,
        opened_at=OPENED_AT,
        entry_call_premium=Decimal("180"),
        entry_put_premium=Decimal("160"),
        strike=Decimal("24300"),
        expiry=EXPIRY,
        quantity=75,
        fees_at_entry=Decimal("0"),
        slippage_at_entry=Decimal("0"),
        status=PaperPositionStatus.OPEN,
        remaining_call_quantity=75,
        remaining_put_quantity=75,
        realised_call_pnl=Decimal("0"),
        realised_put_pnl=Decimal("0"),
        exit_fees_paid=Decimal("0"),
        exit_slippage_paid=Decimal("0"),
        realised_pnl=Decimal("0"),
        closed_at=None,
        exit_reason=None,
    )


@pytest.fixture
def partial_position(position):
    return replace(
        position,
        status=PaperPositionStatus.PARTIALLY_CLOSED,
        remaining_call_quantity=50,
        remaining_put_quantity=75,
        realised_call_pnl=Decimal("1750"),
        realised_pnl=Decimal("1750"),
    )


@pytest.fixture
def market_snapshot():
    return PositionMarketSnapshot(
        call_price=Decimal("250"),
        put_price=Decimal("100"),
        spot_price=Decimal("24500"),
        provider_timestamp=CAPTURED_AT - timedelta(seconds=5),
        received_at=CAPTURED_AT - timedelta(seconds=2),
    )


def monitor(position, market_snapshot, rules_config, **overrides):
    inputs = {
        "captured_at": CAPTURED_AT,
        "rules_config": rules_config,
    }
    inputs.update(overrides)
    return monitor_paper_position(position, market_snapshot, **inputs)


def result_ids(result):
    return {rule.rule_id for rule in result.rule_results}


def test_mandatory_full_position_acceptance_case(position, market_snapshot, rules_config):
    result = monitor(position, market_snapshot, rules_config)
    snapshot = result.snapshot

    assert result.successful is True
    assert snapshot.call_current_value == Decimal("18750")
    assert snapshot.put_current_value == Decimal("7500")
    assert snapshot.combined_value == Decimal("26250")
    assert snapshot.unrealised_pnl == Decimal("750")
    assert snapshot.realised_pnl == Decimal("0")
    assert snapshot.total_pnl == Decimal("750")
    assert snapshot.upper_break_even == Decimal("24640")
    assert snapshot.lower_break_even == Decimal("23960")
    assert snapshot.distance_to_upper_break_even == Decimal("140")
    assert snapshot.distance_to_lower_break_even == Decimal("540")
    assert snapshot.data_status is PositionDataStatus.FRESH


def test_mandatory_partial_position_acceptance_case(
    partial_position, rules_config
):
    current = PositionMarketSnapshot(
        call_price=Decimal("260"),
        put_price=Decimal("90"),
        spot_price=Decimal("24500"),
        provider_timestamp=CAPTURED_AT - timedelta(seconds=5),
        received_at=CAPTURED_AT - timedelta(seconds=2),
    )
    result = monitor(partial_position, current, rules_config)
    snapshot = result.snapshot

    assert snapshot.remaining_call_quantity == 50
    assert snapshot.remaining_put_quantity == 75
    assert snapshot.call_current_value == Decimal("13000")
    assert snapshot.put_current_value == Decimal("6750")
    assert snapshot.unrealised_pnl == Decimal("-1250")
    assert snapshot.realised_pnl == Decimal("1750")
    assert snapshot.total_pnl == Decimal("500")


def test_fresh_open_position_combined_value(position, market_snapshot, rules_config):
    snapshot = monitor(position, market_snapshot, rules_config).snapshot
    assert snapshot.combined_value == snapshot.call_current_value + snapshot.put_current_value
    assert snapshot.combined_value == Decimal("26250")


def test_unrealised_pnl_uses_remaining_entry_basis(position, market_snapshot, rules_config):
    snapshot = monitor(position, market_snapshot, rules_config).snapshot
    expected = (Decimal("250") - Decimal("180")) * 75 + (
        Decimal("100") - Decimal("160")
    ) * 75
    assert snapshot.unrealised_pnl == expected == Decimal("750")


def test_realised_pnl_is_preserved(partial_position, market_snapshot, rules_config):
    snapshot = monitor(partial_position, market_snapshot, rules_config).snapshot
    assert snapshot.realised_pnl is partial_position.realised_pnl
    assert snapshot.realised_pnl == Decimal("1750")


def test_total_pnl_adds_realised_and_unrealised(
    partial_position, market_snapshot, rules_config
):
    snapshot = monitor(partial_position, market_snapshot, rules_config).snapshot
    assert snapshot.total_pnl == snapshot.realised_pnl + snapshot.unrealised_pnl


def test_break_even_distances_are_signed(position, rules_config):
    outside = PositionMarketSnapshot(
        call_price=Decimal("300"),
        put_price=Decimal("50"),
        spot_price=Decimal("24700"),
        provider_timestamp=CAPTURED_AT,
        received_at=CAPTURED_AT,
    )
    snapshot = monitor(position, outside, rules_config).snapshot

    assert snapshot.distance_to_upper_break_even == Decimal("-60")
    assert snapshot.distance_to_lower_break_even == Decimal("740")


def test_supplied_greeks_are_aggregated_by_remaining_quantity(
    partial_position, market_snapshot, rules_config
):
    supplied = replace(
        market_snapshot,
        call_delta=Decimal("0.6"),
        put_delta=Decimal("-0.4"),
        call_gamma=Decimal("0.01"),
        put_gamma=Decimal("0.02"),
        call_theta=Decimal("-2"),
        put_theta=Decimal("-3"),
        call_vega=Decimal("4"),
        put_vega=Decimal("5"),
        implied_volatility=Decimal("18.5"),
    )
    snapshot = monitor(partial_position, supplied, rules_config).snapshot

    assert snapshot.net_delta == Decimal("0")
    assert snapshot.net_gamma == Decimal("2.00")
    assert snapshot.net_theta == Decimal("-325")
    assert snapshot.net_vega == Decimal("575")
    assert snapshot.implied_volatility == Decimal("18.5")
    assert DATA_005 not in result_ids(
        monitor(partial_position, supplied, rules_config)
    )


def test_missing_greek_does_not_break_valuation(
    position, market_snapshot, rules_config
):
    partial_greeks = replace(
        market_snapshot,
        call_delta=Decimal("0.6"),
        put_delta=None,
    )
    result = monitor(position, partial_greeks, rules_config)

    assert result.successful is True
    assert result.snapshot.net_delta is None
    assert DATA_005 in result_ids(result)


def test_warning_stale_uses_current_prices(position, market_snapshot, rules_config):
    stale = replace(
        market_snapshot,
        provider_timestamp=CAPTURED_AT - timedelta(seconds=45),
    )
    result = monitor(position, stale, rules_config)

    assert result.successful is True
    assert DATA_002 in result_ids(result)
    assert DATA_001 not in result_ids(result)
    assert result.snapshot.call_price == Decimal("250")
    assert result.snapshot.data_status is PositionDataStatus.WARNING_STALE
    assert result.snapshot.used_last_known_good is False


def test_hard_stale_uses_last_known_good(position, market_snapshot, rules_config):
    good = monitor(position, market_snapshot, rules_config).snapshot
    hard_stale = replace(
        market_snapshot,
        call_price=Decimal("999"),
        provider_timestamp=CAPTURED_AT - timedelta(seconds=121),
    )
    result = monitor(
        position,
        hard_stale,
        rules_config,
        last_known_good=good,
    )

    assert result.successful is True
    assert DATA_001 in result_ids(result)
    assert POS_002 in result_ids(result)
    assert result.snapshot.call_price == good.call_price
    assert result.snapshot.combined_value == good.combined_value
    assert result.snapshot.used_last_known_good is True
    assert result.snapshot.data_status is PositionDataStatus.LAST_KNOWN_GOOD


def test_hard_stale_without_fallback_is_unsuccessful(
    position, market_snapshot, rules_config
):
    hard_stale = replace(
        market_snapshot,
        provider_timestamp=CAPTURED_AT - timedelta(seconds=121),
    )
    result = monitor(position, hard_stale, rules_config)

    assert result.successful is False
    assert result.snapshot is None
    assert DATA_001 in result_ids(result)
    assert result.events == ()


def test_missing_required_data_uses_last_known_good(
    position, market_snapshot, rules_config
):
    good = monitor(position, market_snapshot, rules_config).snapshot
    missing = replace(market_snapshot, call_price=None)
    result = monitor(position, missing, rules_config, last_known_good=good)

    assert result.successful is True
    assert DATA_004 in result_ids(result)
    assert result.snapshot.call_price == good.call_price
    assert result.snapshot.used_last_known_good is True


def test_missing_required_data_without_fallback_is_blocked(
    position, market_snapshot, rules_config
):
    result = monitor(position, replace(market_snapshot, spot_price=None), rules_config)

    assert result.successful is False
    assert result.snapshot is None
    assert DATA_004 in result_ids(result)


def test_pos_002_is_emitted_for_stale_refresh(
    position, market_snapshot, rules_config
):
    stale = replace(
        market_snapshot,
        provider_timestamp=CAPTURED_AT - timedelta(seconds=61),
    )
    result = monitor(position, stale, rules_config)
    assert POS_002 in result_ids(result)


def test_closed_position_monitoring_is_blocked_by_pos_003(
    position, market_snapshot, rules_config
):
    closed = replace(
        position,
        status=PaperPositionStatus.CLOSED,
        remaining_call_quantity=0,
        remaining_put_quantity=0,
        closed_at=CAPTURED_AT,
        exit_reason="done",
    )
    result = monitor(closed, market_snapshot, rules_config)

    assert result.successful is False
    assert result.snapshot is None
    assert POS_003 in result_ids(result)


def test_monitoring_does_not_mutate_position_or_entry_snapshot(
    position, market_snapshot, rules_config
):
    position_before = repr(position)
    strategy_before = repr(position.strategy)

    monitor(position, market_snapshot, rules_config)

    assert repr(position) == position_before
    assert repr(position.strategy) == strategy_before
    with pytest.raises(FrozenInstanceError):
        position.remaining_call_quantity = 1


def test_identical_inputs_produce_identical_monitoring_results(
    position, market_snapshot, rules_config
):
    first = monitor(position, market_snapshot, rules_config)
    second = monitor(position, market_snapshot, rules_config)
    assert first == second


def test_position_snapshot_financial_values_are_decimal(
    position, market_snapshot, rules_config
):
    snapshot = monitor(position, market_snapshot, rules_config).snapshot
    excluded = {
        "position_id",
        "captured_at",
        "provider_timestamp",
        "received_at",
        "remaining_call_quantity",
        "remaining_put_quantity",
        "data_status",
        "rule_results",
        "used_last_known_good",
    }
    for field in fields(snapshot):
        if field.name not in excluded:
            value = getattr(snapshot, field.name)
            assert value is None or isinstance(value, Decimal)


def test_binary_float_market_value_is_rejected(market_snapshot):
    with pytest.raises(TypeError, match="call_price must be a Decimal"):
        replace(market_snapshot, call_price=250.0)


def test_successful_monitoring_emits_market_snapshot_applied(
    position, market_snapshot, rules_config
):
    result = monitor(position, market_snapshot, rules_config)

    assert len(result.events) == 1
    assert result.events[0].event_type is TradeEventType.MARKET_SNAPSHOT_APPLIED
    assert result.events[0].occurred_at == CAPTURED_AT
    assert ("used_last_known_good", False) in result.events[0].payload


def test_fallback_event_discloses_last_known_good(
    position, market_snapshot, rules_config
):
    good = monitor(position, market_snapshot, rules_config).snapshot
    stale = replace(
        market_snapshot,
        provider_timestamp=CAPTURED_AT - timedelta(seconds=121),
    )
    result = monitor(position, stale, rules_config, last_known_good=good)
    assert ("used_last_known_good", True) in result.events[0].payload


def test_monitoring_module_has_no_clock_provider_network_persistence_or_ui_dependencies():
    source = Path(__file__).resolve().parents[1].joinpath(
        "app/straddle/monitoring.py"
    ).read_text().lower()
    forbidden = (
        "datetime.now",
        "date.today",
        "random",
        "uuid",
        "dhan",
        "yfinance",
        "httpx",
        "sqlalchemy",
        "fastapi",
        "streamlit",
        "scheduler",
        "notifier",
        "market_feed",
        "database",
        "llm",
    )
    assert all(term not in source for term in forbidden)
