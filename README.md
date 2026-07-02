# edgar-mcp

Deep, structured MCP access to SEC EDGAR. The design goal: an AI using this
server can take **any number in any filing** and see exactly what it is — the
XBRL concept behind it, the official FASB/SEC definition of that concept, the
period and segment/geography context it belongs to, what it arithmetically
sums into and what sums to produce it, its rounding precision, and any
footnotes attached. Not filing text blobs — parsed, labeled, linked data.

Built on [edgartools](https://github.com/dgunning/edgartools) (MIT) for XBRL
parsing, plus direct SEC APIs (EFTS full-text search, `companyconcept` fact
history) and the FASB/SEC taxonomy documentation labels (downloaded once,
cached in SQLite at `~/.cache/edgar-mcp/`).

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

**Ownership**
| tool | what it does |
|---|---|
| `insider_transactions` | Forms 3/4/5 parsed from XML: who, role, code, shares, price, owned-after |
| `fund_holdings` | a 13F manager's latest portfolio: issuer, CUSIP, ticker, value, shares, put/call, voting, % of portfolio |

## Run

```sh
uv run edgar-mcp                      # stdio MCP server
uv run python tests/live_smoke.py     # live end-to-end test against EDGAR
```

Registered in Claude Code (user scope) as `edgar`:

```sh
claude mcp add edgar --scope user -- uv run --directory /Users/michaeltabet/edgar-mcp edgar-mcp
```

Identity for SEC rate-limit compliance comes from `EDGAR_IDENTITY` (defaults
to Michael's email). Direct SEC calls are throttled under 10 req/s; parsed
XBRL objects are LRU-cached per accession.

## Example workflow (spin-off hunting)

1. `full_text_search('"intention to spin off"', forms="8-K")` → candidates
2. `list_filings(cik, form="10-12B")` → the Form 10
3. `filing_contents` / `read_section` → Information Statement sections
4. `financial_statements(accession, "income")` → carve-out financials, every number tagged
5. `explain_number(accession, concept=...)` → what each number actually is
6. `concept_timeseries` → history once the spinco trades on its own
