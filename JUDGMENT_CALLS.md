# Accounting and data judgment calls

Each item is a choice that changes numbers. Every one is either a value in
`config.yaml` or flagged here so you can overrule it.

## Phase 1: data layer

| # | Call | Where | Why / risk |
|---|------|-------|-----------|
| 1 | **Fiscal-year label = calendar year of the period end**, except a 52/53-week year ending in the first 7 days of January is labelled the prior year. | `ingest.fiscal_year_january_rollback_days` | Matches AAPL/MSFT/COST/AZO naming. **Mismatches some Jan-FYE retailers' own naming** (Home Depot calls the year ending Jan-2024 "fiscal 2023"; we call it 2024). Deterministic, but not the filer's own label. |
| 2 | **Canonical value = latest-filed vintage.** Every vintage is kept in `concept_vintages` with its accession and filing date; `db.values_as_of()` gives point-in-time views. | `facts.compute_concept` | Later 10-Ks recast comparatives (ASC 606 restatement, discontinued ops, segment moves). Latest = most comparable to recent years, but **not what an investor saw at the time**. Backtests must use `values_as_of`. |
| 3 | **Annual = duration of 340-390 days.** 10-KT transition periods and quarterly figures inside 10-Ks are dropped. | `ingest.annual_duration_days` | A fiscal-year-end change leaves a gap year (reported in the fiscal-period notes). |
| 4 | **Fiscal years = each 10-K's main period + comparative years reported in ≥ 2 filings.** Pre-XBRL comparative years (e.g. 2007-2008) are included. | `facts.detect_fiscal_periods` | Those years have income-statement data but few balance-sheet values. Flagged as warnings. |
| 5 | **Sums: missing optional components count as zero.** E.g. debt = LT debt non-current (required) + current portion + commercial paper + short-term borrowings (optional). | `concepts.*.strategies` | A company that tags its current portion under a tag we don't list gets **understated debt with no error**. The verify printout shows every component so you can catch this. |
| 6 | **Debt excludes finance leases**, and assumes `ShortTermBorrowings` does not already contain `CommercialPaper`. | `concepts.total_debt` | Finance leases are often already inside the debt line, so adding them risks double counting. Some filers *do* nest CP inside short-term borrowings, and for them we would double count. |
| 7 | **No zero-filling.** Missing goodwill, intangibles, debt or SBC is stored as NULL plus a reason ("company may have none"), never 0. | `concepts.*.absent_note` | Phases 2-3 must choose explicitly when NULL means 0. A silent zero would hide real data gaps. |
| 8 | **Operating income has no fallback** to pre-tax income. | `concepts.operating_income` | Banks and insurers get NULL. Mixing EBIT with EBT would corrupt margins and ROIC. |
| 9 | **Revenue: `Revenues` tried before the ASC 606 contract-revenue tag.** | `concepts.revenue` | `Revenues` is total revenue. `RevenueFromContractWithCustomer...` excludes lease, interest and other non-contract revenue. Order affects when tag switches show up. |
| 10 | **Net income = attributable to parent** (`NetIncomeLoss`). `ProfitLoss` (includes NCI) is the last fallback. **Equity**'s fallback also includes NCI. | `concepts.net_income`, `equity` | Flagged in the tag log whenever the fallback is used. |
| 11 | **Cash fallback includes restricted cash.** Marketable securities are **separate** concepts (`short_term_investments`, `long_term_investments`), which I added beyond your list. | `concepts.cash` and the extras | Netting only cash against debt would wrongly put AAPL in heavy net debt. Whether *long-term* securities count as cash is a Phase 2 decision. |
| 12 | **Capex = PP&E purchases only.** Capitalized software, acquisitions and intangible purchases are excluded. | `concepts.capex` | Overstates owner FCF for companies that capitalize a lot of software, and for serial acquirers (e.g. roll-ups). |
| 13 | **SBC = pre-tax cash-flow add-back.** | `concepts.stock_based_comp` | Phase 2 subtracts pre-tax SBC, which ignores the tax shield. That is conservative. |
| 14 | **Receivables = trade receivables only.** | `concepts.receivables` | Excludes AAPL's vendor non-trade receivables, which is correct for DSO. |
| 15 | **Operating leases are "not applicable" for periods ending before 2019-12-15** (pre-ASC 842). | `concepts.operating_lease_liabilities` | Early adopters still get values. Pre-842 lease debt is **not** reconstructed from disclosure notes, so lease-adjusted net debt is not comparable across 2019. |
| 16 | **Diluted shares = weighted-average diluted, as reported. Not split-adjusted.** | `concepts.diluted_shares` | XBRL comparatives are only split-adjusted in filings made after the split. Older years stay pre-split (AAPL 2020 4:1, CPRT, ORLY 2025 15:1). The coverage report flags jumps over 1.6x. **Must be fixed before Phase 2 dilution math.** |
| 17 | **Conflicting duplicates in one filing:** keep the fact whose duration is closest to 364 days, otherwise the first one. A note is stored with the value. | `facts._pick_one` | Rare; visible in the `notes` column and in the verify output. |
| 18 | **Only facts without dimensions are used** (companyfacts excludes dimensional facts). | SEC API | Segment and consolidated-subsidiary breakdowns are unavailable from this endpoint. |
| 19 | **Financials (JPM, BRK-B, MKL) go through the same industrial mapping.** | `tickers.txt` | Revenue, operating income, debt, capex and DSO are not meaningful for a bank or insurer. Expect gaps and misleading values. They are included on purpose to show where the screener breaks. |
