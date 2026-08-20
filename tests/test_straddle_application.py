import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.straddle.api import (
    ApprovalRequest,
    ConstructRequest,
    OpenPositionRequest,
    SimulationRequest,
    approve_strategy_route,
    construct_strategy as construct_strategy_route,
    export_position_route,
    open_position_route,
    simulate_strategy_route,
    straddle_health,
    underlyings,
)
from app.straddle.application import (
    StraddleApplicationService,
    StraddleProviderUnavailableError,
    to_primitive,
)
from app.straddle.domain import OptionType
from app.straddle.market_data import OptionQuoteError
from app.straddle.paper_trading import PaperPositionStatus
from app.straddle.persistence import StraddleRepository, create_straddle_schema
from app.straddle.rules import RULE_REGISTRY, RISK_IV_002, RuleResult
from app.straddle.strategy_service import OptionChainSnapshot, OptionContract

UTC = timezone.utc
EXPIRY = date(2026, 8, 27)
NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


def chain(
    *,
    call=Decimal("180"),
    put=Decimal("160"),
    spot=Decimal("24342"),
    provider_timestamp=NOW - timedelta(seconds=5),
):
    contracts = []
    premiums = {
        Decimal("24200"): (Decimal("245"), Decimal("105")),
        Decimal("24300"): (call, put),
        Decimal("24400"): (Decimal("120"), Decimal("220")),
    }
    for strike, (call_premium, put_premium) in premiums.items():
        contracts.extend(
            (
                OptionContract(
                    underlying="NIFTY",
                    option_type=OptionType.CALL,
                    strike=strike,
                    expiry=EXPIRY,
                    premium=call_premium,
                    quantity=75,
                ),
                OptionContract(
                    underlying="NIFTY",
                    option_type=OptionType.PUT,
                    strike=strike,
                    expiry=EXPIRY,
                    premium=put_premium,
                    quantity=75,
                ),
            )
        )
    return OptionChainSnapshot(
        underlying="NIFTY",
        spot_price=spot,
        provider_timestamp=provider_timestamp,
        received_at=NOW - timedelta(seconds=2),
        contracts=tuple(contracts),
    )


class FakeProvider:
    def __init__(self, snapshots=None, error=None):
        self.snapshots = list(snapshots or [chain()])
        self.error = error
        self.calls = []

    def get_underlyings(self):
        if self.error:
            raise self.error
        return ("NIFTY", "BANKNIFTY")

    def get_expiries(self, underlying, *, evaluation_date=None):
        if self.error:
            raise self.error
        return (EXPIRY,) if evaluation_date is None or EXPIRY >= evaluation_date else ()

    def get_option_chain(self, underlying, expiry):
        self.calls.append((underlying, expiry))
        if self.error:
            raise self.error
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    @staticmethod
    def status():
        return {
            "configured": True,
            "authenticated": True,
            "cache": {"available": True, "fresh": True},
            "ready": True,
        }


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mvp.db'}",
        connect_args={"check_same_thread": False},
    )
    create_straddle_schema(engine)
    return StraddleRepository(sessionmaker(bind=engine, expire_on_commit=False))


@pytest.fixture
def service(repository):
    counters = {"strategy": 0, "paper": 0}

    def identifier(prefix):
        counters[prefix] += 1
        return f"{prefix}-{counters[prefix]}"

    return StraddleApplicationService(
        provider=FakeProvider(),
        repository=repository,
        clock=lambda: NOW,
        id_factory=identifier,
    )


def construct(service, strategy_id="strategy-acceptance"):
    return service.construct(
        underlying="NIFTY",
        expiry=EXPIRY,
        strategy_id=strategy_id,
        evaluated_at=NOW,
    )


def approve_and_open(service, strategy_id="strategy-acceptance", position_id="paper-acceptance"):
    service.approve(strategy_id, acknowledgements=set(), approved_at=NOW)
    return service.open_paper_position(
        strategy_id,
        position_id=position_id,
        opened_at=NOW,
    ).position


def test_provider_discovery_and_unavailable_behavior(repository):
    unavailable = StraddleApplicationService(
        provider=FakeProvider(error=OptionQuoteError("quotes unavailable")),
        repository=repository,
        clock=lambda: NOW,
    )
    with pytest.raises(StraddleProviderUnavailableError) as exc:
        unavailable.get_underlyings()
    assert exc.value.code == "MARKET_DATA_UNAVAILABLE"
    assert "quotes unavailable" in exc.value.message


def test_construct_validate_and_calculate_acceptance(service):
    result = construct(service)
    calculation = result.construction.calculation
    assert result.strategy_id == "strategy-acceptance"
    assert result.construction.recommended_atm_strike == Decimal("24300")
    assert result.construction.can_proceed is True
    assert calculation.combined_premium == Decimal("340")
    assert calculation.total_cost == Decimal("25500")
    assert calculation.upper_break_even == Decimal("24640")
    assert calculation.lower_break_even == Decimal("23960")


def test_missing_premium_blocks_construction_without_fabrication(repository):
    service = StraddleApplicationService(
        provider=FakeProvider([chain(call=None)]),
        repository=repository,
        clock=lambda: NOW,
    )
    result = service.construct(underlying="NIFTY", expiry=EXPIRY)
    assert result.strategy_id is None
    assert result.construction.can_proceed is False
    assert {item.rule_id for item in result.construction.validation_results} >= {
        "STR-008",
        "DATA-004",
    }


def test_warning_stale_construction_persists_and_serializes_evidence(repository):
    service = StraddleApplicationService(
        provider=FakeProvider(
            [chain(provider_timestamp=NOW - timedelta(seconds=45))]
        ),
        repository=repository,
        clock=lambda: NOW,
    )
    result = service.construct(
        underlying="NIFTY",
        expiry=EXPIRY,
        strategy_id="strategy-stale",
        evaluated_at=NOW,
    )
    assert result.construction.can_proceed is True
    assert "DATA-002" in {
        item.rule_id for item in result.construction.validation_results
    }
    evidence = repository.load_risk_assessments("strategy-stale")[-2].evidence
    assert evidence["snapshot_age"] == timedelta(seconds=45)
    assert to_primitive(result)["construction"]["validation_results"]


def test_hard_stale_validation_blocks_api_approval(repository):
    service = StraddleApplicationService(
        provider=FakeProvider(
            [chain(provider_timestamp=NOW - timedelta(seconds=121))]
        ),
        repository=repository,
        clock=lambda: NOW,
    )
    outcome = service.construct(
        underlying="NIFTY",
        expiry=EXPIRY,
        strategy_id="strategy-hard-stale",
        evaluated_at=NOW,
    )
    assert outcome.construction.can_proceed is False
    assert "DATA-001" in {
        item.rule_id for item in outcome.construction.validation_results
    }
    response = approve_strategy_route(
        "strategy-hard-stale",
        ApprovalRequest(acknowledgements=set()),
        service=service,
    )
    assert response.status_code == 409
    assert response_json(response)["code"] == "STRATEGY_BLOCKED"


def test_expiry_simulation_acceptance(service):
    construct(service)
    simulation = service.simulate(
        "strategy-acceptance",
        scenario_prices=(
            Decimal("23960"),
            Decimal("24300"),
            Decimal("24640"),
        ),
    )
    assert [point.net_pnl for point in simulation.points] == [
        Decimal("0"),
        Decimal("-25500"),
        Decimal("0"),
    ]


def test_acknowledgement_required_before_approval(service, repository):
    construct(service)
    definition = RULE_REGISTRY[RISK_IV_002]
    warning = RuleResult(
        rule_id=RISK_IV_002,
        severity=definition.default_severity,
        blocking=definition.blocking,
        requires_acknowledgement=definition.requires_acknowledgement,
        message=definition.description,
        evidence=MappingProxyType({"event_tag": "RBI"}),
    )
    repository.append_risk_assessments(
        "strategy-acceptance", (warning,), created_at=NOW
    )
    blocked = service.approve(
        "strategy-acceptance", acknowledgements=set(), approved_at=NOW
    )
    assert blocked.approved is False
    assert blocked.missing_acknowledgements == (RISK_IV_002,)
    approved = service.approve(
        "strategy-acceptance",
        acknowledgements={RISK_IV_002},
        approved_at=NOW + timedelta(seconds=1),
    )
    assert approved.approved is True
    assert repository.load_acknowledgements("strategy-acceptance")[-1].rule_id == RISK_IV_002


def test_open_requires_approval(service):
    construct(service)
    with pytest.raises(Exception) as exc:
        service.open_paper_position("strategy-acceptance")
    assert "approval" in str(exc.value).lower()


def test_monitoring_refresh_and_last_known_good_fallback(repository):
    provider = FakeProvider(
        [chain(), chain(call=Decimal("250"), put=Decimal("100"), spot=Decimal("24500"))]
    )
    service = StraddleApplicationService(
        provider=provider, repository=repository, clock=lambda: NOW
    )
    construct(service)
    approve_and_open(service)
    refreshed = service.refresh_position("paper-acceptance", captured_at=NOW)
    assert refreshed.monitoring.snapshot.unrealised_pnl == Decimal("750")

    provider.error = OptionQuoteError("temporary outage")
    fallback = service.refresh_position(
        "paper-acceptance", captured_at=NOW + timedelta(minutes=1)
    )
    assert fallback.monitoring.successful is True
    assert fallback.monitoring.snapshot.used_last_known_good is True
    assert fallback.monitoring.snapshot.total_pnl == Decimal("750")
    assert fallback.provider_error == "temporary outage"


def test_unusable_refresh_without_fallback_is_non_successful(repository):
    provider = FakeProvider([chain()])
    service = StraddleApplicationService(
        provider=provider, repository=repository, clock=lambda: NOW
    )
    construct(service)
    approve_and_open(service)
    provider.error = OptionQuoteError("outage")
    outcome = service.refresh_position("paper-acceptance", captured_at=NOW)
    assert outcome.monitoring.successful is False
    assert outcome.monitoring.snapshot is None


def test_partial_exit_acknowledgement_and_terminal_closed_state(service):
    construct(service)
    position = approve_and_open(service)
    blocked = service.exit_position(
        position.position_id,
        call_quantity=25,
        put_quantity=0,
        call_exit_price=Decimal("250"),
        put_exit_price=None,
        exit_reason="reduce call",
        occurred_at=NOW,
    )
    assert blocked.completed is False
    assert "POS-001" in {item.rule_id for item in blocked.validation_results}

    partial = service.exit_position(
        position.position_id,
        call_quantity=25,
        put_quantity=0,
        call_exit_price=Decimal("250"),
        put_exit_price=None,
        exit_reason="reduce call",
        acknowledgements={"POS-001"},
        occurred_at=NOW,
    )
    assert partial.position.status is PaperPositionStatus.PARTIALLY_CLOSED
    assert partial.position.realised_pnl == Decimal("1750")

    closed = service.exit_position(
        position.position_id,
        call_quantity=50,
        put_quantity=75,
        call_exit_price=Decimal("250"),
        put_exit_price=Decimal("100"),
        exit_reason="close remainder",
        occurred_at=NOW,
    )
    assert closed.position.status is PaperPositionStatus.CLOSED
    terminal = service.exit_position(
        position.position_id,
        call_quantity=0,
        put_quantity=0,
        call_exit_price=None,
        put_exit_price=None,
        exit_reason="invalid mutation",
        occurred_at=NOW,
    )
    assert terminal.completed is False
    assert "POS-003" in {item.rule_id for item in terminal.validation_results}


def test_complete_end_to_end_paper_trade_export_and_journal(repository):
    provider = FakeProvider(
        [chain(), chain(call=Decimal("250"), put=Decimal("100"), spot=Decimal("24500"))]
    )
    service = StraddleApplicationService(
        provider=provider, repository=repository, clock=lambda: NOW
    )
    construction = construct(service)
    simulation = service.simulate(
        construction.strategy_id,
        scenario_prices=(Decimal("23960"), Decimal("24300"), Decimal("24640")),
    )
    assert [point.net_pnl for point in simulation.points] == [
        Decimal("0"),
        Decimal("-25500"),
        Decimal("0"),
    ]
    service.approve("strategy-acceptance", acknowledgements=set(), approved_at=NOW)
    opened = service.open_paper_position(
        "strategy-acceptance",
        position_id="paper-acceptance",
        opened_at=NOW,
    )
    refresh = service.refresh_position("paper-acceptance", captured_at=NOW)
    assert refresh.monitoring.snapshot.unrealised_pnl == Decimal("750")
    closed = service.exit_position(
        "paper-acceptance",
        call_quantity=75,
        put_quantity=75,
        call_exit_price=Decimal("250"),
        put_exit_price=Decimal("100"),
        exit_reason="acceptance close",
        exit_fees=Decimal("100"),
        exit_slippage=Decimal("50"),
        occurred_at=NOW,
    )
    assert opened.position.status is PaperPositionStatus.OPEN
    assert closed.position.status is PaperPositionStatus.CLOSED
    assert closed.position.realised_pnl == Decimal("600")

    exported = service.export_position("paper-acceptance", created_at=NOW)
    document = json.loads(exported.content)
    assert document["paper_trading_only"] is True
    assert document["calculations"][0]["engine_version"] == "1.0.0"
    assert document["paper_position"]["realised_pnl"] == "600"
    event_types = [event.event_type for event in service.journal("paper-acceptance")]
    assert event_types == [
        "STRATEGY_CREATED",
        "STRATEGY_VALIDATED",
        "PAPER_POSITION_OPENED",
        "MARKET_SNAPSHOT_APPLIED",
        "POSITION_CLOSED",
        "EXPORT_CREATED",
    ]


def test_health_is_passive_and_reports_paper_only(service, monkeypatch):
    monkeypatch.setattr(
        service.provider,
        "get_option_chain",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("provider network path called")
        ),
    )
    health = service.health()
    assert health["paper_trading_only"] is True
    assert health["database_available"] is True
    assert health["provider"]["ready"] is True


def response_json(response):
    return json.loads(response.body)


def test_api_contract_construct_simulate_approve_and_open(service):
    constructed = construct_strategy_route(
        ConstructRequest(**{
            "underlying": "NIFTY",
            "expiry": "2026-08-27",
            "strategy_id": "strategy-api",
        }),
        service=service,
    )
    assert constructed.status_code == 200
    body = response_json(constructed)
    assert body["construction"]["calculation"]["total_cost"] == "25500"

    simulated = simulate_strategy_route(
        "strategy-api",
        SimulationRequest(scenario_prices=["23960", "24300", "24640"]),
        service=service,
    )
    assert simulated.status_code == 200
    assert [item["net_pnl"] for item in response_json(simulated)["points"]] == [
        "0",
        "-25500",
        "0",
    ]
    assert approve_strategy_route(
        "strategy-api", ApprovalRequest(acknowledgements=set()), service=service
    ).status_code == 200
    opened = open_position_route(
        "strategy-api",
        OpenPositionRequest(position_id="paper-api"),
        service=service,
    )
    assert opened.status_code == 201
    assert response_json(opened)["position"]["status"] == "OPEN"


def test_api_invalid_input_and_not_found_are_stable(service):
    with pytest.raises(ValidationError):
        ConstructRequest(underlying="", expiry="not-a-date")
    with pytest.raises(HTTPException) as exc:
        export_position_route("missing", service=service)
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "POSITION_NOT_FOUND"


def test_api_provider_unavailable_returns_503(repository):
    service = StraddleApplicationService(
        provider=FakeProvider(error=OptionQuoteError("provider offline")),
        repository=repository,
        clock=lambda: NOW,
    )
    with pytest.raises(HTTPException) as exc:
        underlyings(service=service)
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "MARKET_DATA_UNAVAILABLE"


def test_api_health_and_export(service):
    construct(service, "strategy-export")
    approve_and_open(service, "strategy-export", "paper-export")
    health = straddle_health(service=service)
    assert health["paper_trading_only"] is True
    exported = export_position_route("paper-export", service=service)
    assert exported.status_code == 200
    assert exported.media_type == "application/json"
    assert json.loads(exported.body)["paper_trading_only"] is True


def test_streamlit_workflow_is_explicitly_paper_only_and_staged():
    source = (
        Path(__file__).resolve().parents[1] / "app" / "straddle" / "ui.py"
    ).read_text()
    for stage in (
        "CONSTRUCT",
        "VALIDATE",
        "CALCULATE",
        "SIMULATE",
        "APPROVE",
        "PAPER TRADE",
        "MONITOR",
        "EXIT",
        "REVIEW",
    ):
        assert stage in source
    assert "PAPER TRADING ONLY" in source
    assert "NO LIVE ORDERS ARE PLACED" in source
    assert "place_order" not in source
