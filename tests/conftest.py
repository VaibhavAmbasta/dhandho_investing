"""Hand-built companyfacts fixture for a fictional September-FY company, TESTCO.

Filings (all values hand-chosen so expected outputs can be computed by hand):
  K19  10-K   filed 2019-11-01  FY2019 (ends 2019-09-28), comparatives FY2018
  K20  10-K   filed 2020-10-30  FY2020 (ends 2020-09-26), comparatives FY2019, FY2018
  K21  10-K   filed 2021-10-29  FY2021 (ends 2021-09-25), comparative FY2020
  K21A 10-K/A filed 2022-01-15  amends FY2021 revenue 1300 -> 1310
  Q    10-Q   filed 2021-05-01  must be ignored entirely
"""
from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FY18 = ("2017-10-01", "2018-09-29")
FY19 = ("2018-09-30", "2019-09-28")
FY20 = ("2019-09-29", "2020-09-26")
FY21 = ("2020-09-27", "2021-09-25")

K19 = ("0000000001-19-000001", "10-K", "2019-11-01", 2019)
K20 = ("0000000001-20-000001", "10-K", "2020-10-30", 2020)
K21 = ("0000000001-21-000001", "10-K", "2021-10-29", 2021)
K21A = ("0000000001-22-000001", "10-K/A", "2022-01-15", 2021)
Q = ("0000000001-21-000002", "10-Q", "2021-05-01", 2021)


def dur(period, val, filing):
    accn, form, filed, fy = filing
    return {"start": period[0], "end": period[1], "val": val, "accn": accn, "fy": fy,
            "fp": "FY" if form.startswith("10-K") else "Q2", "form": form, "filed": filed}


def inst(end, val, filing):
    accn, form, filed, fy = filing
    return {"end": end, "val": val, "accn": accn, "fy": fy,
            "fp": "FY" if form.startswith("10-K") else "Q2", "form": form, "filed": filed}


def build_companyfacts() -> dict:
    E18, E19, E20, E21 = FY18[1], FY19[1], FY20[1], FY21[1]
    g = {
        # Revenue: old tag through K19, ASC 606 tag from K20 (which also restates FY2019 1100 -> 1150).
        "SalesRevenueNet": {"USD": [
            dur(FY18, 1000, K19), dur(FY19, 1100, K19),
            dur(("2019-06-30", "2019-09-28"), 300, K19),  # Q4 inside the 10-K: not annual, ignore
            dur(FY18, 1000, K20),
        ]},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": [
            dur(FY19, 1150, K20), dur(FY20, 1200, K20),
            dur(FY20, 1200, K21), dur(FY21, 1300, K21),
            dur(FY21, 1310, K21A),
            dur(("2020-12-27", "2021-03-27"), 333, Q),
        ]},
        "Assets": {"USD": [
            inst(E18, 4800, K19), inst(E19, 5000, K19),
            inst(E19, 5000, K20), inst(E20, 5200, K20),
            inst(E20, 5200, K21), inst(E21, 5500, K21), inst(E21, 5501, K21),  # conflicting duplicate
            inst("2021-03-27", 9999, Q),
        ]},
        "LongTermDebtNoncurrent": {"USD": [inst(E19, 900, K19), inst(E20, 950, K20)]},
        "LongTermDebtCurrent": {"USD": [inst(E19, 100, K19), inst(E21, 100, K21)]},
        "CommercialPaper": {"USD": [inst(E19, 50, K19)]},
        "OperatingLeaseLiabilityNoncurrent": {"USD": [inst(E20, 200, K20)]},
        "OperatingLeaseLiabilityCurrent": {"USD": [inst(E20, 30, K20)]},
        # 4:1 split during FY2020. K20 restates FY2019 comparatives; FY2018 only exists pre-split.
        "WeightedAverageNumberOfDilutedSharesOutstanding": {"shares": [
            dur(FY18, 98, K19), dur(FY19, 100, K19),
            dur(FY19, 400, K20), dur(FY20, 400, K20),
        ]},
        "NetCashProvidedByUsedInOperatingActivities": {"USD": [
            dur(FY19, 250, K19), dur(FY20, 270, K20), dur(FY21, 290, K21)]},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"USD": [
            dur(FY19, 40, K19), dur(FY20, 45, K20), dur(FY21, 50, K21)]},
    }
    facts = {"us-gaap": {tag: {"label": tag, "description": "", "units": units} for tag, units in g.items()}}
    facts["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [inst("2021-10-15", 395, K21)]}}}
    return {"cik": 1234567, "entityName": "TestCo Inc.", "facts": facts}


@pytest.fixture
def companyfacts() -> dict:
    return build_companyfacts()


@pytest.fixture
def cfg():
    from dhandho.config import load_config
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def project(tmp_path: Path, companyfacts) -> Path:
    """A project dir with config, tickers and a pre-populated SEC cache (no network needed)."""
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "tickers.txt").write_text("# test\ntestco\n")
    cache = tmp_path / "data" / "raw"
    (cache / "reference").mkdir(parents=True)
    (cache / "companyfacts").mkdir(parents=True)
    with gzip.open(cache / "reference" / "company_tickers.json.gz", "wt") as fh:
        json.dump({"0": {"cik_str": 1234567, "ticker": "TESTCO", "title": "TestCo Inc."}}, fh)
    with gzip.open(cache / "companyfacts" / "CIK0001234567.json.gz", "wt") as fh:
        json.dump(companyfacts, fh)
    return tmp_path
