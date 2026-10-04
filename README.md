# dhandho

A value-investing screener for US stocks in the Pabrai/Buffett/Graham style, built on
SEC EDGAR XBRL data. It is being built in phases; **Phase 1 (data layer) is implemented.**

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
```

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
