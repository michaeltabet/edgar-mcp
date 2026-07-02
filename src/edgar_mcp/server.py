"""EDGAR MCP — deep, structured access to SEC filings.

Layered tool surface:
  discovery : find_company, list_filings, full_text_search
  filing    : filing_contents, read_section, read_document
  xbrl-deep : list_statements, financial_statements, explain_number,
              search_facts, concept_timeseries
  ownership : insider_transactions
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import taxonomy
from .util import (
    IDENTITY,
    company_for,
    concept_colon,
    decimals_meaning,
    df_records,
    filing_for,
    fmt_value,
    jdump,
    norm_concept,
    sec_get,
    xbrl_for,
)

mcp = FastMCP("edgar")


# --------------------------------------------------------------------------- #
# discovery layer
# --------------------------------------------------------------------------- #


@mcp.tool()
def find_company(query: str) -> str:
    """Resolve a ticker, CIK, or company name to its EDGAR identity.

    Returns CIK, name, ticker(s), exchange, SIC industry, fiscal year end,
    and state of incorporation. Start here: every other tool accepts the
    ticker or CIK this returns.
    """
    try:
        c = company_for(query)
        return jdump(
            {
                "cik": c.cik,
                "name": c.name,
                "tickers": getattr(c, "tickers", None),
                "exchanges": getattr(c, "exchanges", None),
                "sic": getattr(c, "sic", None),
                "sic_description": getattr(c, "sic_description", None),
                "fiscal_year_end": getattr(c, "fiscal_year_end", None),
                "state_of_incorporation": getattr(c, "state_of_incorporation", None),
                "website": getattr(c, "website", None),
            }
        )
    except Exception:
        pass
    # fall back to name search on SEC's ticker file
    data = sec_get("https://www.sec.gov/files/company_tickers.json").json()
    q = query.lower()
    hits = [
        {"cik": v["cik_str"], "ticker": v["ticker"], "name": v["title"]}
        for v in data.values()
        if q in v["title"].lower() or q == v["ticker"].lower()
    ][:15]
    if not hits:
        return jdump({"error": f"No company matched {query!r}"})
    return jdump({"matches": hits, "hint": "call find_company again with the CIK"})


@mcp.tool()
def list_filings(
    company: str,
    form: str | None = None,
    filed_after: str | None = None,
    filed_before: str | None = None,
    limit: int = 20,
) -> str:
    """List a company's filings, newest first.

    `company` is a ticker or CIK. `form` filters by type (e.g. "10-K",
    "8-K", "4", "SC TO-T", "10-12B"); dates are YYYY-MM-DD. Each result's
    accession_no is the handle every filing-level and XBRL tool takes.
    """
    c = company_for(company)
    filings = c.get_filings(form=form) if form else c.get_filings()
    if filed_after or filed_before:
        filings = filings.filter(
            date=f"{filed_after or ''}:{filed_before or ''}"
        )
    out = []
    for f in filings.head(limit):
        rec = {
            "accession_no": f.accession_no,
            "form": f.form,
            "filed": str(f.filing_date),
            "primary_document": getattr(f, "primary_document", None),
            "report_date": str(getattr(f, "report_date", "") or "") or None,
        }
        items = getattr(f, "items", None)
        if items:
            rec["items"] = items
        out.append(rec)
    return jdump({"company": c.name, "cik": c.cik, "count": len(out), "filings": out})


@mcp.tool()
def full_text_search(
    query: str,
    forms: str | None = None,
    filed_after: str | None = None,
    filed_before: str | None = None,
    limit: int = 20,
) -> str:
    """Full-text search across ALL EDGAR filings since 2001 (efts.sec.gov).

    Finds filings by their content, not their metadata — e.g.
    query='"intention to spin off"' with forms="8-K", or a subsidiary name,
    or an exact phrase in quotes. `forms` is comma-separated. Returns the
    filing + the accession_no to drill in with.
    """
    params: dict = {"q": query}
    if forms:
        params["forms"] = forms
    if filed_after:
        params["startdt"] = filed_after
    if filed_before:
        params["enddt"] = filed_before
    if filed_after or filed_before:
        params["dateRange"] = "custom"
    out: list[dict] = []
    total = None
    for page_from in range(0, min(limit, 100), 10):  # EFTS pages are fixed at 10
        params["from"] = page_from
        data = sec_get("https://efts.sec.gov/LATEST/search-index", params).json()
        hits = data.get("hits", {})
        total = hits.get("total", {}).get("value")
        page = hits.get("hits", [])
        for h in page:
            src = h.get("_source", {})
            accession, _, doc = h.get("_id", "").partition(":")
            out.append(
                {
                    "accession_no": accession,
                    "document": doc,
                    "form": src.get("file_type") or src.get("form"),
                    "filed": src.get("file_date"),
                    "companies": src.get("display_names"),
                    "ciks": src.get("ciks"),
                }
            )
        if len(page) < 10 or len(out) >= limit:
            break
    return jdump({"total_matches": total, "returned": len(out[:limit]), "hits": out[:limit]})


# --------------------------------------------------------------------------- #
# filing layer
# --------------------------------------------------------------------------- #


@mcp.tool()
def filing_contents(accession_no: str) -> str:
    """Inventory of every document and exhibit inside one filing.

    Returns sequence, filename, type (e.g. EX-99.1, EX-10.3, GRAPHIC) and
    description for each attachment, plus which sections read_section can
    extract. Use read_document with a filename to read any of them.
    """
    f = filing_for(accession_no)
    docs = []
    for a in f.attachments:
        docs.append(
            {
                "sequence": getattr(a, "sequence_number", None),
                "filename": getattr(a, "document", None),
                "type": getattr(a, "document_type", None),
                "description": getattr(a, "description", None),
            }
        )
    sections = None
    try:
        obj = f.obj()
        items = getattr(obj, "items", None)
        if items and not callable(items):
            sections = list(items)
    except Exception:
        pass
    return jdump(
        {
            "accession_no": f.accession_no,
            "form": f.form,
            "company": f.company,
            "filed": str(f.filing_date),
            "extractable_sections": sections,
            "documents": docs,
        }
    )


@mcp.tool()
def read_section(accession_no: str, item: str | None = None, max_chars: int = 30_000, offset: int = 0) -> str:
    """Extract one item/section of a 10-K, 10-Q, 8-K, or 20-F as clean text.

    `item` examples: "Item 1A" (risk factors), "Item 7" (MD&A) for a 10-K;
    "Item 2.02" for an 8-K. Call with no item to list what's available.
    Long sections page via offset.
    """
    f = filing_for(accession_no)
    obj = f.obj()
    items = getattr(obj, "items", None)
    if items is None or callable(items):
        return jdump(
            {
                "error": f"{f.form} has no item extraction; use filing_contents +"
                " read_document instead"
            }
        )
    available = list(items)
    if not item:
        return jdump({"form": f.form, "available_items": available})
    # tolerate "1A", "Item 1A", "item 1a"
    want = item.lower().replace("item", "").strip()
    match = next(
        (i for i in available if i.lower().replace("item", "").strip() == want), None
    )
    if match is None:
        return jdump({"error": f"{item!r} not found", "available_items": available})
    text = obj[match] or ""
    chunk = text[offset : offset + max_chars]
    return jdump(
        {
            "form": f.form,
            "item": match,
            "total_chars": len(text),
            "offset": offset,
            "next_offset": offset + max_chars if offset + max_chars < len(text) else None,
            "text": chunk,
        }
    )


@mcp.tool()
def read_document(
    accession_no: str, filename: str | None = None, max_chars: int = 30_000, offset: int = 0
) -> str:
    """Read any document/exhibit inside a filing as text (paged via offset).

    `filename` comes from filing_contents; omit it to read the primary
    document. This is the escape hatch for merger agreements, press
    releases (EX-99.1), credit agreements, proxy tables — anything.
    """
    f = filing_for(accession_no)
    att = None
    if filename:
        att = next(
            (a for a in f.attachments if getattr(a, "document", None) == filename), None
        )
        if att is None:
            return jdump(
                {
                    "error": f"{filename!r} not in filing",
                    "documents": [getattr(a, "document", None) for a in f.attachments],
                }
            )
        text = att.text() or ""
    else:
        text = f.text() or ""
    chunk = text[offset : offset + max_chars]
    return jdump(
        {
            "filename": filename or getattr(f, "primary_document", "primary"),
            "total_chars": len(text),
            "offset": offset,
            "next_offset": offset + max_chars if offset + max_chars < len(text) else None,
            "text": chunk,
        }
    )


# --------------------------------------------------------------------------- #
# xbrl-deep layer
# --------------------------------------------------------------------------- #

_STATEMENT_ALIASES = {
    "income": "income_statement",
    "income_statement": "income_statement",
    "balance": "balance_sheet",
    "balance_sheet": "balance_sheet",
    "cashflow": "cashflow_statement",
    "cash_flow": "cashflow_statement",
    "cashflow_statement": "cashflow_statement",
    "equity": "statement_of_equity",
    "comprehensive_income": "comprehensive_income",
    "cover": "cover_page",
}


@mcp.tool()
def list_statements(accession_no: str) -> str:
    """List every XBRL statement and disclosure in a filing.

    Includes not just the four core statements but every note/disclosure
    the company tagged (segment data, debt tables, lease maturities, ...).
    Pass a role_name to financial_statements to render any of them.
    """
    x = xbrl_for(accession_no)
    out = [
        {
            "role_name": s.get("role_name"),
            "definition": s.get("definition"),
            "type": s.get("type"),
            "category": s.get("menu_category"),
            "element_count": s.get("element_count"),
        }
        for s in x.get_all_statements()
    ]
    return jdump({"count": len(out), "statements": out})


def _statement_df(x, statement: str):
    key = _STATEMENT_ALIASES.get(statement.lower().strip())
    if key:
        st = getattr(x.statements, key)()
    else:
        st = x.get_statement(statement)
        if hasattr(st, "to_dataframe") is False:
            from edgar.xbrl.statements import Statement

            st = Statement(x, statement)
    return st.to_dataframe()


@mcp.tool()
def financial_statements(
    accession_no: str,
    statement: str = "income",
    include_dimensions: bool = True,
    max_rows: int = 150,
) -> str:
    """Render an XBRL statement with FULL structure — every number tagged.

    `statement`: income | balance | cashflow | equity | comprehensive_income
    | cover, or any role_name from list_statements (segment disclosures,
    debt tables, ...). Every row carries: the XBRL concept behind the
    number, its label, per-period values, hierarchy level, dimension
    axis/member (e.g. revenue split by product), balance direction, and
    calculation weight+parent (what it sums into). Feed any concept here to
    explain_number for the official definition, calc tree and footnotes.
    Set include_dimensions=false for just the headline line items.
    """
    x = xbrl_for(accession_no)
    df = _statement_df(x, statement)
    if not include_dimensions and "dimension" in df.columns:
        df = df[~df["dimension"].fillna(False)]
    period_cols = [c for c in df.columns if c[:2] == "20" or "(" in c]
    keep = [
        c
        for c in [
            "concept",
            "label",
            *period_cols,
            "level",
            "abstract",
            "dimension_label",
            "balance",
            "weight",
            "parent_concept",
        ]
        if c in df.columns
    ]
    rows = df_records(df[keep], limit=max_rows)
    return jdump(
        {
            "entity": x.entity_name,
            "period_of_report": str(x.period_of_report),
            "statement": statement,
            "total_rows": len(df),
            "rows": rows,
        }
    )


def _calc_relationships(x, element_id: str) -> dict:
    """Parents and children of a concept across the filing's calc trees."""
    rels = []
    for role, tree in x.calculation_trees.items():
        nodes = getattr(tree, "all_nodes", {}) or {}
        node = nodes.get(element_id)
        if node is None:
            continue
        children = [
            {
                "concept": ch,
                "weight": getattr(nodes.get(ch), "weight", None),
            }
            for ch in (node.children or [])
        ]
        rels.append(
            {
                "statement_role": role.rsplit("/", 1)[-1],
                "summed_into": node.parent,
                "own_weight_in_parent": node.weight,
                "sums_from_children": children or None,
            }
        )
    return {"calculation": rels or None}


@mcp.tool()
def explain_number(
    accession_no: str,
    concept: str | None = None,
    value: float | None = None,
    max_facts: int = 12,
) -> str:
    """THE deep-dive: take any number in a filing and expose everything
    XBRL knows about it.

    Identify the number by `concept` (e.g. "us-gaap:RevenueFromContract
    WithCustomerExcludingAssessedTax", from financial_statements or
    search_facts) or by its raw `value` (e.g. 416161000000 — reverse
    lookup). Returns:
      - the concept's official FASB/SEC taxonomy definition (what this
        number MEANS under GAAP), all its labels, balance direction,
        period type
      - every fact reported against it: value, exact period, unit,
        rounding precision, and dimensional context (which segment /
        product / geography the number belongs to)
      - calculation linkage: what this number sums into and which tagged
        numbers sum to produce it, with weights (+1/-1)
      - XBRL footnotes attached to the fact, if any
    """
    if concept is None and value is None:
        return jdump({"error": "give either concept or value"})
    x = xbrl_for(accession_no)
    facts_df = x.facts.query().to_dataframe()

    if concept is None:
        matches = facts_df[
            facts_df["numeric_value"].notna()
            & (abs(facts_df["numeric_value"] - value) < max(abs(value) * 1e-9, 0.001))
        ]
        if matches.empty:
            return jdump(
                {
                    "error": f"no fact with value {value} in this filing",
                    "hint": "values are as-tagged: unscaled (416161000000, not 416,161)",
                }
            )
        concepts = sorted(matches["concept"].unique())
        if len(concepts) > 1:
            return jdump(
                {
                    "value_matches_multiple_concepts": concepts,
                    "hint": "call again with the concept you mean",
                }
            )
        concept = concepts[0]

    c_colon = concept_colon(concept)
    c_under = norm_concept(concept)
    rows = facts_df[facts_df["concept"].isin([c_colon, c_under])]
    if rows.empty:
        close = facts_df[
            facts_df["concept"].str.contains(c_under.split("_")[-1], case=False, na=False)
        ]["concept"].unique()[:10]
        return jdump({"error": f"{concept!r} not in filing", "similar_concepts": list(close)})

    el = x.element_catalog.get(c_under)
    contexts = x.contexts

    facts_out = []
    for _, r in rows.head(max_facts).iterrows():
        ctx = contexts.get(r["context_ref"])
        dims = {}
        if ctx is not None:
            for axis, member in (getattr(ctx, "dimensions", None) or {}).items():
                mem_el = x.element_catalog.get(norm_concept(member))
                mem_label = None
                if mem_el is not None:
                    mem_label = next(iter(mem_el.labels.values()), None)
                dims[axis] = {"member": member, "member_label": mem_label}
        footnotes = None
        try:
            fns = x.get_footnotes_for_fact(r["fact_id"])
            if fns:
                footnotes = [getattr(fn, "text", str(fn)) for fn in fns]
        except Exception:
            pass
        facts_out.append(
            {
                "value": r["value"],
                "value_formatted": fmt_value(r["numeric_value"]),
                "period": {"start": r["period_start"], "end": r["period_end"]},
                "unit": r["currency"] or r["unit_ref"],
                "precision": decimals_meaning(r["decimals"]),
                "dimensions": dims or None,
                "statement": r["statement_name"],
                "footnotes": footnotes,
            }
        )

    out = {
        "concept": c_colon,
        "labels": el.labels if el is not None else None,
        "official_definition": taxonomy.definition(c_colon),
        "balance": (el.balance if el is not None else None)
        or (rows.iloc[0]["balance"] if "balance" in rows else None),
        "balance_meaning": None,
        "period_type": el.period_type if el is not None else None,
        "facts_in_filing": int(len(rows)),
        "facts": facts_out,
    }
    bal = out["balance"]
    if bal == "credit":
        out["balance_meaning"] = (
            "credit balance: revenues/liabilities/equity-like; a positive value"
            " increases income or the right side of the balance sheet"
        )
    elif bal == "debit":
        out["balance_meaning"] = (
            "debit balance: expenses/assets-like; positive value increases"
            " expenses or assets"
        )
    out.update(_calc_relationships(x, c_under))
    if out["official_definition"] is None and not c_under.startswith(
        ("us-gaap", "dei", "srt")
    ):
        out["official_definition"] = (
            "company-specific extension concept — no standard taxonomy"
            " definition; rely on the labels and calculation context"
        )
    return jdump(out)


@mcp.tool()
def search_facts(
    accession_no: str,
    query: str,
    dimensioned_only: bool = False,
    statement: str | None = None,
    limit: int = 40,
) -> str:
    """Search every tagged fact in a filing by label or concept name.

    e.g. query="segment", "lease", "share-based", "deferred tax",
    "Greater China". Matches concept names, labels, AND dimension members
    (so segment/geography names hit the facts sliced by them). Set
    dimensioned_only=true to see only dimensional breakdowns. Returns
    concept + label + value + period + statement for each hit — feed
    concepts to explain_number for the full story.
    """
    x = xbrl_for(accession_no)
    df = x.facts.query().to_dataframe()
    q_nospace = query.replace(" ", "")
    # elements (incl. axis members) whose name or any label matches
    matched_elements = {
        name
        for name, el in x.element_catalog.items()
        if q_nospace.lower() in name.lower()
        or any(query.lower() in (lbl or "").lower() for lbl in el.labels.values())
    }
    # contexts whose dimension members are matched elements
    matched_ctx = {
        cid
        for cid, ctx in x.contexts.items()
        if any(
            norm_concept(m) in matched_elements
            for m in (getattr(ctx, "dimensions", None) or {}).values()
        )
    }
    mask = (
        df["label"].str.contains(query, case=False, na=False)
        | df["concept"].str.contains(q_nospace, case=False, na=False)
        | df["concept"].apply(lambda c: norm_concept(str(c)) in matched_elements)
        | df["context_ref"].isin(matched_ctx)
    )
    df = df[mask]
    if dimensioned_only and "is_dimensioned" in df.columns:
        df = df[df["is_dimensioned"].fillna(False)]
    if statement:
        df = df[
            df["statement_type"].str.contains(statement, case=False, na=False)
            | df["statement_name"].str.contains(statement, case=False, na=False)
        ]
    keep = [
        c
        for c in [
            "concept",
            "label",
            "value",
            "period_start",
            "period_end",
            "currency",
            "is_dimensioned",
            "statement_name",
            "context_ref",
        ]
        if c in df.columns
    ]
    records = df_records(df[keep], limit=limit)
    for r in records:
        ctx = x.contexts.get(r.pop("context_ref", None))
        dims = getattr(ctx, "dimensions", None) if ctx is not None else None
        if dims:
            r["dimensions"] = {a.split(":")[-1]: m.split(":")[-1] for a, m in dims.items()}
    return jdump({"total_matches": int(len(df)), "facts": records})


@mcp.tool()
def concept_timeseries(
    company: str,
    concept: str,
    unit: str | None = None,
    annual_only: bool = True,
    limit: int = 60,
) -> str:
    """Full reported history of one XBRL concept for a company — every
    value the company ever filed for that tag (SEC companyconcept API).

    e.g. concept="us-gaap:Revenues" or "us-gaap:PaymentsForRepurchaseOf
    CommonStock". Each point carries the period, form, filing date and
    accession_no it came from (provenance). annual_only=false includes
    quarters. This is the time-series backbone: no re-parsing, straight
    from SEC's fact database.
    """
    c = company_for(company)
    tax, _, name = concept_colon(concept).partition(":")
    data = sec_get(
        f"https://data.sec.gov/api/xbrl/companyconcept/CIK{c.cik:010d}/{tax}/{name}.json"
    ).json()
    units = data.get("units", {})
    if unit is None:
        unit = next(iter(units), None)
    points = units.get(unit, [])
    seen: dict[tuple, dict] = {}
    for p in points:
        if annual_only and p.get("fp") != "FY":
            continue
        key = (p.get("start"), p.get("end"), p.get("frame"))
        prev = seen.get(key)
        if prev is None or (p.get("filed") or "") > (prev.get("filed") or ""):
            seen[key] = p
    series = [
        {
            "start": p.get("start"),
            "end": p.get("end"),
            "value": p.get("val"),
            "value_formatted": fmt_value(p.get("val")),
            "fiscal": f"{p.get('fp')} {p.get('fy')}",
            "form": p.get("form"),
            "filed": p.get("filed"),
            "accession_no": p.get("accn"),
            "frame": p.get("frame"),
        }
        for p in sorted(seen.values(), key=lambda q: q.get("end") or "")
    ][-limit:]
    return jdump(
        {
            "company": data.get("entityName"),
            "concept": f"{tax}:{name}",
            "official_definition": taxonomy.definition(f"{tax}:{name}"),
            "unit": unit,
            "available_units": list(units),
            "points": series,
        }
    )


# --------------------------------------------------------------------------- #
# ownership layer
# --------------------------------------------------------------------------- #


@mcp.tool()
def insider_transactions(company: str, limit: int = 10) -> str:
    """Parsed insider filings (Forms 3/4/5) for a company, newest first.

    For each: who (name, role), the reporting period, and every
    transaction row — buy/sell code, shares, price, value, shares owned
    after, direct/indirect — parsed from the raw XML, not scraped text.
    """
    c = company_for(company)
    filings = c.get_filings(form=[3, 4, 5]).head(limit)
    out = []
    for f in filings:
        rec = {"accession_no": f.accession_no, "form": f.form, "filed": str(f.filing_date)}
        try:
            o = f.obj()
            rec["insider"] = getattr(o, "insider_name", None)
            rec["position"] = str(getattr(o, "position", "") or "") or None
            rec["reporting_period"] = str(getattr(o, "reporting_period", "") or "") or None
            df = o.to_dataframe()
            rec["transactions"] = df_records(df, limit=25)
        except Exception as e:
            rec["parse_error"] = str(e)
        out.append(rec)
    return jdump({"company": c.name, "filings": out})


def main() -> None:
    os.environ.setdefault("EDGAR_IDENTITY", IDENTITY)
    from edgar import set_identity

    set_identity(IDENTITY)
    mcp.run()


if __name__ == "__main__":
    main()
