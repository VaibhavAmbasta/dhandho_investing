"""Pure valuation math. No I/O: every function here is unit-tested against hand-computed numbers."""
from __future__ import annotations

from dataclasses import dataclass


def cagr(start: float, end: float, years: float) -> float:
    """Compound annual growth rate. Undefined (ValueError) for non-positive endpoints."""
    if years <= 0:
        raise ValueError("years must be positive")
    if start <= 0 or end <= 0:
        raise ValueError(f"CAGR undefined for non-positive endpoints (start={start:,.0f}, end={end:,.0f})")
    return (end / start) ** (1.0 / years) - 1.0


def enterprise_value_implied(
    fcf0: float, growth: float, discount_rate: float, terminal_multiple: float, years: int, dilution: float = 0.0
) -> float:
    """Present value, attributable to TODAY's shares, of owner FCF growing at `growth` for
    `years` years plus a terminal value of terminal_multiple x year-N FCF.

    Dilution: if the share count grows at `dilution` per year, today's holders own
    1/(1+dilution)^t of year-t FCF. A negative rate (net buybacks) raises their share.
        PV = sum_{t=1..N} fcf0 * x^t + terminal_multiple * fcf0 * x^N,
        x  = (1+growth) / ((1+discount_rate) * (1+dilution))
    Cash flows are discounted at year ends (no mid-year convention)."""
    x = (1.0 + growth) / ((1.0 + discount_rate) * (1.0 + dilution))
    explicit = sum(fcf0 * x**t for t in range(1, years + 1))
    terminal = terminal_multiple * fcf0 * x**years
    return explicit + terminal


@dataclass(frozen=True)
class Implied:
    growth: float | None
    reason: str | None = None


def implied_growth(
    target_ev: float,
    fcf0: float,
    discount_rate: float,
    terminal_multiple: float,
    years: int,
    dilution: float = 0.0,
    bounds: tuple[float, float] = (-0.5, 1.0),
    tol: float = 1e-7,
) -> Implied:
    """Solve enterprise_value_implied(g) = target_ev for g by bisection.

    target_ev = market cap + net debt (what the market pays for the owner FCF stream).
    The PV is strictly increasing in g when fcf0 > 0, so the root is unique if bracketed."""
    if fcf0 <= 0:
        return Implied(None, f"base owner FCF is not positive ({fcf0:,.0f}); a growth rate cannot justify the price")
    if target_ev <= 0:
        return Implied(None, f"market cap + net debt is not positive ({target_ev:,.0f}); net cash exceeds market cap")
    lo, hi = bounds

    def f(g: float) -> float:
        return enterprise_value_implied(fcf0, g, discount_rate, terminal_multiple, years, dilution) - target_ev

    f_lo, f_hi = f(lo), f(hi)
    if f_lo > 0:
        return Implied(None, f"price implies FCF shrinking faster than {lo:.0%}/yr (below solver bound)")
    if f_hi < 0:
        return Implied(None, f"price implies FCF growth above {hi:.0%}/yr (above solver bound)")
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return Implied((lo + hi) / 2)
