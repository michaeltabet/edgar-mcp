"""EDGAR MCP — deep, structured access to SEC filings.

Layered tool surface:
  discovery : find_company, list_filings, full_text_search
  filing    : filing_contents, read_section, read_document
  xbrl-deep : list_statements, financial_statements, explain_number,
              search_facts, concept_timeseries
  ownership : insider_transactions
"""

from __future__ import annotations

import datetime as dt
import os

from mcp.server.fastmcp import FastMCP

from . import dossier, forensic, macro, market, quality, store, taxonomy, vector
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
        sub = sec_get(f"https://data.sec.gov/submissions/CIK{c.cik:010d}.json").json()
        return jdump(
            {
                "cik": c.cik,
                "name": sub.get("name") or c.name,
                "tickers": sub.get("tickers"),
                "exchanges": sub.get("exchanges"),
                "sic": sub.get("sic"),
                "sic_description": sub.get("sicDescription"),
                "entity_type": sub.get("entityType"),
                "fiscal_year_end": sub.get("fiscalYearEnd"),
                "state_of_incorporation": sub.get("stateOfIncorporation"),
                "former_names": [n.get("name") for n in sub.get("formerNames", [])]
                or None,
                "website": sub.get("website") or None,
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
    # tolerate "1A", "Item 1A", "item 1a", and 10-Q's "Part I, Item 2"
    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    want = norm(item)
    match = next((i for i in available if norm(i) == want), None) or next(
        (i for i in available if norm(i).endswith(want)), None
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


def _calc_relationships(x, element_id: str, facts_df) -> dict:
    """Parents and children of a concept across the filing's calc trees,
    with each child's actual value for the primary period so the
    arithmetic is verifiable."""
    import pandas as pd

    undimmed = facts_df[~facts_df["is_dimensioned"].fillna(False)]

    def value_for(concept_under: str, period_key: str):
        rows = undimmed[
            (undimmed["concept"].apply(lambda c: norm_concept(str(c)) == concept_under))
            & (undimmed["period_key"] == period_key)
        ]
        if rows.empty:
            return None
        v = rows.iloc[0]["numeric_value"]
        return None if pd.isna(v) else v

    own_rows = undimmed[
        undimmed["concept"].apply(lambda c: norm_concept(str(c)) == element_id)
    ]
    period_key = (
        own_rows.sort_values("period_end").iloc[-1]["period_key"]
        if not own_rows.empty
        else None
    )

    rels = []
    for role, tree in x.calculation_trees.items():
        nodes = getattr(tree, "all_nodes", {}) or {}
        node = nodes.get(element_id)
        if node is None:
            continue
        children = []
        contribution = 0.0
        complete = bool(node.children) and period_key is not None
        for ch in node.children or []:
            w = getattr(nodes.get(ch), "weight", None)
            v = value_for(ch, period_key) if period_key else None
            if v is None or w is None:
                complete = False
            else:
                contribution += w * v
            children.append(
                {
                    "concept": ch,
                    "weight": w,
                    "value": v,
                    "value_formatted": fmt_value(v),
                }
            )
        rel = {
            "statement_role": role.rsplit("/", 1)[-1],
            "summed_into": node.parent,
            "own_weight_in_parent": node.weight,
            "sums_from_children": children or None,
        }
        own_val = value_for(element_id, period_key) if period_key else None
        if complete and own_val is not None:
            rel["arithmetic_check"] = {
                "period": period_key,
                "sum_of_children_x_weights": contribution,
                "reported_value": own_val,
                "ties_out": abs(contribution - own_val) < max(abs(own_val) * 1e-6, 1),
            }
        rels.append(rel)
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
    out.update(_calc_relationships(x, c_under, facts_df))
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


@mcp.tool()
def statement_history(
    company: str,
    statement: str = "income",
    n_filings: int = 5,
    form: str = "10-K",
    max_rows: int = 100,
) -> str:
    """One statement stitched across multiple filings — a long multi-year
    (or multi-quarter, form="10-Q") view in a single table.

    `statement`: income | balance | cashflow | equity | comprehensive_income.
    Stitching aligns concepts across filings even when labels shifted, so
    each row is one concept with a column per period. Heavier than
    financial_statements (parses n_filings XBRL documents) — keep
    n_filings modest.
    """
    from edgar.xbrl import XBRLS

    c = company_for(company)
    filings = c.get_filings(form=form).head(n_filings)
    xs = XBRLS.from_filings(filings)
    key = _STATEMENT_ALIASES.get(statement.lower().strip(), statement)
    st = getattr(xs.statements, key)()
    df = st.to_dataframe()
    period_cols = [col for col in df.columns if col[:2] == "20"]
    keep = [col for col in ["concept", "label", *period_cols] if col in df.columns]
    return jdump(
        {
            "company": c.name,
            "statement": statement,
            "filings_stitched": len(filings),
            "periods": period_cols,
            "rows": df_records(df[keep], limit=max_rows),
        }
    )


@mcp.tool()
def compare_peers(
    concept: str,
    period: str,
    unit: str = "USD",
    ciks: list[int] | None = None,
    limit: int = 25,
) -> str:
    """Compare ONE XBRL concept across ALL SEC filers for one period
    (SEC Frames API) — cross-sectional peer comparison.

    `period`: "CY2024" (annual), "CY2024Q1" (quarterly duration), or
    "CY2024Q1I" (instant, needed for balance-sheet concepts like
    us-gaap:Assets). `unit` e.g. USD, shares, USD-per-shares. Pass `ciks`
    to pull specific peers; otherwise returns the largest `limit` values
    economy-wide. Every point has CIK + accession provenance.
    """
    tax, _, name = concept_colon(concept).partition(":")
    data = sec_get(
        f"https://data.sec.gov/api/xbrl/frames/{tax}/{name}/{unit}/{period}.json"
    ).json()
    points = data.get("data", [])
    if ciks:
        want = set(ciks)
        points = [p for p in points if p.get("cik") in want]
    else:
        points = sorted(points, key=lambda p: abs(p.get("val") or 0), reverse=True)
    rows = [
        {
            "cik": p.get("cik"),
            "entity": p.get("entityName"),
            "value": p.get("val"),
            "value_formatted": fmt_value(p.get("val")),
            "location": p.get("loc"),
            "period_end": p.get("end"),
            "accession_no": p.get("accn"),
        }
        for p in points[:limit]
    ]
    return jdump(
        {
            "concept": f"{tax}:{name}",
            "official_definition": taxonomy.definition(f"{tax}:{name}"),
            "period": period,
            "unit": unit,
            "total_filers_in_frame": data.get("pts"),
            "companies": rows,
        }
    )


# --------------------------------------------------------------------------- #
# analyst layer
# --------------------------------------------------------------------------- #


@mcp.tool()
def analyst_flags(accession_no: str) -> str:
    """Quality-of-earnings scan: flag the adjustments an analyst would flag
    in this filing, with placement, sizing, recurrence and implications.

    Scans the tagged facts for stock-based comp, restructuring,
    impairments, one-time gains/losses, acquired-intangible amortization,
    capitalized costs and pension/non-operating items. For each flag:
    where it sits in the calculation tree (inside operating income? above
    the tax line and therefore inside EPS?), its size as % of revenue /
    operating income / pre-tax income, whether it RECURS across the
    filing's periods, and the analyst implication spelled out. Plus
    computed diagnostics the tags don't state: cash conversion (CFO vs
    net income), accruals, receivables-vs-revenue growth, effective tax
    rate swings, and EPS growth decomposed into earnings vs buybacks.
    """
    x = xbrl_for(accession_no)
    return jdump(quality.analyze(x), max_chars=60_000)


@mcp.tool()
def forensic_scan(accession_no: str, severity_min: str = "info") -> str:
    """The full CFA-style forensic checklist on one filing — every finding
    evidence-backed, every judgment call surfaced for the human to decide.

    Checks: classic add-back items (SBC, restructuring, impairments,
    intangible amortization, one-time gains/losses) with recurrence;
    pension (funded status, discount-rate & expected-return assumptions,
    non-service cost in earnings); operating-lease capitalization and
    lease-adjusted debt; equity-method/JV one-line consolidation;
    discontinued operations; tax forensics (valuation allowance changes,
    unrecognized tax benefits, ETR swings); working capital (receivables/
    inventory vs sales, days); capital structure (net debt, current
    portion, interest coverage, NCI); non-operating earnings reliance;
    capitalization policy (capex vs D&A, capitalized software); Beneish
    M-score; SBC dilution vs buyback offset.

    Every finding cites the exact tagged facts (verify any with
    explain_number). Findings needing a judgment carry an options block
    with each option's quantified effect — collect the analyst's choices
    and pass them to apply_adjustments. Nothing is adjusted silently.
    `severity_min`: info | caution | red_flag filters the output.
    """
    x = xbrl_for(accession_no)
    out = forensic.scan(x, accession_no)
    order = {"info": 2, "caution": 1, "red_flag": 0}
    cut = order.get(severity_min, 2)
    out["findings"] = [
        f for f in out["findings"] if order.get(f["severity"], 3) <= cut
    ]
    return jdump(out, max_chars=80_000)


@mcp.tool()
def apply_adjustments(accession_no: str, decisions: dict | None = None) -> str:
    """Build the adjusted-earnings bridge from the analyst's decisions on
    forensic_scan findings. Deterministic and reproducible: same filing +
    same decisions = same numbers, with the full ledger.

    `decisions` maps finding_id -> option_id (both from forensic_scan),
    e.g. {"restructuring:RestructuringCharges": "keep_as_expense",
    "one_time_gain_loss:GainsLossesOnExtinguishmentOfDebt": "strip",
    "tax:etr_swing": "avg_3y"}. Omitted findings use their stated default
    (defaults are conservative: SBC stays an expense, recurring
    restructuring stays a cost). Returns reported -> adjusted EBIT,
    pre-tax, net income and diluted EPS, with each decision's effect and
    evidence in the ledger.
    """
    x = xbrl_for(accession_no)
    return jdump(forensic.apply_decisions(x, accession_no, decisions or {}))


@mcp.tool()
def restatement_check(company: str, years: int = 3) -> str:
    """Restatement / auditor red-flag sweep of a company's filing history.

    Looks for: 8-K Item 4.01 (auditor change) and Item 4.02 (non-reliance
    on previously issued financials — the restatement bomb), amended
    annual/quarterly reports (10-K/A, 10-Q/A), and late-filing notices
    (NT 10-K / NT 10-Q). Any 4.02 is a major earnings-quality event.
    """
    c = company_for(company)
    cutoff = None
    try:
        cutoff = (dt.date.today() - dt.timedelta(days=365 * years)).isoformat()
    except Exception:
        pass
    hits: dict[str, list] = {"auditor_change_8k_401": [], "non_reliance_8k_402": [],
                             "amended_reports": [], "late_filings": []}
    for f in c.get_filings(form=["8-K", "10-K/A", "10-Q/A", "NT 10-K", "NT 10-Q"]).head(300):
        fd = str(f.filing_date)
        if cutoff and fd < cutoff:
            break
        rec = {"accession_no": f.accession_no, "form": f.form, "filed": fd}
        if f.form == "8-K":
            raw = getattr(f, "items", None) or ""
            items = raw if isinstance(raw, str) else ",".join(raw)
            if "4.01" in items:
                hits["auditor_change_8k_401"].append(rec | {"items": items})
            if "4.02" in items:
                hits["non_reliance_8k_402"].append(rec | {"items": items})
        elif f.form.endswith("/A"):
            hits["amended_reports"].append(rec)
        else:
            hits["late_filings"].append(rec)
    clean = not any(hits.values())
    return jdump(
        {
            "company": c.name,
            "window_years": years,
            "clean": clean,
            "read": (
                "no restatement/auditor red flags in the window"
                if clean
                else "review each hit — a 4.02 non-reliance means previously"
                " reported numbers were wrong; amendments can be routine"
                " (exhibits) or substantive (read the amendment's cover)"
            ),
            **hits,
        }
    )


@mcp.tool()
def compare_companies(
    company_a: str, company_b: str, form: str = "10-K"
) -> str:
    """Side-by-side quality-of-earnings comparison of two companies'
    latest filings (default 10-K), common-sized so they are comparable.

    Runs the full analyst_flags scan on both and lines up: margins and
    benchmarks as % of revenue, each adjustment category as % of revenue
    and % of pre-tax income, and the diagnostics (cash conversion, ETR,
    EPS decomposition, receivables trend). Use analyst_flags on each
    accession_no for the full per-item detail.
    """
    reports = {}
    for label, ident in [("a", company_a), ("b", company_b)]:
        c = company_for(ident)
        f = c.latest(form)
        if f is None:
            return jdump({"error": f"{c.name} has no {form} filings"})
        reports[label] = {
            "accession_no": f.accession_no,
            "report": quality.analyze(xbrl_for(f.accession_no)),
        }

    def common_size(rep: dict) -> dict:
        flags_by_cat: dict[str, dict] = {}
        for fl in rep["adjustment_flags"]:
            cat = flags_by_cat.setdefault(
                fl["category"],
                {"pct_of_revenue": 0.0, "pct_of_pretax_income": 0.0, "recurring": False},
            )
            cat["pct_of_revenue"] += fl["pct_of_revenue"] or 0
            cat["pct_of_pretax_income"] += fl["pct_of_pretax_income"] or 0
            cat["recurring"] = cat["recurring"] or fl["recurring"]
        for cat in flags_by_cat.values():
            cat["pct_of_revenue"] = round(cat["pct_of_revenue"], 1)
            cat["pct_of_pretax_income"] = round(cat["pct_of_pretax_income"], 1)
        return flags_by_cat

    out = {
        "companies": {
            lbl: {
                "entity": r["report"]["entity"],
                "accession_no": r["accession_no"],
                "period": r["report"]["period_of_report"],
                "benchmarks": r["report"]["benchmarks"],
                "adjustments_by_category_common_sized": common_size(r["report"]),
                "diagnostics": r["report"]["diagnostics"],
            }
            for lbl, r in reports.items()
        },
        "how_to_read": (
            "adjustments_by_category_common_sized: each category's current-"
            "period total as % of that company's own revenue and pre-tax"
            " income — compare across the two companies directly. A category"
            " marked recurring should generally NOT be treated as one-time."
            " Drill into any item with analyst_flags(accession_no) and"
            " explain_number."
        ),
    }
    return jdump(out, max_chars=60_000)


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


@mcp.tool()
def fund_holdings(manager: str, min_value: float = 0, limit: int = 50) -> str:
    """A 13F institutional manager's latest reported portfolio, parsed.

    `manager` is the manager's CIK or name (e.g. 1067983 for Berkshire).
    Returns each position: issuer, class, CUSIP, ticker, market value,
    shares, put/call flag, voting authority — sorted by value, largest
    first. `min_value` filters small positions (USD).
    """
    c = company_for(manager)
    f = c.get_filings(form="13F-HR").latest(1)
    if f is None:
        return jdump({"error": f"{c.name} has no 13F-HR filings"})
    o = f.obj()
    df = o.infotable
    df = df[df["Value"] >= min_value].sort_values("Value", ascending=False)
    total = float(df["Value"].sum())
    keep = [
        col
        for col in [
            "Issuer",
            "Class",
            "Ticker",
            "Cusip",
            "Value",
            "SharesPrnAmount",
            "Type",
            "PutCall",
            "SoleVoting",
            "SharedVoting",
        ]
        if col in df.columns
    ]
    rows = df_records(df[keep], limit=limit)
    for r in rows:
        r["pct_of_portfolio"] = round(100 * (r["Value"] or 0) / total, 2) if total else None
    return jdump(
        {
            "manager": c.name,
            "accession_no": f.accession_no,
            "period": str(getattr(f, "report_date", "") or "") or None,
            "filed": str(f.filing_date),
            "total_positions": int(len(df)),
            "total_value": total,
            "total_value_formatted": fmt_value(total),
            "holdings": rows,
        }
    )


# --------------------------------------------------------------------------- #
# persistent fact store — cross-filing SQL over parsed XBRL
# --------------------------------------------------------------------------- #


@mcp.tool()
def warm_fact_store(
    company: str,
    forms: list[str] | None = None,
    limit: int = 4,
    force: bool = False,
) -> str:
    """Parse a company's filings and land every tagged XBRL fact into the
    local DuckDB fact store, so you can then query them with SQL across
    filings (no per-filing re-parse, no 40k truncation wall).

    company: ticker / CIK / name. forms: e.g. ["10-K","10-Q"] (default 10-K).
    limit: most-recent N filings per form. force: re-ingest even if present.
    Run this first, then use `query_fact_store`. Idempotent.
    """
    return jdump(store.warm(company, forms=forms, limit=limit, force=force))


@mcp.tool()
def query_fact_store(sql: str, limit: int = 200) -> str:
    """Run a READ-ONLY SQL SELECT across every fact you've warmed into the store.

    Two tables:
      facts(accession, cik, company, form, filed_date, concept, label, value,
            numeric_value, balance, preferred_sign, weight, period_type,
            period_key, period_start, period_end, period_instant, fiscal_year,
            fiscal_period, is_dimensioned, dimensions (JSON {axis:member}),
            decimals, unit_ref, currency, statement_type, statement_name,
            fact_id, context_ref)
      filings(accession, cik, company, form, filed_date, fiscal_year,
              fiscal_period, period_end, fact_count, ingested_at)

    This is the differentiator: ask multi-year, multi-segment, multi-company
    questions in one query. Examples:
      - "SELECT fiscal_year, numeric_value FROM facts WHERE concept='us-gaap:Revenues'
         AND company='Apple Inc.' AND NOT is_dimensioned ORDER BY fiscal_year"
      - segment slices via json_extract(dimensions,'$.StatementBusinessSegmentsAxis')
    DuckDB SQL dialect; JSON via json_extract / dimensions->>'$.Axis'. Only a
    single SELECT/WITH statement; DDL/DML is rejected. Warm data first.
    """
    return jdump(store.query(sql, limit=limit))


@mcp.tool()
def fact_store_status() -> str:
    """What's currently warmed into the fact store: filing/fact counts,
    per-company totals, and the most recently ingested filings."""
    return jdump(store.status())


# --------------------------------------------------------------------------- #
# market data (Yahoo Finance) — global tickers, prices, valuation multiples
# --------------------------------------------------------------------------- #


@mcp.tool()
def market_quote(symbol: str) -> str:
    """Live snapshot for a GLOBAL ticker via Yahoo Finance: price, market cap,
    shares outstanding, enterprise value, and valuation ratios (trailing/forward
    P/E, EV/EBITDA, P/B, dividend yield). Works for US and international
    tickers with the right suffix — Singapore SGX `D05.SI`, London `.L`,
    Hong Kong `.HK`. This is what turns EDGAR fundamentals into real multiples
    (EDGAR itself has no prices). No API key needed.
    """
    return jdump(market.quote(symbol))


@mcp.tool()
def market_history(
    symbol: str, period: str = "5y", interval: str = "1d", persist: bool = True
) -> str:
    """OHLCV price history for a global ticker. period: 1mo/6mo/1y/5y/10y/max;
    interval: 1d/1wk/1mo. When persist=true (default) the series is upserted
    into the DuckDB `prices` table so you can SQL-join prices against `facts`.
    """
    return jdump(market.history(symbol, period=period, interval=interval,
                                persist=persist))


# --------------------------------------------------------------------------- #
# macro data (FRED) — bring-your-own-key (FRED_API_KEY)
# --------------------------------------------------------------------------- #


@mcp.tool()
def fred_search(query: str, limit: int = 15) -> str:
    """Search FRED for macro series by text (e.g. "10-year treasury",
    "Singapore CPI", "unemployment"). Returns series ids to feed fred_series.
    Requires FRED_API_KEY in the environment (free key, nothing stored in repo).
    """
    try:
        return jdump(macro.search(query, limit=limit))
    except macro.FredKeyMissing as e:
        return jdump({"error": str(e)})


@mcp.tool()
def fred_series(
    series_id: str,
    start: str | None = None,
    end: str | None = None,
    persist: bool = True,
) -> str:
    """Observations for one FRED macro series (e.g. CPIAUCSL, DGS10, UNRATE,
    and international series). start/end are YYYY-MM-DD. When persist=true the
    series is upserted into the DuckDB `macro` table for SQL-joining against
    `facts` and `prices`. Requires FRED_API_KEY (bring your own).
    """
    try:
        return jdump(macro.series(series_id, start=start, end=end,
                                  persist=persist))
    except macro.FredKeyMissing as e:
        return jdump({"error": str(e)})


# --------------------------------------------------------------------------- #
# CFA full-financials dossier — the capstone
# --------------------------------------------------------------------------- #


@mcp.tool()
def company_dossier(
    company: str, years: int = 5, ticker: str | None = None, warm: bool = True
) -> str:
    """The full CFA-grade financial profile of one filer in a single call.

    Warms the fact store (unless warm=false), then assembles a multi-year
    three-statement spine (income statement, balance sheet, cash flow) with
    each line resolved through a priority list of XBRL concept aliases (tags
    drift across years), a complete ratio suite (profitability, DuPont ROE
    decomposition, returns, liquidity, leverage & coverage, cash quality /
    accruals), YoY growth, and — if `ticker` is given — live market multiples
    (P/E, EV/EBITDA, P/B). Every reported line carries the us-gaap concept it
    came from; a metric the filer never tagged returns null (never invented).

    company: ticker/CIK/name for EDGAR. ticker: market symbol for multiples
    (US or global, e.g. "D05.SI"). This is the one-shot "everything" view.
    """
    return jdump(dossier.build(company, years=years, ticker=ticker, warm=warm))


# --------------------------------------------------------------------------- #
# semantic search over filing text (LanceDB + fastembed, offline)
# --------------------------------------------------------------------------- #


@mcp.tool()
def index_filing_text(accession_no: str) -> str:
    """Chunk + embed every section of a filing into the local vector index so
    it can be searched by meaning. Offline (small ONNX model, no API key).
    Idempotent per filing. Run before semantic_search_filings.
    """
    return jdump(vector.index_filing(accession_no))


@mcp.tool()
def semantic_search_filings(
    query: str, k: int = 8, company: str | None = None,
    accession_no: str | None = None
) -> str:
    """Semantic search across indexed filing text — find where filings discuss
    a concept even when the wording differs (supply concentration, going
    concern, a specific lawsuit, revenue-recognition policy). Optional company/
    accession filters. Each hit carries section + accession provenance; feed it
    to read_section / explain_number for the underlying data. Index first.
    """
    return jdump(vector.search(query, k=k, company=company,
                               accession=accession_no))


@mcp.tool()
def vector_store_status() -> str:
    """What filing text is currently in the vector index (chunks per filing)."""
    return jdump(vector.status())


def main() -> None:
    os.environ.setdefault("EDGAR_IDENTITY", IDENTITY)
    from edgar import set_identity

    set_identity(IDENTITY)
    transport = os.environ.get("EDGAR_MCP_TRANSPORT", "stdio")
    if transport in ("streamable-http", "http"):
        mcp.settings.host = os.environ.get("EDGAR_MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("EDGAR_MCP_PORT", "8000"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
