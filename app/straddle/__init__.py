"""Pure domain and calculations for StraddleLab long straddles."""

from app.straddle.domain import (
    OptionLeg,
    OptionType,
    PositionSide,
    Strategy,
    StrategyCalculation,
)
from app.straddle.engine import calculate_long_straddle
from app.straddle.rules import (
    RULE_REGISTRY,
    MarketDataSnapshot,
    PositionAction,
    PositionActionInputs,
    PositionStatus,
    RiskInputs,
    RuleResult,
    RuleSeverity,
    RulesConfig,
    StrategyValidationInput,
    evaluate_market_data,
    evaluate_position_action,
    evaluate_risk,
    validate_strategy,
)
from app.straddle.version import STRADDLE_ENGINE_VERSION

__all__ = [
    "OptionLeg",
    "OptionType",
    "PositionSide",
    "Strategy",
    "StrategyCalculation",
    "STRADDLE_ENGINE_VERSION",
    "calculate_long_straddle",
    "RULE_REGISTRY",
    "MarketDataSnapshot",
    "PositionAction",
    "PositionActionInputs",
    "PositionStatus",
    "RiskInputs",
    "RuleResult",
    "RuleSeverity",
    "RulesConfig",
    "StrategyValidationInput",
    "evaluate_market_data",
    "evaluate_position_action",
    "evaluate_risk",
    "validate_strategy",
]
