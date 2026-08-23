"""DhanHQ REST adapter producing provider-neutral option-chain snapshots."""

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Mapping

import httpx

from app import dhan_auth
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
from app.straddle.strategy_service import OptionChainSnapshot, OptionContract

INSTRUMENT_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
OPTION_CHAIN_URL = "https://api.dhan.co/v2/optionchain"
SUPPORTED_UNDERLYINGS = frozenset(("NIFTY", "BANKNIFTY"))
DEFAULT_CACHE_TTL = timedelta(hours=24)
UTC = timezone.utc
DEFAULT_CACHE_FILE = Path(__file__).resolve().parents[3] / ".dhan_instruments.csv"


@dataclass(frozen=True, slots=True)
class DhanOptionInstrument:
    """Provider metadata retained inside the Dhan adapter boundary."""

    underlying: str
    underlying_security_id: str
    underlying_segment: str
    security_id: str
    provider_symbol: str
    expiry: date
    strike: Decimal
    option_type: OptionType
    lot_size: int
    exchange_segment: str = "NSE_FNO"


def _first(row: Mapping[str, object], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _parse_expiry(value: str) -> date:
    candidate = value.strip().split(" ", 1)[0]
    try:
        return date.fromisoformat(candidate)
    except ValueError as exc:
        raise InstrumentMetadataError(f"invalid option expiry: {value!r}") from exc


def _decimal(value: object, *, field: str, error_type=MalformedProviderResponseError) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise error_type(f"missing or invalid {field}")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise error_type(f"invalid {field}: {value!r}") from exc
    if not result.is_finite():
        raise error_type(f"invalid {field}: {value!r}")
    return result


def normalize_instrument_rows(
    rows: Iterable[Mapping[str, object]],
) -> tuple[DhanOptionInstrument, ...]:
    """Purely normalize supported NSE index-option rows from Dhan metadata."""
    instruments: list[DhanOptionInstrument] = []
    for row in rows:
        exchange = _first(row, "EXCH_ID", "SEM_EXM_EXCH_ID").upper()
        segment = _first(row, "SEGMENT", "SEM_SEGMENT").upper()
        instrument = _first(row, "INSTRUMENT", "SEM_INSTRUMENT_NAME").upper()
        option_code = _first(row, "OPTION_TYPE", "SEM_OPTION_TYPE").upper()
        underlying = _first(row, "UNDERLYING_SYMBOL", "SYMBOL_NAME", "SM_SYMBOL_NAME").upper()
        if (
            exchange != "NSE"
            or segment not in ("D", "DERIVATIVES", "NSE_FNO")
            or instrument not in ("OPTIDX", "INDEX OPTION", "INDEX_OPTIONS")
            or option_code not in ("CE", "PE")
            or underlying not in SUPPORTED_UNDERLYINGS
        ):
            continue

        security_id = _first(row, "SECURITY_ID", "SEM_SMST_SECURITY_ID", "securityId")
        underlying_security_id = _first(row, "UNDERLYING_SECURITY_ID")
        symbol = _first(row, "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL", "SEM_TRADING_SYMBOL")
        expiry_value = _first(row, "SM_EXPIRY_DATE", "SEM_EXPIRY_DATE")
        strike_value = _first(row, "STRIKE_PRICE", "SEM_STRIKE_PRICE")
        lot_value = _first(row, "LOT_SIZE", "SEM_LOT_UNITS")
        if not all(
            (security_id, underlying_security_id, symbol, expiry_value, strike_value, lot_value)
        ):
            raise InstrumentMetadataError(
                f"incomplete option metadata for {underlying or 'unknown underlying'}"
            )
        try:
            int(security_id)
            int(underlying_security_id)
        except ValueError as exc:
            raise InstrumentMetadataError("security IDs must be integers") from exc
        try:
            lot_size = int(Decimal(str(lot_value).strip()))
        except (ValueError, InvalidOperation) as exc:
            raise InstrumentMetadataError(f"invalid lot size: {lot_value!r}") from exc
        if lot_size <= 0:
            raise InstrumentMetadataError(f"invalid lot size: {lot_value!r}")
        instruments.append(
            DhanOptionInstrument(
                underlying=underlying,
                underlying_security_id=underlying_security_id,
                underlying_segment="IDX_I",
                security_id=security_id,
                provider_symbol=symbol,
                expiry=_parse_expiry(expiry_value),
                strike=_decimal(
                    strike_value,
                    field="strike",
                    error_type=InstrumentMetadataError,
                ),
                option_type=(
                    OptionType.CALL if option_code == "CE" else OptionType.PUT
                ),
                lot_size=lot_size,
            )
        )

    return tuple(
        sorted(
            instruments,
            key=lambda item: (
                item.underlying,
                item.expiry,
                item.strike,
                item.option_type.value,
                item.security_id,
            ),
        )
    )


def parse_instrument_csv(payload: str | bytes) -> tuple[DhanOptionInstrument, ...]:
    """Parse Dhan's detailed CSV and reject malformed or empty payloads."""
    try:
        text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
        rows = tuple(csv.DictReader(io.StringIO(text)))
    except (UnicodeDecodeError, csv.Error, TypeError) as exc:
        raise InstrumentMetadataError("malformed Dhan instrument CSV") from exc
    if not rows or not rows[0]:
        raise InstrumentMetadataError("Dhan instrument CSV is empty or malformed")
    instruments = normalize_instrument_rows(rows)
    if not instruments:
        raise InstrumentMetadataError("no supported Dhan index options found")
    return instruments


def _parse_provider_timestamp(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            parsed = parsed.astimezone(UTC)
    except (OverflowError, TypeError, ValueError) as exc:
        raise MalformedProviderResponseError(
            f"invalid provider timestamp: {value!r}"
        ) from exc
    return parsed


class DhanOptionsProvider(OptionsMarketDataProvider):
    """Cached Dhan option metadata plus one option-chain request per expiry."""

    def __init__(
        self,
        *,
        cache_file: Path = DEFAULT_CACHE_FILE,
        http_client=httpx,
        auth=dhan_auth,
        clock: Callable[[], datetime] | None = None,
        cache_ttl: timedelta = DEFAULT_CACHE_TTL,
    ):
        if cache_ttl < timedelta(0):
            raise ValueError("cache_ttl must be non-negative")
        self._cache_file = Path(cache_file)
        self._cache_metadata_file = self._cache_file.with_suffix(
            self._cache_file.suffix + ".json"
        )
        self._http = http_client
        self._auth = auth
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache_ttl = cache_ttl
        self._instruments: tuple[DhanOptionInstrument, ...] | None = None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def cache_status(self) -> dict[str, object]:
        """Inspect local metadata cache state without network access or refresh."""
        exists = self._cache_file.exists()
        fetched_at = None
        fresh = False
        try:
            metadata = json.loads(self._cache_metadata_file.read_text())
            parsed = datetime.fromisoformat(metadata["fetched_at"])
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                fetched_at = parsed.astimezone(UTC)
                fresh = exists and self._now() - fetched_at <= self._cache_ttl
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return {
            "available": exists,
            "fresh": fresh,
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "ttl_seconds": int(self._cache_ttl.total_seconds()),
        }

    def status(self) -> dict[str, object]:
        """Return passive provider readiness without authentication or HTTP calls."""
        try:
            auth_status = self._auth.dhan_status()
        except Exception:
            auth_status = {"configured": False, "authenticated": False}
        cache = self.cache_status()
        return {
            "configured": bool(auth_status.get("configured")),
            "authenticated": bool(auth_status.get("authenticated")),
            "cache": cache,
            "ready": bool(auth_status.get("authenticated") and cache["available"]),
        }

    def _read_fresh_cache(self) -> bytes | None:
        try:
            metadata = json.loads(self._cache_metadata_file.read_text())
            fetched_at = datetime.fromisoformat(metadata["fetched_at"])
            if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
                return None
            if self._now() - fetched_at.astimezone(UTC) > self._cache_ttl:
                return None
            return self._cache_file.read_bytes()
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_cache(self, payload: bytes, fetched_at: datetime) -> None:
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_bytes(payload)
            self._cache_metadata_file.write_text(
                json.dumps(
                    {"fetched_at": fetched_at.isoformat()},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except OSError as exc:
            raise InstrumentMetadataError("could not persist Dhan instrument cache") from exc

    def refresh_instruments(
        self, *, force: bool = False
    ) -> tuple[DhanOptionInstrument, ...]:
        if self._instruments is not None and not force:
            return self._instruments
        payload = None if force else self._read_fresh_cache()
        if payload is None:
            try:
                response = self._http.get(INSTRUMENT_URL, timeout=60)
                response.raise_for_status()
                payload = response.content
            except Exception as exc:
                raise InstrumentMetadataError(
                    "Dhan instrument metadata is unavailable"
                ) from exc
            fetched_at = self._now()
            instruments = parse_instrument_csv(payload)
            self._write_cache(payload, fetched_at)
        else:
            instruments = parse_instrument_csv(payload)
        self._instruments = instruments
        return self._instruments

    def get_underlyings(self) -> tuple[str, ...]:
        return tuple(sorted({item.underlying for item in self.refresh_instruments()}))

    def _underlying_instruments(
        self, underlying: str
    ) -> tuple[DhanOptionInstrument, ...]:
        normalized = underlying.strip().upper()
        matches = tuple(
            item
            for item in self.refresh_instruments()
            if item.underlying == normalized
        )
        if not matches:
            raise UnderlyingNotFoundError(f"unknown Dhan options underlying: {normalized}")
        return matches

    def get_expiries(
        self,
        underlying: str,
        *,
        evaluation_date: date | None = None,
    ) -> tuple[date, ...]:
        expiries = sorted(
            {
                item.expiry
                for item in self._underlying_instruments(underlying)
                if evaluation_date is None or item.expiry >= evaluation_date
            }
        )
        return tuple(expiries)

    def _headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        try:
            token = self._auth.get_access_token(force_refresh=force_refresh)
            client_id = self._auth._config()[0]
        except Exception as exc:
            raise OptionsAuthenticationError("Dhan authentication failed") from exc
        if not token or not client_id:
            raise OptionsAuthenticationError("Dhan authentication is unavailable")
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": token,
            "client-id": client_id,
        }

    def _post_option_chain(self, payload: dict[str, object]):
        for attempt in range(2):
            headers = self._headers(force_refresh=attempt == 1)
            try:
                response = self._http.post(
                    OPTION_CHAIN_URL,
                    headers=headers,
                    json=payload,
                    timeout=30,
                )
            except Exception as exc:
                raise OptionQuoteError("Dhan option-chain request failed") from exc
            if response.status_code == 401:
                if attempt == 0:
                    continue
                raise OptionsAuthenticationError(
                    "Dhan authentication failed after one token refresh"
                )
            try:
                response.raise_for_status()
            except Exception as exc:
                raise OptionQuoteError("Dhan option-chain request failed") from exc
            try:
                return response.json()
            except Exception as exc:
                raise MalformedProviderResponseError(
                    "Dhan option-chain response is not JSON"
                ) from exc
        raise OptionsAuthenticationError("Dhan authentication failed")

    @staticmethod
    def _select_metadata(
        candidates: tuple[DhanOptionInstrument, ...],
        quote: Mapping[str, object],
        *,
        strike: Decimal,
        option_type: OptionType,
    ) -> DhanOptionInstrument:
        security_id = quote.get("security_id")
        if security_id is None:
            raise MalformedProviderResponseError(
                f"missing security_id for {strike} {option_type.value}"
            )
        matching_security = tuple(
            item for item in candidates if item.security_id == str(security_id)
        )
        if len(matching_security) != 1:
            raise MalformedProviderResponseError(
                f"ambiguous or unknown contract {security_id} for {strike} {option_type.value}"
            )
        return matching_security[0]

    def get_option_chain(
        self,
        underlying: str,
        expiry: date,
    ) -> OptionChainSnapshot:
        normalized_underlying = underlying.strip().upper()
        candidates = tuple(
            item
            for item in self._underlying_instruments(normalized_underlying)
            if item.expiry == expiry
        )
        if not candidates:
            raise ExpiryNotFoundError(
                f"expiry {expiry.isoformat()} is unavailable for {normalized_underlying}"
            )
        underlying_ids = {
            (item.underlying_security_id, item.underlying_segment)
            for item in candidates
        }
        if len(underlying_ids) != 1:
            raise InstrumentMetadataError(
                f"inconsistent underlying metadata for {normalized_underlying}"
            )
        underlying_security_id, underlying_segment = next(iter(underlying_ids))
        raw = self._post_option_chain(
            {
                "UnderlyingScrip": int(underlying_security_id),
                "UnderlyingSeg": underlying_segment,
                "Expiry": expiry.isoformat(),
            }
        )
        received_at = self._now()
        if isinstance(raw, Mapping) and raw.get("status") in ("failure", "error"):
            raise OptionQuoteError("Dhan rejected the option-chain request")
        if not isinstance(raw, Mapping) or raw.get("status") != "success":
            raise MalformedProviderResponseError("malformed Dhan option-chain envelope")
        data = raw.get("data")
        if not isinstance(data, Mapping):
            raise MalformedProviderResponseError("missing Dhan option-chain data")
        option_chain = data.get("oc")
        if not isinstance(option_chain, Mapping):
            raise MalformedProviderResponseError("missing Dhan option-chain contracts")
        spot_price = _decimal(data.get("last_price"), field="underlying spot")
        if spot_price <= Decimal("0"):
            raise MalformedProviderResponseError("underlying spot must be positive")
        provider_timestamp = _parse_provider_timestamp(
            data.get("provider_timestamp", data.get("timestamp"))
        )

        by_logical: dict[tuple[Decimal, OptionType], tuple[DhanOptionInstrument, ...]] = {}
        for item in candidates:
            key = (item.strike, item.option_type)
            by_logical[key] = (*by_logical.get(key, ()), item)

        contracts: list[OptionContract] = []
        for raw_strike, pair in option_chain.items():
            strike = _decimal(raw_strike, field="strike")
            if not isinstance(pair, Mapping):
                raise MalformedProviderResponseError(
                    f"malformed option pair at strike {strike}"
                )
            for provider_type, option_type in (
                ("ce", OptionType.CALL),
                ("pe", OptionType.PUT),
            ):
                quote = pair.get(provider_type)
                if quote is None:
                    continue
                if not isinstance(quote, Mapping):
                    raise MalformedProviderResponseError(
                        f"malformed {provider_type} quote at strike {strike}"
                    )
                logical_candidates = by_logical.get((strike, option_type), ())
                metadata = self._select_metadata(
                    logical_candidates,
                    quote,
                    strike=strike,
                    option_type=option_type,
                )
                premium_value = quote.get("last_price")
                premium = (
                    None
                    if premium_value is None
                    else _decimal(premium_value, field="option premium")
                )
                if premium is not None and premium < Decimal("0"):
                    raise MalformedProviderResponseError(
                        f"negative option premium for {strike} {option_type.value}"
                    )
                contracts.append(
                    OptionContract(
                        underlying=normalized_underlying,
                        option_type=option_type,
                        strike=metadata.strike,
                        expiry=metadata.expiry,
                        premium=premium,
                        quantity=metadata.lot_size,
                    )
                )

        if not contracts:
            raise MalformedProviderResponseError(
                "Dhan option-chain response contains no usable contracts"
            )
        contracts.sort(
            key=lambda item: (item.strike, item.option_type.value)
        )
        return OptionChainSnapshot(
            underlying=normalized_underlying,
            spot_price=spot_price,
            provider_timestamp=provider_timestamp,
            received_at=received_at,
            contracts=tuple(contracts),
        )
