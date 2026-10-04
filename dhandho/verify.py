"""Print every raw number for one company's last N fiscal years next to its XBRL tag
and source filing, so it can be checked by hand against the actual 10-K."""
from __future__ import annotations

import json
import sqlite3


def filing_url(cik: int, accn: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{accn}-index.htm"


def render(conn: sqlite3.Connection, ticker: str, years: int, concept_order: list[str]) -> str:
    comp = conn.execute("SELECT * FROM companies WHERE ticker = ?", (ticker,)).fetchone()
    if comp is None:
        raise ValueError(f"{ticker} not ingested; run `dhandho ingest` first")
    cik = comp["cik"]
    fys = [r[0] for r in conn.execute(
        "SELECT fiscal_year FROM fiscal_periods WHERE cik = ? ORDER BY fiscal_year DESC LIMIT ?", (cik, years))]
    fys.sort()
    rows = {(r["concept"], r["fiscal_year"]): r for r in conn.execute(
        f"SELECT * FROM concept_values WHERE cik = ? AND fiscal_year IN ({','.join('?' * len(fys))})", (cik, *fys))}
    ends = {r[0]: r[1] for r in conn.execute(
        "SELECT fiscal_year, period_end FROM fiscal_periods WHERE cik = ?", (cik,))}

    out: list[str] = []
    out.append(f"{ticker} — {comp['name']} (CIK {cik})")
    out.append("Fiscal years: " + ", ".join(f"FY{y} (ends {ends[y]})" for y in fys))
    out.append("Values are as reported, in USD (or shares). Latest filing that reported each value is cited;")
    out.append("'first filed' appears only when an earlier filing reported a different number.")
    filings: dict[str, tuple[str, str]] = {}
    for concept in concept_order:
        out.append("")
        out.append(f"[{concept}]")
        for fy in fys:
            r = rows.get((concept, fy))
            if r is None:
                continue
            if r["status"] != "ok":
                out.append(f"  FY{fy}  NULL  ({r['status']}) {r['reason']}")
                continue
            comps = json.loads(r["components"])
            out.append(f"  FY{fy}  {r['value']:>22,.0f}  {r['method']}   [{r['accn']} {r['form']} filed {r['filed']}]")
            if len(comps) > 1:
                for c in comps:
                    out.append(f"  {'':6}{c['value']:>22,.0f}    = {c['tag']}")
            else:
                out.append(f"  {'':6}{'':>22}    tag {comps[0]['tag']}  period {comps[0]['start'] or ''}..{comps[0]['end']}")
            if r["restated"]:
                out.append(f"  {'':6}first filed {r['first_value']:,.0f} in {r['first_accn']} on {r['first_filed']}")
            if r["notes"]:
                out.append(f"  {'':6}NOTE: {r['notes']}")
            filings[r["accn"]] = (r["form"], r["filed"])
    out.append("")
    out.append("Source filings:")
    for accn, (form, filed) in sorted(filings.items(), key=lambda kv: kv[1][1]):
        out.append(f"  {accn} {form} filed {filed}  {filing_url(cik, accn)}")
    return "\n".join(out)
