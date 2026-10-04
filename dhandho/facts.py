"""Turn a companyfacts JSON blob into annual concept values, one per filing (vintage).

Pipeline:
  1. flatten facts, keep only 10-K-family filings
  2. detect fiscal year ends from annual-duration facts
  3. per (fiscal year, concept, filing) evaluate the ordered tag strategies
  4. per (fiscal year, concept) pick the latest-filed vintage, or NULL + reason
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import ConceptSpec, Strategy, _short


@dataclass(frozen=True)
class Fact:
    tag: str  # qualified, e.g. us-gaap:Revenues
    unit: str
    start: dt.date | None
    end: dt.date
    val: float
    accn: str
    form: str
    filed: dt.date
    fy: int | None
    fp: str | None
    frame: str | None

    @property
    def days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days


@dataclass(frozen=True)
class FiscalPeriod:
    fiscal_year: int
    end: dt.date
    is_primary: bool  # True if this is the main period of at least one 10-K
    note: str | None = None


@dataclass
class Vintage:
    """A concept value for one fiscal year as reported in one filing."""

    fiscal_year: int
    concept: str
    value: float
    unit: str
    period_start: dt.date | None
    period_end: dt.date
    strategy: Strategy
    components: list[dict[str, Any]]  # [{tag, value, start, end}]
    accn: str
    form: str
    filed: dt.date
    notes: list[str] = field(default_factory=list)


@dataclass
class ConceptValue:
    """The canonical (latest-filed) value for (fiscal_year, concept), or NULL + reason."""

    fiscal_year: int
    concept: str
    period_end: dt.date
    status: str  # ok | missing | not_applicable
    unit: str
    latest: Vintage | None = None
    first: Vintage | None = None
    n_vintages: int = 0
    restated: bool = False
    reason: str | None = None


def _d(s: str | None) -> dt.date | None:
    return dt.date.fromisoformat(s) if s else None


def flatten(companyfacts: dict[str, Any], forms: Iterable[str]) -> list[Fact]:
    forms = set(forms)
    out: list[Fact] = []
    for taxonomy, tags in (companyfacts.get("facts") or {}).items():
        for tag, body in tags.items():
            for unit, arr in (body.get("units") or {}).items():
                for f in arr:
                    if f.get("form") not in forms:
                        continue
                    out.append(
                        Fact(
                            tag=f"{taxonomy}:{tag}",
                            unit=unit,
                            start=_d(f.get("start")),
                            end=_d(f["end"]),
                            val=float(f["val"]),
                            accn=f["accn"],
                            form=f["form"],
                            filed=_d(f["filed"]),
                            fy=f.get("fy"),
                            fp=f.get("fp"),
                            frame=f.get("frame"),
                        )
                    )
    return out


def fiscal_year_label(end: dt.date, january_rollback_days: int) -> int:
    """Calendar year of the period end; a 52/53-week year ending in early January
    belongs to the prior year (e.g. year ended 2021-01-02 -> FY2020)."""
    if end.month == 1 and end.day <= january_rollback_days:
        return end.year - 1
    return end.year


def is_annual(f: Fact, window: tuple[int, int]) -> bool:
    return f.start is not None and window[0] <= f.days <= window[1]


def detect_fiscal_periods(
    facts: list[Fact], window: tuple[int, int], january_rollback_days: int, history_years: int
) -> list[FiscalPeriod]:
    """Fiscal year ends = the main period of each 10-K (latest annual end in that filing),
    plus comparative annual ends reported in >= 2 filings (covers pre-XBRL years)."""
    ends_by_accn: dict[str, set[dt.date]] = defaultdict(set)
    for f in facts:
        if is_annual(f, window):
            ends_by_accn[f.accn].add(f.end)
    primary = {max(ends) for ends in ends_by_accn.values() if ends}
    accn_count: dict[dt.date, int] = defaultdict(int)
    for ends in ends_by_accn.values():
        for e in ends:
            accn_count[e] += 1
    candidates = primary | {e for e, n in accn_count.items() if n >= 2}

    by_label: dict[int, list[dt.date]] = defaultdict(list)
    for e in candidates:
        by_label[fiscal_year_label(e, january_rollback_days)].append(e)

    periods: list[FiscalPeriod] = []
    for label, ends in by_label.items():
        prim = sorted(e for e in ends if e in primary)
        chosen = prim[-1] if prim else max(ends)
        note = None
        if len(ends) > 1:
            note = (
                f"multiple annual period ends map to FY{label}: "
                f"{', '.join(str(e) for e in sorted(ends))}; using {chosen} (fiscal year-end change?)"
            )
        periods.append(FiscalPeriod(label, chosen, chosen in primary, note))
    periods.sort(key=lambda p: p.fiscal_year)
    if history_years and periods:
        cutoff = periods[-1].fiscal_year - history_years + 1
        periods = [p for p in periods if p.fiscal_year >= cutoff]
    return periods


class _Index:
    """Facts for the tags we care about, indexed by (tag, period end)."""

    def __init__(self, facts: list[Fact], tags: set[str], window: tuple[int, int]):
        self.duration: dict[tuple[str, dt.date], list[Fact]] = defaultdict(list)
        self.instant: dict[tuple[str, dt.date], list[Fact]] = defaultdict(list)
        self.tag_years: dict[str, set[dt.date]] = defaultdict(set)
        for f in facts:
            if f.tag not in tags:
                continue
            if f.start is None:
                self.instant[(f.tag, f.end)].append(f)
                self.tag_years[f.tag].add(f.end)
            elif is_annual(f, window):
                self.duration[(f.tag, f.end)].append(f)
                self.tag_years[f.tag].add(f.end)

    def get(self, spec: ConceptSpec, tag: str, end: dt.date) -> list[Fact]:
        src = self.duration if spec.period == "duration" else self.instant
        return [f for f in src.get((tag, end), []) if f.unit == spec.unit]


def _pick_one(facts: list[Fact]) -> tuple[Fact, str | None]:
    """Collapse duplicate facts (same tag/period/filing). Returns (fact, anomaly note)."""
    if len(facts) == 1:
        return facts[0], None
    # Prefer a duration closest to a 52-week/1-year span if starts differ.
    ordered = sorted(facts, key=lambda f: abs((f.days or 364) - 364))
    vals = {f.val for f in facts}
    if len(vals) == 1:
        return ordered[0], None
    return ordered[0], (
        f"{_short(facts[0].tag)}: conflicting values in filing {facts[0].accn}: "
        f"{sorted(vals)}; used {ordered[0].val:,.0f}"
    )


def _evaluate(strategy: Strategy, found: dict[str, Fact]) -> tuple[float, list[Fact]] | None:
    if any(t not in found for t in strategy.required):
        return None
    used = [found[t] for t in strategy.tags if t in found]
    if not used:
        return None
    return sum(f.val for f in used), used


def compute_concept(
    spec: ConceptSpec,
    index: _Index,
    periods: list[FiscalPeriod],
    restatement_tolerance: float,
) -> tuple[list[Vintage], list[ConceptValue]]:
    vintages_out: list[Vintage] = []
    values_out: list[ConceptValue] = []
    for p in periods:
        # group candidate facts by filing
        per_accn: dict[str, dict[str, list[Fact]]] = defaultdict(lambda: defaultdict(list))
        for tag in spec.all_tags:
            for f in index.get(spec, tag, p.end):
                per_accn[f.accn][tag].append(f)

        vintages: list[Vintage] = []
        for accn, tagfacts in per_accn.items():
            found: dict[str, Fact] = {}
            notes: list[str] = []
            for tag, fl in tagfacts.items():
                found[tag], note = _pick_one(fl)
                if note:
                    notes.append(note)
            for strat in spec.strategies:
                res = _evaluate(strat, found)
                if res is None:
                    continue
                value, used = res
                any_fact = used[0]
                vintages.append(
                    Vintage(
                        fiscal_year=p.fiscal_year,
                        concept=spec.name,
                        value=value,
                        unit=spec.unit,
                        period_start=any_fact.start,
                        period_end=p.end,
                        strategy=strat,
                        components=[
                            {"tag": u.tag, "value": u.val, "start": str(u.start) if u.start else None, "end": str(u.end)}
                            for u in used
                        ],
                        accn=accn,
                        form=any_fact.form,
                        filed=max(u.filed for u in used),
                        notes=notes,
                    )
                )
                break

        if vintages:
            vintages.sort(key=lambda v: (v.filed, v.accn))
            first, latest = vintages[0], vintages[-1]
            denom = abs(first.value) or 1.0
            restated = any(abs(v.value - first.value) / denom > restatement_tolerance for v in vintages[1:])
            values_out.append(
                ConceptValue(
                    fiscal_year=p.fiscal_year,
                    concept=spec.name,
                    period_end=p.end,
                    status="ok",
                    unit=spec.unit,
                    latest=latest,
                    first=first,
                    n_vintages=len(vintages),
                    restated=restated,
                    reason=None,
                )
            )
            vintages_out.extend(vintages)
            continue

        status, reason = _missing_reason(spec, index, p, per_accn)
        values_out.append(
            ConceptValue(
                fiscal_year=p.fiscal_year,
                concept=spec.name,
                period_end=p.end,
                status=status,
                unit=spec.unit,
                reason=reason,
            )
        )
    return vintages_out, values_out


def _missing_reason(
    spec: ConceptSpec, index: _Index, p: FiscalPeriod, per_accn: dict[str, dict[str, list[Fact]]]
) -> tuple[str, str]:
    if spec.not_applicable_before and p.end < spec.not_applicable_before:
        return "not_applicable", spec.na_reason or f"not applicable before {spec.not_applicable_before}"
    kind = "annual duration" if spec.period == "duration" else "instant"
    parts: list[str] = []
    if per_accn:
        present = sorted({_short(t) for d in per_accn.values() for t in d})
        for s in spec.strategies:
            if not s.is_single and s.required:
                missing = [_short(t) for t in s.required if not any(t in d for d in per_accn.values())]
                if missing and any(any(t in d for d in per_accn.values()) for t in s.optional):
                    parts.append(f"{s.label} lacks required {', '.join(missing)}")
        parts.insert(0, f"only partial tags reported ({', '.join(present)})")
    else:
        parts.append(f"no candidate tag reported as {kind} {spec.unit} for period ending {p.end} in any 10-K")
    elsewhere = sorted(
        {_short(t) for t in spec.all_tags if index.tag_years.get(t)}
    )
    if elsewhere:
        parts.append(f"candidate tags seen in other periods: {', '.join(elsewhere)}")
    else:
        parts.append("no candidate tag ever reported by this company")
    if spec.absent_note and not per_accn:
        parts.append(spec.absent_note)
    return "missing", "; ".join(parts)


@dataclass
class CompanyResult:
    cik: int
    name: str
    periods: list[FiscalPeriod]
    vintages: list[Vintage]
    values: list[ConceptValue]
    raw_facts: list[Fact]  # 10-K facts for mapped tags, for traceability


def process_companyfacts(companyfacts: dict[str, Any], concepts: dict[str, ConceptSpec], ingest_cfg: dict) -> CompanyResult:
    window = tuple(ingest_cfg["annual_duration_days"])
    facts = flatten(companyfacts, ingest_cfg["annual_forms"])
    periods = detect_fiscal_periods(
        facts, window, ingest_cfg["fiscal_year_january_rollback_days"], ingest_cfg["history_years"]
    )
    tags = {t for spec in concepts.values() for t in spec.all_tags}
    index = _Index(facts, tags, window)
    vintages: list[Vintage] = []
    values: list[ConceptValue] = []
    for spec in concepts.values():
        v, cv = compute_concept(spec, index, periods, ingest_cfg["restatement_tolerance"])
        vintages.extend(v)
        values.extend(cv)
    return CompanyResult(
        cik=int(companyfacts.get("cik", 0)),
        name=companyfacts.get("entityName", ""),
        periods=periods,
        vintages=vintages,
        values=values,
        raw_facts=[f for f in facts if f.tag in tags],
    )


def components_json(v: Vintage) -> str:
    return json.dumps(v.components)
