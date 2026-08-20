"""SQLAlchemy models and exact storage types for StraddleLab persistence."""

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, Column, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import TypeDecorator


class ExactDecimal(TypeDecorator):
    """Store arbitrary Decimal values as text without a float conversion."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError("ExactDecimal values must be Decimal")
        return str(value)

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator):
    """Store caller-supplied aware timestamps as canonical UTC ISO text."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("UTCDateTime values must be datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()

    def process_result_value(self, value, dialect):
        return None if value is None else datetime.fromisoformat(value)


StraddleBase = declarative_base()


class StrategyRecord(StraddleBase):
    __tablename__ = "straddle_strategy"

    id = Column(String, primary_key=True)
    underlying = Column(String, nullable=False)
    strategy_type = Column(String, nullable=False)
    status = Column(String, nullable=False)
    spot_price = Column(ExactDecimal, nullable=False)
    market_timestamp = Column(UTCDateTime, nullable=True)
    expiry = Column(Date, nullable=False)
    strike = Column(ExactDecimal, nullable=False)
    quantity = Column(Integer, nullable=False)
    created_at = Column(UTCDateTime, nullable=False)


class OptionLegRecord(StraddleBase):
    __tablename__ = "straddle_option_leg"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String, ForeignKey("straddle_strategy.id"), nullable=False, index=True)
    option_type = Column(String, nullable=False)
    side = Column(String, nullable=False)
    strike = Column(ExactDecimal, nullable=False)
    expiry = Column(Date, nullable=False)
    quantity = Column(Integer, nullable=False)
    entry_premium = Column(ExactDecimal, nullable=False)


class StrategyCalculationRecord(StraddleBase):
    __tablename__ = "straddle_strategy_calculation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String, ForeignKey("straddle_strategy.id"), nullable=False, index=True)
    combined_premium = Column(ExactDecimal, nullable=False)
    total_cost = Column(ExactDecimal, nullable=False)
    upper_break_even = Column(ExactDecimal, nullable=False)
    lower_break_even = Column(ExactDecimal, nullable=False)
    call_payoff = Column(ExactDecimal, nullable=False)
    put_payoff = Column(ExactDecimal, nullable=False)
    net_pnl = Column(ExactDecimal, nullable=False)
    maximum_loss = Column(ExactDecimal, nullable=False)
    required_move_points = Column(ExactDecimal, nullable=False)
    required_move_percent = Column(ExactDecimal, nullable=False)
    engine_version = Column(String, nullable=False)
    created_at = Column(UTCDateTime, nullable=False)


class RiskAssessmentRecord(StraddleBase):
    __tablename__ = "straddle_risk_assessment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String, ForeignKey("straddle_strategy.id"), nullable=False, index=True)
    rule_id = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    blocking = Column(Boolean, nullable=False)
    requires_acknowledgement = Column(Boolean, nullable=False)
    message = Column(Text, nullable=False)
    evidence = Column(Text, nullable=False)
    created_at = Column(UTCDateTime, nullable=False)


class StrategyApprovalRecord(StraddleBase):
    __tablename__ = "straddle_strategy_approval"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String, ForeignKey("straddle_strategy.id"), nullable=False, index=True)
    approved = Column(Boolean, nullable=False)
    status = Column(String, nullable=False)
    missing_acknowledgements = Column(Text, nullable=False)
    approved_at = Column(UTCDateTime, nullable=False)


class WarningAcknowledgementRecord(StraddleBase):
    __tablename__ = "straddle_warning_acknowledgement"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(String, ForeignKey("straddle_strategy.id"), nullable=False, index=True)
    rule_id = Column(String, nullable=False)
    acknowledged_at = Column(UTCDateTime, nullable=False)


class PaperPositionRecord(StraddleBase):
    __tablename__ = "straddle_paper_position"

    id = Column(String, primary_key=True)
    strategy_id = Column(String, ForeignKey("straddle_strategy.id"), nullable=False, index=True)
    status = Column(String, nullable=False)
    opened_at = Column(UTCDateTime, nullable=False)
    closed_at = Column(UTCDateTime, nullable=True)
    entry_call_premium = Column(ExactDecimal, nullable=False)
    entry_put_premium = Column(ExactDecimal, nullable=False)
    strike = Column(ExactDecimal, nullable=False)
    expiry = Column(Date, nullable=False)
    quantity = Column(Integer, nullable=False)
    remaining_call_quantity = Column(Integer, nullable=False)
    remaining_put_quantity = Column(Integer, nullable=False)
    realised_call_pnl = Column(ExactDecimal, nullable=False)
    realised_put_pnl = Column(ExactDecimal, nullable=False)
    realised_pnl = Column(ExactDecimal, nullable=False)
    exit_fees_paid = Column(ExactDecimal, nullable=False)
    exit_slippage_paid = Column(ExactDecimal, nullable=False)
    exit_reason = Column(Text, nullable=True)
    entry_fees = Column(ExactDecimal, nullable=False)
    entry_slippage = Column(ExactDecimal, nullable=False)


class PositionSnapshotRecord(StraddleBase):
    __tablename__ = "straddle_position_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(String, ForeignKey("straddle_paper_position.id"), nullable=False, index=True)
    call_price = Column(ExactDecimal, nullable=False)
    put_price = Column(ExactDecimal, nullable=False)
    spot_price = Column(ExactDecimal, nullable=False)
    call_current_value = Column(ExactDecimal, nullable=True)
    put_current_value = Column(ExactDecimal, nullable=True)
    combined_value = Column(ExactDecimal, nullable=False)
    unrealised_pnl = Column(ExactDecimal, nullable=False)
    realised_pnl = Column(ExactDecimal, nullable=False)
    total_pnl = Column(ExactDecimal, nullable=False)
    remaining_call_quantity = Column(Integer, nullable=True)
    remaining_put_quantity = Column(Integer, nullable=True)
    upper_break_even = Column(ExactDecimal, nullable=True)
    lower_break_even = Column(ExactDecimal, nullable=True)
    distance_to_upper_break_even = Column(ExactDecimal, nullable=True)
    distance_to_lower_break_even = Column(ExactDecimal, nullable=True)
    captured_at = Column(UTCDateTime, nullable=False)
    provider_timestamp = Column(UTCDateTime, nullable=True)
    received_at = Column(UTCDateTime, nullable=False)
    data_status = Column(String, nullable=False)
    used_last_known_good = Column(Boolean, nullable=False)
    net_delta = Column(ExactDecimal, nullable=True)
    net_gamma = Column(ExactDecimal, nullable=True)
    net_theta = Column(ExactDecimal, nullable=True)
    net_vega = Column(ExactDecimal, nullable=True)
    implied_volatility = Column(ExactDecimal, nullable=True)
    rule_results = Column(Text, nullable=False)


class TradeEventRecord(StraddleBase):
    __tablename__ = "straddle_trade_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(String, ForeignKey("straddle_paper_position.id"), nullable=True, index=True)
    strategy_id = Column(String, ForeignKey("straddle_strategy.id"), nullable=False, index=True)
    event_type = Column(String, nullable=False)
    payload = Column(Text, nullable=False)
    occurred_at = Column(UTCDateTime, nullable=False)
