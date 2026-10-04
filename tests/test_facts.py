import datetime as dt

import pytest

from dhandho.config import ConceptSpec, Strategy, parse_concepts
from dhandho.facts import (
    Fact,
    _evaluate,
    detect_fiscal_periods,
    fiscal_year_label,
    flatten,
    process_companyfacts,
)

D = dt.date.fromisoformat


@pytest.mark.parametrize(
    "end,expected",
    [
        ("2023-09-30", 2023),  # Apple-style late September
        ("2023-06-30", 2023),  # Microsoft June year end
        ("2023-12-31", 2023),
        ("2021-01-02", 2020),  # 52/53-week year spilling into January
        ("2021-01-07", 2020),
        ("2021-01-08", 2021),  # past the 7-day rollback window
        ("2024-01-28", 2024),  # retailer Jan year end: labelled by end year
    ],
)
def test_fiscal_year_label(end, expected):
    assert fiscal_year_label(D(end), 7) == expected


def _fact(tag, val, start=None, end="2020-12-31", accn="a1", filed="2021-02-01"):
    return Fact(tag=f"us-gaap:{tag}", unit="USD", start=D(start) if start else None, end=D(end), val=val,
                accn=accn, form="10-K", filed=D(filed), fy=2020, fp="FY", frame=None)


def test_sum_strategy_required_and_optional():
    s = Strategy(1, ("us-gaap:A",), ("us-gaap:B", "us-gaap:C"))
    found = {"us-gaap:A": _fact("A", 10), "us-gaap:C": _fact("C", 5)}
    value, used = _evaluate(s, found)
    assert value == 15  # 10 + absent B treated as 0 + 5
    assert [u.tag for u in used] == ["us-gaap:A", "us-gaap:C"]
    assert _evaluate(s, {"us-gaap:B": _fact("B", 1)}) is None  # required A missing


def test_sum_strategy_optional_only_needs_one():
    s = Strategy(1, (), ("us-gaap:B", "us-gaap:C"))
    assert _evaluate(s, {}) is None
    assert _evaluate(s, {"us-gaap:B": _fact("B", 7)})[0] == 7


def test_flatten_drops_non_annual_forms(companyfacts):
    facts = flatten(companyfacts, ["10-K", "10-K/A"])
    assert all(f.form in ("10-K", "10-K/A") for f in facts)
    assert not any(f.val in (333, 9999) for f in facts)  # 10-Q values gone


def test_detect_fiscal_periods(companyfacts):
    facts = flatten(companyfacts, ["10-K", "10-K/A"])
    periods = detect_fiscal_periods(facts, (340, 390), 7, 15)
    assert [(p.fiscal_year, str(p.end), p.is_primary) for p in periods] == [
        (2018, "2018-09-29", False),  # only ever a comparative (in K19 and K20)
        (2019, "2019-09-28", True),
        (2020, "2020-09-26", True),
        (2021, "2021-09-25", True),
    ]


def test_detect_fiscal_periods_history_cutoff(companyfacts):
    facts = flatten(companyfacts, ["10-K", "10-K/A"])
    assert [p.fiscal_year for p in detect_fiscal_periods(facts, (340, 390), 7, 2)] == [2020, 2021]


@pytest.fixture
def result(companyfacts, cfg):
    r = process_companyfacts(companyfacts, cfg.concepts, cfg.ingest)
    return {(v.concept, v.fiscal_year): v for v in r.values}, r


def test_revenue_latest_vintage_tag_switch_and_restatement(result):
    vals, _ = result
    fy18, fy19, fy20, fy21 = (vals[("revenue", y)] for y in (2018, 2019, 2020, 2021))
    assert fy18.latest.value == 1000 and fy18.latest.strategy.label == "SalesRevenueNet"
    assert fy18.n_vintages == 2 and not fy18.restated
    # FY2019: first filed 1100 under SalesRevenueNet (K19), restated 1150 under ASC 606 tag (K20)
    assert fy19.latest.value == 1150
    assert fy19.latest.strategy.label == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert fy19.first.value == 1100 and fy19.first.strategy.label == "SalesRevenueNet"
    assert fy19.restated
    assert str(fy19.latest.filed) == "2020-10-30"
    assert fy20.latest.value == 1200 and not fy20.restated and fy20.n_vintages == 2
    # FY2021 amended by 10-K/A
    assert fy21.latest.value == 1310 and fy21.latest.form == "10-K/A" and fy21.first.value == 1300
    assert fy21.restated


def test_quarterly_fact_inside_10k_ignored(result):
    vals, _ = result
    assert vals[("revenue", 2019)].first.value == 1100  # not the 300 Q4 figure


def test_debt_sum_and_partial_components(result):
    vals, _ = result
    d19 = vals[("total_debt", 2019)]
    assert d19.latest.value == 900 + 100 + 50
    assert d19.latest.strategy.rank == 1
    assert {c["tag"] for c in d19.latest.components} == {
        "us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtCurrent", "us-gaap:CommercialPaper"}
    assert vals[("total_debt", 2020)].latest.value == 950
    d21 = vals[("total_debt", 2021)]  # only the current portion reported: must NOT be silently 100
    assert d21.status == "missing" and d21.latest is None
    assert "lacks required LongTermDebtNoncurrent" in d21.reason
    assert "debt-free" not in d21.reason  # something WAS reported, so not "may be debt-free"
    d18 = vals[("total_debt", 2018)]
    assert d18.status == "missing" and "no candidate tag reported" in d18.reason
    assert "debt-free" in d18.reason  # absent_note from config


def test_operating_leases_not_applicable_pre_asc842(result):
    vals, _ = result
    assert vals[("operating_lease_liabilities", 2019)].status == "not_applicable"
    assert "ASC 842" in vals[("operating_lease_liabilities", 2019)].reason
    assert vals[("operating_lease_liabilities", 2020)].latest.value == 230
    assert vals[("operating_lease_liabilities", 2020)].latest.strategy.rank == 2
    assert vals[("operating_lease_liabilities", 2021)].status == "missing"


def test_missing_concept_has_reason(result):
    vals, _ = result
    gw = vals[("goodwill", 2020)]
    assert gw.status == "missing" and gw.latest is None
    assert "no candidate tag ever reported" in gw.reason


def test_conflicting_duplicate_is_noted(result):
    vals, _ = result
    a21 = vals[("total_assets", 2021)]
    assert a21.latest.value == 5500
    assert any("conflicting values" in n for n in a21.latest.notes)


def test_shares_unit_and_split_restatement(result):
    vals, _ = result
    assert vals[("diluted_shares", 2018)].latest.value == 98   # pre-split, never restated
    assert vals[("diluted_shares", 2019)].latest.value == 400  # split-adjusted comparative
    assert vals[("diluted_shares", 2019)].first.value == 100


def test_every_non_ok_value_has_reason(result):
    vals, _ = result
    for v in vals.values():
        assert (v.status == "ok") == (v.latest is not None)
        if v.status != "ok":
            assert v.reason


def test_parse_concepts_rejects_bad_period():
    with pytest.raises(ValueError):
        parse_concepts({"x": {"period": "quarter", "strategies": ["A"]}})
