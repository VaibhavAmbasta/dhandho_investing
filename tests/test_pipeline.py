"""End-to-end: ingest from a pre-populated cache (zero network), coverage, verify, point-in-time."""
import sqlite3

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
