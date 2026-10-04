"""Price data behind a swappable interface.

The only implementation today is yfinance, which is an UNOFFICIAL scraper of Yahoo
Finance: no SLA, silent schema changes, occasional bad prints, rate limiting.
Every quote it returns is tagged reliable=False so downstream reports can say so.
Swap in a paid provider by implementing PriceProvider and registering it below.
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass


class PriceError(RuntimeError):
    pass


@dataclass(frozen=True)
class PriceQuote:
    ticker: str
    price: float
    currency: str
    as_of: dt.date
    source: str
    reliable: bool
    note: str | None = None


class PriceProvider(ABC):
    name: str = "abstract"
    reliable: bool = False

    @abstractmethod
    def close_on(self, ticker: str, date: dt.date) -> PriceQuote:
        """Closing price on `date`, or the last trading day before it."""

    def latest_close(self, ticker: str) -> PriceQuote:
        return self.close_on(ticker, dt.date.today())


class YFinancePriceProvider(PriceProvider):
    """UNRELIABLE. Development use only; replace before relying on results."""

    name = "yfinance"
    reliable = False
    NOTE = "yfinance is an unofficial Yahoo scraper; verify before trusting"

    def __init__(self):
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:
            raise PriceError("yfinance not installed: pip install 'dhandho[prices]'") from exc
        self._yf = yfinance

    def close_on(self, ticker: str, date: dt.date) -> PriceQuote:
        yahoo = ticker.replace(".", "-")
        hist = self._yf.Ticker(yahoo).history(
            start=date - dt.timedelta(days=10), end=date + dt.timedelta(days=1), auto_adjust=False)
        if hist is None or hist.empty:
            raise PriceError(f"yfinance returned no prices for {ticker} near {date}")
        last = hist.iloc[-1]
        return PriceQuote(
            ticker=ticker,
            price=float(last["Close"]),
            currency="USD",
            as_of=hist.index[-1].date(),
            source=self.name,
            reliable=False,
            note=self.NOTE,
        )


_PROVIDERS = {"yfinance": YFinancePriceProvider}


def get_provider(name: str) -> PriceProvider:
    if name not in _PROVIDERS:
        raise PriceError(f"unknown price provider '{name}'; known: {sorted(_PROVIDERS)}")
    return _PROVIDERS[name]()
