"""Shared helpers: identity, caching, JSON shaping, EDGAR HTTP."""

from __future__ import annotations

import functools
import json
import math
import os
import threading
import time

import httpx

# SEC requires a descriptive User-Agent ("Sample Company name admin@example.com").
# Set EDGAR_IDENTITY to your own name + email; the placeholder below is only a
# last resort so imports don't crash.
IDENTITY = os.environ.get("EDGAR_IDENTITY", "edgar-mcp user you@example.com")

_rate_lock = threading.Lock()
_last_request = 0.0


def throttle() -> None:
    """SEC allows 10 req/s; keep direct HTTP calls under that."""
    global _last_request
    with _rate_lock:
        wait = _last_request + 0.12 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def sec_get(url: str, params: dict | None = None) -> httpx.Response:
    throttle()
    resp = httpx.get(
        url,
        params=params,
        headers={"User-Agent": IDENTITY, "Accept-Encoding": "gzip, deflate"},
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp


@functools.lru_cache(maxsize=64)
def filing_for(accession: str):
    from edgar import get_by_accession_number

    return get_by_accession_number(accession.strip())


@functools.lru_cache(maxsize=8)
def xbrl_for(accession: str):
    x = filing_for(accession).xbrl()
    if x is None:
        raise ValueError(f"Filing {accession} has no XBRL data")
    return x


@functools.lru_cache(maxsize=32)
def company_for(ident: str):
    from edgar import Company

    return Company(ident.strip())


def clean(obj):
    """Make an object JSON-safe: NaN->None, numpy scalars->python, sets->lists."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean(v) for v in obj]
    if hasattr(obj, "item"):  # numpy scalar
        try:
            return clean(obj.item())
        except Exception:
            pass
    return str(obj)


def jdump(obj, max_chars: int = 40_000) -> str:
    s = json.dumps(clean(obj), ensure_ascii=False, indent=1, default=str)
    if len(s) <= max_chars:
        return s
    return (
        s[:max_chars]
        + f'\n... [TRUNCATED at {max_chars} of {len(s)} chars — narrow the query,'
        ' lower `limit`, or use offset-style params to page]'
    )


def norm_concept(concept: str) -> str:
    """'us-gaap:Revenues' / 'us-gaap_Revenues' -> underscore form used internally."""
    return concept.strip().replace(":", "_")


def concept_colon(concept: str) -> str:
    c = concept.strip()
    if ":" in c:
        return c
    return c.replace("_", ":", 1)


def decimals_meaning(decimals) -> str:
    try:
        d = int(decimals)
    except (TypeError, ValueError):
        return "exact as reported"
    names = {
        -9: "rounded to the nearest billion",
        -6: "rounded to the nearest million",
        -3: "rounded to the nearest thousand",
        0: "exact to the unit (e.g. whole dollars/shares)",
        2: "exact to 2 decimal places (e.g. cents / per-share)",
    }
    return names.get(d, f"rounded to 10^{-d}")


def fmt_value(v) -> str | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if math.isnan(f):
        return None
    if f == int(f) and abs(f) >= 1000:
        return f"{int(f):,}"
    return f"{f:,.4f}".rstrip("0").rstrip(".")


def df_records(df, limit: int | None = None) -> list[dict]:
    import pandas as pd

    if limit:
        df = df.head(limit)
    df = df.astype(object).where(pd.notnull(df), None)
    return [dict(r) for r in df.to_dict(orient="records")]
