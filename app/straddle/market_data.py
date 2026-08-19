"""Provider-neutral options market-data boundary for StraddleLab."""

from datetime import date
from typing import Protocol, runtime_checkable

from app.straddle.strategy_service import OptionChainSnapshot


class OptionsMarketDataError(RuntimeError):
    """Base class for normalized options-provider failures."""


class OptionsAuthenticationError(OptionsMarketDataError):
    """The provider could not authenticate the market-data request."""


class InstrumentMetadataError(OptionsMarketDataError):
    """Instrument metadata could not be downloaded or parsed."""


class UnderlyingNotFoundError(OptionsMarketDataError):
    """The requested underlying is absent from instrument metadata."""


class ExpiryNotFoundError(OptionsMarketDataError):
    """The requested expiry is unavailable for the underlying."""


class OptionQuoteError(OptionsMarketDataError):
    """Option-chain quotes could not be retrieved from the provider."""


class MalformedProviderResponseError(OptionsMarketDataError):
    """A provider response did not satisfy the documented data contract."""


@runtime_checkable
class OptionsMarketDataProvider(Protocol):
    """Small provider contract consumed by future orchestration layers."""

    def get_underlyings(self) -> tuple[str, ...]: ...

    def get_expiries(
        self,
        underlying: str,
        *,
        evaluation_date: date | None = None,
    ) -> tuple[date, ...]: ...

    def get_option_chain(
        self,
        underlying: str,
        expiry: date,
    ) -> OptionChainSnapshot: ...
