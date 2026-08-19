from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.straddle import OptionLeg, OptionType, PositionSide, Strategy
from app.straddle.rules import (
    DATA_001,
    DATA_002,
    DATA_003,
    DATA_004,
    DATA_005,
    POS_001,
    POS_002,
    POS_003,
    POS_004,
    POS_005,
    RISK_IV_001,
    RISK_IV_002,
    RISK_MOVE_001,
    RISK_THETA_001,
    RULE_REGISTRY,
    STR_001,
    STR_002,
    STR_003,
    STR_004,
    STR_005,
    STR_006,
    STR_007,
    STR_008,
    MarketDataSnapshot,
    PositionAction,
    PositionActionInputs,
    PositionStatus,
    RiskInputs,
    RuleSeverity,
    RulesConfig,
    StrategyValidationInput,
    evaluate_market_data,
    evaluate_position_action,
    evaluate_risk,
    validate_strategy,
)

UTC = timezone.utc
EVALUATION_DATE = date(2026, 8, 20)
EVALUATION_TIME = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


@pytest.fixture
def config():
    return RulesConfig(
        warning_stale_after=timedelta(seconds=30),
        hard_stale_after=timedelta(seconds=120),
        high_iv_ratio=Decimal("1.5"),
        high_theta_share=Decimal("0.10"),
        required_move_threshold=Decimal("2.0"),
        position_stale_after=timedelta(seconds=60),
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
        call_leg=OptionLeg(option_type=OptionType.CALL, premium=Decimal("180"), **common),
        put_leg=OptionLeg(option_type=OptionType.PUT, premium=Decimal("160"), **common),
    )


@pytest.fixture
def snapshot():
    return MarketDataSnapshot(
        spot_price=Decimal("24300"),
        call_premium=Decimal("180"),
        put_premium=Decimal("160"),
        provider_timestamp=EVALUATION_TIME - timedelta(seconds=10),
        received_at=EVALUATION_TIME - timedelta(seconds=5),
        greeks_available=True,
    )


def ids(results):
    return {result.rule_id for result in results}


def by_id(results, rule_id):
    return next(result for result in results if result.rule_id == rule_id)


def test_registry_contains_one_canonical_definition_per_stable_rule():
    expected = {
        STR_001, STR_002, STR_003, STR_004, STR_005, STR_006, STR_007, STR_008,
        DATA_001, DATA_002, DATA_003, DATA_004, DATA_005,
        RISK_IV_001, RISK_IV_002, RISK_THETA_001, RISK_MOVE_001,
        POS_001, POS_002, POS_003, POS_004, POS_005,
    }

    assert set(RULE_REGISTRY) == expected
    assert len(RULE_REGISTRY) == len(expected)
    assert all(key == definition.rule_id for key, definition in RULE_REGISTRY.items())
    assert all(isinstance(definition.default_severity, RuleSeverity) for definition in RULE_REGISTRY.values())


@pytest.mark.parametrize(
    ("rule_id", "make_invalid"),
    [
        (STR_001, lambda value: replace(value, put_leg=replace(value.put_leg, underlying="BANKNIFTY"))),
        (STR_002, lambda value: replace(value, put_leg=replace(value.put_leg, strike=Decimal("24400")))),
        (STR_003, lambda value: replace(value, put_leg=replace(value.put_leg, expiry=date(2026, 9, 3)))),
        (STR_004, lambda value: replace(value, put_leg=replace(value.put_leg, quantity=50))),
        (STR_005, lambda value: replace(value, call_leg=replace(value.call_leg, side=PositionSide.SELL))),
        (STR_006, lambda value: replace(value, call_leg=replace(value.call_leg, premium=Decimal("-1")))),
        (STR_007, lambda value: replace(value, call_leg=replace(value.call_leg, expiry=date(2026, 8, 19)))),
        (STR_008, lambda value: StrategyValidationInput(call_leg=None, put_leg=value.put_leg)),
    ],
)
def test_each_structural_rule_triggers_as_blocking(strategy, rule_id, make_invalid):
    result = by_id(
        validate_strategy(make_invalid(strategy), evaluation_date=EVALUATION_DATE),
        rule_id,
    )

    assert result.blocking is True
    assert result.severity is RuleSeverity.ERROR
    assert result.evidence


@pytest.mark.parametrize(
    "rule_id",
    [STR_001, STR_002, STR_003, STR_004, STR_005, STR_006, STR_007, STR_008],
)
def test_each_structural_rule_does_not_trigger_for_valid_straddle(strategy, rule_id):
    assert rule_id not in ids(validate_strategy(strategy, evaluation_date=EVALUATION_DATE))


def test_strike_mismatch_required_example(strategy):
    mismatched = replace(strategy, put_leg=replace(strategy.put_leg, strike=Decimal("24400")))
    result = by_id(validate_strategy(mismatched, evaluation_date=EVALUATION_DATE), STR_002)

    assert result.blocking is True
    assert result.evidence == {"call_strike": Decimal("24300"), "put_strike": Decimal("24400")}


def test_expiry_on_evaluation_date_is_not_in_the_past(strategy):
    same_day = replace(
        strategy,
        call_leg=replace(strategy.call_leg, expiry=EVALUATION_DATE),
        put_leg=replace(strategy.put_leg, expiry=EVALUATION_DATE),
    )
    assert STR_007 not in ids(validate_strategy(same_day, evaluation_date=EVALUATION_DATE))


def test_data_warning_staleness_example_emits_data_002_only(snapshot, config):
    aged = replace(snapshot, provider_timestamp=EVALUATION_TIME - timedelta(seconds=45))
    results = evaluate_market_data(aged, evaluation_time=EVALUATION_TIME, config=config)

    assert DATA_002 in ids(results)
    assert DATA_001 not in ids(results)
    assert by_id(results, DATA_002).blocking is False


def test_data_hard_staleness_example_emits_data_001_only(snapshot, config):
    aged = replace(snapshot, provider_timestamp=EVALUATION_TIME - timedelta(seconds=121))
    results = evaluate_market_data(aged, evaluation_time=EVALUATION_TIME, config=config)

    assert DATA_001 in ids(results)
    assert DATA_002 not in ids(results)
    assert by_id(results, DATA_001).blocking is True


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(seconds=30), set()),
        (timedelta(seconds=30, microseconds=1), {DATA_002}),
        (timedelta(seconds=120), {DATA_002}),
        (timedelta(seconds=120, microseconds=1), {DATA_001}),
    ],
)
def test_data_staleness_exact_boundaries(snapshot, config, age, expected):
    aged = replace(snapshot, provider_timestamp=EVALUATION_TIME - age)
    stale_ids = ids(evaluate_market_data(aged, evaluation_time=EVALUATION_TIME, config=config)) & {DATA_001, DATA_002}
    assert stale_ids == expected


def test_data_003_triggers_when_provider_timestamp_missing(snapshot, config):
    missing = replace(snapshot, provider_timestamp=None, received_at=EVALUATION_TIME)
    result = by_id(evaluate_market_data(missing, evaluation_time=EVALUATION_TIME, config=config), DATA_003)
    assert result.severity is RuleSeverity.WARNING
    assert result.blocking is False


def test_data_003_does_not_trigger_when_provider_timestamp_exists(snapshot, config):
    assert DATA_003 not in ids(evaluate_market_data(snapshot, evaluation_time=EVALUATION_TIME, config=config))


def test_data_004_triggers_as_blocking_for_missing_required_value(snapshot, config):
    missing = replace(snapshot, call_premium=None)
    result = by_id(evaluate_market_data(missing, evaluation_time=EVALUATION_TIME, config=config), DATA_004)
    assert result.blocking is True
    assert result.evidence["missing_fields"] == ("call_premium",)


def test_data_004_does_not_trigger_when_required_values_exist(snapshot, config):
    assert DATA_004 not in ids(evaluate_market_data(snapshot, evaluation_time=EVALUATION_TIME, config=config))


def test_data_005_triggers_as_info_when_greeks_unavailable(snapshot, config):
    unavailable = replace(snapshot, greeks_available=False)
    result = by_id(evaluate_market_data(unavailable, evaluation_time=EVALUATION_TIME, config=config), DATA_005)
    assert result.severity is RuleSeverity.INFO
    assert result.blocking is False


def test_data_005_does_not_trigger_when_greeks_available(snapshot, config):
    assert DATA_005 not in ids(evaluate_market_data(snapshot, evaluation_time=EVALUATION_TIME, config=config))


def test_received_at_is_used_for_age_without_provider_timestamp(snapshot, config):
    missing = replace(
        snapshot,
        provider_timestamp=None,
        received_at=EVALUATION_TIME - timedelta(seconds=45),
    )
    results = evaluate_market_data(missing, evaluation_time=EVALUATION_TIME, config=config)
    assert {DATA_002, DATA_003}.issubset(ids(results))
    assert by_id(results, DATA_002).evidence["timestamp_source"] == "received"


def test_risk_iv_001_triggers_above_configured_ratio(config):
    inputs = RiskInputs(current_iv=Decimal("31"), historical_iv_baseline=Decimal("20"))
    result = by_id(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=config), RISK_IV_001)
    assert result.severity is RuleSeverity.WARNING
    assert result.evidence["iv_ratio"] == Decimal("1.55")


def test_risk_iv_001_does_not_trigger_at_exact_ratio(config):
    inputs = RiskInputs(current_iv=Decimal("30"), historical_iv_baseline=Decimal("20"))
    assert RISK_IV_001 not in ids(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=config))


def test_risk_iv_002_triggers_with_acknowledgement_for_occurred_event(config):
    inputs = RiskInputs(event_tag="RBI decision", event_time=EVALUATION_TIME)
    result = by_id(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=config), RISK_IV_002)
    assert result.requires_acknowledgement is True
    assert result.blocking is False


def test_risk_iv_002_does_not_trigger_before_event(config):
    inputs = RiskInputs(event_tag="RBI decision", event_time=EVALUATION_TIME + timedelta(seconds=1))
    assert RISK_IV_002 not in ids(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=config))


def test_risk_theta_001_triggers_above_configured_share(config):
    inputs = RiskInputs(estimated_daily_theta=Decimal("-11"), combined_premium=Decimal("100"))
    result = by_id(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=config), RISK_THETA_001)
    assert result.evidence["theta_share"] == Decimal("0.11")
    assert isinstance(result.evidence["theta_share"], Decimal)


def test_risk_theta_001_does_not_trigger_at_exact_share(config):
    inputs = RiskInputs(estimated_daily_theta=Decimal("-10"), combined_premium=Decimal("100"))
    assert RISK_THETA_001 not in ids(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=config))


def test_risk_move_001_triggers_above_configured_threshold(config):
    inputs = RiskInputs(required_move_percent=Decimal("2.01"))
    result = by_id(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=config), RISK_MOVE_001)
    assert result.severity is RuleSeverity.INFO
    assert result.evidence["required_move_percent"] == Decimal("2.01")


def test_risk_move_001_does_not_trigger_at_exact_threshold(config):
    inputs = RiskInputs(required_move_percent=Decimal("2.0"))
    assert RISK_MOVE_001 not in ids(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=config))


def test_missing_optional_risk_and_greek_values_do_not_crash(config):
    assert evaluate_risk(RiskInputs(), evaluation_time=EVALUATION_TIME, config=config) == ()
    snapshot = MarketDataSnapshot(
        spot_price=Decimal("24300"),
        call_premium=Decimal("180"),
        put_premium=Decimal("160"),
        provider_timestamp=EVALUATION_TIME,
        received_at=EVALUATION_TIME,
    )
    assert DATA_005 in ids(
        evaluate_market_data(snapshot, evaluation_time=EVALUATION_TIME, config=config)
    )


def test_risk_thresholds_are_supplied_by_config(config):
    inputs = RiskInputs(current_iv=Decimal("31"), historical_iv_baseline=Decimal("20"))
    higher_threshold = replace(config, high_iv_ratio=Decimal("2"))
    assert RISK_IV_001 in ids(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=config))
    assert RISK_IV_001 not in ids(evaluate_risk(inputs, evaluation_time=EVALUATION_TIME, config=higher_threshold))


def test_position_001_triggers_with_acknowledgement(config):
    inputs = PositionActionInputs(PositionAction.ONE_LEG_EXIT, PositionStatus.OPEN, 75, 75)
    result = by_id(evaluate_position_action(inputs, config=config), POS_001)
    assert result.requires_acknowledgement is True
    assert result.blocking is False


def test_position_001_does_not_trigger_for_two_leg_exit(config):
    inputs = PositionActionInputs(PositionAction.EXIT, PositionStatus.OPEN, 75, 75)
    assert POS_001 not in ids(evaluate_position_action(inputs, config=config))


def test_position_002_triggers_for_stale_refresh(config):
    inputs = PositionActionInputs(
        PositionAction.REFRESH,
        PositionStatus.OPEN,
        snapshot_age=timedelta(seconds=61),
    )
    result = by_id(evaluate_position_action(inputs, config=config), POS_002)
    assert result.severity is RuleSeverity.WARNING
    assert result.blocking is False


def test_position_002_does_not_trigger_at_exact_boundary(config):
    inputs = PositionActionInputs(
        PositionAction.REFRESH,
        PositionStatus.OPEN,
        snapshot_age=timedelta(seconds=60),
    )
    assert POS_002 not in ids(evaluate_position_action(inputs, config=config))


def test_position_003_blocks_closed_position_mutation(config):
    inputs = PositionActionInputs(PositionAction.MUTATE, PositionStatus.CLOSED)
    assert by_id(evaluate_position_action(inputs, config=config), POS_003).blocking is True


def test_position_003_does_not_trigger_for_open_position(config):
    inputs = PositionActionInputs(PositionAction.MUTATE, PositionStatus.OPEN)
    assert POS_003 not in ids(evaluate_position_action(inputs, config=config))


def test_position_004_blocks_exit_quantity_above_open_quantity(config):
    inputs = PositionActionInputs(PositionAction.EXIT, PositionStatus.OPEN, 75, 76)
    assert by_id(evaluate_position_action(inputs, config=config), POS_004).blocking is True


def test_position_004_does_not_trigger_at_open_quantity(config):
    inputs = PositionActionInputs(PositionAction.EXIT, PositionStatus.OPEN, 75, 75)
    assert POS_004 not in ids(evaluate_position_action(inputs, config=config))


def test_position_005_blocks_new_short_straddle_execution(config):
    inputs = PositionActionInputs(PositionAction.NEW_SHORT_STRADDLE, PositionStatus.OPEN)
    result = by_id(evaluate_position_action(inputs, config=config), POS_005)
    assert result.blocking is True
    assert result.evidence["execution_allowed"] is False


def test_position_005_does_not_trigger_for_long_position_action(config):
    inputs = PositionActionInputs(PositionAction.MUTATE, PositionStatus.OPEN)
    assert POS_005 not in ids(evaluate_position_action(inputs, config=config))


def test_identical_inputs_produce_identical_rule_results(strategy, snapshot, config):
    assert validate_strategy(strategy, evaluation_date=EVALUATION_DATE) == validate_strategy(strategy, evaluation_date=EVALUATION_DATE)
    assert evaluate_market_data(snapshot, evaluation_time=EVALUATION_TIME, config=config) == evaluate_market_data(snapshot, evaluation_time=EVALUATION_TIME, config=config)
    risk = RiskInputs(required_move_percent=Decimal("3"))
    assert evaluate_risk(risk, evaluation_time=EVALUATION_TIME, config=config) == evaluate_risk(risk, evaluation_time=EVALUATION_TIME, config=config)


def test_rules_module_has_no_wall_clock_random_or_external_dependencies():
    source = Path(__file__).resolve().parents[1].joinpath("app/straddle/rules.py").read_text()
    forbidden = (
        "datetime.now",
        "date.today",
        "random",
        "httpx",
        "yfinance",
        "fastapi",
        "sqlalchemy",
        "market_feed",
        "scheduler",
    )
    assert all(value not in source.lower() for value in forbidden)
