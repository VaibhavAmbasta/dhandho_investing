"""dhandho command line.

  dhandho ingest   [--tickers FILE] [--refresh]   fetch (or reuse cached) SEC data -> SQLite
  dhandho coverage                                coverage report (text + HTML + CSV)
  dhandho verify TICKER [--years 3]               raw numbers next to XBRL tags, for hand-checking
  dhandho dcf [--date YYYY-MM-DD] [--refresh-prices]  reverse DCF (Phase 2)
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from . import coverage, db, dcf_report, valuation, verify
from .config import ConfigError, load_config
from .edgar import client_from_config, normalize_ticker
from .ingest import ingest, read_tickers


def _cmd_ingest(cfg, args) -> int:
    tickers = read_tickers(args.tickers or cfg.path("tickers_file"))
    client = client_from_config(cfg, refresh=args.refresh)
    conn = db.connect(cfg.path("db_path"))
    outcomes = ingest(cfg, client, tickers, conn)
    failed = [o for o in outcomes if not o.ok]
    for o in outcomes:
        print(f"{'OK   ' if o.ok else 'ERROR'} {o.ticker:<6} {o.message}")
    print(f"\n{len(outcomes) - len(failed)}/{len(outcomes)} tickers ingested; "
          f"{client.network_calls} network requests (rest served from cache).")
    return 1 if failed else 0


def _cmd_coverage(cfg, args) -> int:
    conn = db.connect(cfg.path("db_path"))
    covs = coverage.build(conn, cfg)
    if not covs:
        print("no data; run `dhandho ingest` first", file=sys.stderr)
        return 1
    out_dir = Path(args.out) if args.out else cfg.path("reports_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    text = coverage.render_text(covs, cfg, verbose_missing=args.reasons)
    (out_dir / "coverage.txt").write_text(text + "\n")
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    (out_dir / "coverage.html").write_text(coverage.render_html(covs, cfg, stamp))
    csvs = coverage.write_csvs(conn, out_dir)
    print(text)
    print(f"\nwrote {out_dir / 'coverage.html'}, {out_dir / 'coverage.txt'}, " + ", ".join(str(p) for p in csvs))
    return 0


def _cmd_verify(cfg, args) -> int:
    conn = db.connect(cfg.path("db_path"))
    text = verify.render(conn, normalize_ticker(args.ticker), args.years, list(cfg.concepts))
    out_dir = cfg.path("reports_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"verify_{normalize_ticker(args.ticker)}.txt"
    path.write_text(text + "\n")
    print(text)
    print(f"\nwrote {path}")
    return 0


def _cmd_dcf(cfg, args) -> int:
    from .prices import provider_from_config

    conn = db.connect(cfg.path("db_path"))
    tickers = [normalize_ticker(t) for t in args.tickers] if args.tickers else [
        r[0] for r in conn.execute("SELECT ticker FROM companies ORDER BY ticker")]
    if not tickers:
        print("no data; run `dhandho ingest` first", file=sys.stderr)
        return 1
    date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    provider = provider_from_config(cfg)
    results = [valuation.value_ticker(conn, cfg, t, provider, date, args.refresh_prices) for t in tickers]
    valuation.store(conn, results)
    out_dir = cfg.path("reports_dir")
    csvs = dcf_report.write_csvs(results, out_dir)
    text = dcf_report.render_text(results, cfg)
    (out_dir / "reverse_dcf.txt").write_text(text + "\n")
    print(text)
    print(f"\nwrote {out_dir / 'reverse_dcf.txt'}, " + ", ".join(str(p) for p in csvs))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dhandho", description="Value-investing screener on SEC EDGAR data")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="ingest 10-K data for tickers")
    s.add_argument("--tickers", help="ticker file (default: paths.tickers_file)")
    s.add_argument("--refresh", action="store_true", help="refetch even if cached")
    s.set_defaults(fn=_cmd_ingest)

    s = sub.add_parser("coverage", help="write the coverage report")
    s.add_argument("--out", help="output directory (default: paths.reports_dir)")
    s.add_argument("--reasons", action="store_true", help="include missing-value reasons in text output")
    s.set_defaults(fn=_cmd_coverage)

    s = sub.add_parser("verify", help="print raw numbers next to XBRL tags for hand-checking")
    s.add_argument("ticker")
    s.add_argument("--years", type=int, default=3)
    s.set_defaults(fn=_cmd_verify)

    s = sub.add_parser("dcf", help="reverse DCF: growth implied by the current price")
    s.add_argument("tickers", nargs="*", help="default: every ingested ticker")
    s.add_argument("--date", help="price date (default: today)")
    s.add_argument("--refresh-prices", action="store_true", help="ignore cached prices")
    s.set_defaults(fn=_cmd_dcf)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
        return args.fn(cfg, args)
    except (ConfigError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
