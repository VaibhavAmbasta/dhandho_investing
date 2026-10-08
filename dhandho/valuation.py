"""Phase 2: reverse DCF per ticker.

Every number in the output carries provenance (concept, fiscal year, tag/method,
accession, filing date) in `inputs`. Every NULL carries a reason in `reasons`.
Every place a NULL input was treated as zero is listed in `assumptions`.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .dcf import cagr, enterprise_value_implied, implied_growth
from .prices import PriceError, PriceProvider
from .shares import adjust_diluted_shares


@dataclass
class Result:
    ticker: str
    cik: int
    status: str = "ok"  # ok | excluded
    values: dict[str, Any] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sensitivity: list[dict[str, Any]] = field(default_factory=list)

    def set(self, key: str, value: Any, reason: str | None = None) -> None:
        self.values[key] = value
        if value is None:
            self.reasons[key] = reason or "not computed"


class _Facts:
    def __init__(self, conn: sqlite3.Connection, cik: int):
        self.rows = {(r["concept"], r["fiscal_year"]): dict(r) for r in conn.execute(
            "SELECT * FROM concept_values WHERE cik = ?", (cik,))}
        self.ever_ok = {c for (c, _), r in self.rows.items() if r["status"] == "ok"}

    def get(self, concept: str, fy: int) -> dict | None:
        return self.rows.get((concept, fy))

    def value(self, concept: str, fy: int) -> tuple[float | None, str | None, dict | None]:
        r = self.get(concept, fy)
        if r is None:
            return None, f"{concept} FY{fy}: no fiscal-year row", None
        if r["status"] != "ok":
            return None, f"{concept} FY{fy}: {r['reason']}", None
        prov = {"concept": concept, "fiscal_year": fy, "value": r["value"], "method": r["method"],
                "accn": r["accn"], "filed": r["filed"]}
        return r["value"], None, prov


@dataclass
class CashYear:
    """One fiscal year's cash economics. Every None has a reason."""

    fcf: float | None                  # owner FCF: OCF - total capex - SBC
    fcf_reason: str | None
    owner_earnings: float | None       # OCF - upkeep capex - SBC (no-growth cash generation)
    oe_reason: str | None
    capex_total: float | None = None
    maintenance_capex: float | None = None
    provenance: list[dict] = field(default_factory=list)
    assumptions: list[tuple[str, int]] = field(default_factory=list)  # (text, fiscal year)


def cash_year(facts: _Facts, fy: int, rd: dict) -> CashYear:
    ocf, r1, p1 = facts.value("operating_cash_flow", fy)
    capex, r2, p2 = facts.value("capex", fy)
    sbc, r3, p3 = facts.value("stock_based_comp", fy)
    assumptions: list[tuple[str, int]] = []
    if ocf is None or capex is None:
        why = "; ".join(r for r in (r1, r2) if r)
        return CashYear(None, why, None, why)
    if sbc is None:
        if "stock_based_comp" in facts.ever_ok:
            why = f"SBC missing this year but reported in others ({r3})"
            return CashYear(None, why, None, why)
        sbc = 0.0
        assumptions.append(("SBC never reported by this company; treated as 0", fy))
    prov = [p for p in (p1, p2, p3) if p]

    capex_total = capex
    include_fl = rd.get("capex", {}).get("include_finance_lease_additions", True)
    if include_fl:
        fl, _, pf = facts.value("finance_lease_additions", fy)
        if fl is None:
            if "finance_lease_additions" in facts.ever_ok:
                assumptions.append(("finance-lease asset additions missing; treated as 0", fy))
            fl = 0.0
        elif pf:
            prov.append(pf)
        capex_total += fl
    fcf = ocf - capex_total - sbc

    # Upkeep capex proxy: PP&E depreciation (+ finance-lease asset amortization), capped at total capex.
    dep, _, pd = facts.value("depreciation", fy)
    if dep is None:
        da, _, pda = facts.value("depreciation_amortization", fy)
        if da is not None:
            am, _, pam = facts.value("amortization_intangibles", fy)
            if am is None:
                assumptions.append(("only total D&A reported; used as depreciation (includes any intangible "
                                    "amortization, so upkeep capex may be overstated)", fy))
                am = 0.0
            dep, pd = da - am, pda
    if dep is None:
        return CashYear(fcf, None, None, "no depreciation or D&A reported", capex_total, None, prov, assumptions)
    if pd:
        prov.append(pd)
    if include_fl:
        fla, _, pfa = facts.value("finance_lease_amortization", fy)
        dep += fla or 0.0
    maintenance = min(capex_total, dep)
    return CashYear(fcf, None, ocf - maintenance - sbc, None, capex_total, maintenance, prov, assumptions)


def normalized_from_margins(series: dict[int, float | None], revenue: dict[int, float | None], last_fy: int,
                            years: int, min_years: int) -> tuple[float | None, float | None, str | None]:
    """Median of (value / revenue) over the last `years` fiscal years x latest revenue.
    Returns (normalized value, median margin, reason)."""
    import statistics

    rev_last = revenue.get(last_fy)
    if not rev_last or rev_last <= 0:
        return None, None, f"no positive revenue for FY{last_fy}"
    margins = [series[y] / revenue[y] for y in range(last_fy - years + 1, last_fy + 1)
               if series.get(y) is not None and revenue.get(y) and revenue[y] > 0]
    if len(margins) < min_years:
        return None, None, f"only {len(margins)} of {years} years have both the value and revenue (need {min_years})"
    m = statistics.median(margins)
    return m * rev_last, m, None


def hist_cagr(series: dict[int, float | None], last_fy: int, span: int, smooth: int, min_span: int
              ) -> tuple[float | None, int | None, str | None]:
    """CAGR between trailing-`smooth`-year means ending at last_fy and last_fy - span.
    Falls back to shorter spans down to min_span. Returns (cagr, span_used, reason)."""
    last_reason = None
    for s in range(span, min_span - 1, -1):
        end_years = [last_fy - i for i in range(smooth)]
        start_years = [last_fy - s - i for i in range(smooth)]
        missing = [y for y in end_years + start_years if series.get(y) is None]
        if missing:
            last_reason = f"missing FY{', FY'.join(str(y) for y in sorted(missing))} for a {s}-year span"
            continue
        end = sum(series[y] for y in end_years) / smooth
        start = sum(series[y] for y in start_years) / smooth
        try:
            return cagr(start, end, s), s, None
        except ValueError as exc:
            return None, s, str(exc)
    return None, None, last_reason


def hist_cagr_normalized(fcf: dict[int, float | None], revenue: dict[int, float | None], last_fy: int,
                         span: int, min_span: int, margin_years: int, min_years: int
                         ) -> tuple[float | None, int | None, str | None]:
    """CAGR between margin-normalized owner FCF at last_fy and at last_fy - span (each = median
    margin of the trailing `margin_years` x that year's revenue). The same normalization as the
    starting FCF, so history and base are compared like for like."""
    end, _, why = normalized_from_margins(fcf, revenue, last_fy, margin_years, min_years)
    if end is None:
        return None, None, f"end: {why}"
    last_reason = None
    for s in range(span, min_span - 1, -1):
        start, _, why = normalized_from_margins(fcf, revenue, last_fy - s, margin_years, min_years)
        if start is None:
            last_reason = f"start FY{last_fy - s}: {why}"
            continue
        try:
            return cagr(start, end, s), s, None
        except ValueError as exc:
            return None, s, str(exc)
    return None, None, last_reason


def _excluded(cfg: Config, sic: int | None) -> str | None:
    if sic is None:
        return None
    for lo, hi in cfg.raw.get("screen", {}).get("exclude_sic_ranges", []):
        if lo <= sic <= hi:
            return f"SIC {sic} is in excluded range {lo}-{hi} (financials)"
    return None


def get_price(conn: sqlite3.Connection, provider: PriceProvider, ticker: str, date: dt.date, refresh: bool):
    """Cached in the prices table so a valuation is reproducible."""
    if not refresh:
        r = conn.execute("SELECT * FROM prices WHERE ticker = ? AND requested = ?", (ticker, date.isoformat())).fetchone()
        if r:
            return dict(r)
    q = provider.close_on(ticker, date)
    row = {"ticker": ticker, "requested": date.isoformat(), "as_of": q.as_of.isoformat(), "price": q.price,
           "currency": q.currency, "source": q.source, "reliable": int(q.reliable), "note": q.note,
           "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    conn.execute("INSERT OR REPLACE INTO prices VALUES (:ticker,:requested,:as_of,:price,:currency,:source,"
                 ":reliable,:note,:fetched_at)", row)
    conn.commit()
    return row


def value_ticker(conn: sqlite3.Connection, cfg: Config, ticker: str, provider: PriceProvider,
                 price_date: dt.date, refresh_prices: bool = False) -> Result:
    comp = conn.execute("SELECT * FROM companies WHERE ticker = ?", (ticker,)).fetchone()
    if comp is None:
        raise ValueError(f"{ticker} not ingested")
    cik = comp["cik"]
    res = Result(ticker, cik)
    rd = cfg.raw["reverse_dcf"]
    res.values.update({"name": comp["name"], "sic": comp["sic"]})

    why = _excluded(cfg, comp["sic"])
    if why:
        res.status = "excluded"
        res.reasons["status"] = why
        return res

    facts = _Facts(conn, cik)
    last_fy = conn.execute("SELECT MAX(fiscal_year) FROM fiscal_periods WHERE cik = ?", (cik,)).fetchone()[0]
    period_end = conn.execute("SELECT period_end FROM fiscal_periods WHERE cik = ? AND fiscal_year = ?",
                              (cik, last_fy)).fetchone()[0]
    res.values.update({"fiscal_year": last_fy, "fy_end": period_end})

    # ---- owner FCF history and base ----------------------------------------
    norm = rd.get("normalization", {"margin_years": 5, "min_years": 3})
    lookback = max(rd["history"]["endpoint_smoothing_years"], norm["margin_years"])
    first_fy = last_fy - rd["history"]["cagr_years"] - lookback - 1
    fcf_series: dict[int, float | None] = {}
    oe_series: dict[int, float | None] = {}
    rev_series: dict[int, float | None] = {}
    grouped: dict[str, list[int]] = {}
    years: dict[int, CashYear] = {}
    for fy in range(first_fy, last_fy + 1):
        cy = cash_year(facts, fy, rd)
        years[fy] = cy
        fcf_series[fy], oe_series[fy] = cy.fcf, cy.owner_earnings
        rev_series[fy] = facts.value("revenue", fy)[0]
        for text, y in cy.assumptions:
            grouped.setdefault(text, []).append(y)
    for text, ys in grouped.items():
        span = f"FY{min(ys)}" if len(ys) == 1 else f"FY{min(ys)}-FY{max(ys)}"
        res.assumptions.append(f"{text} ({span}, {len(ys)} yr)")
    latest = years[last_fy]
    res.inputs["owner_fcf_latest"] = latest.provenance
    res.set("capex_total", latest.capex_total, latest.fcf_reason)
    res.set("maintenance_capex", latest.maintenance_capex, latest.oe_reason)

    res.set("base_fcf_latest", latest.fcf, latest.fcf_reason)
    last3 = [fcf_series.get(last_fy - i) for i in range(3)]
    res.set("base_fcf_avg3", sum(last3) / 3 if all(v is not None for v in last3) else None,
            "owner FCF missing in one of the last 3 years")
    v, m, why = normalized_from_margins(fcf_series, rev_series, last_fy, norm["margin_years"], norm["min_years"])
    res.set("base_fcf_normalized", v, why)
    res.set("fcf_margin_median", m, why)
    mode = rd["base_fcf"]
    key = {"latest": "base_fcf_latest", "avg3": "base_fcf_avg3", "normalized": "base_fcf_normalized"}[mode]
    res.set("base_fcf", res.values.get(key), res.reasons.get(key))
    res.values["base_fcf_mode"] = mode

    if rd.get("earning_power", {}).get("enabled", True):
        v, m, why = normalized_from_margins(oe_series, rev_series, last_fy, norm["margin_years"], norm["min_years"])
        res.set("owner_earnings_normalized", v, why or latest.oe_reason)
    else:
        res.set("owner_earnings_normalized", None, "earning power disabled in config")

    # ---- net debt -----------------------------------------------------------
    nd_cfg = rd["net_debt"]
    parts = [("total_debt", +1), ("cash", -1), ("short_term_investments", -1)]
    if nd_cfg.get("include_operating_leases", True):
        parts.append(("operating_lease_liabilities", +1))
    if nd_cfg.get("include_long_term_investments", True):
        parts.append(("long_term_investments", -1))
    net_debt = 0.0
    res.inputs["net_debt"] = []
    for concept, sign in parts:
        v, reason, prov = facts.value(concept, last_fy)
        if v is None:
            res.assumptions.append(f"{concept} FY{last_fy} treated as 0 in net debt ({reason})")
            continue
        net_debt += sign * v
        res.inputs["net_debt"].append({**prov, "sign": sign})
    if nd_cfg.get("include_held_to_maturity", True):
        v, reason, prov = facts.value("held_to_maturity_securities", last_fy)
        if v is not None:
            others = [c for c in ("short_term_investments", "long_term_investments")
                      if facts.value(c, last_fy)[0] is not None]
            if others:
                res.assumptions.append(
                    f"held-to-maturity securities {v:,.0f} NOT added to cash: {', '.join(others)} also reported "
                    f"and probably include them")
            else:
                net_debt -= v
                res.inputs["net_debt"].append({**prov, "sign": -1})
    res.set("net_debt", net_debt)

    # ---- shares, dilution -----------------------------------------------------
    adj = adjust_diluted_shares(conn, cik, cfg.ingest.get("split_ratio_tolerance", 0.02))
    res.inputs["share_events"] = [e.__dict__ for e in adj.events]
    res.warnings.extend(adj.warnings)
    lb = rd["dilution_lookback_years"]
    s_end, s_start = adj.by_year.get(last_fy), adj.by_year.get(last_fy - lb)
    if s_end and s_start:
        res.set("dilution_rate", cagr(s_start, s_end, lb))
        res.inputs["dilution"] = {"from_fy": last_fy - lb, "from_shares": s_start, "to_fy": last_fy,
                                  "to_shares": s_end, "split_adjusted": True}
    else:
        res.set("dilution_rate", None, f"split-adjusted diluted shares missing for FY{last_fy - lb} or FY{last_fy}")

    cover = conn.execute(
        "SELECT * FROM cover_shares WHERE cik = ? ORDER BY as_of DESC, filed DESC", (cik,)).fetchall()
    shares_out = None
    if not cover:
        res.reasons["shares_outstanding"] = "no dei:EntityCommonStockSharesOutstanding reported"
    else:
        top = [r for r in cover if r["as_of"] == cover[0]["as_of"] and r["accn"] == cover[0]["accn"]]
        if len({r["value"] for r in top}) > 1:
            res.reasons["shares_outstanding"] = "multiple cover-page share counts (multiple share classes?)"
        else:
            shares_out = top[0]["value"]
            res.inputs["shares_outstanding"] = {"tag": "dei:EntityCommonStockSharesOutstanding", "value": shares_out,
                                                "as_of": top[0]["as_of"], "accn": top[0]["accn"],
                                                "form": top[0]["form"], "filed": top[0]["filed"]}
            age = (price_date - dt.date.fromisoformat(top[0]["as_of"])).days
            if age > 120:
                res.warnings.append(f"cover-page share count is {age} days old (as of {top[0]['as_of']})")
            if s_end and not 0.85 <= shares_out / s_end <= 1.15:
                res.warnings.append(
                    f"cover shares {shares_out:,.0f} vs FY{last_fy} diluted {s_end:,.0f} differ by "
                    f"{shares_out / s_end - 1:+.0%}: split after the cover date, or multiple share classes?")
    res.set("shares_outstanding", shares_out, res.reasons.get("shares_outstanding"))

    # ---- price, market cap ----------------------------------------------------
    price = None
    try:
        q = get_price(conn, provider, ticker, price_date, refresh_prices)
        price = q["price"]
        res.values.update({"price_date": q["as_of"], "price_source": q["source"], "price_reliable": bool(q["reliable"])})
        res.inputs["price"] = q
        age = (price_date - dt.date.fromisoformat(q["as_of"])).days
        if age > cfg.raw.get("prices", {}).get("max_price_age_days", 7):
            res.warnings.append(f"price is {age} days old (as of {q['as_of']})")
        if not q["reliable"]:
            res.warnings.append(f"price source {q['source']} is not marked reliable")
    except PriceError as exc:
        res.reasons["price"] = f"no price: {exc}"
    res.set("price", price, res.reasons.get("price"))
    mcap = price * shares_out if price is not None and shares_out is not None else None
    res.set("market_cap", mcap, "needs price and shares outstanding")
    ev = mcap + net_debt if mcap is not None else None
    res.set("ev", ev, "needs market cap")

    # ---- historical CAGRs -----------------------------------------------------
    h = rd["history"]
    g, span, reason = hist_cagr(rev_series, last_fy, h["cagr_years"], h["endpoint_smoothing_years"], h["min_cagr_years"])
    res.set("hist_revenue_cagr", g, reason)
    res.values["hist_revenue_span"] = span
    g, span, reason = hist_cagr(fcf_series, last_fy, h["cagr_years"], h["endpoint_smoothing_years"], h["min_cagr_years"])
    res.set("hist_fcf_cagr_mean", g, reason)
    g2, span2, reason2 = hist_cagr_normalized(fcf_series, rev_series, last_fy, h["cagr_years"], h["min_cagr_years"],
                                              norm["margin_years"], norm["min_years"])
    res.set("hist_fcf_cagr_normalized", g2, reason2)
    if h.get("fcf_endpoints", "normalized") == "normalized":
        res.set("hist_fcf_cagr", g2, reason2)
        res.values["hist_fcf_span"] = span2
    else:
        res.set("hist_fcf_cagr", g, reason)
        res.values["hist_fcf_span"] = span
    res.inputs["owner_fcf_series"] = {str(k): v for k, v in fcf_series.items()}

    # ---- implied growth -------------------------------------------------------
    base = res.values.get("base_fcf")
    d = res.values.get("dilution_rate")
    bounds = tuple(rd["growth_bounds"])
    years = rd["years"]

    def solve(r: float, m: float, dil: float | None) -> tuple[float | None, str | None]:
        if ev is None:
            return None, res.reasons.get("ev") or "no EV"
        if base is None:
            return None, res.reasons.get("base_fcf")
        if dil is None:
            return None, res.reasons.get("dilution_rate")
        out = implied_growth(ev, base, r, m, years, dil, bounds)
        return out.growth, out.reason

    use_dil = rd.get("model_dilution", True)
    g, reason = solve(rd["discount_rate"], rd["terminal_multiple"], d if use_dil else 0.0)
    res.set("implied_growth", g, reason)
    g0, reason0 = solve(rd["discount_rate"], rd["terminal_multiple"], 0.0)
    res.set("implied_growth_no_dilution", g0, reason0)
    for r in rd["sensitivity"]["discount_rates"]:
        for m in rd["sensitivity"]["terminal_multiples"]:
            gs, rs = solve(r, m, d if use_dil else 0.0)
            res.sensitivity.append({"discount_rate": r, "terminal_multiple": m, "implied_growth": gs, "reason": rs})

    # Price-independent anchor: the share price at which the market would be paying
    # for exactly the historical owner-FCF growth (base case, dilution as modelled).
    hg = res.values.get("hist_fcf_cagr")
    dil = (d if use_dil else 0.0)
    if None in (base, hg, dil, shares_out):
        res.set("price_at_hist_growth", None,
                "needs base FCF, historical FCF CAGR, dilution rate and shares outstanding")
    elif base <= 0:
        res.set("price_at_hist_growth", None, "base owner FCF is not positive")
    else:
        pv = enterprise_value_implied(base, hg, rd["discount_rate"], rd["terminal_multiple"], years, dil)
        res.set("price_at_hist_growth", (pv - net_debt) / shares_out)

    # Earning power value: what the business is worth if it never grows, i.e. owner
    # earnings after upkeep capex only, capitalised at the discount rate.
    oe = res.values.get("owner_earnings_normalized")
    if oe is None:
        for k in ("epv", "growth_premium", "price_at_zero_growth"):
            res.set(k, None, f"needs normalized owner earnings ({res.reasons.get('owner_earnings_normalized')})")
    else:
        epv = oe / rd["discount_rate"]
        res.set("epv", epv)
        res.set("growth_premium", 1 - epv / ev if ev and ev > 0 else None, "needs a positive EV")
        res.set("price_at_zero_growth", (epv - net_debt) / shares_out if shares_out else None,
                "needs shares outstanding")

    for key, hist in (("gap_vs_fcf", "hist_fcf_cagr"), ("gap_vs_revenue", "hist_revenue_cagr")):
        a, b = res.values.get("implied_growth"), res.values.get(hist)
        res.set(key, a - b if a is not None and b is not None else None,
                f"needs implied_growth and {hist}")
    return res


SCHEMA = """
CREATE TABLE IF NOT EXISTS reverse_dcf (
    ticker TEXT PRIMARY KEY, cik INTEGER, run_at TEXT, status TEXT, name TEXT, sic INTEGER,
    fiscal_year INTEGER, fy_end TEXT, price REAL, price_date TEXT, price_source TEXT, price_reliable INTEGER,
    shares_outstanding REAL, market_cap REAL, net_debt REAL, ev REAL, base_fcf REAL, base_fcf_mode TEXT,
    dilution_rate REAL, implied_growth REAL, implied_growth_no_dilution REAL,
    hist_revenue_cagr REAL, hist_revenue_span INTEGER, hist_fcf_cagr REAL, hist_fcf_span INTEGER,
    gap_vs_fcf REAL, gap_vs_revenue REAL, price_at_hist_growth REAL,
    base_fcf_latest REAL, base_fcf_avg3 REAL, base_fcf_normalized REAL, fcf_margin_median REAL,
    capex_total REAL, maintenance_capex REAL, owner_earnings_normalized REAL, epv REAL,
    growth_premium REAL, price_at_zero_growth REAL, hist_fcf_cagr_mean REAL, hist_fcf_cagr_normalized REAL,
    reasons TEXT, inputs TEXT, assumptions TEXT, warnings TEXT
);
CREATE TABLE IF NOT EXISTS reverse_dcf_sensitivity (
    ticker TEXT, discount_rate REAL, terminal_multiple REAL, implied_growth REAL, reason TEXT,
    PRIMARY KEY (ticker, discount_rate, terminal_multiple)
);
"""

COLUMNS = ["name", "sic", "fiscal_year", "fy_end", "price", "price_date", "price_source", "price_reliable",
           "shares_outstanding", "market_cap", "net_debt", "ev", "base_fcf", "base_fcf_mode", "dilution_rate",
           "implied_growth", "implied_growth_no_dilution", "hist_revenue_cagr", "hist_revenue_span",
           "hist_fcf_cagr", "hist_fcf_span", "gap_vs_fcf", "gap_vs_revenue", "price_at_hist_growth",
           "base_fcf_latest", "base_fcf_avg3", "base_fcf_normalized", "fcf_margin_median", "capex_total",
           "maintenance_capex", "owner_earnings_normalized", "epv", "growth_premium", "price_at_zero_growth",
           "hist_fcf_cagr_mean", "hist_fcf_cagr_normalized"]


def store(conn: sqlite3.Connection, results: list[Result]) -> None:
    conn.execute("DROP TABLE IF EXISTS reverse_dcf")  # derived output: schema may change between versions
    conn.executescript(SCHEMA)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with conn:
        for r in results:
            conn.execute("DELETE FROM reverse_dcf_sensitivity WHERE ticker = ?", (r.ticker,))
            row = [r.ticker, r.cik, now, r.status] + [r.values.get(c) for c in COLUMNS] + [
                json.dumps(r.reasons), json.dumps(r.inputs, default=str),
                "\n".join(r.assumptions), "\n".join(r.warnings)]
            conn.execute(f"INSERT OR REPLACE INTO reverse_dcf VALUES ({','.join('?' * len(row))})", row)
            conn.executemany("INSERT INTO reverse_dcf_sensitivity VALUES (?,?,?,?,?)", [
                (r.ticker, s["discount_rate"], s["terminal_multiple"], s["implied_growth"], s["reason"])
                for s in r.sensitivity])
