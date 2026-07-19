"""Full-financials dossier for a single filer.

The capstone tool. It stitches the pieces the rest of the server produces —
the DuckDB fact store (multi-year XBRL), market data (multiples), and macro —
into one structured, provenance-tagged financial profile:

  * a multi-year three-statement spine (income statement, balance sheet,
    cash flow) pulled from the fact store, each line resolved through a
    priority list of XBRL concept aliases (tags drift across years/filers);
  * a full ratio suite (profitability, DuPont ROE decomposition, returns on
    capital, liquidity, leverage & coverage, cash quality / accruals, per
    share and growth);
  * live market multiples (P/E, EV/EBITDA, P/B) when a ticker resolves.

Every reported line carries the concept it came from. Ratios show their
inputs. Nothing is invented; a metric a filer never tagged comes back null.
"""

from __future__ import annotations

from . import market, store

# --- canonical line -> priority list of us-gaap concepts ------------------- #
# First alias present for a given year wins. Order = most-specific first.
INCOME = {
    "revenue": ["us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                "us-gaap:Revenues", "us-gaap:SalesRevenueNet"],
    "cost_of_revenue": ["us-gaap:CostOfGoodsAndServicesSold",
                        "us-gaap:CostOfRevenue", "us-gaap:CostOfGoodsSold"],
    "gross_profit": ["us-gaap:GrossProfit"],
    "rd_expense": ["us-gaap:ResearchAndDevelopmentExpense"],
    "sga_expense": ["us-gaap:SellingGeneralAndAdministrativeExpense",
                    "us-gaap:GeneralAndAdministrativeExpense"],
    "operating_income": ["us-gaap:OperatingIncomeLoss"],
    "interest_expense": ["us-gaap:InterestExpense",
                        "us-gaap:InterestExpenseNonoperating"],
    "pretax_income": [
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "income_tax": ["us-gaap:IncomeTaxExpenseBenefit"],
    "net_income": ["us-gaap:NetIncomeLoss",
                  "us-gaap:ProfitLoss"],
    "eps_diluted": ["us-gaap:EarningsPerShareDiluted"],
    "shares_diluted": ["us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"],
}
BALANCE = {
    "cash": ["us-gaap:CashAndCashEquivalentsAtCarryingValue"],
    "current_assets": ["us-gaap:AssetsCurrent"],
    "total_assets": ["us-gaap:Assets"],
    "current_liabilities": ["us-gaap:LiabilitiesCurrent"],
    "total_liabilities": ["us-gaap:Liabilities"],
    "long_term_debt": ["us-gaap:LongTermDebtNoncurrent",
                       "us-gaap:LongTermDebtAndCapitalLeaseObligations",
                       "us-gaap:LongTermDebt"],
    "short_term_debt": ["us-gaap:LongTermDebtCurrent",
                        "us-gaap:LongTermDebtAndCapitalLeaseObligationsCurrent",
                        "us-gaap:DebtCurrent"],
    "inventory": ["us-gaap:InventoryNet"],
    "receivables": ["us-gaap:AccountsReceivableNetCurrent"],
    "equity": ["us-gaap:StockholdersEquity",
              "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}
CASHFLOW = {
    "cfo": ["us-gaap:NetCashProvidedByUsedInOperatingActivities",
            "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
              "us-gaap:PaymentsToAcquireProductiveAssets"],
    "dividends_paid": ["us-gaap:PaymentsOfDividendsCommonStock",
                      "us-gaap:PaymentsOfDividends"],
    "buybacks": ["us-gaap:PaymentsForRepurchaseOfCommonStock"],
    "depreciation_amortization": [
        "us-gaap:DepreciationDepletionAndAmortization",
        "us-gaap:DepreciationAmortizationAndAccretionNet"],
}

_ALL_CONCEPTS = sorted({c for m in (INCOME, BALANCE, CASHFLOW)
                        for lst in m.values() for c in lst})


def _fact_matrix(company: str) -> dict:
    """{concept: {year: value}}, keyed on the fact's actual period end (not the
    edgartools `fiscal_year` column, which is populated inconsistently for a
    filing's comparative-year facts). Flows are restricted to annual-length
    durations; the latest filing wins when several report the same year (so you
    get the most recent, restated figure)."""
    placeholders = ", ".join(f"'{c}'" for c in _ALL_CONCEPTS)
    sql = f"""
        WITH base AS (
          SELECT concept, numeric_value AS val, filed_date, period_type,
            CASE WHEN period_type='instant' THEN year(period_instant)
                 ELSE year(period_end) END AS yr,
            CASE WHEN period_type='instant' THEN 0
                 ELSE date_diff('day', period_start, period_end) END AS span
          FROM facts
          WHERE company = ?
            AND concept IN ({placeholders})
            AND NOT is_dimensioned
            AND numeric_value IS NOT NULL
        )
        SELECT yr AS fiscal_year, concept, val
        FROM base
        WHERE yr IS NOT NULL
          AND (period_type = 'instant' OR span BETWEEN 300 AND 380)
        QUALIFY row_number() OVER (
            PARTITION BY yr, concept
            ORDER BY filed_date DESC, abs(val) DESC
        ) = 1
    """
    with store._lock:
        conn = store._connect()
        try:
            rows = conn.execute(sql, [company]).fetchall()
        finally:
            conn.close()
    matrix: dict[str, dict[int, float]] = {}
    for fy, concept, val in rows:
        matrix.setdefault(concept, {})[int(fy)] = val
    return matrix


def _resolve(matrix: dict, aliases: list[str], year: int):
    """First alias with a value for `year`, plus which concept supplied it."""
    for concept in aliases:
        v = matrix.get(concept, {}).get(year)
        if v is not None:
            return v, concept
    return None, None


def _div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def _line_block(matrix, spec, years):
    """{line: {year: value}} + {line: concept_used} provenance for a statement."""
    values, provenance = {}, {}
    for line, aliases in spec.items():
        values[line] = {}
        used = None
        for y in years:
            v, c = _resolve(matrix, aliases, y)
            values[line][y] = v
            if c:
                used = c
        provenance[line] = used
    return values, provenance


def _ratios(inc, bal, cf, years):
    out = {}
    for y in years:
        rev = inc["revenue"][y]
        gp = inc["gross_profit"][y]
        opinc = inc["operating_income"][y]
        ni = inc["net_income"][y]
        pretax = inc["pretax_income"][y]
        tax = inc["income_tax"][y]
        ta = bal["total_assets"][y]
        eq = bal["equity"][y]
        ca = bal["current_assets"][y]
        cl = bal["current_liabilities"][y]
        inv = bal["inventory"][y]
        ltd = bal["long_term_debt"][y] or 0
        std = bal["short_term_debt"][y] or 0
        cash = bal["cash"][y]
        cfo = cf["cfo"][y]
        capex = cf["capex"][y]
        da = cf["depreciation_amortization"][y]
        intexp = inc["interest_expense"][y]
        total_debt = (ltd or 0) + (std or 0)
        ebitda = None if opinc is None else opinc + (da or 0)
        fcf = None if cfo is None else cfo - (capex or 0)
        net_margin = _div(ni, rev)
        asset_turn = _div(rev, ta)
        leverage = _div(ta, eq)
        out[y] = {
            "gross_margin": _div(gp, rev),
            "operating_margin": _div(opinc, rev),
            "net_margin": net_margin,
            "effective_tax_rate": _div(tax, pretax),
            "roe": _div(ni, eq),
            "roa": _div(ni, ta),
            "dupont": {"net_margin": net_margin, "asset_turnover": asset_turn,
                       "equity_multiplier": leverage,
                       "roe_check": None if None in (net_margin, asset_turn, leverage)
                       else net_margin * asset_turn * leverage},
            "current_ratio": _div(ca, cl),
            "quick_ratio": _div(None if ca is None else ca - (inv or 0), cl),
            "debt_to_equity": _div(total_debt, eq),
            "net_debt": None if not total_debt else total_debt - (cash or 0),
            "net_debt_to_ebitda": _div(
                None if not total_debt else total_debt - (cash or 0), ebitda),
            "interest_coverage": _div(opinc, intexp),
            "ebitda": ebitda,
            "fcf": fcf,
            "fcf_margin": _div(fcf, rev),
            "cash_conversion_cfo_ni": _div(cfo, ni),
            "accruals_ratio": _div(None if (ni is None or cfo is None)
                                   else ni - cfo, ta),
        }
    return out


def _growth(inc, years):
    out = {}
    for line in ("revenue", "net_income", "eps_diluted"):
        series = inc[line]
        g = {}
        for i, y in enumerate(years):
            if i == 0:
                g[y] = None
                continue
            prev = series[years[i - 1]]
            cur = series[y]
            g[y] = None if prev in (None, 0) or cur is None else (cur / prev - 1)
        out[line] = g
    return out


def build(company: str, years: int = 5, ticker: str | None = None,
          warm: bool = True) -> dict:
    """Assemble the full dossier. If warm, ensure recent 10-Ks are in the store."""
    from edgar import Company

    c = Company(company.strip())
    name = getattr(c, "name", None) or company
    if warm:
        store.warm(company, forms=["10-K"], limit=max(years + 1, 5))

    matrix = _fact_matrix(name)
    available = sorted({y for cy in matrix.values() for y in cy})
    yrs = available[-years:] if available else []
    if not yrs:
        return {"company": name, "error": "no facts in store for this company "
                "(warm it first, or the name did not match the stored facts)",
                "hint": "check fact_store_status for the exact company string"}

    inc, inc_prov = _line_block(matrix, INCOME, yrs)
    bal, bal_prov = _line_block(matrix, BALANCE, yrs)
    cf, cf_prov = _line_block(matrix, CASHFLOW, yrs)

    dossier = {
        "company": name,
        "years": yrs,
        "income_statement": inc,
        "balance_sheet": bal,
        "cash_flow": cf,
        "ratios": _ratios(inc, bal, cf, yrs),
        "growth": _growth(inc, yrs),
        "provenance": {"income_statement": inc_prov, "balance_sheet": bal_prov,
                       "cash_flow": cf_prov,
                       "note": "each line shows the us-gaap concept resolved; "
                       "values traceable via query_fact_store on that concept"},
    }

    if ticker:
        try:
            q = market.quote(ticker)
            dossier["market"] = {
                k: q.get(k) for k in
                ("symbol", "currency", "price", "market_cap", "enterprise_value",
                 "trailing_pe", "forward_pe", "ev_to_ebitda", "price_to_book",
                 "dividend_yield")
            }
        except Exception as e:
            dossier["market"] = {"error": str(e)}
    return dossier
