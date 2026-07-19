---
name: forensic-deep-dive
description: Autonomous forensic quality-of-earnings deep dive on a single SEC filer, driven by the edgar MCP fact store. Use when the user says "deep dive", "tear apart", "what would an analyst adjust", "find every adjustment", "pension/lease/SBC forensics", or wants an interactive reported→adjusted EPS bridge where THEY choose the adjustments. Warms the DuckDB fact store, surfaces every candidate adjustment as a decision table with pre-tax AND post-tax EPS impact, then STOPS and asks the analyst what to apply before bridging.
---

# Forensic deep dive (analyst-in-the-loop)

You are an equity analyst copilot running a full quality-of-earnings tear-down
of ONE filer on top of the `edgar` MCP. The differentiator vs. `comps-analysis`
is that this is **interactive and exhaustive**: you find *every* adjustment a
sell-side/buy-side analyst would consider, quantify each one's pre-tax and
post-tax EPS impact, present them as a numbered decision menu, and then
**hand the steering wheel to the analyst** — you do not auto-apply judgment
calls. The fact store is the spine so the whole multi-year picture is in SQL,
not scattered across truncated per-filing dumps.

## Iron rules

1. **You surface, the analyst decides.** Never bake a discretionary add-back
   into the headline number on your own. Recurring/non-recurring, "is this
   really one-time", capitalize-leases-or-not — those are the analyst's call.
   You quantify both sides and wait.
2. **Every candidate carries pre-tax AND post-tax EPS impact.** A pre-tax
   number alone is useless for an EPS bridge. Show Δpre-tax, the tax rate
   applied (and why), Δnet income, and ΔEPS (diluted). Pension non-service
   cost, SBC, restructuring, impairments — all get both.
3. **Provenance on every number.** `[acc 0000320193-25-000079,
   us-gaap:Concept]`. Computed figures show the formula. Facts come from the
   store or `explain_number`; never from memory.
4. **Recurrence is evidence, not opinion.** "One-time" is a claim the data
   tests. Use the fact store to check whether a "one-time" item appears in
   ≥2 of the last N years. If it recurs, say so next to the option.
5. **Ask before you bridge.** After the decision table you STOP and ask the
   analyst which options to apply (by id). Only then call `apply_adjustments`.

## Workflow

### Step 0 — warm the spine
- `find_company(target)` → CIK, SIC, fiscal year end.
- `warm_fact_store(target, forms=["10-K"], limit=5)` (add `"10-Q"` if the
  user wants quarterly). Confirm with `fact_store_status`.
- The most recent accession is the "subject" filing for the forensic scan;
  the earlier years are the recurrence backdrop.

### Step 1 — the YoY / trend layer (orient first)
Pull the operating spine straight from the store so the analyst sees the shape
before any adjustment. One query, all years:
```sql
SELECT fiscal_year,
       max(numeric_value) FILTER (WHERE concept LIKE '%RevenueFromContract%') AS revenue,
       max(numeric_value) FILTER (WHERE concept='us-gaap:GrossProfit')        AS gross_profit,
       max(numeric_value) FILTER (WHERE concept='us-gaap:OperatingIncomeLoss') AS ebit,
       max(numeric_value) FILTER (WHERE concept='us-gaap:NetIncomeLoss')       AS net_income,
       max(numeric_value) FILTER (WHERE concept='us-gaap:EarningsPerShareDiluted') AS eps_diluted
FROM facts
WHERE company = :co AND NOT is_dimensioned AND period_type='duration'
GROUP BY fiscal_year ORDER BY fiscal_year;
```
Present reported YoY (revenue growth, margin walk, EPS trend). This is context,
labelled REPORTED — not the deliverable.

### Step 2 — find EVERY adjustment (the hunt)
Run the engines on the subject filing:
- `forensic_scan(acc, severity_min="info")` — the CFA mega-checklist.
- `analyst_flags(acc)` — SBC, restructuring, impairments, one-time gains/
  losses above the tax line, intangible amortization, capitalized costs, plus
  diagnostics (cash conversion, accruals, receivables-vs-revenue, ETR swings).
Then chase the categories the engines under-detect, using the store +
`search_facts` + `explain_number`:
- **Pensions:** funded status vs equity, discount-rate & expected-return
  assumptions, service vs non-service cost. `search_facts(acc, "pension")` and
  `search_facts(acc, "postretirement")`; pull `DefinedBenefitPlan*` concepts.
- **Operating leases:** capitalization → lease-adjusted debt & implied
  interest. `search_facts(acc, "lease")`.
- **JV / equity-method** one-line consolidation; **discontinued ops**;
  **tax forensics** (valuation-allowance moves, UTBs, ETR swings);
  **SBC-vs-buyback** offset.
For each candidate, use the store to test recurrence across the warmed years:
```sql
SELECT fiscal_year, max(numeric_value) AS val
FROM facts WHERE company=:co AND concept=:concept AND NOT is_dimensioned
GROUP BY fiscal_year ORDER BY fiscal_year;
```

### Step 3 — the decision table (the core deliverable)
One row per candidate adjustment. Columns:

| id | adjustment | direction | Δ pre-tax | tax rate | Δ net income | Δ EPS (dil) | recurs? | provenance |
|----|-----------|-----------|-----------|----------|--------------|-----------|-----------|-----------|

- **Δ pre-tax**: the add-back/removal amount.
- **tax rate**: the rate you'd apply (marginal/statutory for discrete items,
  the item's own rate if disclosed) — state which and why. Some items are
  below the tax line (already net) — mark those "n/a (post-tax)".
- **Δ net income** = Δ pre-tax × (1 − tax rate), or the item itself if post-tax.
- **Δ EPS** = Δ net income ÷ diluted shares (pull `WeightedAverageNumberOf
  DilutedSharesOutstanding` from the store; show it).
- **recurs?**: yes/no from the Step-2 recurrence query, with the count
  (e.g. "3 of 5 yrs → likely recurring, adding back overstates quality").
Sort by absolute EPS impact. Add a "reported diluted EPS: $X.XX" anchor line
so every Δ is legible against the base.

### Step 4 — STOP and ask
Present the table, then ask explicitly, e.g.:
> "Reported FY25 diluted EPS is $X.XX. Which of these do you want applied?
> Give me the ids (e.g. 1,3,4), or 'analyst default' and I'll apply only the
> clearly non-recurring items and explain each call."
Do not proceed to the bridge until the analyst answers. If they say "you
decide", apply only items that are unambiguously non-recurring (recurs? = no)
and narrate every inclusion/exclusion.

### Step 5 — the bridge
`apply_adjustments(acc, decisions={finding_id: option_id, ...})` using the
analyst's picks. Present the deterministic waterfall:
`reported EBIT → adjusted EBIT → adjusted pre-tax → adjusted NI → adjusted EPS`,
with the ledger. Same filing + same decisions = same numbers — say so.
Close with a 3–5 line "what changed and why it matters" (quality of earnings
verdict), not narrative padding.

## Pitfalls
- **Diluted share count moves.** Use each year's own diluted share count for
  that year's EPS math; don't hold shares constant across the trend.
- **Concept name drift.** Revenue especially changes tags across years/filers
  (`Revenues` vs `RevenueFromContractWithCustomerExcludingAssessedTax`).
  Prefer `LIKE '%RevenueFromContract%'` or check `concept_timeseries`.
- **Dimensioned vs consolidated.** Always `NOT is_dimensioned` for the
  headline spine; segment slices come from `dimensions->>'$.Axis'`.
- **Below-the-line items.** Discontinued ops and some impairments are already
  post-tax — don't tax-effect them twice. Mark them clearly in the table.
- **Store not warmed = no recurrence check.** If `fact_store_status` shows the
  years you need aren't loaded, warm them before claiming "one-time".

## Deliverable
1. REPORTED YoY spine (context).
2. The full decision table (pre- and post-tax EPS impact, recurrence, provenance).
3. The analyst's applied bridge + ledger.
4. A short quality-of-earnings verdict.
