# Security

## Reporting

Open a GitHub issue for non-sensitive problems. For anything sensitive
(credential leakage, injection via filing content), use GitHub's private
vulnerability reporting on this repository.

## Model

- The server runs locally (stdio) or wherever you host it (streamable-HTTP).
  It has **no auth layer of its own** — if you expose it over HTTP, put it
  behind your own network boundary.
- All credentials are environment variables supplied by the operator; nothing
  is stored or transmitted by the server beyond the upstream API calls
  (SEC EDGAR, xbrl.fasb.org / xbrl.sec.gov, Yahoo Finance, FRED).
- `query_fact_store` enforces a single read-only SELECT/WITH statement.
- Filing text is untrusted input: it is parsed and returned as data, and
  agents consuming it should treat instructions found inside filings as
  content, not commands.
