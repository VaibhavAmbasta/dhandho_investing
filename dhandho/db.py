"""SQLite storage. Every value carries the filing (accession) and filing date it came from."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pandas as pd

from .facts import CompanyResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    cik            INTEGER PRIMARY KEY,
    ticker         TEXT NOT NULL,
    name           TEXT,
    facts_url      TEXT,
    facts_fetched_at TEXT,
    sic            INTEGER,
    sic_description TEXT
);

-- dei:EntityCommonStockSharesOutstanding from every filing (cover page), for market cap.
CREATE TABLE IF NOT EXISTS cover_shares (
    cik      INTEGER NOT NULL,
    as_of    TEXT NOT NULL,
    value    REAL NOT NULL,
    accn     TEXT NOT NULL,
    form     TEXT NOT NULL,
    filed    TEXT NOT NULL
);

-- Every price used, with its source, so a valuation can be reproduced.
CREATE TABLE IF NOT EXISTS prices (
    ticker     TEXT NOT NULL,
    requested  TEXT NOT NULL,      -- the date asked for
    as_of      TEXT NOT NULL,      -- the trading day the price is from
    price      REAL NOT NULL,
    currency   TEXT NOT NULL,
    source     TEXT NOT NULL,
    reliable   INTEGER NOT NULL,
    note       TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (ticker, requested)
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at   TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    cik      INTEGER,
    status   TEXT NOT NULL,          -- ok | error
    message  TEXT
);

-- Every 10-K-family fact for a mapped tag, untouched, for traceability.
CREATE TABLE IF NOT EXISTS raw_facts (
    cik          INTEGER NOT NULL,
    tag          TEXT NOT NULL,
    unit         TEXT NOT NULL,
    period_start TEXT,
    period_end   TEXT NOT NULL,
    value        REAL NOT NULL,
    accn         TEXT NOT NULL,
    form         TEXT NOT NULL,
    filed        TEXT NOT NULL,
    fy           INTEGER,
    fp           TEXT,
    frame        TEXT
);
CREATE INDEX IF NOT EXISTS raw_facts_idx ON raw_facts (cik, tag, period_end);

CREATE TABLE IF NOT EXISTS fiscal_periods (
    cik          INTEGER NOT NULL,
    fiscal_year  INTEGER NOT NULL,
    period_end   TEXT NOT NULL,
    is_primary   INTEGER NOT NULL,
    note         TEXT,
    PRIMARY KEY (cik, fiscal_year)
);

-- One row per (fiscal year, concept, filing): every time a value was reported.
-- This is the point-in-time table: filter filed <= as_of.
CREATE TABLE IF NOT EXISTS concept_vintages (
    cik            INTEGER NOT NULL,
    fiscal_year    INTEGER NOT NULL,
    concept        TEXT NOT NULL,
    period_start   TEXT,
    period_end     TEXT NOT NULL,
    value          REAL NOT NULL,
    unit           TEXT NOT NULL,
    method         TEXT NOT NULL,      -- strategy label, e.g. Revenues or sum(A + B?)
    strategy_rank  INTEGER NOT NULL,
    components     TEXT NOT NULL,      -- JSON [{tag, value, start, end}]
    accn           TEXT NOT NULL,
    form           TEXT NOT NULL,
    filed          TEXT NOT NULL,
    notes          TEXT,
    PRIMARY KEY (cik, fiscal_year, concept, accn)
);

-- One row per (fiscal year, concept): latest-filed value, or NULL + reason.
-- This doubles as the tag log: method/strategy_rank say which tag was used.
CREATE TABLE IF NOT EXISTS concept_values (
    cik            INTEGER NOT NULL,
    ticker         TEXT NOT NULL,
    fiscal_year    INTEGER NOT NULL,
    concept        TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    status         TEXT NOT NULL,      -- ok | missing | not_applicable
    value          REAL,
    unit           TEXT NOT NULL,
    reason         TEXT,
    method         TEXT,
    strategy_rank  INTEGER,
    components     TEXT,
    accn           TEXT,
    form           TEXT,
    filed          TEXT,
    first_value    REAL,
    first_accn     TEXT,
    first_filed    TEXT,
    n_vintages     INTEGER NOT NULL DEFAULT 0,
    restated       INTEGER NOT NULL DEFAULT 0,
    notes          TEXT,
    PRIMARY KEY (cik, fiscal_year, concept),
    CHECK ((status = 'ok') = (value IS NOT NULL)),
    CHECK (status = 'ok' OR reason IS NOT NULL)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    for col, typ in (("sic", "INTEGER"), ("sic_description", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {typ}")
    conn.commit()


def _s(d: dt.date | None) -> str | None:
    return d.isoformat() if d else None


def log_ingest(conn: sqlite3.Connection, ticker: str, cik: int | None, status: str, message: str) -> None:
    conn.execute(
        "INSERT INTO ingest_log (run_at, ticker, cik, status, message) VALUES (?,?,?,?,?)",
        (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), ticker, cik, status, message),
    )
    conn.commit()


def store_company(
    conn: sqlite3.Connection, ticker: str, result: CompanyResult, facts_url: str | None, fetched_at: str | None,
    sic: int | None = None, sic_description: str | None = None,
) -> None:
    cik = result.cik
    with conn:
        for table in ("raw_facts", "fiscal_periods", "concept_vintages", "concept_values", "cover_shares"):
            conn.execute(f"DELETE FROM {table} WHERE cik = ?", (cik,))
        conn.execute(
            "INSERT OR REPLACE INTO companies (cik, ticker, name, facts_url, facts_fetched_at, sic, sic_description) "
            "VALUES (?,?,?,?,?,?,?)",
            (cik, ticker, result.name, facts_url, fetched_at, sic, sic_description),
        )
        conn.executemany(
            "INSERT INTO cover_shares VALUES (?,?,?,?,?,?)",
            [(cik, _s(f.end), f.val, f.accn, f.form, _s(f.filed)) for f in result.cover_shares],
        )
        conn.executemany(
            "INSERT INTO raw_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (cik, f.tag, f.unit, _s(f.start), _s(f.end), f.val, f.accn, f.form, _s(f.filed), f.fy, f.fp, f.frame)
                for f in result.raw_facts
            ],
        )
        conn.executemany(
            "INSERT INTO fiscal_periods VALUES (?,?,?,?,?)",
            [(cik, p.fiscal_year, _s(p.end), int(p.is_primary), p.note) for p in result.periods],
        )
        conn.executemany(
            "INSERT INTO concept_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    cik, v.fiscal_year, v.concept, _s(v.period_start), _s(v.period_end), v.value, v.unit,
                    v.strategy.label, v.strategy.rank, json.dumps(v.components), v.accn, v.form, _s(v.filed),
                    "; ".join(v.notes) or None,
                )
                for v in result.vintages
            ],
        )
        rows = []
        for c in result.values:
            lt, fs = c.latest, c.first
            rows.append(
                (
                    cik, ticker, c.fiscal_year, c.concept, _s(c.period_end), c.status,
                    lt.value if lt else None, c.unit, c.reason,
                    lt.strategy.label if lt else None, lt.strategy.rank if lt else None,
                    json.dumps(lt.components) if lt else None,
                    lt.accn if lt else None, lt.form if lt else None, _s(lt.filed) if lt else None,
                    fs.value if fs else None, fs.accn if fs else None, _s(fs.filed) if fs else None,
                    c.n_vintages, int(c.restated), ("; ".join(lt.notes) or None) if lt else None,
                )
            )
        conn.executemany(
            "INSERT INTO concept_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )


def concept_values_df(conn: sqlite3.Connection, tickers: list[str] | None = None) -> pd.DataFrame:
    q = "SELECT * FROM concept_values"
    params: list = []
    if tickers:
        q += f" WHERE ticker IN ({','.join('?' * len(tickers))})"
        params = list(tickers)
    return pd.read_sql_query(q + " ORDER BY ticker, concept, fiscal_year", conn, params=params)


def values_as_of(conn: sqlite3.Connection, cik: int, as_of: dt.date | str) -> pd.DataFrame:
    """Point-in-time view: for each (fiscal_year, concept), among filings dated <= as_of,
    the best-ranked strategy and within it the latest filing (same rule as concept_values).
    Facts filed after as_of are invisible."""
    as_of = as_of.isoformat() if isinstance(as_of, dt.date) else as_of
    q = """
    SELECT * FROM (
        SELECT v.*, ROW_NUMBER() OVER (
            PARTITION BY fiscal_year, concept
            ORDER BY strategy_rank ASC, filed DESC, accn DESC) AS rn
        FROM concept_vintages v WHERE cik = ? AND filed <= ?
    ) WHERE rn = 1
    ORDER BY concept, fiscal_year
    """
    return pd.read_sql_query(q, conn, params=(cik, as_of)).drop(columns="rn")
