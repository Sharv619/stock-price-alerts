from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Alert, Base
from app.straddle.domain import OptionLeg, OptionType, PositionSide, Strategy
from app.straddle.engine import calculate_long_straddle
from app.straddle.models import (
    OptionLegRecord,
    PaperPositionRecord,
    PositionSnapshotRecord,
    StrategyApprovalRecord,
    TradeEventRecord,
    WarningAcknowledgementRecord,
)
from app.straddle.monitoring import PositionMarketSnapshot, monitor_paper_position
from app.straddle.paper_trading import (
    PaperPositionStatus,
    TradeEvent,
    TradeEventType,
    approve_strategy,
    close_paper_position,
    exit_paper_position,
    open_paper_position,
)
from app.straddle.persistence import (
    StraddleRepository,
    create_straddle_schema,
    deserialize_json,
    serialize_json,
)
from app.straddle.rules import RiskInputs, RulesConfig, evaluate_risk
from app.straddle.strategy_service import StrategyConstructionResult

UTC = timezone.utc
EXPIRY = date(2026, 8, 27)
CREATED_AT = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
APPROVED_AT = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
OPENED_AT = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
CAPTURED_AT = datetime(2026, 8, 20, 11, 0, tzinfo=UTC)
CLOSED_AT = datetime(2026, 8, 20, 15, 0, tzinfo=UTC)
STRATEGY_ID = "strategy-001"
POSITION_ID = "paper-001"


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'straddle-test.db'}")
    Base.metadata.create_all(engine)
    create_straddle_schema(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def repository(session_factory):
    return StraddleRepository(session_factory)


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
def strategy():
    common = dict(
        underlying="NIFTY",
        side=PositionSide.BUY,
        strike=Decimal("24300"),
        expiry=EXPIRY,
        quantity=75,
    )
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


@pytest.fixture
def calculation(strategy):
    return calculate_long_straddle(
        strategy,
        spot_price=Decimal("24300"),
        expiry_spot=Decimal("25500"),
    )


def save_strategy(repository, strategy, *, created_at=CREATED_AT):
    repository.save_strategy(
        strategy_id=STRATEGY_ID,
        strategy=strategy,
        status="DRAFT",
        spot_price=Decimal("24300.1234567890123456789"),
        market_timestamp=created_at,
        created_at=created_at,
    )


def approved_result(strategy, calculation, validation_results=(), acknowledgements=frozenset()):
    construction = StrategyConstructionResult(
        underlying="NIFTY",
        expiry=EXPIRY,
        spot_price=Decimal("24300"),
        recommended_atm_strike=Decimal("24300"),
        selected_strike=Decimal("24300"),
        strategy=strategy,
        validation_results=tuple(validation_results),
        calculation=calculation,
        can_proceed=True,
    )
    return approve_strategy(
        construction,
        acknowledgements=set(acknowledgements),
        approved_at=APPROVED_AT,
    )


def opened_result(strategy, calculation):
    return open_paper_position(
        approved_result(strategy, calculation),
        position_id=POSITION_ID,
        opened_at=OPENED_AT,
    )


def persist_open_position(repository, strategy, calculation):
    save_strategy(repository, strategy)
    approval = approved_result(strategy, calculation)
    repository.save_approval(STRATEGY_ID, approval, approved_at=APPROVED_AT)
    opened = open_paper_position(
        approval, position_id=POSITION_ID, opened_at=OPENED_AT
    )
    repository.open_position(STRATEGY_ID, opened)
    return opened.position


def fresh_monitoring(position, rules_config, *, captured_at=CAPTURED_AT):
    market = PositionMarketSnapshot(
        call_price=Decimal("250"),
        put_price=Decimal("100"),
        spot_price=Decimal("24500"),
        provider_timestamp=captured_at - timedelta(seconds=5),
        received_at=captured_at - timedelta(seconds=2),
    )
    return monitor_paper_position(
        position,
        market,
        captured_at=captured_at,
        rules_config=rules_config,
    )


def close_result(position, rules_config):
    return close_paper_position(
        position,
        call_exit_price=Decimal("250"),
        put_exit_price=Decimal("100"),
        closed_at=CLOSED_AT,
        exit_reason="planned exit",
        rules_config=rules_config,
        exit_fees=Decimal("100"),
        exit_slippage=Decimal("50"),
    )


def test_strategy_and_option_legs_persist(repository, session_factory, strategy):
    save_strategy(repository, strategy)

    stored = repository.load_strategy(STRATEGY_ID)
    assert stored.strategy == strategy
    assert stored.status == "DRAFT"
    with session_factory() as session:
        legs = session.scalars(
            select(OptionLegRecord).order_by(OptionLegRecord.id)
        ).all()
    assert [(leg.option_type, leg.entry_premium) for leg in legs] == [
        ("CALL", Decimal("180")),
        ("PUT", Decimal("160")),
    ]


def test_decimal_and_engine_version_round_trip(repository, strategy, calculation):
    save_strategy(repository, strategy)
    repository.append_calculation(STRATEGY_ID, calculation, created_at=CREATED_AT)

    stored_strategy = repository.load_strategy(STRATEGY_ID)
    stored_calculation = repository.load_calculations(STRATEGY_ID)[0]
    assert stored_strategy.spot_price == Decimal("24300.1234567890123456789")
    assert isinstance(stored_strategy.spot_price, Decimal)
    assert stored_calculation == calculation
    assert isinstance(stored_calculation.required_move_percent, Decimal)
    assert stored_calculation.engine_version == calculation.engine_version


def test_repeated_calculations_append_history(repository, strategy, calculation):
    save_strategy(repository, strategy)
    repository.append_calculation(STRATEGY_ID, calculation, created_at=CREATED_AT)
    repository.append_calculation(STRATEGY_ID, calculation, created_at=APPROVED_AT)
    assert repository.load_calculations(STRATEGY_ID) == (calculation, calculation)


def test_risk_results_and_json_evidence_round_trip(
    repository, strategy, rules_config
):
    save_strategy(repository, strategy)
    results = evaluate_risk(
        RiskInputs(
            current_iv=Decimal("30"),
            historical_iv_baseline=Decimal("10"),
            event_tag="RBI",
            event_time=CREATED_AT,
        ),
        evaluation_time=APPROVED_AT,
        config=rules_config,
    )
    repository.append_risk_assessments(
        STRATEGY_ID, results, created_at=APPROVED_AT
    )

    stored = repository.load_risk_assessments(STRATEGY_ID)
    assert [item.rule_id for item in stored] == ["RISK-IV-001", "RISK-IV-002"]
    assert stored[0].evidence["iv_ratio"] == Decimal("3")
    assert stored[1].evidence["event_time"] == CREATED_AT


def test_repeated_risk_assessments_append(repository, strategy, rules_config):
    save_strategy(repository, strategy)
    results = evaluate_risk(
        RiskInputs(required_move_percent=Decimal("3")),
        evaluation_time=CREATED_AT,
        config=rules_config,
    )
    repository.append_risk_assessments(STRATEGY_ID, results, created_at=CREATED_AT)
    repository.append_risk_assessments(STRATEGY_ID, results, created_at=APPROVED_AT)
    assert len(repository.load_risk_assessments(STRATEGY_ID)) == 2


def test_approval_and_acknowledgement_are_separate_records(
    repository, session_factory, strategy, calculation, rules_config
):
    save_strategy(repository, strategy)
    warning = evaluate_risk(
        RiskInputs(event_tag="RBI", event_time=CREATED_AT),
        evaluation_time=APPROVED_AT,
        config=rules_config,
    )
    approval = approved_result(
        strategy, calculation, warning, acknowledgements={"RISK-IV-002"}
    )
    repository.save_approval(STRATEGY_ID, approval, approved_at=APPROVED_AT)

    assert repository.load_approvals(STRATEGY_ID)[0].approved is True
    assert repository.load_acknowledgements(STRATEGY_ID)[0].rule_id == "RISK-IV-002"
    with session_factory() as session:
        assert session.scalar(select(func.count(StrategyApprovalRecord.id))) == 1
        assert session.scalar(select(func.count(WarningAcknowledgementRecord.id))) == 1


def test_paper_position_open_and_entry_snapshot_persist(
    repository, strategy, calculation
):
    position = persist_open_position(repository, strategy, calculation)
    assert repository.load_position(POSITION_ID) == position


def test_partial_and_closed_position_state_persist(
    repository, strategy, calculation, rules_config
):
    position = persist_open_position(repository, strategy, calculation)
    partial = exit_paper_position(
        position,
        call_quantity=25,
        put_quantity=0,
        call_exit_price=Decimal("250"),
        put_exit_price=None,
        occurred_at=CAPTURED_AT,
        exit_reason="reduce call",
        rules_config=rules_config,
        acknowledgements={"POS-001"},
    )
    repository.apply_position_transition(STRATEGY_ID, partial)
    persisted_partial = repository.load_position(POSITION_ID)
    assert persisted_partial.status is PaperPositionStatus.PARTIALLY_CLOSED
    assert persisted_partial.remaining_call_quantity == 50

    closed = close_result(persisted_partial, rules_config)
    repository.apply_position_transition(STRATEGY_ID, closed)
    persisted_closed = repository.load_position(POSITION_ID)
    assert persisted_closed.status is PaperPositionStatus.CLOSED
    assert persisted_closed.closed_at == CLOSED_AT


def test_position_snapshots_append(repository, strategy, calculation, rules_config):
    position = persist_open_position(repository, strategy, calculation)
    first = fresh_monitoring(position, rules_config)
    second = fresh_monitoring(
        position, rules_config, captured_at=CAPTURED_AT + timedelta(minutes=1)
    )
    repository.append_monitoring_result(STRATEGY_ID, first)
    repository.append_monitoring_result(STRATEGY_ID, second)

    snapshots = repository.load_position_snapshots(POSITION_ID)
    assert len(snapshots) == 2
    assert [item.captured_at for item in snapshots] == [
        CAPTURED_AT,
        CAPTURED_AT + timedelta(minutes=1),
    ]
    assert all(isinstance(item.total_pnl, Decimal) for item in snapshots)


def test_trade_event_api_is_append_only(repository, strategy):
    save_strategy(repository, strategy)
    repository.append_trade_event(
        strategy_id=STRATEGY_ID,
        event=TradeEvent(
            TradeEventType.NOTE_ADDED,
            APPROVED_AT,
            (("note", "reviewed"),),
        ),
    )
    events = repository.load_trade_events(strategy_id=STRATEGY_ID)
    assert [event.event_type for event in events] == [
        "STRATEGY_CREATED",
        "NOTE_ADDED",
    ]
    assert not hasattr(repository, "update_trade_event")
    assert not hasattr(repository, "delete_trade_event")


def test_entry_fields_cannot_be_changed(
    repository, strategy, calculation, rules_config
):
    position = persist_open_position(repository, strategy, calculation)
    forged_position = replace(position, entry_call_premium=Decimal("999"))
    forged = replace(
        close_result(position, rules_config),
        position=forged_position,
    )
    with pytest.raises(ValueError, match="entry fields are immutable"):
        repository.apply_position_transition(STRATEGY_ID, forged)
    assert repository.load_position(POSITION_ID).entry_call_premium == Decimal("180")


def test_closed_position_mutation_is_blocked(
    repository, strategy, calculation, rules_config
):
    position = persist_open_position(repository, strategy, calculation)
    closed = close_result(position, rules_config)
    repository.apply_position_transition(STRATEGY_ID, closed)
    with pytest.raises(ValueError, match="terminal"):
        repository.apply_position_transition(STRATEGY_ID, closed)


def test_open_and_event_are_atomic_on_event_failure(
    repository, session_factory, strategy, calculation, monkeypatch
):
    save_strategy(repository, strategy)
    opened = opened_result(strategy, calculation)
    monkeypatch.setattr(
        repository,
        "_insert_trade_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("event failed")),
    )
    with pytest.raises(RuntimeError, match="event failed"):
        repository.open_position(STRATEGY_ID, opened)
    with session_factory() as session:
        assert session.get(PaperPositionRecord, POSITION_ID) is None


def test_snapshot_and_event_are_atomic_on_event_failure(
    repository, session_factory, strategy, calculation, rules_config, monkeypatch
):
    position = persist_open_position(repository, strategy, calculation)
    result = fresh_monitoring(position, rules_config)
    monkeypatch.setattr(
        repository,
        "_insert_trade_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("event failed")),
    )
    with pytest.raises(RuntimeError, match="event failed"):
        repository.append_monitoring_result(STRATEGY_ID, result)
    with session_factory() as session:
        assert session.scalar(select(func.count(PositionSnapshotRecord.id))) == 0


def test_close_and_event_are_atomic_on_event_failure(
    repository, strategy, calculation, rules_config, monkeypatch
):
    position = persist_open_position(repository, strategy, calculation)
    closed = close_result(position, rules_config)
    monkeypatch.setattr(
        repository,
        "_insert_trade_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("event failed")),
    )
    with pytest.raises(RuntimeError, match="event failed"):
        repository.apply_position_transition(STRATEGY_ID, closed)
    assert repository.load_position(POSITION_ID).status is PaperPositionStatus.OPEN


def test_lifecycle_reconstructs_from_ordered_events_only(
    repository, strategy, calculation, rules_config
):
    position = persist_open_position(repository, strategy, calculation)
    repository.append_monitoring_result(
        STRATEGY_ID, fresh_monitoring(position, rules_config)
    )
    repository.apply_position_transition(
        STRATEGY_ID, close_result(position, rules_config)
    )

    events = repository.load_trade_events(strategy_id=STRATEGY_ID)
    assert [event.event_type for event in events] == [
        "STRATEGY_CREATED",
        "STRATEGY_VALIDATED",
        "PAPER_POSITION_OPENED",
        "MARKET_SNAPSHOT_APPLIED",
        "POSITION_CLOSED",
    ]
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    )


def test_utc_timestamps_are_normalized_and_preserved(repository, strategy):
    offset = timezone(timedelta(hours=5, minutes=30))
    local_time = datetime(2026, 8, 20, 14, 30, tzinfo=offset)
    save_strategy(repository, strategy, created_at=local_time)
    stored = repository.load_strategy(STRATEGY_ID)
    assert stored.created_at == CREATED_AT
    assert stored.created_at.tzinfo == UTC


def test_json_serialization_is_deterministic_and_typed():
    value = {
        "decimal": Decimal("1.2300"),
        "timestamp": CREATED_AT,
        "date": EXPIRY,
        "enum": TradeEventType.NOTE_ADDED,
    }
    encoded = serialize_json(value)
    assert encoded == serialize_json(value)
    decoded = deserialize_json(encoded)
    assert decoded == {
        "decimal": Decimal("1.2300"),
        "timestamp": CREATED_AT,
        "date": EXPIRY,
        "enum": "NOTE_ADDED",
    }


def test_pure_domain_modules_do_not_import_sqlalchemy():
    root = Path(__file__).resolve().parents[1] / "app" / "straddle"
    pure_modules = (
        "engine.py",
        "rules.py",
        "scenario.py",
        "strategy_service.py",
        "paper_trading.py",
        "monitoring.py",
    )
    for module in pure_modules:
        assert "sqlalchemy" not in (root / module).read_text().lower()


def test_existing_alert_table_works_in_isolated_database(session_factory):
    with session_factory() as session, session.begin():
        alert = Alert(
            ticker="NIFTY",
            target_price=24500.0,
            condition="above",
        )
        session.add(alert)
    with session_factory() as session:
        stored = session.scalar(select(Alert).where(Alert.ticker == "NIFTY"))
        assert stored.target_price == 24500.0
        assert session.scalar(select(func.count(TradeEventRecord.id))) == 0
