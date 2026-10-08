"""End-to-end: ingest from a pre-populated cache (zero network), coverage, verify, point-in-time."""
import json
import sqlite3

import pytest

from dhandho import db
from dhandho.cli import main


def run(project, *args):
    return main(["--config", str(project / "config.yaml"), *args])


def test_end_to_end_offline(project, capsys):
    assert run(project, "ingest") == 0
    out = capsys.readouterr().out
    assert "OK    TESTCO" in out and "0 network requests" in out

    conn = sqlite3.connect(project / "data" / "dhandho.sqlite")
    conn.row_factory = sqlite3.Row
    rev = {r["fiscal_year"]: r for r in conn.execute(
        "SELECT * FROM concept_values WHERE concept='revenue'")}
    assert rev[2019]["value"] == 1150 and rev[2019]["restated"] == 0
    assert rev[2019]["filed"] == "2020-10-30" and "SalesRevenueNet=1,100" in rev[2019]["notes"]
    assert rev[2021]["value"] == 1310 and rev[2021]["first_value"] == 1300 and rev[2021]["restated"] == 1
    # schema forbids a NULL value without a reason
    for r in conn.execute("SELECT * FROM concept_values WHERE status != 'ok'"):
        assert r["value"] is None and r["reason"]

    assert run(project, "coverage") == 0
    out = capsys.readouterr().out
    assert "revenue: FY2018 SalesRevenueNet -> FY2019 RevenueFromContractWithCustomerExcludingAssessedTax" in out
    assert "likely stock split" in out
    assert "pre-XBRL" in out
    reports = project / "reports"
    assert (reports / "coverage.html").read_text().startswith("<!doctype html>")
    assert (reports / "tag_log.csv").exists() and (reports / "coverage_long.csv").exists()

    assert run(project, "verify", "testco", "--years", "2") == 0
    out = capsys.readouterr().out
    assert "FY2020 (ends 2020-09-26)" in out and "FY2019" not in out.split("Fiscal years:")[1].splitlines()[0]
    assert "us-gaap:LongTermDebtNoncurrent" in out
    assert "first filed 1,300" in out  # FY2021 revenue amended by 10-K/A


def test_point_in_time(project):
    assert run(project, "ingest") == 0
    conn = db.connect(project / "data" / "dhandho.sqlite")

    def rev(as_of):
        df = db.values_as_of(conn, 1234567, as_of)
        return dict(df[df.concept == "revenue"][["fiscal_year", "value"]].values.tolist())

    assert rev("2019-10-31") == {}                                  # before first 10-K
    assert rev("2020-01-01") == {2018: 1000, 2019: 1100}            # as originally reported
    assert rev("2020-12-31") == {2018: 1000, 2019: 1150, 2020: 1200}  # after ASC 606 restatement
    assert rev("2021-12-31")[2021] == 1300                          # before the 10-K/A
    assert rev("2022-06-30")[2021] == 1310

    df = db.values_as_of(conn, 1234567, "2021-01-01")
    assert df[(df.concept == "cash") & (df.fiscal_year == 2019)]["value"].tolist() == [60]  # rank A beats later rank B


def test_unknown_ticker_logged_not_silent(project, capsys):
    (project / "tickers.txt").write_text("TESTCO\nNOPE\n")
    assert run(project, "ingest") == 1
    out = capsys.readouterr().out
    assert "ERROR NOPE" in out
    conn = sqlite3.connect(project / "data" / "dhandho.sqlite")
    assert conn.execute("SELECT status FROM ingest_log WHERE ticker='NOPE'").fetchone()[0] == "error"


def test_split_adjustment_from_restated_comparatives(project):
    from dhandho.shares import adjust_diluted_shares

    assert run(project, "ingest") == 0
    conn = db.connect(project / "data" / "dhandho.sqlite")
    adj = adjust_diluted_shares(conn, 1234567)
    # K19 reports FY2019 = 100, K20 restates it as 400 -> 4:1 split; FY2018 (only in K19) becomes 98 * 4
    assert [(e.kind, round(e.ratio, 6)) for e in adj.events] == [("split", 4.0)]
    assert adj.by_year == {2018: 392, 2019: 400, 2020: 400}


def test_reverse_dcf_end_to_end(project, capsys):
    from dhandho.dcf import enterprise_value_implied

    (project / "prices.csv").write_text("ticker,date,close,source\nTESTCO,2022-03-01,10,test\n")
    cfg_text = (project / "config.yaml").read_text().replace("providers: [csv, yfinance]", "providers: [csv]")
    (project / "config.yaml").write_text(cfg_text)
    assert run(project, "ingest") == 0
    assert run(project, "dcf", "--date", "2022-03-04") == 0
    capsys.readouterr()

    conn = sqlite3.connect(project / "data" / "dhandho.sqlite")
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM reverse_dcf WHERE ticker = 'TESTCO'").fetchone()
    # FY2021 owner FCF = OCF 290 - capex 50 - SBC (never reported -> 0, stated as an assumption)
    assert r["base_fcf"] == 240
    assert "SBC FY2021 never reported" in r["assumptions"]
    # net debt: debt / leases / cash all missing for FY2021 -> each 0, each stated
    assert r["net_debt"] == 0 and "total_debt FY2021 treated as 0" in r["assumptions"]
    # market cap = csv price 10 x cover-page shares 395
    assert r["price"] == 10 and r["shares_outstanding"] == 395 and r["market_cap"] == 3950
    assert r["price_date"] == "2022-03-01" and r["price_reliable"] == 0
    # dilution needs FY2016 shares, which don't exist -> NULL with reason, so implied_growth is NULL
    reasons = json.loads(r["reasons"])
    assert r["dilution_rate"] is None and "FY2016" in reasons["dilution_rate"]
    assert r["implied_growth"] is None and "FY2016" in reasons["implied_growth"]
    # ...but the no-dilution solve still works and round-trips
    g = r["implied_growth_no_dilution"]
    assert enterprise_value_implied(240, g, 0.10, 15, 10) == pytest.approx(3950, rel=1e-6)
    # sensitivity grid: 4 discount rates x 5 multiples, all NULL here with the dilution reason
    rows = conn.execute("SELECT * FROM reverse_dcf_sensitivity WHERE ticker = 'TESTCO'").fetchall()
    assert len(rows) == 20 and all(x["implied_growth"] is None and x["reason"] for x in rows)
    # price is cached for reproducibility
    assert conn.execute("SELECT price FROM prices WHERE ticker='TESTCO'").fetchone()[0] == 10


def test_reverse_dcf_without_dilution_model(project, capsys):
    (project / "prices.csv").write_text("ticker,date,close,source\nTESTCO,2022-03-01,10,test\n")
    cfg_text = (project / "config.yaml").read_text().replace("providers: [csv, yfinance]", "providers: [csv]")
    (project / "config.yaml").write_text(cfg_text.replace("model_dilution: true", "model_dilution: false"))
    assert run(project, "ingest") == 0
    assert run(project, "dcf", "--date", "2022-03-04") == 0
    conn = sqlite3.connect(project / "data" / "dhandho.sqlite")
    ig, ind = conn.execute(
        "SELECT implied_growth, implied_growth_no_dilution FROM reverse_dcf WHERE ticker='TESTCO'").fetchone()
    assert ig is not None and ig == pytest.approx(ind)
    n = conn.execute("SELECT COUNT(*) FROM reverse_dcf_sensitivity WHERE implied_growth IS NOT NULL").fetchone()[0]
    assert n == 20
    # higher discount rate -> the same price implies more growth
    g8, g11 = (conn.execute("SELECT implied_growth FROM reverse_dcf_sensitivity WHERE discount_rate=? AND "
                            "terminal_multiple=15", (dr,)).fetchone()[0] for dr in (0.08, 0.11))
    assert g11 > g8
