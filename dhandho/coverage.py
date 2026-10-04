"""Coverage report: per ticker, which concepts are present for which fiscal years,
which tag supplied each value, where tags switch, and what is missing and why."""
from __future__ import annotations

import html
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .config import Config

NA, MISSING = "-", "."


def _letter(rank: int) -> str:
    return chr(ord("A") + rank - 1)


@dataclass
class TickerCoverage:
    ticker: str
    cik: int
    name: str
    years: list[int]
    concepts: list[str]
    cells: dict[tuple[str, int], dict]  # (concept, fy) -> row dict
    switches: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    restated: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def code(self, concept: str, fy: int) -> str:
        r = self.cells.get((concept, fy))
        if r is None:
            return " "
        if r["status"] == "ok":
            return _letter(int(r["strategy_rank"]))
        return NA if r["status"] == "not_applicable" else MISSING

    @property
    def populated(self) -> tuple[int, int]:
        applicable = [r for r in self.cells.values() if r["status"] != "not_applicable"]
        return sum(r["status"] == "ok" for r in applicable), len(applicable)


def build(conn: sqlite3.Connection, cfg: Config) -> list[TickerCoverage]:
    df = pd.read_sql_query("SELECT * FROM concept_values ORDER BY ticker, concept, fiscal_year", conn)
    comp = {r["cik"]: dict(r) for r in conn.execute("SELECT * FROM companies")}
    periods = pd.read_sql_query("SELECT * FROM fiscal_periods", conn)
    split_ratio = float(cfg.ingest["share_split_flag_ratio"])
    order = list(cfg.concepts)
    out: list[TickerCoverage] = []

    for ticker, g in df.groupby("ticker", sort=False):
        cik = int(g["cik"].iloc[0])
        years = sorted(g["fiscal_year"].unique().tolist())
        concepts = [c for c in order if c in set(g["concept"])]
        cells = {(r["concept"], int(r["fiscal_year"])): r for r in g.to_dict("records")}
        tc = TickerCoverage(ticker, cik, comp.get(cik, {}).get("name", ""), years, concepts, cells)

        for concept in concepts:
            prev = None
            seen_ok = False
            series = [cells[(concept, y)] for y in years if (concept, y) in cells]
            later_ok = {y for y in years if cells.get((concept, y), {}).get("status") == "ok"}
            for r in series:
                fy = int(r["fiscal_year"])
                if r["status"] == "ok":
                    if prev is not None and r["method"] != prev["method"]:
                        tc.switches.append({
                            "concept": concept, "fiscal_year": fy,
                            "from": prev["method"], "from_fy": int(prev["fiscal_year"]), "to": r["method"],
                        })
                    prev, seen_ok = r, True
                    if r["restated"]:
                        tc.restated.append({
                            "concept": concept, "fiscal_year": fy, "first_value": r["first_value"],
                            "first_filed": r["first_filed"], "value": r["value"], "filed": r["filed"],
                        })
                elif r["status"] == "missing":
                    gap = "interior gap" if seen_ok and any(y > fy for y in later_ok) else (
                        "leading" if not seen_ok else "trailing")
                    tc.missing.append({"concept": concept, "fiscal_year": fy, "position": gap, "reason": r["reason"]})

        # Unadjusted stock splits show up as big jumps in share counts.
        for concept in ("diluted_shares",):
            ok = [cells[(concept, y)] for y in years if cells.get((concept, y), {}).get("status") == "ok"]
            for a, b in zip(ok, ok[1:]):
                if a["value"] and b["value"]:
                    ratio = b["value"] / a["value"]
                    if ratio > split_ratio or ratio < 1 / split_ratio:
                        tc.warnings.append(
                            f"{concept} FY{a['fiscal_year']}->FY{b['fiscal_year']} changed {ratio:.2f}x "
                            f"({a['value']:,.0f} -> {b['value']:,.0f}): likely stock split not adjusted "
                            f"in older filings. Must be split-adjusted before Phase 2 dilution math."
                        )
        for _, p in periods[periods["cik"] == cik].iterrows():
            if p["note"]:
                tc.warnings.append(f"FY{p['fiscal_year']}: {p['note']}")
            if not p["is_primary"]:
                tc.warnings.append(
                    f"FY{p['fiscal_year']} (ends {p['period_end']}) appears only as a comparative "
                    f"in later 10-Ks (pre-XBRL year); balance-sheet concepts will be sparse."
                )
        out.append(tc)
    return out


def legend(cfg: Config) -> dict[str, list[str]]:
    return {name: [f"{_letter(s.rank)}={s.label}" for s in spec.strategies] for name, spec in cfg.concepts.items()}


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------
def render_text(covs: list[TickerCoverage], cfg: Config, verbose_missing: bool = False) -> str:
    lines: list[str] = []
    lines.append("Legend: letter = which strategy in config.yaml supplied the value "
                 "(A = first choice). '.' = missing (NULL + reason). '-' = not applicable.")
    for tc in covs:
        have, total = tc.populated
        lines.append("")
        lines.append(f"== {tc.ticker}  {tc.name}  (CIK {tc.cik})  FY{tc.years[0]}-FY{tc.years[-1]}  "
                     f"{have}/{total} applicable concept-years populated")
        yrs = "".join(f"{y % 100:02d} " for y in tc.years)
        lines.append(f"  {'concept':<28}{yrs}")
        for c in tc.concepts:
            row = "".join(f" {tc.code(c, y)} " for y in tc.years)
            lines.append(f"  {c:<28}{row}")
        if tc.switches:
            lines.append("  Tag switches:")
            for s in tc.switches:
                lines.append(f"    {s['concept']}: FY{s['from_fy']} {s['from']} -> FY{s['fiscal_year']} {s['to']}")
        interior = [m for m in tc.missing if m["position"] == "interior gap"]
        lines.append(f"  Missing: {len(tc.missing)} concept-years "
                     f"({len(interior)} interior gaps, i.e. present before and after)")
        by_concept: dict[str, list[int]] = {}
        for m in tc.missing:
            by_concept.setdefault(m["concept"], []).append(m["fiscal_year"])
        for c, ys in by_concept.items():
            first = next(m for m in tc.missing if m["concept"] == c)
            lines.append(f"    {c}: FY{_ranges(ys)}"
                         + (f"\n      reason (FY{first['fiscal_year']}): {first['reason']}" if verbose_missing else ""))
        if tc.restated:
            lines.append(f"  Restated values (later filing differs from first-filed): {len(tc.restated)}")
        for w in tc.warnings:
            lines.append(f"  WARNING: {w}")
    return "\n".join(lines)


def _ranges(years: list[int]) -> str:
    years = sorted(years)
    out, start, prev = [], years[0], years[0]
    for y in years[1:] + [None]:
        if y is not None and y == prev + 1:
            prev = y
            continue
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
        if y is not None:
            start = prev = y
    return ", FY".join(out)


# ---------------------------------------------------------------------------
# csv
# ---------------------------------------------------------------------------
def write_csvs(conn: sqlite3.Connection, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_sql_query(
        "SELECT ticker, cik, fiscal_year, period_end, concept, status, value, unit, method, strategy_rank, "
        "components, accn, form, filed, first_value, first_filed, n_vintages, restated, reason, notes "
        "FROM concept_values ORDER BY ticker, concept, fiscal_year", conn)
    p1 = out_dir / "coverage_long.csv"
    df.to_csv(p1, index=False)
    p2 = out_dir / "tag_log.csv"
    df[["ticker", "fiscal_year", "concept", "status", "method", "strategy_rank", "accn", "filed", "reason"]].to_csv(
        p2, index=False)
    return [p1, p2]


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------
_CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a64;--line:#e4e1d8;--ok:#dcefe0;--okfg:#1f5130;
--sw:#fbe7c6;--swfg:#7a4a00;--miss:#f6d9d6;--missfg:#8a1f17;--na:#eeede9;--nafg:#8d8b84;--card:#ffffff;--link:#1a56a8}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#171716;--fg:#ecebe6;--muted:#a19f97;
--line:#33322f;--ok:#1f3b27;--okfg:#b7e3c2;--sw:#4a3511;--swfg:#f6d39a;--miss:#4a1d1a;--missfg:#f4b9b2;
--na:#262624;--nafg:#77756e;--card:#1f1f1d;--link:#8fb8f0}}
:root[data-theme="dark"]{--bg:#171716;--fg:#ecebe6;--muted:#a19f97;--line:#33322f;--ok:#1f3b27;--okfg:#b7e3c2;
--sw:#4a3511;--swfg:#f6d39a;--miss:#4a1d1a;--missfg:#f4b9b2;--na:#262624;--nafg:#77756e;--card:#1f1f1d;--link:#8fb8f0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px 16px}
main{max-width:1200px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 6px}
.muted{color:var(--muted)}section{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:16px;margin:16px 0}.scroll{overflow-x:auto}table{border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:3px 6px;text-align:center;border-bottom:1px solid var(--line);white-space:nowrap}
th.c,td.c{text-align:left}td.k{min-width:26px;font-weight:600;font-size:12px;cursor:help}
td.ok{background:var(--ok);color:var(--okfg)}td.sw{background:var(--sw);color:var(--swfg)}
td.miss{background:var(--miss);color:var(--missfg)}td.na{background:var(--na);color:var(--nafg)}
ul{margin:6px 0;padding-left:20px}li{margin:2px 0}.warn{color:var(--swfg)}
.key span{display:inline-block;padding:1px 8px;border-radius:4px;margin-right:6px;font-size:12px}
details{margin-top:8px}summary{cursor:pointer;color:var(--muted)}code{font-size:12px;overflow-wrap:anywhere}
a{color:var(--link)}section h2{margin-top:0}li{overflow-wrap:anywhere}
.summary td,.summary th{text-align:right}.summary td:first-child,.summary th:first-child{text-align:left}
"""


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _cell_title(r: dict) -> str:
    if r["status"] != "ok":
        return f"{r['status'].upper()}: {r['reason']}"
    comps = json.loads(r["components"] or "[]")
    parts = [f"{r['method']} = {r['value']:,.0f} {r['unit']}"]
    if len(comps) > 1:
        parts += [f"  {c['tag'].split(':')[-1]} = {c['value']:,.0f}" for c in comps]
    parts.append(f"filing {r['accn']} ({r['form']}) filed {r['filed']}")
    if r["restated"]:
        parts.append(f"RESTATED: first filed {r['first_value']:,.0f} on {r['first_filed']}")
    return "\n".join(parts)


def render_html(covs: list[TickerCoverage], cfg: Config, generated_at: str) -> str:
    leg = legend(cfg)
    h: list[str] = []
    h.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             "<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Dhandho Coverage Report</title><style>{_CSS}</style></head><body><main>")
    h.append("<h1>Dhandho coverage report</h1>")
    h.append(f"<p class='muted'>Phase 1 data layer &middot; SEC companyfacts, 10-K family only &middot; "
             f"generated {_e(generated_at)}. Hover a cell for tag, value, filing and filing date "
             f"(or the reason a value is NULL).</p>")
    h.append("<p class='key'><span style='background:var(--ok);color:var(--okfg)'>A</span>present (letter = "
             "strategy rank)<span style='background:var(--sw);color:var(--swfg)'>B</span>tag switched vs prior "
             "year<span style='background:var(--miss);color:var(--missfg)'>.</span>missing"
             "<span style='background:var(--na);color:var(--nafg)'>-</span>not applicable</p>")

    h.append("<section><div class='scroll'><table class='summary'><tr><th>Ticker</th><th>Name</th><th>Years</th><th>Populated</th>"
             "<th>Tag switches</th><th>Missing</th><th>Interior gaps</th><th>Restated</th><th>Warnings</th></tr>")
    for tc in covs:
        have, total = tc.populated
        gaps = sum(m["position"] == "interior gap" for m in tc.missing)
        h.append(f"<tr><td><a href='#{_e(tc.ticker)}'>{_e(tc.ticker)}</a></td><td>{_e(tc.name)}</td>"
                 f"<td>FY{tc.years[0]}&ndash;{tc.years[-1]}</td><td>{have}/{total} ({100 * have / max(total, 1):.0f}%)</td>"
                 f"<td>{len(tc.switches)}</td><td>{len(tc.missing)}</td><td>{gaps}</td><td>{len(tc.restated)}</td>"
                 f"<td>{len(tc.warnings)}</td></tr>")
    h.append("</table></div></section>")

    for tc in covs:
        switch_keys = {(s["concept"], s["fiscal_year"]) for s in tc.switches}
        h.append(f"<section id='{_e(tc.ticker)}'><h2>{_e(tc.ticker)} &mdash; {_e(tc.name)} "
                 f"<span class='muted'>CIK {tc.cik}</span></h2><div class='scroll'><table><tr><th class='c'>concept</th>")
        h += [f"<th>{y}</th>" for y in tc.years]
        h.append("</tr>")
        for c in tc.concepts:
            h.append(f"<tr><td class='c'>{_e(c)}</td>")
            for y in tc.years:
                r = tc.cells.get((c, y))
                if r is None:
                    h.append("<td></td>")
                    continue
                cls = {"ok": "ok", "missing": "miss", "not_applicable": "na"}[r["status"]]
                if (c, y) in switch_keys:
                    cls = "sw"
                h.append(f"<td class='k {cls}' title='{_e(_cell_title(r))}'>{_e(tc.code(c, y))}</td>")
            h.append("</tr>")
        h.append("</table></div>")
        if tc.warnings:
            h.append("<p><strong>Warnings</strong></p><ul>" + "".join(
                f"<li class='warn'>{_e(w)}</li>" for w in tc.warnings) + "</ul>")
        if tc.switches:
            h.append("<p><strong>Tag switches</strong></p><ul>" + "".join(
                f"<li>{_e(s['concept'])}: FY{s['from_fy']} <code>{_e(s['from'])}</code> &rarr; "
                f"FY{s['fiscal_year']} <code>{_e(s['to'])}</code></li>" for s in tc.switches) + "</ul>")
        if tc.missing:
            h.append(f"<details><summary>Missing values ({len(tc.missing)})</summary><ul>" + "".join(
                f"<li><strong>{_e(m['concept'])}</strong> FY{m['fiscal_year']} <span class='muted'>"
                f"({_e(m['position'])})</span>: {_e(m['reason'])}</li>" for m in tc.missing) + "</ul></details>")
        if tc.restated:
            h.append(f"<details><summary>Restated values ({len(tc.restated)})</summary><ul>" + "".join(
                f"<li>{_e(r['concept'])} FY{r['fiscal_year']}: first {r['first_value']:,.0f} "
                f"(filed {_e(r['first_filed'])}) &rarr; latest {r['value']:,.0f} (filed {_e(r['filed'])})</li>"
                for r in tc.restated) + "</ul></details>")
        h.append("</section>")

    h.append("<section><h2>Strategy legend (from config.yaml)</h2><ul>")
    for name, items in leg.items():
        h.append(f"<li><strong>{_e(name)}</strong>: " + ", ".join(f"<code>{_e(i)}</code>" for i in items) + "</li>")
    h.append("</ul></section></main></body></html>")
    return "".join(h)
