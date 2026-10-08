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
