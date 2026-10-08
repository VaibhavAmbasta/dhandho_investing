"""Hand-computed fixtures for every Phase 2 formula."""
import pytest

from dhandho.dcf import cagr, enterprise_value_implied, implied_growth
from dhandho.shares import classify_ratio
from dhandho.valuation import _excluded, hist_cagr


def test_cagr():
    assert cagr(100, 121, 2) == pytest.approx(0.10)
    assert cagr(200, 100, 1) == pytest.approx(-0.50)
    with pytest.raises(ValueError):
        cagr(-5, 100, 3)
    with pytest.raises(ValueError):
        cagr(100, 0, 3)


def test_ev_zero_growth_by_hand():
    # fcf 100, g 0, r 10%, 2 years, 10x terminal:
    # 100/1.1 + 100/1.21 + 10*100/1.21 = 90.909 + 82.645 + 826.446 = 1000.000
    assert enterprise_value_implied(100, 0.0, 0.10, 10, 2) == pytest.approx(1000.0)


def test_ev_growth_equals_discount_rate():
    # x = 1: explicit 100 + 100, terminal 10 * 100 -> 1200
    assert enterprise_value_implied(100, 0.10, 0.10, 10, 2) == pytest.approx(1200.0)


def test_dilution_offsets_growth():
    # 10% FCF growth fully diluted away by 10%/yr share growth == zero growth per share
    assert enterprise_value_implied(100, 0.10, 0.10, 10, 2, dilution=0.10) == pytest.approx(1000.0)


def test_buybacks_raise_value_per_existing_share():
    base = enterprise_value_implied(100, 0.0, 0.10, 10, 2)
    assert enterprise_value_implied(100, 0.0, 0.10, 10, 2, dilution=-0.03) > base


def test_implied_growth_recovers_hand_cases():
    assert implied_growth(1200, 100, 0.10, 10, 2).growth == pytest.approx(0.10, abs=1e-6)
    assert implied_growth(1000, 100, 0.10, 10, 2).growth == pytest.approx(0.0, abs=1e-6)
    assert implied_growth(1000, 100, 0.10, 10, 2, dilution=0.10).growth == pytest.approx(0.10, abs=1e-6)


def test_implied_growth_round_trip():
    g = implied_growth(5_000, 240, 0.09, 17.5, 10, dilution=-0.02).growth
    assert enterprise_value_implied(240, g, 0.09, 17.5, 10, -0.02) == pytest.approx(5_000, rel=1e-6)


def test_implied_growth_null_reasons():
    assert implied_growth(1000, -5, 0.1, 10, 10).growth is None
    assert "not positive" in implied_growth(1000, -5, 0.1, 10, 10).reason
    assert "net cash exceeds" in implied_growth(-10, 100, 0.1, 10, 10).reason
    assert "above solver bound" in implied_growth(1e12, 1, 0.1, 10, 10).reason
    assert "below solver bound" in implied_growth(1, 1000, 0.1, 10, 10).reason


def test_hist_cagr_smoothed_geometric_series():
    series = {y: 100 * 1.1 ** (y - 2010) for y in range(2010, 2026)}
    g, span, reason = hist_cagr(series, 2025, 10, 3, 5)
    assert g == pytest.approx(0.10) and span == 10 and reason is None


def test_hist_cagr_point_to_point():
    series = {2015: 100.0, 2025: 200.0}
    g, span, _ = hist_cagr(series, 2025, 10, 1, 5)
    assert g == pytest.approx(2 ** 0.1 - 1) and span == 10


def test_hist_cagr_falls_back_to_shorter_span():
    series = {y: 100.0 for y in range(2013, 2026)}
    series[2013] = None  # needed for the 10y span with 3y smoothing
    g, span, _ = hist_cagr(series, 2025, 10, 3, 5)
    assert span == 9 and g == pytest.approx(0.0)


def test_hist_cagr_negative_endpoint_is_null_with_reason():
    series = {y: (-50.0 if y < 2018 else 100.0) for y in range(2010, 2026)}
    g, _, reason = hist_cagr(series, 2025, 10, 3, 5)
    assert g is None and "non-positive" in reason


@pytest.mark.parametrize("ratio,kind", [
    (4.0, "split"), (15.0001, "split"), (7.0, "split"), (1.5, "split"), (0.5, "reverse_split"),
    (0.1, "reverse_split"), (1000.0, "scale_error"), (0.001, "scale_error"), (1.01, "none"),
    (1.3, "unexplained"), (2.3, "unexplained"),
])
def test_classify_ratio(ratio, kind):
    assert classify_ratio(ratio, 0.02) == kind


class _Cfg:
    raw = {"screen": {"exclude_sic_ranges": [[6000, 6799]]}}


def test_financials_excluded_by_sic():
    assert "financials" in _excluded(_Cfg, 6021)
    assert _excluded(_Cfg, 3571) is None
    assert _excluded(_Cfg, None) is None


# ---- normalization, upkeep capex, earning power -------------------------------------------
from dhandho.valuation import cash_year, hist_cagr_normalized, normalized_from_margins  # noqa: E402


def test_normalized_from_margins_median_times_latest_revenue():
    fcf = {2021: 20.0, 2022: 5.0, 2023: 30.0, 2024: 24.0, 2025: 6.0}      # two one-off bad years
    rev = {2021: 100.0, 2022: 100.0, 2023: 150.0, 2024: 120.0, 2025: 200.0}
    # margins 20%, 5%, 20%, 20%, 3% -> median 20% -> x latest revenue 200 = 40
    v, m, why = normalized_from_margins(fcf, rev, 2025, 5, 3)
    assert m == pytest.approx(0.20) and v == pytest.approx(40.0) and why is None


def test_normalized_needs_min_years_and_revenue():
    v, _, why = normalized_from_margins({2025: 10.0, 2024: 9.0}, {2025: 100.0, 2024: 90.0}, 2025, 5, 3)
    assert v is None and "only 2 of 5" in why
    v, _, why = normalized_from_margins({2025: 10.0}, {2025: None}, 2025, 5, 1)
    assert v is None and "no positive revenue" in why


def test_hist_cagr_normalized_constant_margin():
    rev = {y: 100 * 1.08 ** (y - 2010) for y in range(2010, 2026)}
    fcf = {y: 0.2 * v for y, v in rev.items()}
    fcf[2024] = 1.0  # a one-off year inside the end window: the median ignores it
    g, span, why = hist_cagr_normalized(fcf, rev, 2025, 10, 5, 5, 3)
    assert span == 10 and g == pytest.approx(0.08) and why is None


class FakeFacts:
    def __init__(self, vals):
        self.vals = vals
        self.ever_ok = {c for (c, _), v in vals.items() if v is not None}

    def value(self, concept, fy):
        v = self.vals.get((concept, fy))
        return (v, None, {"concept": concept}) if v is not None else (None, f"{concept} missing", None)


RD = {"capex": {"include_finance_lease_additions": True}}


def test_cash_year_finance_leases_and_depreciation():
    f = FakeFacts({("operating_cash_flow", 1): 180.0, ("capex", 1): 115.0, ("stock_based_comp", 1): 12.0,
                   ("finance_lease_additions", 1): 25.0, ("depreciation", 1): 34.0,
                   ("finance_lease_amortization", 1): 5.0})
    cy = cash_year(f, 1, RD)
    assert cy.capex_total == 140.0                  # 115 capex + 25 finance-lease assets
    assert cy.fcf == pytest.approx(180 - 140 - 12)  # 28
    assert cy.maintenance_capex == 39.0             # depreciation 34 + lease amortization 5
    assert cy.owner_earnings == pytest.approx(180 - 39 - 12)


def test_cash_year_da_minus_amortization_and_cap_at_capex():
    f = FakeFacts({("operating_cash_flow", 1): 100.0, ("capex", 1): 10.0, ("stock_based_comp", 1): 0.0,
                   ("depreciation_amortization", 1): 30.0, ("amortization_intangibles", 1): 8.0})
    cy = cash_year(f, 1, RD)
    assert cy.maintenance_capex == 10.0             # D&A 30 - amortization 8 = 22, capped at total capex 10
    assert cy.owner_earnings == cy.fcf == 90.0


def test_cash_year_da_without_amortization_is_stated():
    f = FakeFacts({("operating_cash_flow", 1): 100.0, ("capex", 1): 40.0, ("stock_based_comp", 1): 5.0,
                   ("depreciation_amortization", 1): 30.0})
    cy = cash_year(f, 1, RD)
    assert cy.maintenance_capex == 30.0 and cy.owner_earnings == 65.0
    assert any("only total D&A" in a for a, _ in cy.assumptions)


def test_cash_year_no_depreciation_gives_reason():
    f = FakeFacts({("operating_cash_flow", 1): 100.0, ("capex", 1): 40.0, ("stock_based_comp", 1): 5.0})
    cy = cash_year(f, 1, RD)
    assert cy.fcf == 55.0 and cy.owner_earnings is None and "depreciation" in cy.oe_reason
