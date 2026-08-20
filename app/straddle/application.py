"""Thin StraddleLab MVP orchestration over the pure domain services."""

import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping
from uuid import uuid4

from app.straddle.market_data import OptionsMarketDataError, OptionsMarketDataProvider
from app.straddle.monitoring import (
    PositionDataStatus,
    PositionMarketSnapshot,
    PositionMonitoringResult,
    PositionSnapshot,
    monitor_paper_position,
)
from app.straddle.paper_trading import (
    ApprovedStrategy,
    PaperPositionOpenResult,
    PaperPositionStatus,
    PaperPositionTransitionResult,
    StrategyApprovalResult,
    StrategyStatus,
    TradeEvent,
    TradeEventType,
    approve_strategy,
    exit_paper_position,
    open_paper_position,
)
from app.straddle.persistence import StraddleRepository, StoredPositionSnapshot
from app.straddle.rules import RuleResult, RulesConfig
from app.straddle.scenario import ScenarioSimulation, simulate_long_straddle
from app.straddle.strategy_service import (
    OptionChainSnapshot,
    StrategyConstructionResult,
    construct_long_straddle,
)

UTC = timezone.utc
ZERO = Decimal("0")

DEFAULT_RULES_CONFIG = RulesConfig(
    warning_stale_after=timedelta(seconds=30),
    hard_stale_after=timedelta(seconds=120),
    high_iv_ratio=Decimal("1.5"),
    high_theta_share=Decimal("0.10"),
    required_move_threshold=Decimal("2.0"),
    position_stale_after=timedelta(seconds=60),
)


class StraddleApplicationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class StraddleNotFoundError(StraddleApplicationError):
    pass


class StraddleConflictError(StraddleApplicationError):
    pass


class StraddleProviderUnavailableError(StraddleApplicationError):
    pass


@dataclass(frozen=True, slots=True)
class ConstructionOutcome:
    strategy_id: str | None
    snapshot: OptionChainSnapshot
    construction: StrategyConstructionResult


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    monitoring: PositionMonitoringResult
    provider_error: str | None


@dataclass(frozen=True, slots=True)
class TradeExport:
    filename: str
    media_type: str
    content: bytes


def to_primitive(value):
    """Convert domain/persistence values into deterministic JSON-safe values."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("exported timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: to_primitive(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_primitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [to_primitive(item) for item in sorted(value)]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported response value: {type(value).__name__}")


class StraddleApplicationService:
    """Paper-only application workflow; contains no payoff or P&L formulas."""

    def __init__(
        self,
        *,
        provider: OptionsMarketDataProvider,
        repository: StraddleRepository,
        rules_config: RulesConfig = DEFAULT_RULES_CONFIG,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ):
        self.provider = provider
        self.repository = repository
        self.rules_config = rules_config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda prefix: f"{prefix}-{uuid4().hex}")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("application clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _provider_error(exc: OptionsMarketDataError):
        raise StraddleProviderUnavailableError(
            "MARKET_DATA_UNAVAILABLE",
            str(exc),
        ) from exc

    def get_underlyings(self) -> tuple[str, ...]:
        try:
            return self.provider.get_underlyings()
        except OptionsMarketDataError as exc:
            self._provider_error(exc)

    def get_expiries(
        self, underlying: str, *, evaluation_date: date | None = None
    ) -> tuple[date, ...]:
        try:
            return self.provider.get_expiries(
                underlying, evaluation_date=evaluation_date
            )
        except OptionsMarketDataError as exc:
            self._provider_error(exc)

    def get_chain(self, underlying: str, expiry: date) -> OptionChainSnapshot:
        try:
            return self.provider.get_option_chain(underlying, expiry)
        except OptionsMarketDataError as exc:
            self._provider_error(exc)

    def construct(
        self,
        *,
        underlying: str,
        expiry: date,
        selected_strike: Decimal | None = None,
        strategy_id: str | None = None,
        evaluated_at: datetime | None = None,
        fees: Decimal = ZERO,
        slippage: Decimal = ZERO,
    ) -> ConstructionOutcome:
        evaluated_at = evaluated_at or self._now()
        snapshot = self.get_chain(underlying, expiry)
        construction = construct_long_straddle(
            snapshot,
            expiry=expiry,
            evaluation_time=evaluated_at,
            rules_config=self.rules_config,
            selected_strike=selected_strike,
            fees=fees,
            slippage=slippage,
        )
        persisted_id = None
        if construction.strategy is not None:
            persisted_id = strategy_id or self._id_factory("strategy")
            try:
                self.repository.save_strategy(
                    strategy_id=persisted_id,
                    strategy=construction.strategy,
                    status=StrategyStatus.DRAFT,
                    spot_price=construction.spot_price,
                    market_timestamp=snapshot.provider_timestamp,
                    created_at=evaluated_at,
                )
                if construction.calculation is not None:
                    self.repository.append_calculation(
                        persisted_id,
                        construction.calculation,
                        created_at=evaluated_at,
                    )
                self.repository.append_risk_assessments(
                    persisted_id,
                    construction.validation_results,
                    created_at=evaluated_at,
                )
            except ValueError as exc:
                raise StraddleConflictError("STRATEGY_CONFLICT", str(exc)) from exc
        return ConstructionOutcome(persisted_id, snapshot, construction)

    def _validation_results(self, strategy_id: str) -> tuple[RuleResult, ...]:
        return tuple(
            RuleResult(
                rule_id=item.rule_id,
                severity=item.severity,
                blocking=item.blocking,
                requires_acknowledgement=item.requires_acknowledgement,
                message=item.message,
                evidence=MappingProxyType(dict(item.evidence)),
            )
            for item in self.repository.load_risk_assessments(strategy_id)
        )

    def _construction_from_storage(
        self, strategy_id: str
    ) -> StrategyConstructionResult:
        stored = self.repository.load_strategy(strategy_id)
        if stored is None:
            raise StraddleNotFoundError("STRATEGY_NOT_FOUND", "Strategy not found")
        calculations = self.repository.load_calculations(strategy_id)
        validation = self._validation_results(strategy_id)
        calculation = calculations[-1] if calculations else None
        can_proceed = calculation is not None and not any(
            result.blocking for result in validation
        )
        strike = stored.strategy.call_leg.strike
        return StrategyConstructionResult(
            underlying=stored.strategy.call_leg.underlying,
            expiry=stored.strategy.call_leg.expiry,
            spot_price=stored.spot_price,
            recommended_atm_strike=strike,
            selected_strike=strike,
            strategy=stored.strategy,
            validation_results=validation,
            calculation=calculation,
            can_proceed=can_proceed,
        )

    def approve(
        self,
        strategy_id: str,
        *,
        acknowledgements: set[str] | frozenset[str],
        approved_at: datetime | None = None,
    ) -> StrategyApprovalResult:
        approved_at = approved_at or self._now()
        result = approve_strategy(
            self._construction_from_storage(strategy_id),
            acknowledgements=acknowledgements,
            approved_at=approved_at,
        )
        self.repository.save_approval(
            strategy_id, result, approved_at=approved_at
        )
        return result

    def simulate(
        self,
        strategy_id: str,
        *,
        scenario_prices: tuple[Decimal, ...],
        fees: Decimal = ZERO,
        slippage: Decimal = ZERO,
    ) -> ScenarioSimulation:
        stored = self.repository.load_strategy(strategy_id)
        if stored is None:
            raise StraddleNotFoundError("STRATEGY_NOT_FOUND", "Strategy not found")
        return simulate_long_straddle(
            stored.strategy,
            spot_price=stored.spot_price,
            scenario_prices=scenario_prices,
            fees=fees,
            slippage=slippage,
        )

    def _approved_strategy(self, strategy_id: str) -> StrategyApprovalResult:
        stored = self.repository.load_strategy(strategy_id)
        approvals = self.repository.load_approvals(strategy_id)
        if stored is None:
            raise StraddleNotFoundError("STRATEGY_NOT_FOUND", "Strategy not found")
        if not approvals or not approvals[-1].approved:
            raise StraddleConflictError(
                "APPROVAL_REQUIRED",
                "Strategy approval is required before opening a paper position",
            )
        approval = approvals[-1]
        acknowledgements = frozenset(
            item.rule_id
            for item in self.repository.load_acknowledgements(strategy_id)
        )
        approved = ApprovedStrategy(
            strategy=stored.strategy,
            approved_at=approval.approved_at,
            acknowledgements=acknowledgements,
            status=StrategyStatus.READY_FOR_PAPER,
        )
        return StrategyApprovalResult(
            approved=True,
            status=StrategyStatus.READY_FOR_PAPER,
            approved_strategy=approved,
            validation_results=self._validation_results(strategy_id),
            missing_acknowledgements=(),
            events=(),
        )

    def open_paper_position(
        self,
        strategy_id: str,
        *,
        position_id: str | None = None,
        opened_at: datetime | None = None,
        entry_fees: Decimal = ZERO,
        entry_slippage: Decimal = ZERO,
    ) -> PaperPositionOpenResult:
        result = open_paper_position(
            self._approved_strategy(strategy_id),
            position_id=position_id or self._id_factory("paper"),
            opened_at=opened_at or self._now(),
            fees_at_entry=entry_fees,
            slippage_at_entry=entry_slippage,
        )
        if not result.opened:
            raise StraddleConflictError(
                "PAPER_POSITION_NOT_OPENED", result.issues[0].message
            )
        self.repository.open_position(strategy_id, result)
        return result

    def list_positions(self):
        return self.repository.list_positions()

    def get_position(self, position_id: str):
        stored = self.repository.load_stored_position(position_id)
        if stored is None:
            raise StraddleNotFoundError("POSITION_NOT_FOUND", "Paper position not found")
        return stored

    @staticmethod
    def _last_known_good(
        stored: StoredPositionSnapshot | None,
    ) -> PositionSnapshot | None:
        if stored is None:
            return None
        required = (
            stored.call_current_value,
            stored.put_current_value,
            stored.remaining_call_quantity,
            stored.remaining_put_quantity,
            stored.upper_break_even,
            stored.lower_break_even,
            stored.distance_to_upper_break_even,
            stored.distance_to_lower_break_even,
        )
        if any(value is None for value in required):
            return None
        return PositionSnapshot(
            position_id=stored.position_id,
            captured_at=stored.captured_at,
            provider_timestamp=stored.provider_timestamp,
            received_at=stored.received_at,
            call_price=stored.call_price,
            put_price=stored.put_price,
            call_current_value=stored.call_current_value,
            put_current_value=stored.put_current_value,
            combined_value=stored.combined_value,
            unrealised_pnl=stored.unrealised_pnl,
            realised_pnl=stored.realised_pnl,
            total_pnl=stored.total_pnl,
            remaining_call_quantity=stored.remaining_call_quantity,
            remaining_put_quantity=stored.remaining_put_quantity,
            spot_price=stored.spot_price,
            upper_break_even=stored.upper_break_even,
            lower_break_even=stored.lower_break_even,
            distance_to_upper_break_even=stored.distance_to_upper_break_even,
            distance_to_lower_break_even=stored.distance_to_lower_break_even,
            net_delta=stored.net_delta,
            net_gamma=stored.net_gamma,
            net_theta=stored.net_theta,
            net_vega=stored.net_vega,
            implied_volatility=stored.implied_volatility,
            data_status=PositionDataStatus(stored.data_status),
            rule_results=(),
            used_last_known_good=stored.used_last_known_good,
        )

    def refresh_position(
        self,
        position_id: str,
        *,
        captured_at: datetime | None = None,
    ) -> RefreshOutcome:
        stored = self.get_position(position_id)
        position = stored.position
        captured_at = captured_at or self._now()
        provider_error = None
        try:
            chain = self.provider.get_option_chain(
                position.strategy.call_leg.underlying,
                position.expiry,
            )
            call = next(
                (
                    item
                    for item in chain.contracts
                    if item.strike == position.strike
                    and item.option_type.value == "CALL"
                ),
                None,
            )
            put = next(
                (
                    item
                    for item in chain.contracts
                    if item.strike == position.strike
                    and item.option_type.value == "PUT"
                ),
                None,
            )
            market = PositionMarketSnapshot(
                call_price=call.premium if call else None,
                put_price=put.premium if put else None,
                spot_price=chain.spot_price,
                provider_timestamp=chain.provider_timestamp,
                received_at=chain.received_at,
            )
        except OptionsMarketDataError as exc:
            provider_error = str(exc)
            market = PositionMarketSnapshot(
                call_price=None,
                put_price=None,
                spot_price=None,
                provider_timestamp=None,
                received_at=captured_at,
            )
        snapshots = self.repository.load_position_snapshots(position_id)
        last_known_good = self._last_known_good(snapshots[-1] if snapshots else None)
        result = monitor_paper_position(
            position,
            market,
            captured_at=captured_at,
            rules_config=self.rules_config,
            last_known_good=last_known_good,
        )
        if result.successful:
            self.repository.append_monitoring_result(stored.strategy_id, result)
        return RefreshOutcome(result, provider_error)

    def exit_position(
        self,
        position_id: str,
        *,
        call_quantity: int,
        put_quantity: int,
        call_exit_price: Decimal | None,
        put_exit_price: Decimal | None,
        exit_reason: str,
        acknowledgements: set[str] | frozenset[str] = frozenset(),
        exit_fees: Decimal = ZERO,
        exit_slippage: Decimal = ZERO,
        occurred_at: datetime | None = None,
    ) -> PaperPositionTransitionResult:
        stored = self.get_position(position_id)
        result = exit_paper_position(
            stored.position,
            call_quantity=call_quantity,
            put_quantity=put_quantity,
            call_exit_price=call_exit_price,
            put_exit_price=put_exit_price,
            occurred_at=occurred_at or self._now(),
            exit_reason=exit_reason,
            rules_config=self.rules_config,
            acknowledgements=acknowledgements,
            exit_fees=exit_fees,
            exit_slippage=exit_slippage,
        )
        if result.completed:
            self.repository.apply_position_transition(stored.strategy_id, result)
        return result

    def journal(self, position_id: str):
        stored = self.get_position(position_id)
        return self.repository.load_trade_events(
            strategy_id=stored.strategy_id,
            position_id=None,
        )

    def review(self, position_id: str) -> dict[str, object]:
        stored_position = self.get_position(position_id)
        strategy_id = stored_position.strategy_id
        strategy = self.repository.load_strategy(strategy_id)
        return {
            "strategy": strategy,
            "calculations": self.repository.load_calculations(strategy_id),
            "risk_assessments": self.repository.load_risk_assessments(strategy_id),
            "approvals": self.repository.load_approvals(strategy_id),
            "acknowledgements": self.repository.load_acknowledgements(strategy_id),
            "paper_position": stored_position.position,
            "snapshots": self.repository.load_position_snapshots(position_id),
            "journal": self.repository.load_trade_events(strategy_id=strategy_id),
        }

    def export_position(
        self,
        position_id: str,
        *,
        created_at: datetime | None = None,
    ) -> TradeExport:
        stored = self.get_position(position_id)
        created_at = created_at or self._now()
        self.repository.append_trade_event(
            strategy_id=stored.strategy_id,
            position_id=position_id,
            event=TradeEvent(
                event_type=TradeEventType.EXPORT_CREATED,
                occurred_at=created_at,
                payload=(("format", "json"), ("position_id", position_id)),
            ),
        )
        document = {
            "schema_version": "1.0",
            "paper_trading_only": True,
            "exported_at": created_at,
            **self.review(position_id),
        }
        content = json.dumps(
            to_primitive(document),
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        return TradeExport(
            filename=f"straddle-{position_id}.json",
            media_type="application/json",
            content=content,
        )

    def health(self) -> dict[str, object]:
        provider_status = (
            self.provider.status()
            if hasattr(self.provider, "status")
            else {"ready": False, "detail": "Provider status unavailable"}
        )
        return {
            "status": "ok",
            "paper_trading_only": True,
            "database_available": self.repository.database_available(),
            "provider": provider_status,
        }
