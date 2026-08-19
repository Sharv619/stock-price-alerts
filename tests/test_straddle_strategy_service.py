from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.straddle.domain import OptionType
from app.straddle.engine import calculate_long_straddle
from app.straddle.rules import DATA_001, DATA_002, STR_001, STR_003, STR_004, STR_008, RulesConfig
from app.straddle.strategy_service import (
    ConstructionIssueCode,
    OptionChainSnapshot,
    OptionContract,
    construct_long_straddle,
    recommend_atm_strike,
)

UTC = timezone.utc
EXPIRY = date(2026, 8, 27)
EVALUATION_TIME = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


@pytest.fixture
def rules_config():
    return RulesConfig(
        warning_stale_after=timedelta(seconds=30),
        hard_stale_after=timedelta(seconds=120),
        high_iv_ratio=Decimal("1.5"),
        high_theta_share=Decimal("0.10"),
        required_move_threshold=Decimal("2"),
        position_stale_after=timedelta(seconds=60),
    )


def contract(
    strike,
    option_type,
    premium,
    *,
    underlying="NIFTY",
    expiry=EXPIRY,
    quantity=75,
):
    return OptionContract(
        underlying=underlying,
        option_type=option_type,
        strike=Decimal(str(strike)),
        expiry=expiry,
        premium=None if premium is None else Decimal(str(premium)),
        quantity=quantity,
    )


@pytest.fixture
def contracts():
    return tuple(
        contract(strike, option_type, premium)
        for strike, call_premium, put_premium in (
            (24200, 220, 120),
            (24300, 180, 160),
            (24400, 130, 210),
        )
        for option_type, premium in (
            (OptionType.CALL, call_premium),
            (OptionType.PUT, put_premium),
        )
    )


@pytest.fixture
def snapshot(contracts):
    return OptionChainSnapshot(
        underlying="NIFTY",
        spot_price=Decimal("24342"),
        provider_timestamp=EVALUATION_TIME - timedelta(seconds=10),
        received_at=EVALUATION_TIME - timedelta(seconds=5),
        contracts=contracts,
    )


def build(snapshot, rules_config, **overrides):
    inputs = {
        "expiry": EXPIRY,
        "evaluation_time": EVALUATION_TIME,
        "rules_config": rules_config,
    }
    inputs.update(overrides)
    return construct_long_straddle(snapshot, **inputs)


def result_ids(result):
    return {item.rule_id for item in result.validation_results}


def test_atm_recommendation_required_example():
    assert recommend_atm_strike(
        Decimal("24342"),
        (Decimal("24200"), Decimal("24300"), Decimal("24400")),
    ) == Decimal("24300")


def test_exact_atm_strike():
    assert recommend_atm_strike(
        Decimal("24300"),
        (Decimal("24200"), Decimal("24300"), Decimal("24400")),
    ) == Decimal("24300")


def test_nearest_lower_strike():
    assert recommend_atm_strike(
        Decimal("24342"),
        (Decimal("24200"), Decimal("24300"), Decimal("24400")),
    ) == Decimal("24300")


def test_nearest_upper_strike():
    assert recommend_atm_strike(
        Decimal("24370"),
        (Decimal("24200"), Decimal("24300"), Decimal("24400")),
    ) == Decimal("24400")


def test_equal_distance_tie_prefers_lower_strike():
    assert recommend_atm_strike(
        Decimal("24350"),
        (Decimal("24300"), Decimal("24400")),
    ) == Decimal("24300")


def test_atm_never_invents_a_strike():
    strikes = (Decimal("24200"), Decimal("24400"))
    assert recommend_atm_strike(Decimal("24300"), strikes) in strikes


def test_manual_strike_override(snapshot, rules_config):
    result = build(snapshot, rules_config, selected_strike=Decimal("24400"))

    assert result.recommended_atm_strike == Decimal("24300")
    assert result.selected_strike == Decimal("24400")
    assert result.strategy.call_leg.strike == Decimal("24400")
    assert result.can_proceed is True


def test_invalid_override_is_structured_block(snapshot, rules_config):
    result = build(snapshot, rules_config, selected_strike=Decimal("24500"))

    assert result.can_proceed is False
    assert result.strategy is None
    assert result.calculation is None
    assert ConstructionIssueCode.SELECTED_STRIKE_UNAVAILABLE.value in result_ids(result)


def test_missing_call_surfaces_str_008(snapshot, rules_config):
    filtered = replace(
        snapshot,
        contracts=tuple(
            item
            for item in snapshot.contracts
            if not (item.strike == Decimal("24300") and item.option_type is OptionType.CALL)
        ),
    )
    result = build(filtered, rules_config)

    assert STR_008 in result_ids(result)
    assert result.can_proceed is False
    assert result.calculation is None


def test_missing_put_surfaces_str_008(snapshot, rules_config):
    filtered = replace(
        snapshot,
        contracts=tuple(
            item
            for item in snapshot.contracts
            if not (item.strike == Decimal("24300") and item.option_type is OptionType.PUT)
        ),
    )
    result = build(filtered, rules_config)

    assert STR_008 in result_ids(result)
    assert result.can_proceed is False


def test_duplicate_call_fails_without_arbitrary_selection(snapshot, rules_config):
    duplicate = next(
        item
        for item in snapshot.contracts
        if item.strike == Decimal("24300") and item.option_type is OptionType.CALL
    )
    result = build(replace(snapshot, contracts=(*snapshot.contracts, duplicate)), rules_config)

    assert ConstructionIssueCode.DUPLICATE_CALL.value in result_ids(result)
    assert STR_008 in result_ids(result)
    assert result.strategy is None
    assert result.can_proceed is False


def test_duplicate_put_fails_without_arbitrary_selection(snapshot, rules_config):
    duplicate = next(
        item
        for item in snapshot.contracts
        if item.strike == Decimal("24300") and item.option_type is OptionType.PUT
    )
    result = build(replace(snapshot, contracts=(*snapshot.contracts, duplicate)), rules_config)

    assert ConstructionIssueCode.DUPLICATE_PUT.value in result_ids(result)
    assert STR_008 in result_ids(result)
    assert result.strategy is None


def test_mismatched_expiry_contract_is_not_selected(snapshot, rules_config):
    changed = tuple(
        replace(item, expiry=date(2026, 9, 3))
        if item.strike == Decimal("24300") and item.option_type is OptionType.PUT
        else item
        for item in snapshot.contracts
    )
    result = build(replace(snapshot, contracts=changed), rules_config)

    assert STR_008 in result_ids(result)
    assert STR_003 not in result_ids(result)
    assert result.can_proceed is False


def test_mismatched_underlying_contract_is_not_selected(snapshot, rules_config):
    changed = tuple(
        replace(item, underlying="BANKNIFTY")
        if item.strike == Decimal("24300") and item.option_type is OptionType.PUT
        else item
        for item in snapshot.contracts
    )
    result = build(replace(snapshot, contracts=changed), rules_config)

    assert STR_008 in result_ids(result)
    assert STR_001 not in result_ids(result)
    assert result.can_proceed is False


def test_mandatory_valid_long_straddle_construction(snapshot, rules_config):
    result = build(snapshot, rules_config)

    assert result.underlying == "NIFTY"
    assert result.expiry == EXPIRY
    assert result.recommended_atm_strike == Decimal("24300")
    assert result.selected_strike == Decimal("24300")
    assert result.strategy is not None
    assert not any(item.blocking for item in result.validation_results)
    assert result.calculation.combined_premium == Decimal("340")
    assert result.calculation.total_cost == Decimal("25500")
    assert result.calculation.upper_break_even == Decimal("24640")
    assert result.calculation.lower_break_even == Decimal("23960")
    assert result.can_proceed is True


def test_structural_validation_blocks_calculation(snapshot, rules_config):
    changed = tuple(
        replace(item, quantity=50)
        if item.strike == Decimal("24300") and item.option_type is OptionType.PUT
        else item
        for item in snapshot.contracts
    )
    result = build(replace(snapshot, contracts=changed), rules_config)

    assert STR_004 in result_ids(result)
    assert result.strategy is not None
    assert result.calculation is None
    assert result.can_proceed is False


def test_warning_only_validation_allows_calculation(snapshot, rules_config):
    stale = replace(
        snapshot,
        provider_timestamp=EVALUATION_TIME - timedelta(seconds=45),
    )
    result = build(stale, rules_config)

    assert DATA_002 in result_ids(result)
    assert not any(item.blocking for item in result.validation_results)
    assert result.calculation is not None
    assert result.can_proceed is True


def test_calculation_matches_phase_1_engine(snapshot, rules_config):
    result = build(
        snapshot,
        rules_config,
        expiry_spot=Decimal("25500"),
        fees=Decimal("12.50"),
        slippage=Decimal("7.50"),
    )
    expected = calculate_long_straddle(
        result.strategy,
        spot_price=Decimal("24342"),
        expiry_spot=Decimal("25500"),
        fees=Decimal("12.50"),
        slippage=Decimal("7.50"),
    )

    assert result.calculation == expected


def test_stale_market_data_warning(snapshot, rules_config):
    result = build(
        replace(snapshot, provider_timestamp=EVALUATION_TIME - timedelta(seconds=45)),
        rules_config,
    )
    assert DATA_002 in result_ids(result)
    assert DATA_001 not in result_ids(result)
    assert result.can_proceed is True


def test_hard_stale_market_data_blocks(snapshot, rules_config):
    result = build(
        replace(snapshot, provider_timestamp=EVALUATION_TIME - timedelta(seconds=121)),
        rules_config,
    )
    assert DATA_001 in result_ids(result)
    assert DATA_002 not in result_ids(result)
    assert result.strategy is not None
    assert result.calculation is None
    assert result.can_proceed is False


def test_missing_premium_blocks_through_rules(snapshot, rules_config):
    changed = tuple(
        replace(item, premium=None)
        if item.strike == Decimal("24300") and item.option_type is OptionType.CALL
        else item
        for item in snapshot.contracts
    )
    result = build(replace(snapshot, contracts=changed), rules_config)
    assert STR_008 in result_ids(result)
    assert result.can_proceed is False


def test_identical_inputs_produce_identical_outputs(snapshot, rules_config):
    assert build(snapshot, rules_config) == build(snapshot, rules_config)


def test_decimal_preservation(snapshot, rules_config):
    result = build(snapshot, rules_config)

    assert isinstance(result.spot_price, Decimal)
    assert isinstance(result.recommended_atm_strike, Decimal)
    assert isinstance(result.selected_strike, Decimal)
    assert all(
        isinstance(getattr(result.calculation, field.name), Decimal)
        for field in fields(result.calculation)
        if field.name != "engine_version"
    )


def test_empty_contract_scope_returns_structured_failure(snapshot, rules_config):
    result = build(replace(snapshot, contracts=()), rules_config)
    assert ConstructionIssueCode.NO_AVAILABLE_STRIKES.value in result_ids(result)
    assert result.can_proceed is False


def test_float_contract_money_is_rejected():
    with pytest.raises(TypeError, match="strike must be a Decimal"):
        OptionContract("NIFTY", OptionType.CALL, 24300.0, EXPIRY, Decimal("180"), 75)


def test_strategy_service_has_no_provider_api_database_or_ui_imports():
    source = Path(__file__).resolve().parents[1].joinpath(
        "app/straddle/strategy_service.py"
    ).read_text().lower()
    forbidden = (
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
        "random",
        "datetime.now",
    )
    assert all(value not in source for value in forbidden)
