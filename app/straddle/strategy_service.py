"""Provider-neutral orchestration for constructing long straddles."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping, TypeAlias

from app.straddle.domain import (
    OptionLeg,
    OptionType,
    PositionSide,
    Strategy,
    StrategyCalculation,
)
from app.straddle.engine import calculate_long_straddle
from app.straddle.rules import (
    MarketDataSnapshot,
    RuleResult,
    RuleSeverity,
    RulesConfig,
    StrategyValidationInput,
    evaluate_market_data,
    validate_strategy,
)

ZERO = Decimal("0")


class ConstructionIssueCode(str, Enum):
    NO_AVAILABLE_STRIKES = "CON-001"
    SELECTED_STRIKE_UNAVAILABLE = "CON-002"
    DUPLICATE_CALL = "CON-003"
    DUPLICATE_PUT = "CON-004"


@dataclass(frozen=True, slots=True)
class ConstructionIssue:
    code: ConstructionIssueCode
    severity: RuleSeverity
    blocking: bool
    message: str
    evidence: Mapping[str, object]
    requires_acknowledgement: bool = False

    @property
    def rule_id(self) -> str:
        """Uniform identifier access alongside Phase 2 RuleResult objects."""
        return self.code.value


ValidationResult: TypeAlias = RuleResult | ConstructionIssue


@dataclass(frozen=True, slots=True)
class OptionContract:
    underlying: str
    option_type: OptionType
    strike: Decimal
    expiry: date
    premium: Decimal | None
    quantity: int

    def __post_init__(self):
        if not self.underlying:
            raise ValueError("underlying must not be empty")
        if not isinstance(self.strike, Decimal):
            raise TypeError("strike must be a Decimal")
        if self.premium is not None and not isinstance(self.premium, Decimal):
            raise TypeError("premium must be a Decimal or None")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool) or self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    underlying: str
    spot_price: Decimal
    provider_timestamp: datetime | None
    received_at: datetime
    contracts: tuple[OptionContract, ...]

    def __post_init__(self):
        if not self.underlying:
            raise ValueError("underlying must not be empty")
        if not isinstance(self.spot_price, Decimal):
            raise TypeError("spot_price must be a Decimal")
        if self.spot_price <= ZERO:
            raise ValueError("spot_price must be positive")
        if not isinstance(self.contracts, tuple):
            raise TypeError("contracts must be a tuple")


@dataclass(frozen=True, slots=True)
class StrategyConstructionResult:
    underlying: str
    expiry: date
    spot_price: Decimal
    recommended_atm_strike: Decimal | None
    selected_strike: Decimal | None
    strategy: Strategy | None
    validation_results: tuple[ValidationResult, ...]
    calculation: StrategyCalculation | None
    can_proceed: bool


def recommend_atm_strike(
    spot_price: Decimal,
    available_strikes: Iterable[Decimal],
) -> Decimal:
    """Return the supplied strike nearest spot, preferring lower on ties."""
    if not isinstance(spot_price, Decimal):
        raise TypeError("spot_price must be a Decimal")
    strikes = tuple(set(available_strikes))
    if not strikes:
        raise ValueError("available_strikes must not be empty")
    if any(not isinstance(strike, Decimal) for strike in strikes):
        raise TypeError("available strikes must be Decimals")
    return min(strikes, key=lambda strike: (abs(strike - spot_price), strike))


def _issue(
    code: ConstructionIssueCode,
    message: str,
    **evidence: object,
) -> ConstructionIssue:
    return ConstructionIssue(
        code=code,
        severity=RuleSeverity.ERROR,
        blocking=True,
        message=message,
        evidence=MappingProxyType(dict(evidence)),
    )


def _to_leg(contract: OptionContract | None) -> OptionLeg | None:
    if contract is None or contract.premium is None:
        return None
    return OptionLeg(
        underlying=contract.underlying,
        option_type=contract.option_type,
        side=PositionSide.BUY,
        strike=contract.strike,
        expiry=contract.expiry,
        premium=contract.premium,
        quantity=contract.quantity,
    )


def construct_long_straddle(
    snapshot: OptionChainSnapshot,
    *,
    expiry: date,
    evaluation_time: datetime,
    rules_config: RulesConfig,
    selected_strike: Decimal | None = None,
    expiry_spot: Decimal | None = None,
    fees: Decimal = ZERO,
    slippage: Decimal = ZERO,
) -> StrategyConstructionResult:
    """Select contracts, validate the draft, and calculate when non-blocking."""
    matching_scope = tuple(
        contract
        for contract in snapshot.contracts
        if contract.underlying == snapshot.underlying and contract.expiry == expiry
    )
    available_strikes = tuple(sorted({contract.strike for contract in matching_scope}))
    issues: list[ConstructionIssue] = []

    if not available_strikes:
        issues.append(
            _issue(
                ConstructionIssueCode.NO_AVAILABLE_STRIKES,
                "No option strikes are available for the selected underlying and expiry.",
                underlying=snapshot.underlying,
                expiry=expiry,
            )
        )
        return StrategyConstructionResult(
            underlying=snapshot.underlying,
            expiry=expiry,
            spot_price=snapshot.spot_price,
            recommended_atm_strike=None,
            selected_strike=selected_strike,
            strategy=None,
            validation_results=tuple(issues),
            calculation=None,
            can_proceed=False,
        )

    recommended = recommend_atm_strike(snapshot.spot_price, available_strikes)
    chosen = recommended if selected_strike is None else selected_strike
    if not isinstance(chosen, Decimal):
        raise TypeError("selected_strike must be a Decimal or None")
    if chosen not in available_strikes:
        issues.append(
            _issue(
                ConstructionIssueCode.SELECTED_STRIKE_UNAVAILABLE,
                "Selected strike is not available for the chosen underlying and expiry.",
                selected_strike=chosen,
                available_strikes=available_strikes,
            )
        )
        return StrategyConstructionResult(
            underlying=snapshot.underlying,
            expiry=expiry,
            spot_price=snapshot.spot_price,
            recommended_atm_strike=recommended,
            selected_strike=chosen,
            strategy=None,
            validation_results=tuple(issues),
            calculation=None,
            can_proceed=False,
        )

    calls = tuple(
        contract
        for contract in matching_scope
        if contract.strike == chosen and contract.option_type is OptionType.CALL
    )
    puts = tuple(
        contract
        for contract in matching_scope
        if contract.strike == chosen and contract.option_type is OptionType.PUT
    )
    if len(calls) > 1:
        issues.append(
            _issue(
                ConstructionIssueCode.DUPLICATE_CALL,
                "Multiple CALL contracts match the selected long-straddle leg.",
                matching_contract_count=len(calls),
                strike=chosen,
            )
        )
    if len(puts) > 1:
        issues.append(
            _issue(
                ConstructionIssueCode.DUPLICATE_PUT,
                "Multiple PUT contracts match the selected long-straddle leg.",
                matching_contract_count=len(puts),
                strike=chosen,
            )
        )

    call_contract = calls[0] if len(calls) == 1 else None
    put_contract = puts[0] if len(puts) == 1 else None
    call_leg = _to_leg(call_contract)
    put_leg = _to_leg(put_contract)
    structural_results = validate_strategy(
        StrategyValidationInput(call_leg=call_leg, put_leg=put_leg),
        evaluation_date=evaluation_time.date(),
    )
    market_results = evaluate_market_data(
        MarketDataSnapshot(
            spot_price=snapshot.spot_price,
            call_premium=call_contract.premium if call_contract else None,
            put_premium=put_contract.premium if put_contract else None,
            provider_timestamp=snapshot.provider_timestamp,
            received_at=snapshot.received_at,
            greeks_available=False,
        ),
        evaluation_time=evaluation_time,
        config=rules_config,
    )
    validation_results: tuple[ValidationResult, ...] = (
        *issues,
        *structural_results,
        *market_results,
    )
    strategy = (
        Strategy(call_leg=call_leg, put_leg=put_leg)
        if call_leg is not None and put_leg is not None
        else None
    )
    can_proceed = strategy is not None and not any(
        result.blocking for result in validation_results
    )
    calculation = None
    if can_proceed:
        calculation = calculate_long_straddle(
            strategy,
            spot_price=snapshot.spot_price,
            expiry_spot=snapshot.spot_price if expiry_spot is None else expiry_spot,
            fees=fees,
            slippage=slippage,
        )

    return StrategyConstructionResult(
        underlying=snapshot.underlying,
        expiry=expiry,
        spot_price=snapshot.spot_price,
        recommended_atm_strike=recommended,
        selected_strike=chosen,
        strategy=strategy,
        validation_results=validation_results,
        calculation=calculation,
        can_proceed=can_proceed,
    )
