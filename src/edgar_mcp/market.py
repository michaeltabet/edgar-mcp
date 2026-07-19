"""Market data via Yahoo Finance (yfinance).

SEC filings carry fundamentals but no prices. This module adds live/historical
prices, market cap, and share counts for GLOBAL tickers — US plus Singapore
(SGX, e.g. `D05.SI`), London (`.L`), Hong Kong (`.HK`), etc. — so the
operating fundamentals from EDGAR can be turned into real valuation multiples
(P/E, EV/EBITDA). No API key required.

Prices can be persisted into the shared DuckDB store (`prices` table) so they
join against `facts` in one SQL query.
"""

from __future__ import annotations

from . import store


def _ticker(symbol: str):
    import yfinance as yf

    return yf.Ticker(symbol.strip())


def quote(symbol: str) -> dict:
    """Snapshot for one global ticker: price, market cap, shares, currency,
    exchange, and headline valuation ratios where Yahoo provides them."""
    t = _ticker(symbol)
    info = {}
    try:
        info = dict(t.get_info())
    except Exception:  # noqa: BLE001 — fall back to fast_info
        pass
    fast = {}
    try:
        fast = dict(t.fast_info)
    except Exception:  # noqa: BLE001
        pass

    def pick(*keys):
        for k in keys:
            v = info.get(k) if info else None
            if v is None and fast:
                v = fast.get(k)
            if v is not None:
                return v
        return None

    return {
        "symbol": symbol.upper(),
        "name": pick("longName", "shortName"),
        "exchange": pick("exchange", "fullExchangeName"),
        "currency": pick("currency"),
        "price": pick("currentPrice", "lastPrice", "regularMarketPrice"),
        "market_cap": pick("marketCap", "market_cap"),
        "shares_outstanding": pick("sharesOutstanding", "shares"),
        "enterprise_value": pick("enterpriseValue"),
        "trailing_pe": pick("trailingPE"),
        "forward_pe": pick("forwardPE"),
        "ev_to_ebitda": pick("enterpriseToEbitda"),
        "price_to_book": pick("priceToBook"),
        "dividend_yield": pick("dividendYield"),
        "fifty_two_week_high": pick("fiftyTwoWeekHigh"),
        "fifty_two_week_low": pick("fiftyTwoWeekLow"),
    }


def history(symbol: str, period: str = "5y", interval: str = "1d",
            persist: bool = True) -> dict:
    """OHLCV history for a ticker. period: 1mo/6mo/1y/5y/10y/max.
    interval: 1d/1wk/1mo. If persist, upsert into the DuckDB `prices` table."""
    t = _ticker(symbol)
    df = t.history(period=period, interval=interval, auto_adjust=False)
    if df is None or len(df) == 0:
        return {"symbol": symbol.upper(), "rows": 0, "persisted": 0,
                "note": "no data returned (check the ticker / suffix)"}
    df = df.reset_index()
    # Normalize column names to the store schema.
    rename = {
        "Date": "date", "Datetime": "date", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Adj Close": "adj_close",
        "Volume": "volume",
    }
    df = df.rename(columns=rename)
    if "adj_close" not in df.columns:
        df["adj_close"] = df.get("close")
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    currency = None
    try:
        currency = dict(t.fast_info).get("currency")
    except Exception:  # noqa: BLE001
        pass
    df["currency"] = currency

    persisted = 0
    if persist:
        persisted = store.upsert_prices(symbol.upper(), df)

    last = df.iloc[-1]
    return {
        "symbol": symbol.upper(),
        "currency": currency,
        "rows": int(len(df)),
        "persisted": persisted,
        "range": [str(df.iloc[0]["date"]), str(last["date"])],
        "latest_close": None if last.get("close") is None else float(last["close"]),
    }
