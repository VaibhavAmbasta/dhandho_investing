"""Render Phase 2 reverse-DCF results: ranked CSV, sensitivity CSV, text summary."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .valuation import COLUMNS, Result


def _pct(x) -> str:
    return "NULL" if x is None else f"{x:+.1%}"


def _px(x) -> str:
    return "NULL" if x is None else f"{x:,.2f}"


def _b(x) -> str:
    return "NULL" if x is None else f"{x / 1e9:,.1f}B"


def rank(results: list[Result]) -> list[Result]:
    """Most negative gap first: the price implies less growth than the business delivered."""
    def key(r: Result):
        gap = r.values.get("gap_vs_fcf")
        return (r.status != "ok", gap is None, gap if gap is not None else 0.0)
    return sorted(results, key=key)


def write_csvs(results: list[Result], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    p1 = out_dir / "reverse_dcf.csv"
    with p1.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "ticker", "status"] + COLUMNS + ["reasons", "assumptions", "warnings", "inputs"])
        for i, r in enumerate(rank(results), 1):
            w.writerow([i if r.status == "ok" else "", r.ticker, r.status] + [r.values.get(c) for c in COLUMNS]
                       + [json.dumps(r.reasons), " | ".join(r.assumptions), " | ".join(r.warnings),
                          json.dumps(r.inputs, default=str)])
    p2 = out_dir / "reverse_dcf_sensitivity.csv"
    with p2.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "discount_rate", "terminal_multiple", "implied_growth", "reason"])
        for r in results:
            for s in r.sensitivity:
                w.writerow([r.ticker, s["discount_rate"], s["terminal_multiple"], s["implied_growth"], s["reason"]])
    return [p1, p2]


def render_text(results: list[Result], cfg) -> str:
    rd = cfg.raw["reverse_dcf"]
    L: list[str] = []
    L.append(f"Reverse DCF: base case {rd['discount_rate']:.0%} discount, {rd['terminal_multiple']}x terminal "
             f"owner FCF, {rd['years']} years, base FCF = {rd['base_fcf']}, "
             f"dilution {'modelled' if rd.get('model_dilution', True) else 'not modelled'}.")
    L.append("Gap = implied growth - historical 10y owner-FCF CAGR. Negative = price asks for less growth than delivered.")
    L.append("px@hist = share price if growth equalled the historical owner-FCF CAGR. "
             "px@0g = earning power value per share: worth if it never grows (upkeep capex only).")
    L.append("growth prem = share of today's price that is a bet on growth (1 - earning power value / EV).")
    L.append("")
    hdr = (f"{'#':>2} {'ticker':<6} {'FY':>4} {'mcap':>9} {'net debt':>9} {'base FCF':>9} {'implied':>8} "
           f"{'rev CAGR':>8} {'FCF CAGR':>8} {'gap FCF':>8} {'price':>9} {'px@hist':>9} {'px@0g':>9} "
           f"{'growth prem':>11}")
    L.append(hdr)
    L.append("-" * len(hdr))
    for i, r in enumerate(rank(results), 1):
        v = r.values
        if r.status != "ok":
            L.append(f"{'':>2} {r.ticker:<6} EXCLUDED: {r.reasons.get('status')}")
            continue
        gp = v.get("growth_premium")
        L.append(f"{i:>2} {r.ticker:<6} {v.get('fiscal_year') or '':>4} {_b(v.get('market_cap')):>9} "
                 f"{_b(v.get('net_debt')):>9} {_b(v.get('base_fcf')):>9} {_pct(v.get('implied_growth')):>8} "
                 f"{_pct(v.get('hist_revenue_cagr')):>8} {_pct(v.get('hist_fcf_cagr')):>8} "
                 f"{_pct(v.get('gap_vs_fcf')):>8} {_px(v.get('price')):>9} "
                 f"{_px(v.get('price_at_hist_growth')):>9} {_px(v.get('price_at_zero_growth')):>9} "
                 f"{'NULL' if gp is None else f'{gp:.0%}':>11}")
    L.append("")
    L.append("Starting owner FCF under each method (the solver uses the one in config), and upkeep vs total capex:")
    hdr2 = (f"   {'ticker':<6} {'latest':>9} {'avg3':>9} {'normalized':>10} {'med margin':>10} "
            f"{'total capex':>11} {'upkeep capex':>12} {'owner earn.':>11} {'FCF CAGR mean':>13} {'FCF CAGR norm':>13}")
    L.append(hdr2)
    for r in rank(results):
        if r.status != "ok":
            continue
        v = r.values
        m = v.get("fcf_margin_median")
        L.append(f"   {r.ticker:<6} {_b(v.get('base_fcf_latest')):>9} {_b(v.get('base_fcf_avg3')):>9} "
                 f"{_b(v.get('base_fcf_normalized')):>10} {'NULL' if m is None else f'{m:.1%}':>10} "
                 f"{_b(v.get('capex_total')):>11} {_b(v.get('maintenance_capex')):>12} "
                 f"{_b(v.get('owner_earnings_normalized')):>11} {_pct(v.get('hist_fcf_cagr_mean')):>13} "
                 f"{_pct(v.get('hist_fcf_cagr_normalized')):>13}")
    for r in rank(results):
        if r.status != "ok":
            continue
        L.append("")
        L.append(f"== {r.ticker}  {r.values.get('name')}")
        nulls = {k: v for k, v in r.reasons.items() if r.values.get(k) is None}
        for k, why in nulls.items():
            L.append(f"  NULL {k}: {why}")
        if r.sensitivity and any(s["implied_growth"] is not None for s in r.sensitivity):
            mults = rd["sensitivity"]["terminal_multiples"]
            L.append("  implied growth   " + "".join(f"{m:>8}x" for m in mults))
            for dr in rd["sensitivity"]["discount_rates"]:
                cells = [next(s for s in r.sensitivity if s["discount_rate"] == dr and s["terminal_multiple"] == m)
                         for m in mults]
                L.append(f"  discount {dr:>5.0%}   " + "".join(f"{_pct(c['implied_growth']):>9}" for c in cells))
        for e in r.inputs.get("share_events", []):
            L.append(f"  shares: {e['kind']} x{e['ratio']:.3f} between filings of {e['before_filed']} and "
                     f"{e['after_filed']}{'' if e['applied'] else ' (NOT applied)'}")
        for a in r.assumptions:
            L.append(f"  ASSUMED: {a}")
        for w in r.warnings:
            L.append(f"  WARNING: {w}")
    return "\n".join(L)
