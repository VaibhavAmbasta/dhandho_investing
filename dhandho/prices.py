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
            import logging

            import yfinance  # noqa: F401
        except ImportError as exc:
            raise PriceError("yfinance not installed: pip install 'dhandho[prices]'") from exc
        # yfinance logs its own failures noisily; ours are captured as reasons instead.
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
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


class CsvPriceProvider(PriceProvider):
    """Prices you supply in a CSV (ticker,date,close,source). Reliable only if you say so."""

    name = "csv"

    def __init__(self, path, reliable: bool = False):
        import csv
        from pathlib import Path

        self.reliable = reliable
        self.rows: dict[str, list[tuple[dt.date, float, str]]] = {}
        p = Path(path)
        if not p.exists():
            return
        with p.open() as fh:
            for row in csv.DictReader(fh):
                if not row.get("ticker"):
                    continue
                t = row["ticker"].strip().upper().replace(".", "-")
                self.rows.setdefault(t, []).append(
                    (dt.date.fromisoformat(row["date"].strip()), float(row["close"]), (row.get("source") or "").strip()))
        for v in self.rows.values():
            v.sort()

    def close_on(self, ticker: str, date: dt.date) -> PriceQuote:
        rows = [r for r in self.rows.get(ticker, []) if r[0] <= date]
        if not rows:
            raise PriceError(f"no row for {ticker} on or before {date} in prices csv")
        d, close, source = rows[-1]
        return PriceQuote(ticker, close, "USD", d, f"csv:{source or 'unspecified'}", self.reliable,
                          None if self.reliable else "user-supplied price, not marked reliable")


class ChainPriceProvider(PriceProvider):
    """Try providers in order; collect every failure so none is silent."""

    name = "chain"

    def __init__(self, providers: list[PriceProvider], init_errors: list[str] | None = None):
        self.providers = providers
        self.init_errors = init_errors or []

    def close_on(self, ticker: str, date: dt.date) -> PriceQuote:
        errors = list(self.init_errors)
        for p in self.providers:
            try:
                return p.close_on(ticker, date)
            except Exception as exc:  # noqa: BLE001 - every provider failure is reported below
                errors.append(f"{p.name}: {type(exc).__name__}: {exc}")
        raise PriceError("; ".join(errors) or "no price providers configured")


def provider_from_config(cfg) -> ChainPriceProvider:
    pc = cfg.raw.get("prices", {})
    providers: list[PriceProvider] = []
    init_errors: list[str] = []
    for name in pc.get("providers", ["yfinance"]):
        try:
            if name == "csv":
                providers.append(CsvPriceProvider(cfg.root / pc.get("prices_file", "prices.csv"),
                                                  reliable=bool(pc.get("csv_reliable", False))))
            elif name == "yfinance":
                providers.append(YFinancePriceProvider())
            else:
                init_errors.append(f"unknown price provider '{name}'")
        except PriceError as exc:
            init_errors.append(f"{name}: {exc}")
    return ChainPriceProvider(providers, init_errors)
