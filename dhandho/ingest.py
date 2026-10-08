"""Ingest annual 10-K data for a ticker list into SQLite."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import Config
from .edgar import EdgarClient, EdgarError, normalize_ticker
from .facts import process_companyfacts

log = logging.getLogger(__name__)


def read_tickers(path: str | Path) -> list[str]:
    out: list[str] = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            t = normalize_ticker(line)
            if t not in out:
                out.append(t)
    if not out:
        raise ValueError(f"no tickers in {path}")
    return out


@dataclass
class IngestOutcome:
    ticker: str
    cik: int | None
    ok: bool
    message: str


def ingest(cfg: Config, client: EdgarClient, tickers: list[str], conn) -> list[IngestOutcome]:
    outcomes: list[IngestOutcome] = []
    try:
        tmap = client.ticker_map()
    except EdgarError as exc:
        msg = f"cannot load SEC ticker->CIK map: {exc}"
        for t in tickers:
            db.log_ingest(conn, t, None, "error", msg)
            outcomes.append(IngestOutcome(t, None, False, msg))
        return outcomes

    for ticker in tickers:
        if ticker not in tmap:
            msg = "ticker not found in SEC company_tickers.json"
            db.log_ingest(conn, ticker, None, "error", msg)
            outcomes.append(IngestOutcome(ticker, None, False, msg))
            continue
        cik, _ = tmap[ticker]
        try:
            facts = client.companyfacts(cik)
            result = process_companyfacts(facts, cfg.concepts, cfg.ingest)
            if not result.periods:
                raise ValueError("no annual 10-K periods found in companyfacts")
            result.cik = cik
            meta = client.cache_meta("companyfacts", f"CIK{cik:010d}") or {}
            sub = client.submissions(cik)
            sic = int(sub["sic"]) if str(sub.get("sic") or "").isdigit() else None
            db.store_company(conn, ticker, result, meta.get("url"), meta.get("fetched_at"),
                             sic=sic, sic_description=sub.get("sicDescription"))
            n_ok = sum(1 for v in result.values if v.status == "ok")
            msg = (
                f"FY{result.periods[0].fiscal_year}-FY{result.periods[-1].fiscal_year}: "
                f"{n_ok}/{len(result.values)} concept-years populated"
            )
            db.log_ingest(conn, ticker, cik, "ok", msg)
            outcomes.append(IngestOutcome(ticker, cik, True, msg))
            log.info("%s (CIK %d): %s", ticker, cik, msg)
        except (EdgarError, ValueError, KeyError) as exc:
            msg = f"{type(exc).__name__}: {exc}"
            db.log_ingest(conn, ticker, cik, "error", msg)
            outcomes.append(IngestOutcome(ticker, cik, False, msg))
            log.error("%s: %s", ticker, msg)
    return outcomes
