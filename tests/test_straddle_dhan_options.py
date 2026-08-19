from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.straddle.domain import OptionType
from app.straddle.market_data import (
    ExpiryNotFoundError,
    InstrumentMetadataError,
    MalformedProviderResponseError,
    OptionQuoteError,
    OptionsAuthenticationError,
    OptionsMarketDataProvider,
    UnderlyingNotFoundError,
)
from app.straddle.providers.dhan_options import (
    DhanOptionsProvider,
    parse_instrument_csv,
)
from app.straddle.rules import DATA_004, RulesConfig
from app.straddle.strategy_service import (
    OptionChainSnapshot,
    construct_long_straddle,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
EXPIRY = date(2026, 8, 27)
NEXT_EXPIRY = date(2026, 9, 3)

HEADERS = (
    "EXCH_ID,SEGMENT,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,"
    "SECURITY_ID,DISPLAY_NAME,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE"
)


def instrument_row(
    underlying,
    underlying_id,
    security_id,
    strike,
    option_type,
    *,
    expiry=EXPIRY,
    lot_size=75,
):
    symbol = f"{underlying}-{expiry.isoformat()}-{strike}-{option_type}"
    return (
        f"NSE,D,OPTIDX,{underlying_id},{underlying},{security_id},{symbol},"
        f"{lot_size},{expiry.isoformat()},{strike},{option_type}"
    )


def instrument_csv(*, duplicate_call=False):
    rows = []
    security = 1001
    for strike in (24200, 24300, 24400):
        for option_type in ("CE", "PE"):
            rows.append(instrument_row("NIFTY", 13, security, strike, option_type))
            security += 1
    for option_type in ("CE", "PE"):
        rows.append(
            instrument_row(
                "NIFTY", 13, security, 24300, option_type, expiry=NEXT_EXPIRY
            )
        )
        security += 1
    for option_type in ("CE", "PE"):
        rows.append(
            instrument_row(
                "NIFTY",
                13,
                security,
                24000,
                option_type,
                expiry=date(2026, 8, 13),
            )
        )
        security += 1
    for option_type in ("CE", "PE"):
        rows.append(
            instrument_row(
                "BANKNIFTY",
                25,
                security,
                55000,
                option_type,
                lot_size=30,
            )
        )
        security += 1
    if duplicate_call:
        rows.append(instrument_row("NIFTY", 13, 9999, 24300, "CE"))
    rows.append("NSE,E,EQUITY,,,2885,RELIANCE,1,,0,")
    return "\n".join((HEADERS, *rows)).encode()


def option_chain_payload(*, missing_premium=False, timestamp="2026-08-20T09:59:55Z"):
    premiums = {
        "24200.000000": (Decimal("245"), Decimal("105"), 1001, 1002),
        "24300.000000": (Decimal("180"), Decimal("160"), 1003, 1004),
        "24400.000000": (Decimal("120"), Decimal("220"), 1005, 1006),
    }
    oc = {}
    for strike, (call, put, call_id, put_id) in premiums.items():
        oc[strike] = {
            "ce": {
                "security_id": call_id,
                "last_price": None if missing_premium and strike == "24300.000000" else call,
            },
            "pe": {"security_id": put_id, "last_price": put},
        }
    return {
        "status": "success",
        "data": {
            "last_price": Decimal("24342"),
            "provider_timestamp": timestamp,
            "oc": oc,
        },
    }


class FakeResponse:
    def __init__(self, *, payload=None, content=b"", status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttp:
    def __init__(self, *, csv_payload=None, post_responses=()):
        self.csv_payload = instrument_csv() if csv_payload is None else csv_payload
        self.post_responses = list(post_responses or [FakeResponse(payload=option_chain_payload())])
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return FakeResponse(content=self.csv_payload)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.post_responses:
            raise AssertionError("unexpected extra option quote request")
        return self.post_responses.pop(0)


class FakeAuth:
    def __init__(self, *, error=None):
        self.error = error
        self.refresh_flags = []

    def get_access_token(self, force_refresh=False):
        self.refresh_flags.append(force_refresh)
        if self.error is not None:
            raise self.error
        return "refreshed-token" if force_refresh else "token"

    @staticmethod
    def _config():
        return ("1000000001", "pin", "secret")


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


def make_provider(tmp_path, *, http=None, auth=None):
    return DhanOptionsProvider(
        cache_file=tmp_path / "dhan-options.csv",
        http_client=http or FakeHttp(),
        auth=auth or FakeAuth(),
        clock=lambda: NOW,
    )


def test_instrument_metadata_normalization_and_option_mapping():
    instruments = parse_instrument_csv(instrument_csv())
    call = next(item for item in instruments if item.security_id == "1003")
    put = next(item for item in instruments if item.security_id == "1004")
    assert call.underlying == "NIFTY"
    assert call.option_type is OptionType.CALL
    assert put.option_type is OptionType.PUT
    assert call.provider_symbol == "NIFTY-2026-08-27-24300-CE"


def test_nifty_and_banknifty_discovery(tmp_path):
    provider = make_provider(tmp_path)
    assert provider.get_underlyings() == ("BANKNIFTY", "NIFTY")


def test_banknifty_option_discovery_preserves_lot_size(tmp_path):
    provider = make_provider(tmp_path)
    instruments = provider._underlying_instruments("banknifty")
    assert {item.option_type for item in instruments} == {
        OptionType.CALL,
        OptionType.PUT,
    }
    assert {item.lot_size for item in instruments} == {30}


def test_expiry_discovery_filters_expired_contracts_explicitly(tmp_path):
    provider = make_provider(tmp_path)
    assert provider.get_expiries("NIFTY", evaluation_date=date(2026, 8, 20)) == (
        EXPIRY,
        NEXT_EXPIRY,
    )
    assert date(2026, 8, 13) in provider.get_expiries("NIFTY")


def test_strikes_and_lot_sizes_remain_decimal_and_integer(tmp_path):
    provider = make_provider(tmp_path)
    instrument = next(
        item
        for item in provider._underlying_instruments("NIFTY")
        if item.security_id == "1003"
    )
    assert instrument.strike == Decimal("24300")
    assert isinstance(instrument.strike, Decimal)
    assert instrument.lot_size == 75


def test_option_chain_normalizes_spot_contracts_and_timestamps(tmp_path):
    provider = make_provider(tmp_path)
    snapshot = provider.get_option_chain("nifty", EXPIRY)
    assert isinstance(snapshot, OptionChainSnapshot)
    assert snapshot.underlying == "NIFTY"
    assert snapshot.spot_price == Decimal("24342")
    assert snapshot.provider_timestamp == datetime(
        2026, 8, 20, 9, 59, 55, tzinfo=UTC
    )
    assert snapshot.received_at == NOW
    assert len(snapshot.contracts) == 6
    assert all(contract.quantity == 75 for contract in snapshot.contracts)


def test_whole_expiry_is_retrieved_in_one_batched_request(tmp_path):
    http = FakeHttp()
    provider = make_provider(tmp_path, http=http)
    provider.get_option_chain("NIFTY", EXPIRY)
    assert len(http.post_calls) == 1
    _, request = http.post_calls[0]
    assert request["json"] == {
        "UnderlyingScrip": 13,
        "UnderlyingSeg": "IDX_I",
        "Expiry": "2026-08-27",
    }


def test_instrument_cache_avoids_repeated_downloads(tmp_path):
    first_http = FakeHttp()
    first = make_provider(tmp_path, http=first_http)
    first.get_underlyings()
    assert len(first_http.get_calls) == 1

    second_http = FakeHttp(csv_payload=b"must not be read")
    second = make_provider(tmp_path, http=second_http)
    assert second.get_underlyings() == ("BANKNIFTY", "NIFTY")
    assert second_http.get_calls == []


def test_force_refresh_replaces_in_memory_metadata(tmp_path):
    http = FakeHttp()
    provider = make_provider(tmp_path, http=http)
    provider.get_underlyings()
    http.csv_payload = instrument_csv()
    provider.refresh_instruments(force=True)
    assert len(http.get_calls) == 2


def test_expired_disk_cache_is_refreshed(tmp_path):
    first = make_provider(tmp_path)
    first.get_underlyings()

    later_http = FakeHttp()
    later = DhanOptionsProvider(
        cache_file=tmp_path / "dhan-options.csv",
        http_client=later_http,
        auth=FakeAuth(),
        clock=lambda: NOW + timedelta(hours=25),
    )
    assert later.get_underlyings() == ("BANKNIFTY", "NIFTY")
    assert len(later_http.get_calls) == 1


@pytest.mark.parametrize(
    "payload",
    [b"garbage", (HEADERS + "\nNSE,D,OPTIDX,13,NIFTY,,,,,,CE").encode()],
)
def test_malformed_instrument_payload_is_typed(payload):
    with pytest.raises(InstrumentMetadataError):
        parse_instrument_csv(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "success", "data": {"last_price": 24342, "oc": []}},
        {"status": "success", "data": {"last_price": "bad", "oc": {}}},
        {"unexpected": "shape"},
    ],
)
def test_malformed_quote_payload_is_typed(tmp_path, payload):
    http = FakeHttp(post_responses=[FakeResponse(payload=payload)])
    provider = make_provider(tmp_path, http=http)
    with pytest.raises(MalformedProviderResponseError):
        provider.get_option_chain("NIFTY", EXPIRY)


def test_provider_quote_failure_is_distinct(tmp_path):
    http = FakeHttp(
        post_responses=[FakeResponse(payload={"status": "failure", "remarks": "rate"})]
    )
    with pytest.raises(OptionQuoteError):
        make_provider(tmp_path, http=http).get_option_chain("NIFTY", EXPIRY)


def test_missing_underlying_is_typed(tmp_path):
    with pytest.raises(UnderlyingNotFoundError):
        make_provider(tmp_path).get_expiries("FINNIFTY")


def test_missing_expiry_is_typed(tmp_path):
    with pytest.raises(ExpiryNotFoundError):
        make_provider(tmp_path).get_option_chain("NIFTY", date(2027, 1, 1))


def test_authentication_failure_is_typed_and_no_quote_is_sent(tmp_path):
    http = FakeHttp()
    provider = make_provider(
        tmp_path,
        http=http,
        auth=FakeAuth(error=RuntimeError("missing credentials")),
    )
    with pytest.raises(OptionsAuthenticationError):
        provider.get_option_chain("NIFTY", EXPIRY)
    assert http.post_calls == []


def test_401_refreshes_once_then_retries_once(tmp_path):
    auth = FakeAuth()
    http = FakeHttp(
        post_responses=[
            FakeResponse(status_code=401),
            FakeResponse(payload=option_chain_payload()),
        ]
    )
    snapshot = make_provider(tmp_path, http=http, auth=auth).get_option_chain(
        "NIFTY", EXPIRY
    )
    assert snapshot.spot_price == Decimal("24342")
    assert auth.refresh_flags == [False, True]
    assert len(http.post_calls) == 2


def test_exhausted_401_is_authentication_failure(tmp_path):
    http = FakeHttp(
        post_responses=[FakeResponse(status_code=401), FakeResponse(status_code=401)]
    )
    with pytest.raises(OptionsAuthenticationError):
        make_provider(tmp_path, http=http).get_option_chain("NIFTY", EXPIRY)


def test_missing_premium_is_preserved_and_never_fabricated(tmp_path, rules_config):
    http = FakeHttp(
        post_responses=[
            FakeResponse(payload=option_chain_payload(missing_premium=True))
        ]
    )
    snapshot = make_provider(tmp_path, http=http).get_option_chain("NIFTY", EXPIRY)
    call = next(
        contract
        for contract in snapshot.contracts
        if contract.strike == Decimal("24300")
        and contract.option_type is OptionType.CALL
    )
    assert call.premium is None
    result = construct_long_straddle(
        snapshot,
        expiry=EXPIRY,
        evaluation_time=NOW,
        rules_config=rules_config,
    )
    assert result.can_proceed is False
    assert DATA_004 in {item.rule_id for item in result.validation_results}


def test_duplicate_provider_metadata_resolves_by_quote_security_id(tmp_path):
    http = FakeHttp(csv_payload=instrument_csv(duplicate_call=True))
    snapshot = make_provider(tmp_path, http=http).get_option_chain("NIFTY", EXPIRY)
    calls = [
        contract
        for contract in snapshot.contracts
        if contract.strike == Decimal("24300")
        and contract.option_type is OptionType.CALL
    ]
    assert len(calls) == 1
    assert calls[0].premium == Decimal("180")


def test_provider_satisfies_provider_neutral_protocol(tmp_path):
    assert isinstance(make_provider(tmp_path), OptionsMarketDataProvider)


def test_mandatory_atm_strategy_construction_integration(tmp_path, rules_config):
    snapshot = make_provider(tmp_path).get_option_chain("NIFTY", EXPIRY)
    result = construct_long_straddle(
        snapshot,
        expiry=EXPIRY,
        evaluation_time=NOW,
        rules_config=rules_config,
    )
    assert result.recommended_atm_strike == Decimal("24300")
    assert result.selected_strike == Decimal("24300")
    assert result.calculation.combined_premium == Decimal("340")
    assert result.calculation.total_cost == Decimal("25500")
    assert result.can_proceed is True


def test_strategy_service_accepts_snapshot_without_provider_awareness(
    tmp_path, rules_config
):
    snapshot = make_provider(tmp_path).get_option_chain("NIFTY", EXPIRY)
    copied = replace(snapshot, contracts=tuple(snapshot.contracts))
    assert construct_long_straddle(
        copied,
        expiry=EXPIRY,
        evaluation_time=NOW,
        rules_config=rules_config,
    ).strategy is not None


def test_domain_rules_and_calculation_do_not_import_dhan():
    root = Path(__file__).resolve().parents[1] / "app" / "straddle"
    for module in ("domain.py", "engine.py", "rules.py"):
        assert "dhan" not in (root / module).read_text().lower()


def test_adapter_references_no_broker_order_api():
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "straddle"
        / "providers"
        / "dhan_options.py"
    ).read_text().lower()
    assert "place_order" not in source
    assert "/orders" not in source
