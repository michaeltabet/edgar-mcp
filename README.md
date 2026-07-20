# fundamentalsmcp

Deep, structured MCP access to SEC EDGAR — plus a queryable fact store,
global market data, macro, and semantic search over filing text.

The design goal: an AI using this server can take **any number in any
filing** and see exactly what it is — the XBRL concept behind it, the
official FASB/SEC definition of that concept, the period and
segment/geography context it belongs to, what it arithmetically sums into
and what sums to produce it, its rounding precision, and any footnotes
attached. Not filing text blobs — parsed, labeled, linked data. And then
ask questions **across** filings, companies, and years in plain SQL.

Built on [edgartools](https://github.com/dgunning/edgartools) (MIT) for XBRL
parsing, plus direct SEC APIs (EFTS full-text search, `companyconcept` fact
history), the FASB/SEC taxonomy documentation labels, DuckDB (fact store),
Yahoo Finance via yfinance (market data), FRED (macro, bring-your-own-key),
and LanceDB + fastembed (offline semantic search). Local caches live in
`~/.cache/fundamentalsmcp/`.

## Why this exists

I built this to make access to financial data actually easy — for agents and
for analysts. The current focus is **structuring the data as far as it can
go** (the DuckDB fact store, cross-filing SQL, the dossier) and **better
retrieval** (semantic search over filing text, concept-alias resolution
across tag drift). Auditability is tight by construction: every number
carries its source — accession + XBRL concept — so anything the tools state
can be traced back to the filing it came from.

**Feedback and contributions are very welcome.** If you want a capability
that isn't here, [open an issue](../../issues) and tell me what you want —
feature requests are as useful as PRs. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Tool layers

**Discovery**
| tool | what it does |
|---|---|
| `find_company` | ticker / CIK / name → EDGAR identity (CIK, SIC, fiscal year end) |
| `list_filings` | a company's filings filtered by form + date; yields accession numbers |
| `full_text_search` | content search across ALL filings since 2001 (exact phrases, subsidiary names, spin-off language) |

**Filing**
| tool | what it does |
|---|---|
| `filing_contents` | every document/exhibit in a filing + which sections are extractable |
| `read_section` | one 10-K/10-Q/8-K item ("Item 1A", "Item 7", "Item 2.02") as clean paged text |
| `read_document` | any exhibit (EX-99.1 press release, merger agreement, …) as paged text |

**XBRL-deep** (the differentiator)
| tool | what it does |
|---|---|
| `list_statements` | all ~70 tagged statements & disclosures in a filing, not just the big four |
| `financial_statements` | any statement with every row carrying concept, label, per-period values, hierarchy level, dimension axis/member, balance direction, calc weight + parent |
| `explain_number` | a concept **or a raw value** → official taxonomy definition, all labels, balance meaning, every fact with period/unit/precision/dimensions, calculation parents & children **with their actual values and an arithmetic tie-out check**, footnotes |
| `search_facts` | search all tagged facts by label, concept, or dimension member ("Greater China" finds the facts sliced by that segment) |
| `concept_timeseries` | every value a company ever reported for one tag, with form/date/accession provenance (SEC companyconcept API) |
| `statement_history` | one statement stitched across N filings — long multi-year (or multi-quarter) table, concepts aligned across label changes |
| `compare_peers` | one concept across ALL SEC filers for one period (Frames API) — economy-wide or a specific CIK list |

**Fact store** (cross-filing SQL — no truncation wall)
| tool | what it does |
|---|---|
| `warm_fact_store` | parse a company's filings once; land every tagged fact (concept · value · period · dimensions · calc weight · precision · provenance) into a local DuckDB |
| `query_fact_store` | arbitrary **read-only** SQL across everything warmed: `facts`, `filings`, `prices`, `macro` tables — multi-year, multi-segment, multi-company questions in one query |
| `concept_graph` | walk the calculation **knowledge graph** of a filing from any concept — down (what sums into it, with weights and values) or up (what it rolls into), recursive, depth-limited |
| `fact_store_status` | what's loaded |

**Dossier** (the capstone)
| tool | what it does |
|---|---|
| `company_dossier` | one call → multi-year three-statement spine (concept-alias resolution across tag drift), full ratio suite (margins, DuPont ROE decomposition, returns, liquidity, leverage & coverage, cash quality / accruals), YoY growth, live market multiples, and per-line concept provenance. Nulls are never invented |

**Market & macro**
| tool | what it does |
|---|---|
| `market_quote` | live snapshot for GLOBAL tickers (US, SGX `D05.SI`, LSE `.L`, HKEX `.HK`): price, market cap, EV, P/E, EV/EBITDA, P/B — turns EDGAR fundamentals into real multiples |
| `market_history` | OHLCV history, persisted into the DuckDB `prices` table for SQL joins against fundamentals |
| `fred_search` / `fred_series` | FRED macro series (rates, CPI, FX, international), persisted into the `macro` table. Bring your own free key via `FRED_API_KEY` |

**Semantic search** (offline, no API key)
| tool | what it does |
|---|---|
| `index_filing_text` | chunk + embed every section of a filing into a local LanceDB index (small ONNX embedder — no torch, nothing leaves your machine) |
| `semantic_search_filings` | find where filings discuss a concept even when the wording differs (supply concentration, going concern, a specific lawsuit), with section + accession provenance |
| `vector_store_status` | what's indexed |

**UK Companies House** (first non-US source, BYO key)
| tool | what it does |
|---|---|
| `uk_find_company` | search UK companies by name/number |
| `uk_company_profile` | profile + officers + **persons with significant control** (the beneficial-ownership register — who actually controls the company), accounts status, charges, insolvency history |
| `uk_filings` | filing history incl. the filed accounts documents |

**Statistics / ML** (honest about sample size)
| tool | what it does |
|---|---|
| `trend_analysis` | OLS trend on one concept's annual series: slope/year, R², CAGR, per-year residuals showing which years break trend. Reports `n` and states plainly that ~5 annual points are **descriptive, not inferential** — no p-values are fabricated |
| `anomaly_scan` | flags years statistically unlike a company's *own* other years, using robust z-scores (median/MAD) with small-sample guards: needs n≥5, scale floored at 5% of the level, scores capped — so a near-constant series can't manufacture a fake "18-sigma" event |
| `peer_scan` | cross-sectional comparison across 3+ companies (the regime where multivariate methods genuinely apply): robust z-scores per metric to find who's the outlier on what, plus optional KMeans clustering of standardized ratio vectors. With <5 peers the outlier list is reported as **NOT MEASURED**, never as an empty "all clear" |

**Forensic** (evidence-first; nothing adjusted without a human decision)
| tool | what it does |
|---|---|
| `forensic_scan` | the earnings-quality mega-checklist: add-back items with recurrence, pension (funded status vs equity, discount-rate & expected-return assumptions, non-service cost), operating-lease capitalization & lease-adjusted debt, JV/equity-method one-line consolidation, discontinued ops, tax forensics (valuation-allowance changes, unrecognized tax benefits, ETR swings), working capital days, capital structure & interest coverage, non-operating reliance, capitalization policy, Beneish M-score, SBC-vs-buyback offset. **Every finding cites its exact tagged facts; every judgment call is surfaced as pre-quantified options, never auto-applied** |
| `apply_adjustments` | deterministic adjusted EBIT → pre-tax → NI → EPS bridge from the analyst's decisions (finding_id → option_id); same filing + same decisions = same numbers, ledger included |
| `restatement_check` | 8-K Item 4.01 (auditor change) / 4.02 (non-reliance), 10-K/A / 10-Q/A amendments, NT late-filing notices |

**Analyst** (quality of earnings)
| tool | what it does |
|---|---|
| `analyst_flags` | flags the adjustments an analyst would flag — SBC, restructuring, impairments, one-time gains/losses above the tax line, intangible amortization, capitalized costs — each with calc-tree placement (is it inside EPS?), % of revenue / operating / pre-tax income, recurrence across periods, and the implication spelled out. Plus computed diagnostics: cash conversion, accruals, receivables-vs-revenue, ETR swings, EPS growth split into earnings vs buybacks |
| `compare_companies` | the full quality scan on two companies side by side, common-sized (each adjustment category as % of that company's own revenue/pre-tax) so they compare directly |

**Ownership**
| tool | what it does |
|---|---|
| `insider_transactions` | Forms 3/4/5 parsed from XML: who, role, code, shares, price, owned-after |
| `fund_holdings` | a 13F manager's latest portfolio: issuer, CUSIP, ticker, value, shares, put/call, voting, % of portfolio |

## Skills (agent playbooks, in `skills/`)

Methodology files an agent loads to use the tools *well* — each with hard
anti-hallucination rules (provenance on every number; never assert what other
investors are doing; sentiment is a walled, labelled layer):

- **comps-analysis** — adjustment-first comparable-companies & time-series analysis
- **forensic-deep-dive** — analyst-in-the-loop quality of earnings: hunt every adjustment (pensions, leases, SBC, tax, M&A deal effects, capitalization policy) → decision table with pre-tax AND post-tax EPS impact + recurrence evidence → the analyst chooses → deterministic bridge
- **company-dossier** — full primary-source workup: business model (Item 1), ranked risks (Item 1A), financials, comps, YoY, insiders
- **market-sentiment** — news/retail narrative as a clearly-labelled opinion layer, fact-checked against the filings

## Run

```sh
uv run fundamentalsmcp                      # stdio MCP server
uv run python tests/live_smoke.py     # live end-to-end test against EDGAR
```

Register with any MCP client (stdio). Example client config:

```json
{
  "mcpServers": {
    "fundamentals": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/fundamentalsmcp", "fundamentalsmcp"],
      "env": { "EDGAR_IDENTITY": "Your Name you@example.com" }
    }
  }
}
```

Configuration (environment variables):

- `EDGAR_IDENTITY` — **required by SEC fair-use policy**: your name + email
  for the User-Agent (e.g. `"Jane Doe jane@example.com"`)
- `FRED_API_KEY` — optional, for macro tools ([free key](https://fredaccount.stlouisfed.org/apikeys))
- `COMPANIES_HOUSE_API_KEY` — optional, for UK tools ([free key](https://developer.company-information.service.gov.uk))
- `EDGAR_MCP_TRANSPORT=streamable-http` (+ `EDGAR_MCP_HOST`/`EDGAR_MCP_PORT`) — serve over HTTP instead of stdio

Direct SEC calls are throttled under 10 req/s; parsed XBRL objects are
LRU-cached per accession; taxonomy definitions, the DuckDB fact store, and
the vector index cache under `~/.cache/fundamentalsmcp/`.

## Example workflow (spin-off hunting)

1. `full_text_search('"intention to spin off"', forms="8-K")` → candidates
2. `list_filings(cik, form="10-12B")` → the Form 10
3. `filing_contents` / `read_section` → Information Statement sections
4. `financial_statements(accession, "income")` → carve-out financials, every number tagged
5. `explain_number(accession, concept=...)` → what each number actually is
6. `concept_timeseries` → history once the spinco trades on its own

## Example workflow (cross-year in one query)

1. `warm_fact_store("AAPL", forms=["10-K"], limit=5)`
2. `query_fact_store("SELECT fiscal_year, max(numeric_value) FROM facts WHERE concept LIKE '%RevenueFromContract%' AND NOT is_dimensioned AND period_type='duration' GROUP BY 1 ORDER BY 1")`
3. `company_dossier("AAPL", ticker="AAPL")` → the whole picture, provenance-tagged

## Scheduled / orchestrated use

The server works well as a data layer under an orchestrator — e.g. a
[Temporal](https://temporal.io) workflow or an Airflow DAG that re-warms the
fact store daily, refreshes prices/macro, and re-runs forensic scans on new
filings. Two things to respect:

- **SEC rate limits.** All direct SEC calls are already throttled under
  10 req/s with your `EDGAR_IDENTITY`, but a parallel fan-out across many
  workers multiplies that — keep concurrency low (one warm task at a time is
  plenty; a daily batch of even hundreds of filings is minutes of work) and
  back off on 403s.
- **Warm incrementally.** `warm_fact_store` is idempotent and skips filings
  already ingested, so a daily run only pays for what's new. The same applies
  to `index_filing_text` (per-accession) and `market_history`/`fred_series`
  (upserts).

Run it over streamable-HTTP (`EDGAR_MCP_TRANSPORT=streamable-http`) to share
one server — and one cache — across workflow workers.

## License

Apache-2.0. Data comes from SEC EDGAR (public domain), FASB/SEC taxonomies,
Yahoo Finance (via yfinance — check Yahoo's terms for your use), and FRED
(your own API key). This is an analysis tool, not investment advice.
