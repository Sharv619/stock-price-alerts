from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.straddle.domain import OptionType
from app.straddle.paper_trading import (
    LifecycleIssueCode,
    PaperPositionStatus,
    StrategyStatus,
    TradeEventType,
    approve_strategy,
    close_paper_position,
    exit_paper_position,
    open_paper_position,
)
from app.straddle.rules import (
    DATA_002,
    DATA_005,
    POS_001,
    POS_003,
    POS_004,
    RISK_IV_002,
    STR_002,
    RiskInputs,
    RulesConfig,
    evaluate_risk,
    validate_strategy,
)
from app.straddle.strategy_service import (
    OptionChainSnapshot,
    OptionContract,
    construct_long_straddle,
)

UTC = timezone.utc
EXPIRY = date(2026, 8, 27)
OPENED_AT = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
CLOSED_AT = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


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
def construction(rules_config):
    contracts = (
        OptionContract("NIFTY", OptionType.CALL, Decimal("24300"), EXPIRY, Decimal("180"), 75),
        OptionContract("NIFTY", OptionType.PUT, Decimal("24300"), EXPIRY, Decimal("160"), 75),
    )
    snapshot = OptionChainSnapshot(
        underlying="NIFTY",
        spot_price=Decimal("24300"),
        provider_timestamp=OPENED_AT - timedelta(seconds=5),
        received_at=OPENED_AT - timedelta(seconds=2),
        contracts=contracts,
    )
    return construct_long_straddle(
        snapshot,
        expiry=EXPIRY,
        evaluation_time=OPENED_AT,
        rules_config=rules_config,
    )


@pytest.fixture
def approval(construction):
    return approve_strategy(
        construction,
        acknowledgements=set(),
        approved_at=OPENED_AT,
    )


@pytest.fixture
def position(approval):
    return open_paper_position(
        approval,
        position_id="paper-001",
        opened_at=OPENED_AT,
    ).position


def result_ids(results):
    return {result.rule_id for result in results}


def test_blocking_strategy_cannot_be_approved(construction):
    invalid_strategy = replace(
        construction.strategy,
        put_leg=replace(construction.strategy.put_leg, strike=Decimal("24400")),
    )
    blocking_rules = validate_strategy(invalid_strategy, evaluation_date=OPENED_AT.date())
    blocking = replace(
        construction,
        strategy=invalid_strategy,
        validation_results=(*construction.validation_results, *blocking_rules),
        can_proceed=False,
    )
    result = approve_strategy(blocking, acknowledgements=set(), approved_at=OPENED_AT)

    assert STR_002 in result_ids(result.validation_results)
    assert result.approved is False
    assert result.status is StrategyStatus.DRAFT
    assert result.approved_strategy is None


def test_acknowledgement_required_warning_blocks_until_acknowledged(construction, rules_config):
    warning = evaluate_risk(
        RiskInputs(event_tag="RBI decision", event_time=OPENED_AT),
        evaluation_time=OPENED_AT,
        config=rules_config,
    )
    with_warning = replace(
        construction,
        validation_results=(*construction.validation_results, *warning),
    )

    blocked = approve_strategy(with_warning, acknowledgements=set(), approved_at=OPENED_AT)
    approved = approve_strategy(
        with_warning,
        acknowledgements={RISK_IV_002},
        approved_at=OPENED_AT,
    )

    assert blocked.approved is False
    assert blocked.status is StrategyStatus.VALIDATED
    assert blocked.missing_acknowledgements == (RISK_IV_002,)
    assert approved.approved is True
    assert approved.approved_strategy.acknowledgements == frozenset({RISK_IV_002})


def test_non_acknowledgement_warning_does_not_block(construction):
    stale_warning = next(
        result for result in construction.validation_results if result.rule_id == DATA_005
    )
    non_ack_warning = replace(
        stale_warning,
        rule_id=DATA_002,
        severity=stale_warning.severity.WARNING,
        requires_acknowledgement=False,
    )
    modified = replace(
        construction,
        validation_results=(non_ack_warning,),
    )

    assert approve_strategy(modified, acknowledgements=set(), approved_at=OPENED_AT).approved is True


def test_informational_rule_does_not_block(construction):
    assert DATA_005 in result_ids(construction.validation_results)
    assert approve_strategy(construction, acknowledgements=set(), approved_at=OPENED_AT).approved is True


def test_approval_is_deterministic(construction):
    first = approve_strategy(construction, acknowledgements={"UNKNOWN"}, approved_at=OPENED_AT)
    second = approve_strategy(construction, acknowledgements={"UNKNOWN"}, approved_at=OPENED_AT)

    assert first == second
    assert first.approved_strategy.acknowledgements == frozenset()


def test_approval_emits_typed_events(construction, rules_config):
    warning = evaluate_risk(
        RiskInputs(event_tag="RBI decision", event_time=OPENED_AT),
        evaluation_time=OPENED_AT,
        config=rules_config,
    )
    modified = replace(
        construction,
        validation_results=(*construction.validation_results, *warning),
    )
    result = approve_strategy(
        modified,
        acknowledgements={RISK_IV_002},
        approved_at=OPENED_AT,
    )

    assert tuple(event.event_type for event in result.events) == (
        TradeEventType.STRATEGY_VALIDATED,
        TradeEventType.WARNING_ACKNOWLEDGED,
    )


def test_opening_requires_approval(construction):
    rejected = replace(
        approve_strategy(construction, acknowledgements=set(), approved_at=OPENED_AT),
        approved=False,
        approved_strategy=None,
    )
    result = open_paper_position(
        rejected,
        position_id="paper-001",
        opened_at=OPENED_AT,
    )

    assert result.opened is False
    assert result.position is None
    assert result.issues[0].code is LifecycleIssueCode.APPROVAL_REQUIRED


def test_opening_snapshot_is_immutable_and_exact(approval):
    result = open_paper_position(
        approval,
        position_id="paper-001",
        opened_at=OPENED_AT,
    )
    position = result.position

    assert result.opened is True
    assert position.status is PaperPositionStatus.OPEN
    assert position.opened_at == OPENED_AT
    assert position.entry_call_premium == Decimal("180")
    assert position.entry_put_premium == Decimal("160")
    assert position.remaining_call_quantity == 75
    assert position.remaining_put_quantity == 75
    assert result.events[0].event_type is TradeEventType.PAPER_POSITION_OPENED
    with pytest.raises(FrozenInstanceError):
        position.status = PaperPositionStatus.CLOSED


def test_opening_does_not_mutate_strategy(approval):
    original = approval.approved_strategy.strategy
    before = repr(original)

    opened = open_paper_position(
        approval,
        position_id="paper-001",
        opened_at=OPENED_AT,
    )

    assert approval.approved_strategy.strategy is original
    assert opened.position.strategy is original
    assert repr(original) == before


def test_mandatory_full_close_acceptance_case(position, rules_config):
    result = close_paper_position(
        position,
        call_exit_price=Decimal("250"),
        put_exit_price=Decimal("100"),
        closed_at=CLOSED_AT,
        exit_reason="scenario complete",
        rules_config=rules_config,
        exit_fees=Decimal("100"),
        exit_slippage=Decimal("50"),
    )
    closed = result.position

    assert result.completed is True
    assert closed.realised_call_pnl == Decimal("5250")
    assert closed.realised_put_pnl == Decimal("-4500")
    assert closed.realised_call_pnl + closed.realised_put_pnl == Decimal("750")
    assert closed.realised_pnl == Decimal("600")
    assert closed.status is PaperPositionStatus.CLOSED
    assert closed.remaining_call_quantity == 0
    assert closed.remaining_put_quantity == 0
    assert closed.closed_at == CLOSED_AT
    assert closed.exit_reason == "scenario complete"
    assert result.events[-1].event_type is TradeEventType.POSITION_CLOSED


def test_mandatory_partial_call_exit_requires_acknowledgement(position, rules_config):
    blocked = exit_paper_position(
        position,
        call_quantity=25,
        put_quantity=0,
        call_exit_price=Decimal("250"),
        put_exit_price=None,
        occurred_at=CLOSED_AT,
        exit_reason="reduce call",
        rules_config=rules_config,
    )
    completed = exit_paper_position(
        position,
        call_quantity=25,
        put_quantity=0,
        call_exit_price=Decimal("250"),
        put_exit_price=None,
        occurred_at=CLOSED_AT,
        exit_reason="reduce call",
        rules_config=rules_config,
        acknowledgements={POS_001},
    )

    assert POS_001 in result_ids(blocked.validation_results)
    assert blocked.completed is False
    assert blocked.position is position
    assert completed.completed is True
    assert completed.position.realised_call_pnl == Decimal("1750")
    assert completed.position.remaining_call_quantity == 50
    assert completed.position.remaining_put_quantity == 75
    assert completed.position.status is PaperPositionStatus.PARTIALLY_CLOSED
    assert tuple(event.event_type for event in completed.events) == (
        TradeEventType.WARNING_ACKNOWLEDGED,
        TradeEventType.LEG_PARTIALLY_CLOSED,
    )


def test_partial_put_exit(position, rules_config):
    result = exit_paper_position(
        position,
        call_quantity=0,
        put_quantity=25,
        call_exit_price=None,
        put_exit_price=Decimal("100"),
        occurred_at=CLOSED_AT,
        exit_reason="reduce put",
        rules_config=rules_config,
        acknowledgements={POS_001},
    )

    assert result.completed is True
    assert result.position.realised_put_pnl == Decimal("-1500")
    assert result.position.remaining_call_quantity == 75
    assert result.position.remaining_put_quantity == 50
    assert result.position.status is PaperPositionStatus.PARTIALLY_CLOSED


def test_excessive_exit_quantity_is_blocked_by_pos_004(position, rules_config):
    result = exit_paper_position(
        position,
        call_quantity=76,
        put_quantity=0,
        call_exit_price=Decimal("250"),
        put_exit_price=None,
        occurred_at=CLOSED_AT,
        exit_reason="invalid",
        rules_config=rules_config,
        acknowledgements={POS_001},
    )

    assert result.completed is False
    assert POS_004 in result_ids(result.validation_results)
    assert result.position is position


def test_closed_is_terminal_through_pos_003(position, rules_config):
    closed = close_paper_position(
        position,
        call_exit_price=Decimal("250"),
        put_exit_price=Decimal("100"),
        closed_at=CLOSED_AT,
        exit_reason="done",
        rules_config=rules_config,
    ).position
    attempted = close_paper_position(
        closed,
        call_exit_price=Decimal("250"),
        put_exit_price=Decimal("100"),
        closed_at=CLOSED_AT + timedelta(hours=1),
        exit_reason="again",
        rules_config=rules_config,
    )

    assert attempted.completed is False
    assert POS_003 in result_ids(attempted.validation_results)
    assert attempted.position is closed


def test_zero_quantity_transition_is_rejected(position, rules_config):
    result = exit_paper_position(
        position,
        call_quantity=0,
        put_quantity=0,
        call_exit_price=None,
        put_exit_price=None,
        occurred_at=CLOSED_AT,
        exit_reason="nothing",
        rules_config=rules_config,
    )

    assert result.completed is False
    assert LifecycleIssueCode.EXIT_QUANTITY_REQUIRED.value in result_ids(
        result.validation_results
    )


def test_entry_and_exit_costs_are_each_deducted_once(approval, rules_config):
    position = open_paper_position(
        approval,
        position_id="paper-costs",
        opened_at=OPENED_AT,
        fees_at_entry=Decimal("20"),
        slippage_at_entry=Decimal("10"),
    ).position
    result = close_paper_position(
        position,
        call_exit_price=Decimal("180"),
        put_exit_price=Decimal("160"),
        closed_at=CLOSED_AT,
        exit_reason="flat",
        rules_config=rules_config,
        exit_fees=Decimal("15"),
        exit_slippage=Decimal("5"),
    )

    assert position.realised_pnl == Decimal("-30")
    assert result.position.realised_pnl == Decimal("-50")


def test_all_position_financial_fields_remain_decimal(position, rules_config):
    result = close_paper_position(
        position,
        call_exit_price=Decimal("250"),
        put_exit_price=Decimal("100"),
        closed_at=CLOSED_AT,
        exit_reason="done",
        rules_config=rules_config,
    )
    decimal_fields = (
        "entry_call_premium",
        "entry_put_premium",
        "strike",
        "fees_at_entry",
        "slippage_at_entry",
        "realised_call_pnl",
        "realised_put_pnl",
        "exit_fees_paid",
        "exit_slippage_paid",
        "realised_pnl",
    )
    assert all(isinstance(getattr(result.position, name), Decimal) for name in decimal_fields)


def test_binary_float_exit_price_is_rejected(position, rules_config):
    with pytest.raises(TypeError, match="call_exit_price must be a Decimal"):
        exit_paper_position(
            position,
            call_quantity=1,
            put_quantity=0,
            call_exit_price=250.0,
            put_exit_price=None,
            occurred_at=CLOSED_AT,
            exit_reason="invalid",
            rules_config=rules_config,
            acknowledgements={POS_001},
        )


def test_identical_lifecycle_inputs_produce_identical_outputs(position, rules_config):
    kwargs = {
        "call_quantity": 25,
        "put_quantity": 0,
        "call_exit_price": Decimal("250"),
        "put_exit_price": None,
        "occurred_at": CLOSED_AT,
        "exit_reason": "reduce call",
        "rules_config": rules_config,
        "acknowledgements": {POS_001},
    }
    assert exit_paper_position(position, **kwargs) == exit_paper_position(position, **kwargs)


def test_paper_trading_has_no_clock_provider_network_broker_or_ui_dependencies():
    source = Path(__file__).resolve().parents[1].joinpath(
        "app/straddle/paper_trading.py"
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
        "broker",
        "order placement",
        "llm",
    )
    assert all(term not in source for term in forbidden)
