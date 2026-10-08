# dhandho

A value-investing screener for US stocks in the Pabrai/Buffett/Graham style, built on
SEC EDGAR XBRL data. It is being built in phases; **Phase 1 (data layer) and Phase 2 (reverse DCF) are implemented.**

## Setup

```bash
pip install -e '.[dev]'          # add ,prices for the (unreliable) yfinance provider
pytest
```

Network access to `data.sec.gov` and `www.sec.gov` is required for the first fetch.
After that, everything is served from `data/raw/` (gzipped raw SEC JSON). Data is never
refetched unless you pass `--refresh`.

## Commands

```bash
dhandho ingest                 # tickers.txt -> SQLite (data/dhandho.sqlite)
dhandho ingest --refresh       # ignore the cache and refetch
dhandho coverage [--reasons]   # reports/coverage.{html,txt}, coverage_long.csv, tag_log.csv
dhandho verify AAPL --years 3  # every raw number next to its XBRL tag and source filing
dhandho dcf [TICKER ...] [--date YYYY-MM-DD] [--refresh-prices]
                               # reports/reverse_dcf.{txt,csv}, reverse_dcf_sensitivity.csv
```

Prices are tried in the order of `prices.providers`. `prices.csv` (`ticker,date,close,source`) lets you
supply quotes from any source; yfinance is the unreliable fallback and needs network access to
`query1.finance.yahoo.com`, `query2.finance.yahoo.com` and `fc.yahoo.com`. Every price used is cached
in the `prices` table, so a run can be reproduced.

## Layout

| Path | What |
|------|------|
| `config.yaml` | SEC settings, every threshold, and the concept → ordered-tag-strategy mapping |
| `tickers.txt` | Universe |
| `dhandho/edgar.py` | Rate-limited (< 10 req/s), disk-cached SEC client |
| `dhandho/facts.py` | companyfacts → fiscal years → per-filing concept values → canonical value or NULL + reason |
| `dhandho/db.py` | SQLite schema; `values_as_of()` for point-in-time queries |
| `dhandho/coverage.py` | Coverage matrix, tag switches, missing values, restatements, split warnings |
| `dhandho/verify.py` | Hand-verification printout |
| `dhandho/prices.py` | `PriceProvider` interface; `YFinancePriceProvider` (**unreliable**, development only) |
| `dhandho/shares.py` | Split adjustment inferred from restated SEC comparatives |
| `dhandho/dcf.py` | Valuation maths (pure functions) |
| `dhandho/valuation.py` | Phase 2 runner: inputs with provenance, NULL + reason, stated assumptions |
| `JUDGMENT_CALLS.md` | Every accounting and data choice that changes numbers |

## Storage model

- `raw_facts` holds every 10-K-family fact for a mapped tag, unmodified.
- `concept_vintages` has one row per (fiscal year, concept, filing): the value as reported in that filing,
  the strategy and tags used, its components, the accession number and the filing date.
  This is the point-in-time table.
- `concept_values` has one row per (fiscal year, concept): the latest-filed value, or NULL with a
  `reason`. It also holds the first-filed value, the vintage count and a `restated` flag. It doubles
  as the tag log. Schema `CHECK` constraints forbid a NULL value without a reason.
- `ingest_log` records every ticker attempt, including failures.
