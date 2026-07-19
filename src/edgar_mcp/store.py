"""Persistent, queryable XBRL fact store (DuckDB).

The rest of the server parses one filing's XBRL on demand and returns JSON
that is capped at ~40k chars. That ceiling makes genuine cross-filing,
cross-company analysis impossible — you cannot ask "every segment's gross
margin for this company over 8 years" in one shot.

This module lands every parsed fact into a local DuckDB file as a normalized
fact table so it can be queried with SQL across any number of filings. One
row per tagged fact, carrying its concept, value, period, unit, dimensions,
calc weight/parent-direction, precision, statement placement, and full
filing provenance (accession + cik + form + filed date). Warm it once per
company/form set, then run arbitrary read-only SQL over it.

Store lives at ~/.cache/edgar-mcp/facts.duckdb (alongside the taxonomy cache).
"""

from __future__ import annotations

import json
import pathlib
import threading

from .util import filing_for, norm_concept, xbrl_for

CACHE_DIR = pathlib.Path.home() / ".cache" / "edgar-mcp"
DB_PATH = CACHE_DIR / "facts.duckdb"

_lock = threading.Lock()

# Column order is the contract between the enriched dataframe and the table.
_FACT_COLUMNS = [
    "accession", "cik", "company", "form", "filed_date",
    "concept", "label",
    "value", "numeric_value",
    "balance", "preferred_sign", "weight",
    "period_type", "period_key", "period_start", "period_end", "period_instant",
    "fiscal_year", "fiscal_period",
    "is_dimensioned", "dimensions",
    "decimals", "unit_ref", "currency",
    "statement_type", "statement_name",
    "fact_id", "context_ref",
]

_DDL_FILINGS = """
CREATE TABLE IF NOT EXISTS filings(
    accession   VARCHAR PRIMARY KEY,
    cik         BIGINT,
    company     VARCHAR,
    form        VARCHAR,
    filed_date  DATE,
    fiscal_year INTEGER,
    fiscal_period VARCHAR,
    period_end  DATE,
    fact_count  INTEGER,
    ingested_at TIMESTAMP DEFAULT current_timestamp
)
"""

_DDL_FACTS = """
CREATE TABLE IF NOT EXISTS facts(
    accession   VARCHAR,
    cik         BIGINT,
    company     VARCHAR,
    form        VARCHAR,
    filed_date  DATE,
    concept     VARCHAR,
    label       VARCHAR,
    value       VARCHAR,
    numeric_value DOUBLE,
    balance     VARCHAR,
    preferred_sign DOUBLE,
    weight      DOUBLE,
    period_type VARCHAR,
    period_key  VARCHAR,
    period_start DATE,
    period_end  DATE,
    period_instant DATE,
    fiscal_year INTEGER,
    fiscal_period VARCHAR,
    is_dimensioned BOOLEAN,
    dimensions  JSON,
    decimals    INTEGER,
    unit_ref    VARCHAR,
    currency    VARCHAR,
    statement_type VARCHAR,
    statement_name VARCHAR,
    fact_id     VARCHAR,
    context_ref VARCHAR
)
"""


_DDL_PRICES = """
CREATE TABLE IF NOT EXISTS prices(
    ticker    VARCHAR,
    date      DATE,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    adj_close DOUBLE,
    volume    BIGINT,
    currency  VARCHAR,
    source    VARCHAR DEFAULT 'yfinance',
    PRIMARY KEY (ticker, date)
)
"""

_DDL_MACRO = """
CREATE TABLE IF NOT EXISTS macro(
    series_id VARCHAR,
    date      DATE,
    value     DOUBLE,
    title     VARCHAR,
    units     VARCHAR,
    source    VARCHAR DEFAULT 'FRED',
    PRIMARY KEY (series_id, date)
)
"""


def _connect():
    import duckdb

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(DB_PATH))
    conn.execute(_DDL_FILINGS)
    conn.execute(_DDL_FACTS)
    conn.execute(_DDL_PRICES)
    conn.execute(_DDL_MACRO)
    return conn


def _dimensions_for(x, context_ref):
    """{axis: member} for a context, prefixes stripped, JSON-encoded (or None)."""
    ctx = x.contexts.get(context_ref) if context_ref else None
    dims = getattr(ctx, "dimensions", None) if ctx is not None else None
    if not dims:
        return None
    return json.dumps(
        {a.split(":")[-1]: str(m).split(":")[-1] for a, m in dims.items()},
        ensure_ascii=False,
    )


def _enriched_frame(accession: str):
    """The filing's fact dataframe, enriched with filing metadata + dimensions,
    shaped to exactly _FACT_COLUMNS. Returns (df, filing_meta)."""
    import pandas as pd

    f = filing_for(accession)
    x = xbrl_for(accession)
    df = x.facts.query().to_dataframe().copy()

    try:
        cik = int(getattr(f, "cik", None) or 0) or None
    except (TypeError, ValueError):
        cik = None
    meta = {
        "accession": accession,
        "cik": cik,
        "company": getattr(f, "company", None),
        "form": getattr(f, "form", None),
        "filed_date": str(getattr(f, "filing_date", "") or "") or None,
    }
    df["accession"] = meta["accession"]
    df["cik"] = meta["cik"]
    df["company"] = meta["company"]
    df["form"] = meta["form"]
    df["filed_date"] = meta["filed_date"]
    df["dimensions"] = df["context_ref"].map(lambda c: _dimensions_for(x, c))

    # `decimals` is 'INF'/'-INF' for infinite-precision facts (share counts,
    # per-share) — parseable as float inf, not castable to INT. Coerce to a
    # nullable integer, mapping infinities to NULL.
    import numpy as np

    dec = pd.to_numeric(df["decimals"], errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    df["decimals"] = dec.astype("Int64")
    df["numeric_value"] = pd.to_numeric(
        df.get("numeric_value"), errors="coerce"
    ).replace([np.inf, -np.inf], np.nan)

    for col in _FACT_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[_FACT_COLUMNS]
    # DuckDB is strict about NaN -> typed columns; make them real NULLs.
    df = df.astype(object).where(pd.notnull(df), None)
    return df, meta


def ingest(accession: str, force: bool = False) -> dict:
    """Parse one filing's XBRL and upsert its facts into the store."""
    accession = accession.strip()
    with _lock:
        conn = _connect()
        try:
            exists = conn.execute(
                "SELECT fact_count FROM filings WHERE accession=?", [accession]
            ).fetchone()
            if exists and not force:
                return {"accession": accession, "status": "already_ingested",
                        "fact_count": exists[0]}

            df, meta = _enriched_frame(accession)
            conn.execute("DELETE FROM facts WHERE accession=?", [accession])
            conn.execute("DELETE FROM filings WHERE accession=?", [accession])
            conn.register("df_facts", df)
            conn.execute(
                f"INSERT INTO facts SELECT {', '.join(_FACT_COLUMNS)} FROM df_facts"
            )
            conn.unregister("df_facts")

            period_end = conn.execute(
                "SELECT max(period_end) FROM facts WHERE accession=?", [accession]
            ).fetchone()[0]
            fy, fp = conn.execute(
                "SELECT max(fiscal_year), max(fiscal_period) FROM facts "
                "WHERE accession=? AND fiscal_year IS NOT NULL", [accession]
            ).fetchone()
            conn.execute(
                "INSERT INTO filings(accession,cik,company,form,filed_date,"
                "fiscal_year,fiscal_period,period_end,fact_count) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [meta["accession"], meta["cik"], meta["company"], meta["form"],
                 meta["filed_date"], fy, fp, period_end, len(df)],
            )
            return {"accession": accession, "company": meta["company"],
                    "form": meta["form"], "status": "ingested" if not exists
                    else "reingested", "fact_count": len(df)}
        finally:
            conn.close()


def warm(company: str, forms: list[str] | None = None, limit: int = 4,
         force: bool = False) -> dict:
    """Ingest a company's most recent `limit` filings of the given form(s)."""
    from edgar import Company

    forms = forms or ["10-K"]
    c = Company(company.strip())
    ingested, skipped, errors = [], [], []
    for form in forms:
        try:
            filings = c.get_filings(form=form).head(limit)
        except Exception as e:  # noqa: BLE001
            errors.append({"form": form, "error": str(e)})
            continue
        for f in filings:
            acc = getattr(f, "accession_no", None) or getattr(f, "accession", None)
            if not acc:
                continue
            try:
                res = ingest(acc, force=force)
                (skipped if res["status"] == "already_ingested" else ingested).append(
                    {"accession": acc, "form": form, "facts": res.get("fact_count")}
                )
            except Exception as e:  # noqa: BLE001
                errors.append({"accession": acc, "form": form, "error": str(e)})
    return {"company": company, "ingested": ingested, "skipped": skipped,
            "errors": errors}


def upsert_prices(ticker: str, df) -> int:
    """Upsert an OHLCV dataframe (index=date; cols open/high/low/close/
    adj_close/volume/currency) into the prices table. Returns rows written."""
    import pandas as pd

    if df is None or len(df) == 0:
        return 0
    df = df.copy()
    df["ticker"] = ticker
    cols = ["ticker", "date", "open", "high", "low", "close", "adj_close",
            "volume", "currency"]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols].astype(object).where(pd.notnull(df[cols]), None)
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM prices WHERE ticker=?", [ticker])
            conn.register("df_prices", df)
            conn.execute(
                "INSERT INTO prices(ticker,date,open,high,low,close,adj_close,"
                "volume,currency) SELECT ticker,date,open,high,low,close,"
                "adj_close,volume,currency FROM df_prices"
            )
            conn.unregister("df_prices")
            return len(df)
        finally:
            conn.close()


def upsert_macro(series_id: str, df, title: str | None = None,
                 units: str | None = None) -> int:
    """Upsert a FRED observations dataframe (cols date/value) into macro."""
    import pandas as pd

    if df is None or len(df) == 0:
        return 0
    df = df.copy()
    df["series_id"] = series_id
    df["title"] = title
    df["units"] = units
    cols = ["series_id", "date", "value", "title", "units"]
    df = df[cols].astype(object).where(pd.notnull(df[cols]), None)
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM macro WHERE series_id=?", [series_id])
            conn.register("df_macro", df)
            conn.execute(
                "INSERT INTO macro(series_id,date,value,title,units) "
                "SELECT series_id,date,value,title,units FROM df_macro"
            )
            conn.unregister("df_macro")
            return len(df)
        finally:
            conn.close()


_FORBIDDEN = ("insert", "update", "delete", "drop", "create", "alter",
              "attach", "copy", "pragma", "install", "load", "export")


def query(sql: str, limit: int = 200) -> dict:
    """Run a READ-ONLY SQL SELECT against the fact store.

    Tables: `facts` (one row per tagged fact) and `filings` (one row per
    ingested filing). Only a single SELECT/WITH statement is allowed.
    """
    stripped = sql.strip().rstrip(";").strip()
    low = stripped.lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise ValueError("Only SELECT / WITH queries are allowed.")
    if ";" in stripped:
        raise ValueError("Only a single statement is allowed (no ';').")
    for kw in _FORBIDDEN:
        if f" {kw} " in f" {low} ":
            raise ValueError(f"Statement '{kw}' is not allowed (read-only store).")

    with _lock:
        conn = _connect()
        try:
            rel = conn.execute(stripped)
            cols = [d[0] for d in rel.description]
            rows = rel.fetchmany(limit + 1)
            truncated = len(rows) > limit
            records = [dict(zip(cols, r)) for r in rows[:limit]]
            return {"columns": cols, "row_count": len(records),
                    "truncated": truncated, "rows": records}
        finally:
            conn.close()


def status() -> dict:
    """What's currently in the store."""
    with _lock:
        conn = _connect()
        try:
            n_filings = conn.execute("SELECT count(*) FROM filings").fetchone()[0]
            n_facts = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
            per_company = conn.execute(
                "SELECT company, count(*) AS filings, sum(fact_count) AS facts "
                "FROM filings GROUP BY company ORDER BY facts DESC"
            ).fetchall()
            recent = conn.execute(
                "SELECT accession, company, form, filed_date, fact_count "
                "FROM filings ORDER BY filed_date DESC LIMIT 15"
            ).fetchall()
            return {
                "db_path": str(DB_PATH),
                "filings": n_filings,
                "facts": n_facts,
                "by_company": [
                    {"company": c, "filings": fl, "facts": fa}
                    for c, fl, fa in per_company
                ],
                "recent": [
                    {"accession": a, "company": c, "form": fm,
                     "filed_date": str(fd), "facts": fc}
                    for a, c, fm, fd, fc in recent
                ],
            }
        finally:
            conn.close()
