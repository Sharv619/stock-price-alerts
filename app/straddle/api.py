"""Bounded FastAPI surface for the paper-only StraddleLab workflow."""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import database
from app.straddle.application import (
    StraddleApplicationError,
    StraddleApplicationService,
    StraddleConflictError,
    StraddleNotFoundError,
    StraddleProviderUnavailableError,
    to_primitive,
)
from app.straddle.persistence import StraddleRepository, create_straddle_schema
from app.straddle.providers.dhan_options import DhanOptionsProvider

router = APIRouter(prefix="/straddle", tags=["StraddleLab"])
_repository = StraddleRepository(database.SessionLocal)
_provider = DhanOptionsProvider()
_service = StraddleApplicationService(provider=_provider, repository=_repository)


def initialize_straddle_schema() -> None:
    create_straddle_schema(database.engine)


def get_straddle_service() -> StraddleApplicationService:
    return _service


def _raise_http(exc: StraddleApplicationError):
    if isinstance(exc, StraddleNotFoundError):
        status = 404
    elif isinstance(exc, StraddleProviderUnavailableError):
        status = 503
    elif isinstance(exc, StraddleConflictError):
        status = 409
    else:
        status = 400
    raise HTTPException(status, {"code": exc.code, "message": exc.message}) from exc


def _json(value, *, status_code: int = 200):
    return JSONResponse(to_primitive(value), status_code=status_code)


class ConstructRequest(BaseModel):
    underlying: str = Field(..., min_length=1)
    expiry: date
    selected_strike: Decimal | None = None
    strategy_id: str | None = None
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    slippage: Decimal = Field(default=Decimal("0"), ge=0)


class ApprovalRequest(BaseModel):
    acknowledgements: set[str] = Field(default_factory=set)


class OpenPositionRequest(BaseModel):
    position_id: str | None = None
    entry_fees: Decimal = Field(default=Decimal("0"), ge=0)
    entry_slippage: Decimal = Field(default=Decimal("0"), ge=0)


class SimulationRequest(BaseModel):
    scenario_prices: list[Decimal] = Field(..., min_length=1)
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    slippage: Decimal = Field(default=Decimal("0"), ge=0)


class ExitRequest(BaseModel):
    call_quantity: int = Field(default=0, ge=0)
    put_quantity: int = Field(default=0, ge=0)
    call_exit_price: Decimal | None = Field(default=None, ge=0)
    put_exit_price: Decimal | None = Field(default=None, ge=0)
    exit_reason: str = Field(..., min_length=1)
    acknowledgements: set[str] = Field(default_factory=set)
    exit_fees: Decimal = Field(default=Decimal("0"), ge=0)
    exit_slippage: Decimal = Field(default=Decimal("0"), ge=0)


@router.get("/underlyings")
def underlyings(service=Depends(get_straddle_service)):
    try:
        return {"underlyings": service.get_underlyings()}
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.get("/expiries/{underlying}")
def expiries(
    underlying: str,
    evaluation_date: date | None = None,
    service=Depends(get_straddle_service),
):
    try:
        return _json(
            {
                "underlying": underlying.upper(),
                "expiries": service.get_expiries(
                    underlying, evaluation_date=evaluation_date
                ),
            }
        )
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.get("/chain/{underlying}/{expiry}")
def option_chain(
    underlying: str,
    expiry: date,
    service=Depends(get_straddle_service),
):
    try:
        return _json(service.get_chain(underlying, expiry))
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.post("/strategies/construct")
def construct_strategy(
    request: ConstructRequest,
    service=Depends(get_straddle_service),
):
    try:
        return _json(service.construct(**request.model_dump()))
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.post("/strategies/{strategy_id}/approve")
def approve_strategy_route(
    strategy_id: str,
    request: ApprovalRequest,
    service=Depends(get_straddle_service),
):
    try:
        result = service.approve(
            strategy_id, acknowledgements=request.acknowledgements
        )
        if not result.approved:
            code = (
                "ACKNOWLEDGEMENT_REQUIRED"
                if result.missing_acknowledgements
                else "STRATEGY_BLOCKED"
            )
            return _json(
                {"code": code, "approval": result}, status_code=409
            )
        return _json(result)
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.post("/strategies/{strategy_id}/paper-position")
def open_position_route(
    strategy_id: str,
    request: OpenPositionRequest,
    service=Depends(get_straddle_service),
):
    try:
        return _json(
            service.open_paper_position(
                strategy_id,
                position_id=request.position_id,
                entry_fees=request.entry_fees,
                entry_slippage=request.entry_slippage,
            ),
            status_code=201,
        )
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.post("/strategies/{strategy_id}/simulate")
def simulate_strategy_route(
    strategy_id: str,
    request: SimulationRequest,
    service=Depends(get_straddle_service),
):
    try:
        return _json(
            service.simulate(
                strategy_id,
                scenario_prices=tuple(request.scenario_prices),
                fees=request.fees,
                slippage=request.slippage,
            )
        )
    except (StraddleApplicationError, ValueError, TypeError) as exc:
        if isinstance(exc, StraddleApplicationError):
            _raise_http(exc)
        raise HTTPException(422, {"code": "INVALID_SCENARIO", "message": str(exc)})


@router.get("/positions")
def positions(service=Depends(get_straddle_service)):
    return _json(service.list_positions())


@router.get("/positions/{position_id}")
def position_review(position_id: str, service=Depends(get_straddle_service)):
    try:
        return _json(service.review(position_id))
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.post("/positions/{position_id}/refresh")
def refresh_position_route(position_id: str, service=Depends(get_straddle_service)):
    try:
        outcome = service.refresh_position(position_id)
        if not outcome.monitoring.successful:
            return _json(
                {
                    "code": "MARKET_DATA_UNAVAILABLE",
                    "message": outcome.provider_error
                    or "Current market data is unusable and no fallback exists",
                    "outcome": outcome,
                },
                status_code=503,
            )
        return _json(outcome)
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.post("/positions/{position_id}/exit")
def exit_position_route(
    position_id: str,
    request: ExitRequest,
    service=Depends(get_straddle_service),
):
    try:
        result = service.exit_position(
            position_id,
            call_quantity=request.call_quantity,
            put_quantity=request.put_quantity,
            call_exit_price=request.call_exit_price,
            put_exit_price=request.put_exit_price,
            exit_reason=request.exit_reason,
            acknowledgements=request.acknowledgements,
            exit_fees=request.exit_fees,
            exit_slippage=request.exit_slippage,
        )
        if not result.completed:
            return _json(
                {"code": "POSITION_ACTION_BLOCKED", "transition": result},
                status_code=409,
            )
        return _json(result)
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.get("/positions/{position_id}/journal")
def position_journal(position_id: str, service=Depends(get_straddle_service)):
    try:
        return _json({"events": service.journal(position_id)})
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.get("/positions/{position_id}/export")
def export_position_route(position_id: str, service=Depends(get_straddle_service)):
    try:
        export = service.export_position(position_id)
        return Response(
            content=export.content,
            media_type=export.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename}"'
            },
        )
    except StraddleApplicationError as exc:
        _raise_http(exc)


@router.get("/health")
def straddle_health(service=Depends(get_straddle_service)):
    return service.health()
