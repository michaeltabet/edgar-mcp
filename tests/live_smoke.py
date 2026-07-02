"""Live smoke test: call every tool function against real EDGAR."""

import json
import sys

from edgar import set_identity

from edgar_mcp import server as s
from edgar_mcp.util import IDENTITY

set_identity(IDENTITY)

AAPL_10K = "0000320193-25-000079"
failures = []


def check(name, fn, *a, **kw):
    try:
        out = fn(*a, **kw)
        data = json.loads(out.split("\n... [TRUNCATED")[0]) if out.startswith(("{", "[")) else None
        bad = isinstance(data, dict) and "error" in data and len(data) <= 2
        print(f"{'FAIL' if bad else 'ok  '} {name}: {out[:180].replace(chr(10),' ')}")
        if bad:
            failures.append(name)
        return data
    except Exception as e:
        print(f"FAIL {name}: {type(e).__name__}: {e}")
        failures.append(name)
        return None


check("find_company ticker", s.find_company, "AAPL")
check("find_company name", s.find_company, "coherent")
check("list_filings", s.list_filings, "AAPL", form="10-K", limit=3)
check("full_text_search", s.full_text_search, '"intention to spin off"', forms="8-K", limit=5)
check("filing_contents", s.filing_contents, AAPL_10K)
check("read_section list", s.read_section, AAPL_10K)
check("read_section 1A", s.read_section, AAPL_10K, item="1A", max_chars=500)
check("read_document", s.read_document, AAPL_10K, max_chars=500)
check("list_statements", s.list_statements, AAPL_10K)
check("financial_statements income", s.financial_statements, AAPL_10K, statement="income")
check("financial_statements balance no dims", s.financial_statements, AAPL_10K, statement="balance", include_dimensions=False)
exp = check(
    "explain_number concept",
    s.explain_number,
    AAPL_10K,
    concept="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
)
check("explain_number by value", s.explain_number, AAPL_10K, value=416161000000)
check("search_facts", s.search_facts, AAPL_10K, query="Greater China")
check("concept_timeseries", s.concept_timeseries, "AAPL", "us-gaap:PaymentsForRepurchaseOfCommonStock", limit=5)
check("insider_transactions", s.insider_transactions, "AAPL", limit=2)

if exp:
    print("\n--- explain_number depth check ---")
    print("definition present:", bool(exp.get("official_definition")))
    print("calc present:", bool(exp.get("calculation")))
    print("dims on a fact:", any(f.get("dimensions") for f in exp.get("facts", [])))

print("\nFAILURES:", failures or "none")
sys.exit(1 if failures else 0)
