"""Macro data via the FRED API (Federal Reserve Bank of St. Louis).

Bring-your-own-key: reads FRED_API_KEY from the environment. Nothing is
hardcoded and no key ships with the repo — a free key is issued instantly at
https://fredaccount.stlouisfed.org/apikeys . FRED covers US series (rates,
CPI, GDP, unemployment) plus a wide set of international series (many
countries' CPI, policy rates, FX), which is how the "US and Singapore or
wherever" macro overlay is served.

Observations can be persisted into the shared DuckDB store (`macro` table) so
they join against `facts` and `prices` in one SQL query.
"""

from __future__ import annotations

import os

import httpx

from . import store

BASE = "https://api.stlouisfed.org/fred"


class FredKeyMissing(RuntimeError):
    pass


def _key() -> str:
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise FredKeyMissing(
            "FRED_API_KEY is not set. Get a free key at "
            "https://fredaccount.stlouisfed.org/apikeys and export it as "
            "FRED_API_KEY (nothing is stored in the repo)."
        )
    return key


def _get(path: str, params: dict) -> dict:
    params = {**params, "api_key": _key(), "file_type": "json"}
    resp = httpx.get(f"{BASE}/{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def search(query: str, limit: int = 15) -> dict:
    """Find FRED series by text. Returns id, title, units, frequency, dates."""
    data = _get("series/search", {"search_text": query, "limit": limit,
                                  "order_by": "popularity",
                                  "sort_order": "desc"})
    out = [
        {
            "id": s.get("id"),
            "title": s.get("title"),
            "units": s.get("units"),
            "frequency": s.get("frequency"),
            "start": s.get("observation_start"),
            "end": s.get("observation_end"),
            "popularity": s.get("popularity"),
        }
        for s in data.get("seriess", [])
    ]
    return {"query": query, "count": len(out), "series": out}


def series(series_id: str, start: str | None = None, end: str | None = None,
           persist: bool = True) -> dict:
    """Observations for one FRED series (e.g. CPIAUCSL, DGS10, SGPRGDPR).
    start/end are YYYY-MM-DD. If persist, upsert into the DuckDB `macro` table."""
    import pandas as pd

    meta = _get("series", {"series_id": series_id}).get("seriess", [{}])
    meta = meta[0] if meta else {}
    title, units = meta.get("title"), meta.get("units")

    params = {"series_id": series_id}
    if start:
        params["observation_start"] = start
    if end:
        params["observation_end"] = end
    obs = _get("series/observations", params).get("observations", [])

    rows = []
    for o in obs:
        v = o.get("value")
        if v in (None, "", "."):  # FRED uses "." for missing
            continue
        try:
            rows.append({"date": o["date"], "value": float(v)})
        except (TypeError, ValueError):
            continue

    persisted = 0
    if persist and rows:
        persisted = store.upsert_macro(series_id, pd.DataFrame(rows),
                                       title=title, units=units)

    latest = rows[-1] if rows else None
    return {
        "series_id": series_id,
        "title": title,
        "units": units,
        "observations": len(rows),
        "persisted": persisted,
        "range": [rows[0]["date"], rows[-1]["date"]] if rows else None,
        "latest": latest,
    }
