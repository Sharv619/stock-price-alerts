"""Pure approval and paper-position lifecycle for long straddles."""

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, localcontext
from enum import Enum

from app.straddle.domain import Strategy
from app.straddle.rules import (
    PositionAction,
    PositionActionInputs,
    PositionStatus,
    RuleResult,
    RuleSeverity,
    RulesConfig,
    evaluate_position_action,
)
from app.straddle.strategy_service import StrategyConstructionResult, ValidationResult

ZERO = Decimal("0")
LIFECYCLE_DECIMAL_PRECISION = 28


class StrategyStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    READY_FOR_PAPER = "READY_FOR_PAPER"
    ARCHIVED = "ARCHIVED"


class PaperPositionStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"


class TradeEventType(str, Enum):
    STRATEGY_VALIDATED = "STRATEGY_VALIDATED"
    WARNING_ACKNOWLEDGED = "WARNING_ACKNOWLEDGED"
    PAPER_POSITION_OPENED = "PAPER_POSITION_OPENED"
    LEG_PARTIALLY_CLOSED = "LEG_PARTIALLY_CLOSED"
    POSITION_CLOSED = "POSITION_CLOSED"
    MARKET_SNAPSHOT_APPLIED = "MARKET_SNAPSHOT_APPLIED"


EventValue = str | int | bool | None


@dataclass(frozen=True, slots=True)
class TradeEvent:
    event_type: TradeEventType
    occurred_at: datetime
    payload: tuple[tuple[str, EventValue], ...]


@dataclass(frozen=True, slots=True)
class ApprovedStrategy:
    strategy: Strategy
    approved_at: datetime
    acknowledgements: frozenset[str]
    status: StrategyStatus


@dataclass(frozen=True, slots=True)
class StrategyApprovalResult:
    approved: bool
    status: StrategyStatus
    approved_strategy: ApprovedStrategy | None
    validation_results: tuple[ValidationResult, ...]
    missing_acknowledgements: tuple[str, ...]
    events: tuple[TradeEvent, ...]


class LifecycleIssueCode(str, Enum):
    APPROVAL_REQUIRED = "LIFE-001"
    EXIT_QUANTITY_REQUIRED = "LIFE-002"
    EXIT_PRICE_REQUIRED = "LIFE-003"


@dataclass(frozen=True, slots=True)
class LifecycleIssue:
    code: LifecycleIssueCode
    message: str
    blocking: bool = True
    severity: RuleSeverity = RuleSeverity.ERROR
    requires_acknowledgement: bool = False

    @property
    def rule_id(self) -> str:
        return self.code.value


@dataclass(frozen=True, slots=True)
class PaperPosition:
    position_id: str
    strategy: Strategy
    opened_at: datetime
    entry_call_premium: Decimal
    entry_put_premium: Decimal
    strike: Decimal
    expiry: date
    quantity: int
    fees_at_entry: Decimal
    slippage_at_entry: Decimal
    status: PaperPositionStatus
    remaining_call_quantity: int
    remaining_put_quantity: int
    realised_call_pnl: Decimal
    realised_put_pnl: Decimal
    exit_fees_paid: Decimal
    exit_slippage_paid: Decimal
    realised_pnl: Decimal
    closed_at: datetime | None
    exit_reason: str | None


@dataclass(frozen=True, slots=True)
class PaperPositionOpenResult:
    opened: bool
    position: PaperPosition | None
    issues: tuple[LifecycleIssue, ...]
    events: tuple[TradeEvent, ...]


PositionValidationResult = RuleResult | LifecycleIssue


@dataclass(frozen=True, slots=True)
class PaperPositionTransitionResult:
    completed: bool
    position: PaperPosition
    validation_results: tuple[PositionValidationResult, ...]
    events: tuple[TradeEvent, ...]


def _event(
    event_type: TradeEventType,
    occurred_at: datetime,
    **payload: EventValue,
) -> TradeEvent:
    return TradeEvent(
        event_type=event_type,
        occurred_at=occurred_at,
        payload=tuple(payload.items()),
    )


def _result_id(result: ValidationResult) -> str:
    return result.rule_id


def approve_strategy(
    construction_result: StrategyConstructionResult,
    *,
    acknowledgements: set[str] | frozenset[str],
    approved_at: datetime,
) -> StrategyApprovalResult:
    """Approve a non-blocking constructed strategy with required acknowledgements."""
    validation_results = construction_result.validation_results
    if (
        not construction_result.can_proceed
        or construction_result.strategy is None
        or any(
            result.blocking for result in validation_results
        )
    ):
        return StrategyApprovalResult(
            approved=False,
            status=StrategyStatus.DRAFT,
            approved_strategy=None,
            validation_results=validation_results,
            missing_acknowledgements=(),
            events=(),
        )

    required = tuple(
        sorted(
            _result_id(result)
            for result in validation_results
            if result.requires_acknowledgement
        )
    )
    provided = frozenset(acknowledgements)
    missing = tuple(rule_id for rule_id in required if rule_id not in provided)
    validated_event = _event(
        TradeEventType.STRATEGY_VALIDATED,
        approved_at,
        underlying=construction_result.underlying,
        strike=str(construction_result.selected_strike),
    )
    if missing:
        return StrategyApprovalResult(
            approved=False,
            status=StrategyStatus.VALIDATED,
            approved_strategy=None,
            validation_results=validation_results,
            missing_acknowledgements=missing,
            events=(validated_event,),
        )

    recognised = frozenset(rule_id for rule_id in required if rule_id in provided)
    acknowledgement_events = tuple(
        _event(
            TradeEventType.WARNING_ACKNOWLEDGED,
            approved_at,
            rule_id=rule_id,
        )
        for rule_id in sorted(recognised)
    )
    approved_strategy = ApprovedStrategy(
        strategy=construction_result.strategy,
        approved_at=approved_at,
        acknowledgements=recognised,
        status=StrategyStatus.READY_FOR_PAPER,
    )
    return StrategyApprovalResult(
        approved=True,
        status=StrategyStatus.READY_FOR_PAPER,
        approved_strategy=approved_strategy,
        validation_results=validation_results,
        missing_acknowledgements=(),
        events=(validated_event, *acknowledgement_events),
    )


def _require_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")


def _require_nonnegative_decimal(name: str, value: Decimal) -> None:
    _require_decimal(name, value)
    if value < ZERO:
        raise ValueError(f"{name} must be non-negative")


def open_paper_position(
    approval: StrategyApprovalResult,
    *,
    position_id: str,
    opened_at: datetime,
    fees_at_entry: Decimal = ZERO,
    slippage_at_entry: Decimal = ZERO,
) -> PaperPositionOpenResult:
    """Create an immutable paper entry snapshot from an approved strategy."""
    _require_nonnegative_decimal("fees_at_entry", fees_at_entry)
    _require_nonnegative_decimal("slippage_at_entry", slippage_at_entry)
    if not approval.approved or approval.approved_strategy is None:
        return PaperPositionOpenResult(
            opened=False,
            position=None,
            issues=(
                LifecycleIssue(
                    LifecycleIssueCode.APPROVAL_REQUIRED,
                    "A READY_FOR_PAPER approval is required before opening.",
                ),
            ),
            events=(),
        )
    if approval.approved_strategy.status is not StrategyStatus.READY_FOR_PAPER:
        return PaperPositionOpenResult(
            opened=False,
            position=None,
            issues=(
                LifecycleIssue(
                    LifecycleIssueCode.APPROVAL_REQUIRED,
                    "A READY_FOR_PAPER approval is required before opening.",
                ),
            ),
            events=(),
        )
    if not position_id:
        raise ValueError("position_id must not be empty")

    strategy = approval.approved_strategy.strategy
    quantity = strategy.call_leg.quantity
    with localcontext() as context:
        context.prec = LIFECYCLE_DECIMAL_PRECISION
        initial_realised = -fees_at_entry - slippage_at_entry
    position = PaperPosition(
        position_id=position_id,
        strategy=strategy,
        opened_at=opened_at,
        entry_call_premium=strategy.call_leg.premium,
        entry_put_premium=strategy.put_leg.premium,
        strike=strategy.call_leg.strike,
        expiry=strategy.call_leg.expiry,
        quantity=quantity,
        fees_at_entry=fees_at_entry,
        slippage_at_entry=slippage_at_entry,
        status=PaperPositionStatus.OPEN,
        remaining_call_quantity=quantity,
        remaining_put_quantity=quantity,
        realised_call_pnl=ZERO,
        realised_put_pnl=ZERO,
        exit_fees_paid=ZERO,
        exit_slippage_paid=ZERO,
        realised_pnl=initial_realised,
        closed_at=None,
        exit_reason=None,
    )
    event = _event(
        TradeEventType.PAPER_POSITION_OPENED,
        opened_at,
        position_id=position_id,
        quantity=quantity,
    )
    return PaperPositionOpenResult(
        opened=True,
        position=position,
        issues=(),
        events=(event,),
    )


def _rules_status(status: PaperPositionStatus) -> PositionStatus:
    return (
        PositionStatus.CLOSED
        if status is PaperPositionStatus.CLOSED
        else PositionStatus.OPEN
    )


def _validate_quantity(name: str, quantity: int) -> None:
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        raise TypeError(f"{name} must be an integer")


def exit_paper_position(
    position: PaperPosition,
    *,
    call_quantity: int,
    put_quantity: int,
    call_exit_price: Decimal | None,
    put_exit_price: Decimal | None,
    occurred_at: datetime,
    exit_reason: str,
    rules_config: RulesConfig,
    acknowledgements: set[str] | frozenset[str] = frozenset(),
    exit_fees: Decimal = ZERO,
    exit_slippage: Decimal = ZERO,
) -> PaperPositionTransitionResult:
    """Return a new position after a deterministic simulated partial/full exit."""
    _validate_quantity("call_quantity", call_quantity)
    _validate_quantity("put_quantity", put_quantity)
    _require_nonnegative_decimal("exit_fees", exit_fees)
    _require_nonnegative_decimal("exit_slippage", exit_slippage)
    if not exit_reason:
        raise ValueError("exit_reason must not be empty")

    one_leg = (call_quantity > 0) != (put_quantity > 0)
    action = PositionAction.ONE_LEG_EXIT if one_leg else PositionAction.EXIT
    base_rules = evaluate_position_action(
        PositionActionInputs(
            action=action,
            status=_rules_status(position.status),
        ),
        config=rules_config,
    )
    if any(result.blocking for result in base_rules):
        return PaperPositionTransitionResult(False, position, base_rules, ())

    issues: list[LifecycleIssue] = []
    if call_quantity < 0 or put_quantity < 0 or (
        call_quantity == 0 and put_quantity == 0
    ):
        issues.append(
            LifecycleIssue(
                LifecycleIssueCode.EXIT_QUANTITY_REQUIRED,
                "At least one positive exit quantity is required.",
            )
        )
    if call_quantity > 0 and call_exit_price is None:
        issues.append(
            LifecycleIssue(
                LifecycleIssueCode.EXIT_PRICE_REQUIRED,
                "call_exit_price is required for a CALL exit.",
            )
        )
    if put_quantity > 0 and put_exit_price is None:
        issues.append(
            LifecycleIssue(
                LifecycleIssueCode.EXIT_PRICE_REQUIRED,
                "put_exit_price is required for a PUT exit.",
            )
        )
    if call_exit_price is not None:
        _require_nonnegative_decimal("call_exit_price", call_exit_price)
    if put_exit_price is not None:
        _require_nonnegative_decimal("put_exit_price", put_exit_price)
    if issues:
        return PaperPositionTransitionResult(
            False,
            position,
            (*base_rules, *issues),
            (),
        )

    quantity_rules: list[RuleResult] = []
    for requested, remaining in (
        (call_quantity, position.remaining_call_quantity),
        (put_quantity, position.remaining_put_quantity),
    ):
        if requested > 0:
            quantity_rules.extend(
                evaluate_position_action(
                    PositionActionInputs(
                        action=PositionAction.EXIT,
                        status=PositionStatus.OPEN,
                        open_quantity=remaining,
                        requested_quantity=requested,
                    ),
                    config=rules_config,
                )
            )
    rule_results = (*base_rules, *quantity_rules)
    if any(result.blocking for result in rule_results):
        return PaperPositionTransitionResult(False, position, rule_results, ())

    required_acknowledgements = {
        result.rule_id for result in rule_results if result.requires_acknowledgement
    }
    provided = frozenset(acknowledgements)
    if not required_acknowledgements.issubset(provided):
        return PaperPositionTransitionResult(False, position, rule_results, ())

    with localcontext() as context:
        context.prec = LIFECYCLE_DECIMAL_PRECISION
        call_delta = (
            (call_exit_price - position.entry_call_premium) * call_quantity
            if call_quantity
            else ZERO
        )
        put_delta = (
            (put_exit_price - position.entry_put_premium) * put_quantity
            if put_quantity
            else ZERO
        )
        realised_call = position.realised_call_pnl + call_delta
        realised_put = position.realised_put_pnl + put_delta
        exit_fees_paid = position.exit_fees_paid + exit_fees
        exit_slippage_paid = position.exit_slippage_paid + exit_slippage
        realised_pnl = (
            realised_call
            + realised_put
            - position.fees_at_entry
            - position.slippage_at_entry
            - exit_fees_paid
            - exit_slippage_paid
        )

    remaining_call = position.remaining_call_quantity - call_quantity
    remaining_put = position.remaining_put_quantity - put_quantity
    closed = remaining_call == 0 and remaining_put == 0
    new_status = (
        PaperPositionStatus.CLOSED
        if closed
        else PaperPositionStatus.PARTIALLY_CLOSED
    )
    new_position = replace(
        position,
        status=new_status,
        remaining_call_quantity=remaining_call,
        remaining_put_quantity=remaining_put,
        realised_call_pnl=realised_call,
        realised_put_pnl=realised_put,
        exit_fees_paid=exit_fees_paid,
        exit_slippage_paid=exit_slippage_paid,
        realised_pnl=realised_pnl,
        closed_at=occurred_at if closed else None,
        exit_reason=exit_reason if closed else None,
    )
    acknowledgement_events = tuple(
        _event(
            TradeEventType.WARNING_ACKNOWLEDGED,
            occurred_at,
            rule_id=rule_id,
        )
        for rule_id in sorted(required_acknowledgements)
    )
    if closed:
        transition_event = _event(
            TradeEventType.POSITION_CLOSED,
            occurred_at,
            position_id=position.position_id,
            exit_reason=exit_reason,
            realised_pnl=str(realised_pnl),
        )
    else:
        transition_event = _event(
            TradeEventType.LEG_PARTIALLY_CLOSED,
            occurred_at,
            position_id=position.position_id,
            call_quantity=call_quantity,
            put_quantity=put_quantity,
            call_realised_delta=str(call_delta),
            put_realised_delta=str(put_delta),
            realised_pnl=str(realised_pnl),
            exit_reason=exit_reason,
        )
    return PaperPositionTransitionResult(
        True,
        new_position,
        rule_results,
        (*acknowledgement_events, transition_event),
    )


def close_paper_position(
    position: PaperPosition,
    *,
    call_exit_price: Decimal,
    put_exit_price: Decimal,
    closed_at: datetime,
    exit_reason: str,
    rules_config: RulesConfig,
    acknowledgements: set[str] | frozenset[str] = frozenset(),
    exit_fees: Decimal = ZERO,
    exit_slippage: Decimal = ZERO,
) -> PaperPositionTransitionResult:
    """Close every remaining unit, respecting one-leg acknowledgement rules."""
    return exit_paper_position(
        position,
        call_quantity=position.remaining_call_quantity,
        put_quantity=position.remaining_put_quantity,
        call_exit_price=call_exit_price,
        put_exit_price=put_exit_price,
        occurred_at=closed_at,
        exit_reason=exit_reason,
        rules_config=rules_config,
        acknowledgements=acknowledgements,
        exit_fees=exit_fees,
        exit_slippage=exit_slippage,
    )
