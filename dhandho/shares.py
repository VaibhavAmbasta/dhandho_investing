"""Split-adjust diluted share counts using only SEC data.

Each 10-K reports diluted shares for ~3 fiscal years. After a split, the next 10-K
restates the overlapping years on the new basis. So for every pair of consecutive
filings, the ratio of the same fiscal year's share count is the split factor between
them (1.0 if nothing happened). Chaining those ratios puts every filing on the basis
of the most recent one.

The same mechanism repairs filing-wide XBRL scale errors (e.g. CPRT's FY2014 10-K
tagged all share counts in thousands: ratio exactly 1000). Ratios that are neither
~1, a plausible split, nor a power-of-1000 scale error are NOT applied and are
reported as unexplained.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class ShareEvent:
    before_accn: str
    before_filed: str
    after_accn: str
    after_filed: str
    ratio: float
    kind: str  # split | reverse_split | scale_error | unexplained
    applied: bool


@dataclass
class AdjustedShares:
    by_year: dict[int, float]                       # split-adjusted, latest basis
    raw_by_year: dict[int, float]
    source: dict[int, tuple[str, str, float]]        # fy -> (accn, filed, factor applied)
    events: list[ShareEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def classify_ratio(r: float, tol: float) -> str:
    if abs(r - 1) <= tol:
        return "none"
    for scale in (1000.0, 1e6):
        if abs(r / scale - 1) <= tol:
            return "scale_error"
        if abs(r * scale - 1) <= tol:
            return "scale_error"
    if r > 1:
        n = round(r * 2) / 2  # allow 3:2 style splits (1.5, 2.5)
        if 1.5 <= n <= 100 and abs(r / n - 1) <= tol:
            return "split"
    else:
        n = round((1 / r) * 2) / 2
        if 1.5 <= n <= 100 and abs((1 / r) / n - 1) <= tol:
            return "reverse_split"
    return "unexplained"


def adjust_diluted_shares(conn: sqlite3.Connection, cik: int, tol: float = 0.02) -> AdjustedShares:
    rows = conn.execute(
        "SELECT fiscal_year, value, accn, filed FROM concept_vintages "
        "WHERE cik = ? AND concept = 'diluted_shares' ORDER BY filed, accn",
        (cik,),
    ).fetchall()
    filings: dict[str, dict[int, float]] = defaultdict(dict)
    filed_of: dict[str, str] = {}
    for fy, val, accn, filed in rows:
        filings[accn][fy] = val
        filed_of[accn] = filed
    order = sorted(filings, key=lambda a: (filed_of[a], a))
    out = AdjustedShares({}, {}, {})
    if not order:
        return out

    # factor[a] converts filing a's numbers to the basis of the latest filing.
    factor = {order[-1]: 1.0}
    for prev, nxt in reversed(list(zip(order, order[1:]))):
        common = sorted(set(filings[prev]) & set(filings[nxt]))
        ratios = [filings[nxt][y] / filings[prev][y] for y in common if filings[prev][y]]
        if not ratios:
            out.warnings.append(f"no overlapping fiscal year between {prev} and {nxt}; assumed no split")
            factor[prev] = factor[nxt]
            continue
        r = sorted(ratios)[len(ratios) // 2]
        kind = classify_ratio(r, tol)
        if kind == "none":
            factor[prev] = factor[nxt]
            continue
        applied = kind != "unexplained"
        out.events.append(ShareEvent(prev, filed_of[prev], nxt, filed_of[nxt], r, kind, applied))
        if applied:
            factor[prev] = factor[nxt] * r
        else:
            factor[prev] = factor[nxt]
            out.warnings.append(
                f"unexplained share-count ratio {r:.3f} between filings {prev} ({filed_of[prev]}) and "
                f"{nxt} ({filed_of[nxt]}); not applied")

    # For each fiscal year use the latest filing that reported it, on the latest basis.
    for accn in order:
        for fy, val in filings[accn].items():
            out.raw_by_year[fy] = val
            out.by_year[fy] = val * factor[accn]
            out.source[fy] = (accn, filed_of[accn], factor[accn])
    return out
