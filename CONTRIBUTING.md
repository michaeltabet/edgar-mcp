# Contributing

Contributions welcome — especially new forensic checks, concept-alias
coverage for the dossier, and non-US data sources.

## Ground rules

- **Provenance is non-negotiable.** Any tool output that states a number must
  be traceable to an accession + XBRL concept (or an explicit external source
  like yfinance/FRED). No estimated or interpolated figures presented as filed
  data.
- **Human-judgment contract.** The forensic layer never silently adjusts:
  findings carry evidence and pre-quantified options; `apply_adjustments` only
  executes decisions the analyst made.
- **Read-only SQL.** `query_fact_store` must stay SELECT/WITH-only.
- **No bundled credentials.** All external keys are bring-your-own via
  environment variables (`EDGAR_IDENTITY`, `FRED_API_KEY`).

## Dev setup

```sh
uv sync
EDGAR_IDENTITY="Your Name you@example.com" uv run python tests/live_smoke.py
```

The smoke test runs live against SEC EDGAR — keep changes under the 10 req/s
throttle in `util.sec_get`.

## Style

Python 3.12, standard library + the pinned deps. Match the existing module
layout (one concern per module: `store`, `market`, `macro`, `vector`,
`dossier`, `forensic`, `quality`, `taxonomy`). Docstrings on every MCP tool —
they are the tool descriptions an agent sees, so write them for an AI reader:
what it does, when to use it, and its gotchas.
