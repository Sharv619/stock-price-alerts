"""Transactional SQLAlchemy repository for durable StraddleLab history."""

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Iterable

from sqlalchemy import select

from app.straddle.domain import (
    OptionLeg,
    OptionType,
    PositionSide,
    Strategy,
    StrategyCalculation,
)
from app.straddle.models import (
    OptionLegRecord,
    PaperPositionRecord,
    PositionSnapshotRecord,
    RiskAssessmentRecord,
    StraddleBase,
    StrategyApprovalRecord,
    StrategyCalculationRecord,
    StrategyRecord,
    TradeEventRecord,
    WarningAcknowledgementRecord,
)
from app.straddle.monitoring import PositionMonitoringResult
from app.straddle.paper_trading import (
    PaperPosition,
    PaperPositionOpenResult,
    PaperPositionStatus,
    PaperPositionTransitionResult,
    StrategyApprovalResult,
    TradeEvent,
    TradeEventType,
)
from app.straddle.rules import RuleResult, RuleSeverity


def create_straddle_schema(engine) -> None:
    """Create only StraddleLab tables; existing app metadata is untouched."""
    StraddleBase.metadata.create_all(bind=engine)


def _jsonable(value):
    if isinstance(value, Decimal):
        return {"__type__": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("serialized datetimes must be timezone-aware")
        return {
            "__type__": "datetime",
            "value": value.astimezone(timezone.utc).isoformat(),
        }
    if isinstance(value, date):
        return {"__type__": "date", "value": value.isoformat()}
    if isinstance(value, Enum):
        return {"__type__": "enum", "value": value.value}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def serialize_json(value) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _object_hook(value):
    value_type = value.get("__type__")
    if value_type == "decimal":
        return Decimal(value["value"])
    if value_type == "datetime":
        return datetime.fromisoformat(value["value"])
    if value_type == "date":
        return date.fromisoformat(value["value"])
    if value_type == "enum":
        return value["value"]
    return value


def deserialize_json(value: str):
    return json.loads(value, object_hook=_object_hook)


@dataclass(frozen=True, slots=True)
class StoredStrategy:
    strategy_id: str
    strategy: Strategy
    status: str
    spot_price: Decimal
    market_timestamp: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredRiskAssessment:
    record_id: int
    strategy_id: str
    rule_id: str
    severity: RuleSeverity
    blocking: bool
    requires_acknowledgement: bool
    message: str
    evidence: object
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredAcknowledgement:
    record_id: int
    strategy_id: str
    rule_id: str
    acknowledged_at: datetime


@dataclass(frozen=True, slots=True)
class StoredApproval:
    record_id: int
    strategy_id: str
    approved: bool
    status: str
    missing_acknowledgements: tuple[str, ...]
    approved_at: datetime


@dataclass(frozen=True, slots=True)
class StoredPositionSnapshot:
    record_id: int
    position_id: str
    combined_value: Decimal
    unrealised_pnl: Decimal
    realised_pnl: Decimal
    total_pnl: Decimal
    captured_at: datetime
    data_status: str
    used_last_known_good: bool


@dataclass(frozen=True, slots=True)
class StoredTradeEvent:
    sequence: int
    strategy_id: str
    position_id: str | None
    event_type: str
    payload: object
    occurred_at: datetime


class StraddleRepository:
    """Focused append/history APIs plus guarded mutable position state."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def _insert_trade_event(
        self,
        session,
        *,
        strategy_id: str,
        event: TradeEvent,
        position_id: str | None,
    ) -> TradeEventRecord:
        record = TradeEventRecord(
            strategy_id=strategy_id,
            position_id=position_id,
            event_type=event.event_type.value,
            payload=serialize_json(event.payload),
            occurred_at=event.occurred_at,
        )
        session.add(record)
        return record

    def save_strategy(
        self,
        *,
        strategy_id: str,
        strategy: Strategy,
        status: str | Enum,
        spot_price: Decimal,
        market_timestamp: datetime | None,
        created_at: datetime,
    ) -> None:
        status_value = status.value if isinstance(status, Enum) else status
        with self._session_factory() as session, session.begin():
            if session.get(StrategyRecord, strategy_id) is not None:
                raise ValueError(f"strategy already exists: {strategy_id}")
            call, put = strategy.call_leg, strategy.put_leg
            session.add(
                StrategyRecord(
                    id=strategy_id,
                    underlying=call.underlying,
                    strategy_type="LONG_STRADDLE",
                    status=status_value,
                    spot_price=spot_price,
                    market_timestamp=market_timestamp,
                    expiry=call.expiry,
                    strike=call.strike,
                    quantity=call.quantity,
                    created_at=created_at,
                )
            )
            session.add_all(
                [
                    OptionLegRecord(
                        strategy_id=strategy_id,
                        option_type=leg.option_type.value,
                        side=leg.side.value,
                        strike=leg.strike,
                        expiry=leg.expiry,
                        quantity=leg.quantity,
                        entry_premium=leg.premium,
                    )
                    for leg in (call, put)
                ]
            )
            self._insert_trade_event(
                session,
                strategy_id=strategy_id,
                position_id=None,
                event=TradeEvent(
                    TradeEventType.STRATEGY_CREATED,
                    created_at,
                    (("strategy_id", strategy_id),),
                ),
            )

    def append_calculation(
        self,
        strategy_id: str,
        calculation: StrategyCalculation,
        *,
        created_at: datetime,
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.add(
                StrategyCalculationRecord(
                    strategy_id=strategy_id,
                    combined_premium=calculation.combined_premium,
                    total_cost=calculation.total_cost,
                    upper_break_even=calculation.upper_break_even,
                    lower_break_even=calculation.lower_break_even,
                    call_payoff=calculation.call_payoff,
                    put_payoff=calculation.put_payoff,
                    net_pnl=calculation.net_pnl,
                    maximum_loss=calculation.maximum_loss,
                    required_move_points=calculation.required_move_points,
                    required_move_percent=calculation.required_move_percent,
                    engine_version=calculation.engine_version,
                    created_at=created_at,
                )
            )

    def append_risk_assessments(
        self,
        strategy_id: str,
        results: Iterable[RuleResult],
        *,
        created_at: datetime,
    ) -> None:
        with self._session_factory() as session, session.begin():
            for result in results:
                session.add(
                    RiskAssessmentRecord(
                        strategy_id=strategy_id,
                        rule_id=result.rule_id,
                        severity=result.severity.value,
                        blocking=result.blocking,
                        requires_acknowledgement=result.requires_acknowledgement,
                        message=result.message,
                        evidence=serialize_json(result.evidence),
                        created_at=created_at,
                    )
                )
                if result.severity is RuleSeverity.WARNING:
                    self._insert_trade_event(
                        session,
                        strategy_id=strategy_id,
                        position_id=None,
                        event=TradeEvent(
                            TradeEventType.WARNING_RAISED,
                            created_at,
                            (("rule_id", result.rule_id),),
                        ),
                    )

    def save_approval(
        self,
        strategy_id: str,
        approval: StrategyApprovalResult,
        *,
        approved_at: datetime,
    ) -> None:
        with self._session_factory() as session, session.begin():
            strategy = session.get(StrategyRecord, strategy_id)
            if strategy is None:
                raise KeyError(strategy_id)
            session.add(
                StrategyApprovalRecord(
                    strategy_id=strategy_id,
                    approved=approval.approved,
                    status=approval.status.value,
                    missing_acknowledgements=serialize_json(
                        approval.missing_acknowledgements
                    ),
                    approved_at=approved_at,
                )
            )
            strategy.status = approval.status.value
            acknowledgements = (
                approval.approved_strategy.acknowledgements
                if approval.approved_strategy is not None
                else frozenset()
            )
            for rule_id in sorted(acknowledgements):
                session.add(
                    WarningAcknowledgementRecord(
                        strategy_id=strategy_id,
                        rule_id=rule_id,
                        acknowledged_at=approved_at,
                    )
                )
            for event in approval.events:
                self._insert_trade_event(
                    session,
                    strategy_id=strategy_id,
                    position_id=None,
                    event=event,
                )

    def open_position(
        self,
        strategy_id: str,
        result: PaperPositionOpenResult,
    ) -> None:
        if not result.opened or result.position is None:
            raise ValueError("successful paper-position open result required")
        position = result.position
        with self._session_factory() as session, session.begin():
            stored_strategy = self._strategy_from_session(session, strategy_id)
            if stored_strategy is None:
                raise KeyError(strategy_id)
            if stored_strategy.strategy != position.strategy:
                raise ValueError("position strategy does not match persisted strategy")
            if session.get(PaperPositionRecord, position.position_id) is not None:
                raise ValueError(f"position already exists: {position.position_id}")
            session.add(self._position_record(strategy_id, position))
            for event in result.events:
                self._insert_trade_event(
                    session,
                    strategy_id=strategy_id,
                    position_id=position.position_id,
                    event=event,
                )

    @staticmethod
    def _position_record(
        strategy_id: str, position: PaperPosition
    ) -> PaperPositionRecord:
        return PaperPositionRecord(
            id=position.position_id,
            strategy_id=strategy_id,
            status=position.status.value,
            opened_at=position.opened_at,
            closed_at=position.closed_at,
            entry_call_premium=position.entry_call_premium,
            entry_put_premium=position.entry_put_premium,
            strike=position.strike,
            expiry=position.expiry,
            quantity=position.quantity,
            remaining_call_quantity=position.remaining_call_quantity,
            remaining_put_quantity=position.remaining_put_quantity,
            realised_call_pnl=position.realised_call_pnl,
            realised_put_pnl=position.realised_put_pnl,
            realised_pnl=position.realised_pnl,
            exit_fees_paid=position.exit_fees_paid,
            exit_slippage_paid=position.exit_slippage_paid,
            exit_reason=position.exit_reason,
            entry_fees=position.fees_at_entry,
            entry_slippage=position.slippage_at_entry,
        )

    def apply_position_transition(
        self,
        strategy_id: str,
        result: PaperPositionTransitionResult,
    ) -> None:
        if not result.completed:
            raise ValueError("completed position transition required")
        position = result.position
        with self._session_factory() as session, session.begin():
            record = session.get(PaperPositionRecord, position.position_id)
            if record is None:
                raise KeyError(position.position_id)
            if record.strategy_id != strategy_id:
                raise ValueError("position does not belong to strategy")
            if record.status == PaperPositionStatus.CLOSED.value:
                raise ValueError("closed positions are terminal")
            self._assert_entry_unchanged(record, position)
            record.status = position.status.value
            record.closed_at = position.closed_at
            record.remaining_call_quantity = position.remaining_call_quantity
            record.remaining_put_quantity = position.remaining_put_quantity
            record.realised_call_pnl = position.realised_call_pnl
            record.realised_put_pnl = position.realised_put_pnl
            record.realised_pnl = position.realised_pnl
            record.exit_fees_paid = position.exit_fees_paid
            record.exit_slippage_paid = position.exit_slippage_paid
            record.exit_reason = position.exit_reason
            for event in result.events:
                self._insert_trade_event(
                    session,
                    strategy_id=strategy_id,
                    position_id=position.position_id,
                    event=event,
                )

    @staticmethod
    def _assert_entry_unchanged(record, position: PaperPosition) -> None:
        immutable = (
            (record.opened_at, position.opened_at),
            (record.entry_call_premium, position.entry_call_premium),
            (record.entry_put_premium, position.entry_put_premium),
            (record.strike, position.strike),
            (record.expiry, position.expiry),
            (record.quantity, position.quantity),
            (record.entry_fees, position.fees_at_entry),
            (record.entry_slippage, position.slippage_at_entry),
        )
        if any(stored != supplied for stored, supplied in immutable):
            raise ValueError("paper-position entry fields are immutable")

    def append_monitoring_result(
        self,
        strategy_id: str,
        result: PositionMonitoringResult,
    ) -> None:
        if not result.successful or result.snapshot is None:
            raise ValueError("successful monitoring result required")
        snapshot = result.snapshot
        with self._session_factory() as session, session.begin():
            position = session.get(PaperPositionRecord, snapshot.position_id)
            if position is None:
                raise KeyError(snapshot.position_id)
            if position.strategy_id != strategy_id:
                raise ValueError("position does not belong to strategy")
            session.add(
                PositionSnapshotRecord(
                    position_id=snapshot.position_id,
                    call_price=snapshot.call_price,
                    put_price=snapshot.put_price,
                    spot_price=snapshot.spot_price,
                    combined_value=snapshot.combined_value,
                    unrealised_pnl=snapshot.unrealised_pnl,
                    realised_pnl=snapshot.realised_pnl,
                    total_pnl=snapshot.total_pnl,
                    captured_at=snapshot.captured_at,
                    provider_timestamp=snapshot.provider_timestamp,
                    received_at=snapshot.received_at,
                    data_status=snapshot.data_status.value,
                    used_last_known_good=snapshot.used_last_known_good,
                    net_delta=snapshot.net_delta,
                    net_gamma=snapshot.net_gamma,
                    net_theta=snapshot.net_theta,
                    net_vega=snapshot.net_vega,
                    implied_volatility=snapshot.implied_volatility,
                    rule_results=serialize_json(
                        tuple(rule.rule_id for rule in snapshot.rule_results)
                    ),
                )
            )
            for event in result.events:
                self._insert_trade_event(
                    session,
                    strategy_id=strategy_id,
                    position_id=snapshot.position_id,
                    event=event,
                )

    def append_trade_event(
        self,
        *,
        strategy_id: str,
        event: TradeEvent,
        position_id: str | None = None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            self._insert_trade_event(
                session,
                strategy_id=strategy_id,
                position_id=position_id,
                event=event,
            )

    def _strategy_from_session(self, session, strategy_id: str) -> StoredStrategy | None:
        record = session.get(StrategyRecord, strategy_id)
        if record is None:
            return None
        legs = session.scalars(
            select(OptionLegRecord)
            .where(OptionLegRecord.strategy_id == strategy_id)
            .order_by(OptionLegRecord.id)
        ).all()
        by_type = {leg.option_type: leg for leg in legs}

        def domain_leg(option_type: OptionType) -> OptionLeg:
            leg = by_type[option_type.value]
            return OptionLeg(
                underlying=record.underlying,
                option_type=option_type,
                side=PositionSide(leg.side),
                strike=leg.strike,
                expiry=leg.expiry,
                premium=leg.entry_premium,
                quantity=leg.quantity,
            )

        return StoredStrategy(
            strategy_id=record.id,
            strategy=Strategy(
                call_leg=domain_leg(OptionType.CALL),
                put_leg=domain_leg(OptionType.PUT),
            ),
            status=record.status,
            spot_price=record.spot_price,
            market_timestamp=record.market_timestamp,
            created_at=record.created_at,
        )

    def load_strategy(self, strategy_id: str) -> StoredStrategy | None:
        with self._session_factory() as session:
            return self._strategy_from_session(session, strategy_id)

    def load_position(self, position_id: str) -> PaperPosition | None:
        with self._session_factory() as session:
            record = session.get(PaperPositionRecord, position_id)
            if record is None:
                return None
            stored_strategy = self._strategy_from_session(session, record.strategy_id)
            return PaperPosition(
                position_id=record.id,
                strategy=stored_strategy.strategy,
                opened_at=record.opened_at,
                entry_call_premium=record.entry_call_premium,
                entry_put_premium=record.entry_put_premium,
                strike=record.strike,
                expiry=record.expiry,
                quantity=record.quantity,
                fees_at_entry=record.entry_fees,
                slippage_at_entry=record.entry_slippage,
                status=PaperPositionStatus(record.status),
                remaining_call_quantity=record.remaining_call_quantity,
                remaining_put_quantity=record.remaining_put_quantity,
                realised_call_pnl=record.realised_call_pnl,
                realised_put_pnl=record.realised_put_pnl,
                exit_fees_paid=record.exit_fees_paid,
                exit_slippage_paid=record.exit_slippage_paid,
                realised_pnl=record.realised_pnl,
                closed_at=record.closed_at,
                exit_reason=record.exit_reason,
            )

    def load_calculations(self, strategy_id: str) -> tuple[StrategyCalculation, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(StrategyCalculationRecord)
                .where(StrategyCalculationRecord.strategy_id == strategy_id)
                .order_by(StrategyCalculationRecord.id)
            ).all()
            return tuple(
                StrategyCalculation(
                    engine_version=record.engine_version,
                    combined_premium=record.combined_premium,
                    total_cost=record.total_cost,
                    upper_break_even=record.upper_break_even,
                    lower_break_even=record.lower_break_even,
                    call_payoff=record.call_payoff,
                    put_payoff=record.put_payoff,
                    net_pnl=record.net_pnl,
                    maximum_loss=record.maximum_loss,
                    required_move_points=record.required_move_points,
                    required_move_percent=record.required_move_percent,
                )
                for record in records
            )

    def load_risk_assessments(
        self, strategy_id: str
    ) -> tuple[StoredRiskAssessment, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(RiskAssessmentRecord)
                .where(RiskAssessmentRecord.strategy_id == strategy_id)
                .order_by(RiskAssessmentRecord.id)
            ).all()
            return tuple(
                StoredRiskAssessment(
                    record_id=record.id,
                    strategy_id=record.strategy_id,
                    rule_id=record.rule_id,
                    severity=RuleSeverity(record.severity),
                    blocking=record.blocking,
                    requires_acknowledgement=record.requires_acknowledgement,
                    message=record.message,
                    evidence=deserialize_json(record.evidence),
                    created_at=record.created_at,
                )
                for record in records
            )

    def load_acknowledgements(
        self, strategy_id: str
    ) -> tuple[StoredAcknowledgement, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(WarningAcknowledgementRecord)
                .where(WarningAcknowledgementRecord.strategy_id == strategy_id)
                .order_by(WarningAcknowledgementRecord.id)
            ).all()
            return tuple(
                StoredAcknowledgement(
                    record.id,
                    record.strategy_id,
                    record.rule_id,
                    record.acknowledged_at,
                )
                for record in records
            )

    def load_approvals(self, strategy_id: str) -> tuple[StoredApproval, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(StrategyApprovalRecord)
                .where(StrategyApprovalRecord.strategy_id == strategy_id)
                .order_by(StrategyApprovalRecord.id)
            ).all()
            return tuple(
                StoredApproval(
                    record_id=record.id,
                    strategy_id=record.strategy_id,
                    approved=record.approved,
                    status=record.status,
                    missing_acknowledgements=tuple(
                        deserialize_json(record.missing_acknowledgements)
                    ),
                    approved_at=record.approved_at,
                )
                for record in records
            )

    def load_position_snapshots(
        self, position_id: str
    ) -> tuple[StoredPositionSnapshot, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(PositionSnapshotRecord)
                .where(PositionSnapshotRecord.position_id == position_id)
                .order_by(PositionSnapshotRecord.id)
            ).all()
            return tuple(
                StoredPositionSnapshot(
                    record.id,
                    record.position_id,
                    record.combined_value,
                    record.unrealised_pnl,
                    record.realised_pnl,
                    record.total_pnl,
                    record.captured_at,
                    record.data_status,
                    record.used_last_known_good,
                )
                for record in records
            )

    def load_trade_events(
        self,
        *,
        strategy_id: str,
        position_id: str | None = None,
    ) -> tuple[StoredTradeEvent, ...]:
        with self._session_factory() as session:
            statement = select(TradeEventRecord).where(
                TradeEventRecord.strategy_id == strategy_id
            )
            if position_id is not None:
                statement = statement.where(
                    TradeEventRecord.position_id == position_id
                )
            records = session.scalars(statement.order_by(TradeEventRecord.id)).all()
            return tuple(
                StoredTradeEvent(
                    sequence=record.id,
                    strategy_id=record.strategy_id,
                    position_id=record.position_id,
                    event_type=record.event_type,
                    payload=deserialize_json(record.payload),
                    occurred_at=record.occurred_at,
                )
                for record in records
            )
