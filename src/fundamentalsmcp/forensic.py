"""Forensic engine: the earnings-quality mega-checklist, evidence-first.

Design contract:
  * Every finding cites the exact facts it is built from (concept, period,
    value) — the filing accession is the reference for all of them.
  * Every computed metric shows its formula with the numbers substituted.
  * The engine NEVER silently adjusts. Where analyst judgment is required,
    the finding carries a `judgment` block: a question, options, and the
    quantified effect of each option on adjusted EBIT / pre-tax income.
    `apply_adjustments` then builds the bridge from the analyst's decisions.

Checks are registered in CHECKS; each takes a FactStore and yields finding
dicts. Coverage is stated honestly: a check that lacks its input tags
reports nothing (or an info-level "not measurable") rather than guessing.
"""

from __future__ import annotations

import datetime as dt

from .util import fmt_value, norm_concept

# ------------------------------------------------------------- fact store


class FactStore:
    def __init__(self, x, accession: str):
        self.x = x
        self.accession = accession
        df = x.facts.query().to_dataframe()
        self.all_df = df
        cons = df[~df["is_dimensioned"].fillna(False) & df["numeric_value"].notna()]
        self.map: dict[str, dict[str, float]] = {}
        for _, r in cons.iterrows():
            self.map.setdefault(norm_concept(str(r["concept"])), {})[
                r["period_key"]
            ] = r["numeric_value"]

        def days(t):
            try:
                return (
                    dt.date.fromisoformat(str(t[2]))
                    - dt.date.fromisoformat(str(t[1]))
                ).days
            except (ValueError, TypeError):
                return 0

        durations = sorted(
            {
                (r["period_key"], r["period_start"], r["period_end"])
                for _, r in cons[cons["period_type"] == "duration"].iterrows()
            },
            key=lambda t: (t[2], t[1]),
            reverse=True,
        )
        annual = [t for t in durations if days(t) > 300]
        self.periods = [t[0] for t in (annual or durations)]
        # keep only balance-sheet-dense instants: the cover page contributes
        # a near-empty instant (dei shares outstanding at filing date) that
        # would otherwise sort first and break every .inst() lookup
        counts: dict[str, int] = {}
        for _, r in cons[cons["period_type"] == "instant"].iterrows():
            counts[r["period_key"]] = counts.get(r["period_key"], 0) + 1
        dense = max(counts.values()) if counts else 0
        self.instants = sorted(
            (k for k, n in counts.items() if n >= max(4, dense * 0.05)),
            reverse=True,
        )

    # -- consolidated lookups (fallback chains) ------------------------------
    def get(self, candidates: list[str], period_idx: int = 0, periods=None):
        """(concept, value) for the first candidate with a value at period_idx."""
        plist = periods if periods is not None else self.periods
        if period_idx >= len(plist):
            return None, None
        for c in candidates:
            v = self.map.get(norm_concept(c), {}).get(plist[period_idx])
            if v is not None:
                return c, v
        return None, None

    def inst(self, candidates: list[str], idx: int = 0):
        return self.get(candidates, idx, periods=self.instants)

    # -- dimensioned lookup (e.g. pension by plan type) -----------------------
    def dim(self, candidates: list[str], member_substr: str | None = None):
        """First dimensioned fact matching a candidate concept (and member)."""
        cands = {norm_concept(c) for c in candidates}
        rows = self.all_df[
            self.all_df["is_dimensioned"].fillna(False)
            & self.all_df["numeric_value"].notna()
        ]
        best = None
        for _, r in rows.iterrows():
            if norm_concept(str(r["concept"])) not in cands:
                continue
            ctx = self.x.contexts.get(r["context_ref"])
            dims = getattr(ctx, "dimensions", None) or {}
            if member_substr and not any(
                member_substr.lower() in str(m).lower() for m in dims.values()
            ):
                continue
            key = (str(r["period_end"] or r["period_start"] or ""), r["numeric_value"])
            if best is None or key > best[0]:
                best = (key, r, dims)
        if best is None:
            return None
        _, r, dims = best
        return {
            "concept": str(r["concept"]),
            "value": r["numeric_value"],
            "period": r["period_end"] or r["period_start"],
            "dimensions": {a.split(":")[-1]: m.split(":")[-1] for a, m in dims.items()},
        }

    # -- evidence helpers ------------------------------------------------------
    def ev(self, concept, value, period_idx=0, periods=None, note=None):
        plist = periods if periods is not None else self.periods
        e = {
            "concept": (concept or "").replace("_", ":", 1) if concept else None,
            "period": plist[period_idx] if period_idx < len(plist) else None,
            "value": value,
            "value_formatted": fmt_value(value),
        }
        if note:
            e["note"] = note
        return e


def F(fid, category, severity, title, evidence, metrics=None, formula=None,
      implication=None, judgment=None):
    d = {
        "id": fid,
        "category": category,
        "severity": severity,  # red_flag | caution | info
        "title": title,
        "evidence": [e for e in evidence if e and e.get("value") is not None],
        "metrics": metrics or {},
    }
    if formula:
        d["formula"] = formula
    if implication:
        d["implication"] = implication
    if judgment:
        d["judgment"] = judgment
    return d


def pctf(a, b):
    return round(100.0 * a / b, 1) if a is not None and b else None


# --------------------------------------------------------------- tag chains

REVENUE = [
    "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap_Revenues",
    "us-gaap_RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap_SalesRevenueNet",
]
EBIT = ["us-gaap_OperatingIncomeLoss"]
PRETAX = [
    "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
]
NET_INCOME = ["us-gaap_NetIncomeLoss", "us-gaap_ProfitLoss"]
TAXEXP = ["us-gaap_IncomeTaxExpenseBenefit"]
CFO = [
    "us-gaap_NetCashProvidedByUsedInOperatingActivities",
    "us-gaap_NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
SHARES_D = ["us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding"]
EPS_D = ["us-gaap_EarningsPerShareDiluted"]
INTEREST = [
    "us-gaap_InterestExpense",
    "us-gaap_InterestExpenseNonoperating",
    "us-gaap_InterestIncomeExpenseNet",
    "us-gaap_InterestExpenseDebt",
]
CAPEX = [
    "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
    "us-gaap_PaymentsToAcquireProductiveAssets",
    "us-gaap_PaymentsForCapitalImprovements",
]
DANDA = [
    "us-gaap_DepreciationDepletionAndAmortization",
    "us-gaap_DepreciationAmortizationAndAccretionNet",
    "us-gaap_Depreciation",
]
EQUITY = [
    "us-gaap_StockholdersEquity",
    "us-gaap_StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
]
AR = ["us-gaap_AccountsReceivableNetCurrent", "us-gaap_ReceivablesNetCurrent"]
INV = ["us-gaap_InventoryNet"]
COGS = ["us-gaap_CostOfGoodsAndServicesSold", "us-gaap_CostOfRevenue", "us-gaap_CostOfGoodsSold"]
TOT_ASSETS = ["us-gaap_Assets"]
CUR_ASSETS = ["us-gaap_AssetsCurrent"]
CUR_LIAB = ["us-gaap_LiabilitiesCurrent"]
PPE_NET = ["us-gaap_PropertyPlantAndEquipmentNet"]
SGA = ["us-gaap_SellingGeneralAndAdministrativeExpense"]
LTD = [
    "us-gaap_LongTermDebtNoncurrent",
    "us-gaap_LongTermDebtAndCapitalLeaseObligations",
    "us-gaap_LongTermDebt",
]
LTD_CUR = [
    "us-gaap_LongTermDebtCurrent",
    "us-gaap_LongTermDebtAndCapitalLeaseObligationsCurrent",
    "us-gaap_DebtCurrent",
]
STD_OTHER = [
    "us-gaap_OtherShortTermBorrowings",
    "us-gaap_ShortTermBorrowings",
    "us-gaap_NotesPayableCurrent",
]
CASH = ["us-gaap_CashAndCashEquivalentsAtCarryingValue"]
STI = [
    "us-gaap_MarketableSecuritiesCurrent",
    "us-gaap_ShortTermInvestments",
    "us-gaap_CashCashEquivalentsAndShortTermInvestments",
]

ADJ_ITEMS = {
    # concept chains -> (finding id, title, default action, above-operating?)
    "sbc": (
        ["us-gaap_ShareBasedCompensation", "us-gaap_AllocatedShareBasedCompensationExpense"],
        "stock-based compensation",
        "keep_as_expense",
        True,
    ),
    "restructuring": (
        [
            "us-gaap_RestructuringCharges",
            "us-gaap_RestructuringCostsAndAssetImpairmentCharges",
            "us-gaap_RestructuringAndRelatedCostIncurredCost",
            "us-gaap_SeveranceCosts1",
        ],
        "restructuring / severance",
        None,  # depends on recurrence
        True,
    ),
    "impairment": (
        [
            "us-gaap_GoodwillImpairmentLoss",
            "us-gaap_AssetImpairmentCharges",
            "us-gaap_ImpairmentOfIntangibleAssetsExcludingGoodwill",
            "us-gaap_ImpairmentOfLongLivedAssetsHeldForUse",
            "us-gaap_TangibleAssetImpairmentCharges",
        ],
        "impairments",
        "add_back",
        True,
    ),
    "intangible_amortization": (
        ["us-gaap_AmortizationOfIntangibleAssets",
         "us-gaap_FiniteLivedIntangibleAssetsAmortizationExpense"],
        "amortization of acquired intangibles",
        "keep_as_expense",
        True,
    ),
    "one_time_gain_loss": (
        [
            "us-gaap_GainLossOnSaleOfBusiness",
            "us-gaap_GainsLossesOnSalesOfBusinesses",
            "us-gaap_GainLossOnDispositionOfAssets1",
            "us-gaap_GainLossOnDispositionOfAssets",
            "us-gaap_GainsLossesOnExtinguishmentOfDebt",
            "us-gaap_LitigationSettlementExpense",
            "us-gaap_GainLossRelatedToLitigationSettlement",
        ],
        "one-time gains / losses",
        "strip",
        False,
    ),
}


# ------------------------------------------------------------------ checks


def check_adjustment_items(s: FactStore):
    """The classic add-back candidates, each with a quantified judgment."""
    out = []
    _, rev = s.get(REVENUE)
    _, pretax = s.get(PRETAX)
    for key, (chain, title, default, _above_op) in ADJ_ITEMS.items():
        seen_vals = set()
        for concept in chain:
            vals = s.map.get(norm_concept(concept), {})
            per = [vals.get(p) for p in s.periods]
            cur = per[0] if per else None
            if not cur or cur in seen_vals:
                continue
            seen_vals.add(cur)
            n_present = sum(1 for v in per if v)
            recurring = n_present >= 2
            fid = f"{key}:{concept.split('_', 1)[-1]}"
            if key == "restructuring":
                default_opt = "keep_as_expense" if recurring else "add_back"
            elif key == "one_time_gain_loss":
                default_opt = "strip"
            else:
                default_opt = default
            # sign convention: expenses reduce EBIT; a "gain" concept with
            # positive value INCREASED income, stripping it lowers EBIT/pretax
            is_gain_item = key == "one_time_gain_loss" and "Expense" not in concept
            delta_if_removed = -cur if is_gain_item else cur
            judgment = {
                "question": f"How should {title} ({fmt_value(cur)}) be treated"
                " in adjusted earnings?",
                "options": [
                    {
                        "id": "keep_as_expense" if not is_gain_item else "keep",
                        "label": "keep in earnings as reported",
                        "ebit_delta": 0,
                        "pretax_delta": 0,
                    },
                    {
                        "id": "add_back" if not is_gain_item else "strip",
                        "label": (
                            "add back to adjusted earnings"
                            if not is_gain_item
                            else "strip from adjusted earnings"
                        ),
                        "ebit_delta": delta_if_removed if _above_op else 0,
                        "pretax_delta": delta_if_removed,
                    },
                ],
                "default": default_opt,
            }
            out.append(
                F(
                    fid,
                    key,
                    "caution" if recurring and key in ("restructuring", "impairment") else "info",
                    f"{title}: {fmt_value(cur)} ({n_present}/{len(s.periods)} periods)",
                    [s.ev(concept, cur)]
                    + [
                        s.ev(concept, v, i)
                        for i, v in enumerate(per[1:], start=1)
                        if v is not None
                    ],
                    metrics={
                        "pct_of_revenue": pctf(cur, rev),
                        "pct_of_pretax_income": pctf(cur, pretax),
                        "recurring": recurring,
                    },
                    implication=(
                        f"appears in {n_present} of {len(s.periods)} periods —"
                        " recurring items are operating costs; adding them back"
                        " every year overstates run-rate earnings"
                        if recurring
                        else "single-period item — candidate for run-rate exclusion"
                    ),
                    judgment=judgment,
                )
            )
    return out


def check_pension(s: FactStore):
    out = []
    _, equity = s.inst(EQUITY)
    # pension facts are typically dimensioned by plan type — use dim lookup
    assets = s.dim(["us-gaap_DefinedBenefitPlanFairValueOfPlanAssets"], "Pension")
    pbo = s.dim(["us-gaap_DefinedBenefitPlanBenefitObligation"], "Pension")
    funded = s.dim(["us-gaap_DefinedBenefitPlanFundedStatusOfPlan"], "Pension")
    disc = s.dim(
        ["us-gaap_DefinedBenefitPlanAssumptionsUsedCalculatingBenefitObligationDiscountRate"],
        "Pension",
    )
    eror = s.dim(
        ["us-gaap_DefinedBenefitPlanAssumptionsUsedCalculatingNetPeriodicBenefitCostExpectedLongTermReturnOnAssets"],
        "Pension",
    )
    _, nonsvc_v = s.get(
        ["us-gaap_NetPeriodicDefinedBenefitsExpenseReversalOfExpenseExcludingServiceCostComponent"]
    )
    if nonsvc_v is None:
        d = s.dim(
            ["us-gaap_NetPeriodicDefinedBenefitsExpenseReversalOfExpenseExcludingServiceCostComponent"],
            "Pension",
        )
        nonsvc_v = d["value"] if d else None
    if not any([assets, pbo, funded]):
        return out

    fs = None
    formula = None
    if funded:
        fs = funded["value"]
        formula = f"funded status (as tagged) = {fmt_value(fs)}"
    elif assets and pbo:
        fs = assets["value"] - pbo["value"]
        formula = (
            f"funded status = plan assets − PBO = {fmt_value(assets['value'])} −"
            f" {fmt_value(pbo['value'])} = {fmt_value(fs)}"
        )
    ev = []
    for d, _c in [
        (funded, "us-gaap:DefinedBenefitPlanFundedStatusOfPlan"),
        (assets, "us-gaap:DefinedBenefitPlanFairValueOfPlanAssets"),
        (pbo, "us-gaap:DefinedBenefitPlanBenefitObligation"),
        (disc, "us-gaap:...DiscountRate"),
        (eror, "us-gaap:...ExpectedLongTermReturnOnAssets"),
    ]:
        if d:
            ev.append(
                {
                    "concept": d["concept"],
                    "period": str(d["period"]),
                    "value": d["value"],
                    "value_formatted": fmt_value(d["value"]),
                    "dimensions": d["dimensions"],
                }
            )
    metrics = {
        "funded_status": fs,
        "funded_status_pct_of_equity": pctf(fs, equity),
        "discount_rate_pct": disc["value"] * 100
        if disc and disc["value"] and disc["value"] < 1
        else (disc["value"] if disc else None),
        "expected_return_on_assets_pct": eror["value"] * 100
        if eror and eror["value"] and eror["value"] < 1
        else (eror["value"] if eror else None),
        "non_service_pension_cost_in_earnings": nonsvc_v,
    }
    severity = "info"
    if fs is not None and equity and fs < 0 and abs(fs) > 0.05 * abs(equity):
        severity = "caution"
    if fs is not None and equity and fs < 0 and abs(fs) > 0.15 * abs(equity):
        severity = "red_flag"
    judgment = None
    if nonsvc_v:
        judgment = {
            "question": "Non-service pension cost sits in earnings — reclassify"
            " below operating (IFRS-style) for comparability?",
            "options": [
                {"id": "as_reported", "label": "leave as reported",
                 "ebit_delta": 0, "pretax_delta": 0},
                {
                    "id": "reclassify_below_operating",
                    "label": "move non-service cost out of operating income",
                    "ebit_delta": nonsvc_v,
                    "pretax_delta": 0,
                },
            ],
            "default": "as_reported",
        }
    out.append(
        F(
            "pension:funded_status",
            "pension",
            severity,
            f"pension funded status {fmt_value(fs)}"
            + (f" ({pctf(fs, equity)}% of equity)" if equity and fs is not None else ""),
            ev,
            metrics=metrics,
            formula=formula,
            implication=(
                "an underfunded plan is debt-like: add the deficit to net debt."
                " High expected-return and high discount-rate assumptions both"
                " flatter reported figures — compare the assumption tags above"
                " against peers; >7% expected return deserves skepticism."
            ),
            judgment=judgment,
        )
    )
    return out


def check_leases(s: FactStore):
    out = []
    _, op_liab = s.inst(["us-gaap_OperatingLeaseLiability"])
    if op_liab is None:
        _, v1 = s.inst(["us-gaap_OperatingLeaseLiabilityCurrent"])
        _, v2 = s.inst(["us-gaap_OperatingLeaseLiabilityNoncurrent"])
        op_liab = (v1 or 0) + (v2 or 0) if (v1 or v2) else None
    _, rou = s.inst(["us-gaap_OperatingLeaseRightOfUseAsset"])
    _, fin_liab = s.inst(["us-gaap_FinanceLeaseLiability"])
    _, rate = s.inst(["us-gaap_OperatingLeaseWeightedAverageDiscountRatePercent"])
    if op_liab is None:
        return out
    _, ltd = s.inst(LTD)
    _, ltdc = s.inst(LTD_CUR)
    debt = (ltd or 0) + (ltdc or 0)
    out.append(
        F(
            "leases:capitalization",
            "leases",
            "info",
            f"operating lease liability {fmt_value(op_liab)} vs reported debt {fmt_value(debt)}",
            [
                s.ev("us-gaap_OperatingLeaseLiability", op_liab, periods=s.instants),
                s.ev("us-gaap_OperatingLeaseRightOfUseAsset", rou, periods=s.instants),
                s.ev("us-gaap_FinanceLeaseLiability", fin_liab, periods=s.instants),
                s.ev(
                    "us-gaap_OperatingLeaseWeightedAverageDiscountRatePercent",
                    rate,
                    periods=s.instants,
                ),
            ],
            metrics={
                "operating_lease_liability": op_liab,
                "reported_debt": debt,
                "lease_adjusted_debt": debt + op_liab,
                "op_leases_pct_of_debt": pctf(op_liab, debt),
            },
            formula=f"lease-adjusted debt = {fmt_value(debt)} +"
            f" {fmt_value(op_liab)} = {fmt_value(debt + op_liab)}",
            implication=(
                "rating agencies treat operating leases as debt. If leases are"
                " a large share of adjusted debt, EBIT (which excludes the"
                " implied interest) and leverage are both understated vs a"
                " lease-heavy peer analysis"
            ),
            judgment={
                "question": "Capitalize operating leases into debt for leverage metrics?",
                "options": [
                    {"id": "capitalize",
                     "label": "yes — lease-adjusted debt (rating-agency style)",
                     "ebit_delta": 0, "pretax_delta": 0},
                    {"id": "as_reported", "label": "no — reported debt only",
                     "ebit_delta": 0, "pretax_delta": 0},
                ],
                "default": "capitalize",
            },
        )
    )
    return out


def check_equity_method(s: FactStore):
    out = []
    _, inv = s.inst(["us-gaap_EquityMethodInvestments"])
    _, inc = s.get(["us-gaap_IncomeLossFromEquityMethodInvestments"])
    if inv is None and inc is None:
        return out
    _, pretax = s.get(PRETAX)
    _, assets = s.inst(TOT_ASSETS)
    sev = "info"
    if inc is not None and pretax and abs(inc) > 0.10 * abs(pretax):
        sev = "caution"
    out.append(
        F(
            "jv:equity_method",
            "jv_equity_method",
            sev,
            f"equity-method (one-line consolidated) income {fmt_value(inc)}"
            f" on investments of {fmt_value(inv)}",
            [
                s.ev("us-gaap_EquityMethodInvestments", inv, periods=s.instants),
                s.ev("us-gaap_IncomeLossFromEquityMethodInvestments", inc),
            ],
            metrics={
                "equity_income_pct_of_pretax": pctf(inc, pretax),
                "investment_pct_of_assets": pctf(inv, assets),
            },
            implication=(
                "one-line consolidation hides the JVs' own leverage, margins"
                " and cash flow: net margin ratios are overstated (revenue"
                " excluded, profit included) and JV debt is invisible. If the"
                " share of pre-tax income is material, look through: pull the"
                " JV's own filings if it files, or the JV disclosure note"
            ),
        )
    )
    return out


def check_discontinued(s: FactStore):
    out = []
    concept, v = s.get(
        [
            "us-gaap_IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity",
            "us-gaap_IncomeLossFromDiscontinuedOperationsNetOfTax",
        ]
    )
    if v is None:
        return out
    _, ni = s.get(NET_INCOME)
    out.append(
        F(
            "discontinued:present",
            "discontinued_operations",
            "caution",
            f"discontinued operations {fmt_value(v)} inside net income",
            [s.ev(concept, v)],
            metrics={"pct_of_net_income": pctf(v, ni)},
            implication=(
                "all forward-looking analysis must use CONTINUING operations"
                " only; growth rates computed off total net income across the"
                " disposal year are meaningless"
            ),
            judgment={
                "question": "Which earnings base for analysis?",
                "options": [
                    {"id": "continuing_only",
                     "label": "continuing operations only",
                     "ebit_delta": 0, "pretax_delta": 0},
                    {"id": "total", "label": "total incl. discontinued",
                     "ebit_delta": 0, "pretax_delta": 0},
                ],
                "default": "continuing_only",
            },
        )
    )
    return out


def check_tax_forensics(s: FactStore):
    out = []
    _, va0 = s.inst(["us-gaap_DeferredTaxAssetsValuationAllowance"], 0)
    _, va1 = s.inst(["us-gaap_DeferredTaxAssetsValuationAllowance"], 1)
    _, utb = s.inst(["us-gaap_UnrecognizedTaxBenefits"])
    _, ni = s.get(NET_INCOME)
    if va0 is not None:
        delta = va0 - va1 if va1 is not None else None
        sev = "info"
        imp = (
            "a valuation allowance RELEASE (decrease) flows through the tax"
            " line and inflates EPS with zero operating content; a build"
            " signals management doubts future taxable income"
        )
        if delta is not None and ni and abs(delta) > 0.05 * abs(ni):
            sev = "caution"
        out.append(
            F(
                "tax:valuation_allowance",
                "tax",
                sev,
                f"DTA valuation allowance {fmt_value(va0)}"
                + (f" (change {fmt_value(delta)})" if delta is not None else ""),
                [
                    s.ev("us-gaap_DeferredTaxAssetsValuationAllowance", va0, 0, s.instants),
                    s.ev("us-gaap_DeferredTaxAssetsValuationAllowance", va1, 1, s.instants),
                ],
                metrics={"change": delta, "change_pct_of_net_income": pctf(delta, ni)},
                implication=imp,
            )
        )
    if utb is not None:
        out.append(
            F(
                "tax:unrecognized_benefits",
                "tax",
                "info",
                f"unrecognized tax benefits {fmt_value(utb)}",
                [s.ev("us-gaap_UnrecognizedTaxBenefits", utb, 0, s.instants)],
                metrics={"pct_of_net_income": pctf(utb, ni)},
                implication=(
                    "positions the company itself concedes may not survive"
                    " audit — a reserve that can release INTO earnings later"
                    " (watch for sudden ETR drops) or become cash tax out"
                ),
            )
        )
    # ETR trend
    tax_s = [s.map.get(norm_concept(TAXEXP[0]), {}).get(p) for p in s.periods]
    pre_s = []
    for i in range(len(s.periods)):
        _, v = s.get(PRETAX, i)
        pre_s.append(v)
    etr = [
        round(100 * t / p, 1) if t is not None and p else None
        for t, p in zip(tax_s, pre_s, strict=False)
    ]
    real = [e for e in etr if e is not None]
    if len(real) >= 2 and abs(real[0] - real[1]) > 3:
        out.append(
            F(
                "tax:etr_swing",
                "tax",
                "caution",
                f"effective tax rate moved {real[1]}% -> {real[0]}%",
                [
                    s.ev(TAXEXP[0], tax_s[i], i, note=f"ETR {etr[i]}%")
                    for i in range(min(len(etr), 3))
                    if tax_s[i] is not None
                ],
                metrics={"etr_by_period": dict(zip(s.periods, etr, strict=False))},
                formula="ETR = IncomeTaxExpenseBenefit / pre-tax income, per period",
                implication="the EPS change is partly a tax-line event, not operations",
                judgment={
                    "question": "Which tax rate for the adjusted-earnings bridge?",
                    "options": [
                        {"id": "company_etr",
                         "label": f"company current ETR ({real[0]}%)",
                         "ebit_delta": 0, "pretax_delta": 0},
                        {"id": "statutory_21", "label": "US statutory 21%",
                         "ebit_delta": 0, "pretax_delta": 0},
                        {"id": "avg_3y",
                         "label": f"3-period average"
                         f" ({round(sum(real) / len(real), 1)}%)",
                         "ebit_delta": 0, "pretax_delta": 0},
                    ],
                    "default": "avg_3y",
                },
            )
        )
    return out


def check_working_capital(s: FactStore):
    out = []
    if len(s.instants) < 2 or len(s.periods) < 2:
        return out
    _, rev0 = s.get(REVENUE, 0)
    _, rev1 = s.get(REVENUE, 1)
    _, cogs0 = s.get(COGS, 0)
    for label, chain, denom0, denom_name in [
        ("receivables", AR, rev0, "revenue"),
        ("inventory", INV, cogs0, "COGS"),
    ]:
        c, b0 = s.inst(chain, 0)
        _, b1 = s.inst(chain, 1)
        if not (b0 and b1 and rev0 and rev1):
            continue
        g = 100 * (b0 / b1 - 1)
        rg = 100 * (rev0 / rev1 - 1)
        dso_like = round(365 * b0 / denom0, 1) if denom0 else None
        sev = "red_flag" if g - rg > 20 else ("caution" if g - rg > 10 else "info")
        out.append(
            F(
                f"wc:{label}",
                "working_capital",
                sev,
                f"{label} grew {g:.1f}% vs revenue {rg:.1f}%",
                [s.ev(c, b0, 0, s.instants), s.ev(c, b1, 1, s.instants)],
                metrics={
                    f"{label}_growth_pct": round(g, 1),
                    "revenue_growth_pct": round(rg, 1),
                    f"days_{label}": dso_like,
                },
                formula=f"days = 365 × {fmt_value(b0)} / {denom_name} {fmt_value(denom0)}",
                implication=(
                    f"{label} outrunning sales is the classic revenue-quality/"
                    "demand red flag (channel stuffing, loosened terms, or"
                    " obsolescence building)"
                    if g - rg > 10
                    else "in line with sales"
                ),
            )
        )
    return out


def check_capital_structure(s: FactStore):
    out = []
    _, ltd = s.inst(LTD)
    _, ltdc = s.inst(LTD_CUR)
    _, cp = s.inst(["us-gaap_CommercialPaper"])
    _, std = s.inst(STD_OTHER)
    _, cash = s.inst(CASH)
    _, sti = s.inst(STI)
    _, ebit = s.get(EBIT)
    _, interest = s.get(INTEREST)
    _, nci = s.inst(["us-gaap_MinorityInterest"])
    debt = sum(v for v in (ltd, ltdc, cp, std) if v)
    if not debt:
        return out
    net_debt = debt - (cash or 0) - (sti or 0)
    cover = round(ebit / abs(interest), 1) if ebit and interest else None
    sev = "info"
    if cover is not None and cover < 3:
        sev = "caution"
    if cover is not None and cover < 1.5:
        sev = "red_flag"
    out.append(
        F(
            "capstruct:overview",
            "capital_structure",
            sev,
            f"total debt {fmt_value(debt)}, net debt {fmt_value(net_debt)}"
            + (f", interest coverage {cover}x" if cover else ""),
            [
                s.ev(LTD[0], ltd, 0, s.instants),
                s.ev(LTD_CUR[0], ltdc, 0, s.instants),
                s.ev("us-gaap_CommercialPaper", cp, 0, s.instants),
                s.ev(CASH[0], cash, 0, s.instants),
                s.ev(STI[0], sti, 0, s.instants),
                s.ev(INTEREST[0], interest),
                s.ev("us-gaap_MinorityInterest", nci, 0, s.instants),
            ],
            metrics={
                "total_debt": debt,
                "net_debt": net_debt,
                "current_portion_pct": pctf(ltdc, debt),
                "commercial_paper": cp,
                "interest_coverage_x": cover,
                "noncontrolling_interest": nci,
            },
            formula=(
                f"coverage = EBIT / |interest| = {fmt_value(ebit)} /"
                f" {fmt_value(abs(interest) if interest else None)} = {cover}x"
                if cover
                else None
            ),
            implication=(
                "check the current portion and commercial-paper reliance for"
                " refinancing risk; NCI means part of consolidated earnings"
                " belongs to someone else — use income attributable to parent"
                " for per-share work"
            ),
        )
    )
    return out


def _concept_scan(s: FactStore, pattern: str, top: int = 12) -> list[dict]:
    """All facts (consolidated + dimensioned) whose concept matches `pattern`,
    largest absolute values first. Evidence rows for hiding-place surveys."""
    df = s.all_df[s.all_df["numeric_value"].notna()]
    hits = df[df["concept"].str.contains(pattern, case=False, regex=True)]
    rows = []
    for _, r in hits.iterrows():
        ctx = s.x.contexts.get(r["context_ref"])
        dims = getattr(ctx, "dimensions", None) or {}
        rows.append(
            {
                "concept": str(r["concept"]),
                "value": r["numeric_value"],
                "value_formatted": fmt_value(r["numeric_value"]),
                "period": r["period_end"] or r["period_instant"] or r["period_start"],
                "dimensions": {
                    a.split(":")[-1]: str(m).split(":")[-1] for a, m in dims.items()
                }
                or None,
            }
        )
    rows.sort(key=lambda r: abs(r["value"] or 0), reverse=True)
    # one row per (concept, dimension signature): keep the largest
    seen, out = set(), []
    for r in rows:
        key = (r["concept"], tuple(sorted((r["dimensions"] or {}).items())))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out[:top]


def check_fx_derivatives(s: FactStore):
    """FX / derivatives / hedging — a classic surface for smoothing and
    hiding: gains parked in OCI, hedge de-designations, notional far larger
    than the exposure, FX 'transaction' losses recurring every year."""
    out = []
    notional = _concept_scan(s, r"DerivativeNotional|NotionalAmount")
    dv_asset = _concept_scan(s, r"DerivativeFairValueOfDerivativeAsset|DerivativeAssets?$", 4)
    dv_liab = _concept_scan(
        s, r"DerivativeFairValueOfDerivativeLiabilit|DerivativeLiabilit(?:y|ies)$", 4
    )
    pnl = _concept_scan(
        s,
        r"GainLossOnDerivativeInstruments"
        r"|DerivativeInstrumentsNotDesignatedAsHedgingInstrumentsGainLoss",
        6,
    )
    oci = _concept_scan(s, r"CashFlowHedgeGainLoss|OtherComprehensiveIncomeLossDerivative", 6)
    fx = _concept_scan(s, r"ForeignCurrencyTransactionGainLoss", 4)
    if not any((notional, dv_asset, dv_liab, pnl, oci, fx)):
        return out
    _, rev = s.get(REVENUE)
    biggest_notional = notional[0]["value"] if notional else None
    metrics = {
        "largest_notional": biggest_notional,
        "notional_pct_revenue": pctf(biggest_notional, rev),
        "derivative_gain_loss_in_earnings": pnl[0]["value"] if pnl else None,
        "hedge_gain_loss_in_oci": oci[0]["value"] if oci else None,
        "fx_transaction_gain_loss": fx[0]["value"] if fx else None,
    }
    sev = "info"
    if rev and biggest_notional and abs(biggest_notional) > rev:
        sev = "caution"
    out.append(
        F(
            "fx:derivatives",
            "fx_derivatives",
            sev,
            "derivative / FX hedging surface"
            + (
                f" — largest notional {fmt_value(biggest_notional)}"
                if biggest_notional
                else ""
            ),
            [],
            metrics=metrics,
            implication=(
                "hedging is legitimate, but watch: (1) gains sitting in OCI that"
                " get reclassified into earnings exactly when needed, (2)"
                " de-designated hedges flipping mark-to-market into P&L, (3)"
                " notional far above the underlying exposure = speculation"
                " dressed as hedging, (4) recurring 'transaction' FX losses"
                " excluded from adjusted earnings while hedge gains are kept"
            ),
            judgment={
                "question": "treat derivative gains in earnings as operating?",
                "default": "keep",
                "options": [
                    {"id": "keep", "label": "keep as reported"},
                    {"id": "strip",
                     "label": "strip derivative gains/losses from adjusted earnings"},
                ],
            },
        )
    )
    # attach the survey rows as evidence-style detail
    out[-1]["survey"] = {
        "notional": notional,
        "derivative_assets": dv_asset,
        "derivative_liabilities": dv_liab,
        "in_earnings": pnl,
        "in_oci": oci,
        "fx_transaction": fx,
    }
    return out


def check_implied_cost_of_debt(s: FactStore):
    """Implied rate = interest expense / average total debt, tied against any
    disclosed weighted-average rate. A markup (implied >> disclosed) hints at
    off-balance-sheet borrowings, big intra-year swings, or interest hiding in
    capitalized costs; implied << disclosed hints at capitalized interest."""
    out = []

    def total_debt(idx):
        vals = [
            s.inst(LTD, idx)[1],
            s.inst(LTD_CUR, idx)[1],
            s.inst(["us-gaap_CommercialPaper"], idx)[1],
            s.inst(STD_OTHER, idx)[1],
        ]
        t = sum(v for v in vals if v)
        return t or None

    d0, d1 = total_debt(0), total_debt(1)
    _, interest = s.get(INTEREST)
    if not (d0 and interest):
        return out
    avg_debt = (d0 + d1) / 2 if d1 else d0
    implied = abs(interest) / avg_debt
    _, wavg_lt = s.get(
        ["us-gaap_LongtermDebtWeightedAverageInterestRate",
         "us-gaap_DebtWeightedAverageInterestRate"], 0, s.instants
    )
    _, cap_int = s.get(["us-gaap_InterestCostsCapitalized",
                        "us-gaap_InterestCostsCapitalizedAdjustment"])
    gap_bp = round((implied - wavg_lt) * 10_000) if wavg_lt else None
    sev = "info"
    if gap_bp is not None and abs(gap_bp) > 150:
        sev = "caution"
    out.append(
        F(
            "capstruct:implied_cost_of_debt",
            "capital_structure",
            sev,
            f"implied cost of debt {implied:.2%}"
            + (f" vs disclosed weighted-avg {wavg_lt:.2%} ({gap_bp:+d}bp)"
               if wavg_lt else " (no disclosed weighted-average rate tagged)"),
            [
                s.ev(INTEREST[0], interest),
                s.ev(LTD[0], d0, 0, s.instants, note="total debt, current year"),
                s.ev(LTD[0], d1, 1, s.instants, note="total debt, prior year"),
                s.ev("us-gaap_InterestCostsCapitalized", cap_int,
                     note="interest capitalized out of P&L"),
            ],
            metrics={
                "implied_rate": round(implied, 5),
                "disclosed_weighted_avg_rate": wavg_lt,
                "gap_bp": gap_bp,
                "avg_total_debt": avg_debt,
                "capitalized_interest": cap_int,
            },
            formula=(
                f"implied = |interest| / avg debt = {fmt_value(abs(interest))}"
                f" / {fmt_value(avg_debt)} = {implied:.2%}"
            ),
            implication=(
                "implied >> disclosed: interest on debt that isn't on the"
                " balance sheet at year end (window-dressed repayments,"
                " off-BS vehicles) or expensive short-term paper; implied <<"
                " disclosed: capitalized interest flattering the P&L or debt"
                " raised late in the year"
            ),
        )
    )
    return out


def check_vie_offbalance(s: FactStore):
    """VIEs / unconsolidated entities / guarantees — where 'empty companies'
    live. Surveys VIE involvement, maximum-loss exposure, and guarantees the
    balance sheet doesn't consolidate."""
    out = []
    vie = _concept_scan(s, r"VariableInterestEntity")
    guar = _concept_scan(s, r"Guarantee[A-Z]|GuaranteeObligations", 6)
    if not vie and not guar:
        return out
    _, equity = s.inst(EQUITY)
    max_loss = next(
        (r["value"] for r in vie if "MaximumLossExposure" in r["concept"]), None
    )
    biggest_guar = guar[0]["value"] if guar else None
    exposure = (max_loss or 0) + (biggest_guar or 0)
    sev = "info"
    if equity and exposure > 0.10 * abs(equity):
        sev = "caution"
    out.append(
        F(
            "vie:offbalance",
            "vie_offbalance_sheet",
            sev,
            "VIE / guarantee off-balance-sheet surface"
            + (f" — max loss exposure {fmt_value(max_loss)}" if max_loss else "")
            + (f", guarantees {fmt_value(biggest_guar)}" if biggest_guar else ""),
            [s.ev(EQUITY[0], equity, 0, s.instants, note="total equity, for scale")],
            metrics={
                "vie_max_loss_exposure": max_loss,
                "largest_guarantee": biggest_guar,
                "exposure_pct_equity": pctf(exposure, equity),
            },
            implication=(
                "unconsolidated VIEs and guarantees are the classic place to"
                " park debt and losses outside the consolidated statements —"
                " read the VIE footnote: who absorbs losses, what triggers"
                " consolidation, and whether guaranteed obligations of"
                " unconsolidated entities would be debt if brought on-book"
            ),
        )
    )
    out[-1]["survey"] = {"vie": vie, "guarantees": guar}
    return out


def check_nonop_reliance(s: FactStore):
    out = []
    _, pretax = s.get(PRETAX)
    _, ebit = s.get(EBIT)
    if not (pretax and ebit):
        return out
    gap = pretax - ebit
    share = pctf(gap, pretax)
    if share is None or abs(share) < 10:
        return out
    out.append(
        F(
            "nonop:reliance",
            "non_operating",
            "caution" if abs(share) >= 20 else "info",
            f"{share}% of pre-tax income arises between operating income and"
            " the tax line",
            [s.ev(EBIT[0], ebit), s.ev(PRETAX[0], pretax)],
            metrics={"non_operating_bridge": gap, "pct_of_pretax": share},
            formula=f"bridge = pre-tax {fmt_value(pretax)} − EBIT"
            f" {fmt_value(ebit)} = {fmt_value(gap)}",
            implication=(
                "interest income, equity income, pension non-service items and"
                " one-offs live here — earnings driven from this zone are"
                " lower-multiple than operating earnings; decompose with"
                " search_facts before extrapolating"
            ),
        )
    )
    return out


def check_capitalization_policy(s: FactStore):
    out = []
    _, capex = s.get(CAPEX)
    _, danda = s.get(DANDA)
    if not (capex and danda):
        return out
    ratio = round(abs(capex) / abs(danda), 2)
    _, sw0 = s.inst(["us-gaap_CapitalizedComputerSoftwareNet"], 0)
    _, sw1 = s.inst(["us-gaap_CapitalizedComputerSoftwareNet"], 1)
    metrics = {"capex_over_danda": ratio, "capitalized_software_net": sw0}
    if sw0 and sw1:
        metrics["capitalized_software_growth_pct"] = round(100 * (sw0 / sw1 - 1), 1)
    out.append(
        F(
            "capitalization:policy",
            "capitalization",
            "info",
            f"capex/D&A = {ratio}x",
            [
                s.ev(CAPEX[0], capex),
                s.ev(DANDA[0], danda),
                s.ev("us-gaap_CapitalizedComputerSoftwareNet", sw0, 0, s.instants),
            ],
            metrics=metrics,
            formula=f"{fmt_value(abs(capex))} / {fmt_value(abs(danda))} = {ratio}x",
            implication=(
                "sustained ratio well above 1 = growth capex or aging-asset"
                " catch-up; well below 1 = harvesting (earnings flattered by"
                " an old depreciated base). Rising capitalized software vs"
                " expensed R&D shifts cost out of today's income statement"
            ),
        )
    )
    return out


def check_beneish(s: FactStore):
    """Beneish M-score (1999) from the filing's own two balance dates /
    two-plus durations. A screening heuristic, not an accusation."""
    if len(s.instants) < 2 or len(s.periods) < 2:
        return []

    def g(chain, i, inst=False):
        _, v = (s.inst if inst else s.get)(chain, i)
        return v

    rev0, rev1 = g(REVENUE, 0), g(REVENUE, 1)
    ar0, ar1 = g(AR, 0, True), g(AR, 1, True)
    cogs0, cogs1 = g(COGS, 0), g(COGS, 1)
    ca0, ca1 = g(CUR_ASSETS, 0, True), g(CUR_ASSETS, 1, True)
    ppe0, ppe1 = g(PPE_NET, 0, True), g(PPE_NET, 1, True)
    ta0, ta1 = g(TOT_ASSETS, 0, True), g(TOT_ASSETS, 1, True)
    sga0, sga1 = g(SGA, 0), g(SGA, 1)
    dep0, dep1 = g(DANDA, 0), g(DANDA, 1)
    ltd0, ltd1 = g(LTD, 0, True), g(LTD, 1, True)
    cl0, cl1 = g(CUR_LIAB, 0, True), g(CUR_LIAB, 1, True)
    ni0 = g(NET_INCOME, 0)
    cfo0 = g(CFO, 0)

    idx = {}
    inputs_missing = []

    def safe(name, fn, *vals):
        if any(v is None or v == 0 for v in vals):
            inputs_missing.append(name)
            return
        try:
            idx[name] = round(fn(), 3)
        except ZeroDivisionError:
            inputs_missing.append(name)

    safe("DSRI", lambda: (ar0 / rev0) / (ar1 / rev1), ar0, rev0, ar1, rev1)
    safe(
        "GMI",
        lambda: ((rev1 - cogs1) / rev1) / ((rev0 - cogs0) / rev0),
        rev0, rev1, cogs0, cogs1,
    )
    safe(
        "AQI",
        lambda: (1 - (ca0 + ppe0) / ta0) / (1 - (ca1 + ppe1) / ta1),
        ca0, ppe0, ta0, ca1, ppe1, ta1,
    )
    safe("SGI", lambda: rev0 / rev1, rev0, rev1)
    safe(
        "DEPI",
        lambda: (dep1 / (dep1 + ppe1)) / (dep0 / (dep0 + ppe0)),
        dep0, dep1, ppe0, ppe1,
    )
    safe("SGAI", lambda: (sga0 / rev0) / (sga1 / rev1), sga0, rev0, sga1, rev1)
    safe(
        "LVGI",
        lambda: ((ltd0 + cl0) / ta0) / ((ltd1 + cl1) / ta1),
        ltd0, cl0, ta0, ltd1, cl1, ta1,
    )
    safe("TATA", lambda: (ni0 - cfo0) / ta0, ni0, cfo0, ta0)

    score = None
    if len(idx) >= 6:
        d = {
            "DSRI": 1.0, "GMI": 1.0, "AQI": 1.0, "SGI": 1.0,
            "DEPI": 1.0, "SGAI": 1.0, "LVGI": 1.0, "TATA": 0.0,
        }
        d.update(idx)
        score = round(
            -4.84
            + 0.92 * d["DSRI"] + 0.528 * d["GMI"] + 0.404 * d["AQI"]
            + 0.892 * d["SGI"] + 0.115 * d["DEPI"] - 0.172 * d["SGAI"]
            + 4.679 * d["TATA"] - 0.327 * d["LVGI"],
            2,
        )
    sev = "info"
    if score is not None and score > -1.78:
        sev = "red_flag"
    return [
        F(
            "beneish:mscore",
            "beneish_m_score",
            sev,
            f"Beneish M-score {score}"
            + (" — ABOVE the -1.78 manipulation threshold"
               if sev == "red_flag" else " (below -1.78 threshold)")
            if score is not None
            else "Beneish M-score not computable (missing inputs)",
            [],
            metrics={
                "m_score": score,
                "indices": idx,
                "missing_inputs_defaulted_to_neutral": inputs_missing or None,
                "threshold": -1.78,
            },
            implication=(
                "screening heuristic (Beneish 1999): scores above -1.78"
                " statistically resemble earnings manipulators. It flags"
                " aggressive accrual/receivable/margin patterns — investigate"
                " the component indices, do not treat as proof"
            ),
        )
    ]


def check_sbc_extras(s: FactStore):
    out = []
    _, sbc = s.get(["us-gaap_ShareBasedCompensation"])
    _, unrec = s.inst(
        ["us-gaap_EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognized"]
    )
    _, buyback = s.get(["us-gaap_PaymentsForRepurchaseOfCommonStock"])
    if not sbc:
        return out
    metrics = {
        "sbc": sbc,
        "unrecognized_comp_cost_committed": unrec,
        "buybacks": buyback,
        "buyback_to_sbc_ratio": round(abs(buyback) / sbc, 2) if buyback else None,
    }
    out.append(
        F(
            "sbc:dilution_offset",
            "compensation",
            "info",
            f"SBC {fmt_value(sbc)}; buybacks {fmt_value(abs(buyback) if buyback else None)}"
            + (f" ({metrics['buyback_to_sbc_ratio']}x SBC)"
               if metrics["buyback_to_sbc_ratio"] else ""),
            [
                s.ev("us-gaap_ShareBasedCompensation", sbc),
                s.ev(
                    "us-gaap_EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognized",
                    unrec, 0, s.instants,
                ),
                s.ev("us-gaap_PaymentsForRepurchaseOfCommonStock", buyback),
            ],
            metrics=metrics,
            implication=(
                "part of the buyback is not shareholder return — it is"
                " sterilizing SBC dilution. Unrecognized comp cost is already-"
                "granted future expense: tomorrow's SBC is largely locked in"
            ),
        )
    )
    return out


CHECKS = [
    check_adjustment_items,
    check_pension,
    check_leases,
    check_equity_method,
    check_discontinued,
    check_tax_forensics,
    check_working_capital,
    check_capital_structure,
    check_implied_cost_of_debt,
    check_fx_derivatives,
    check_vie_offbalance,
    check_nonop_reliance,
    check_capitalization_policy,
    check_beneish,
    check_sbc_extras,
]

_SEV_ORDER = {"red_flag": 0, "caution": 1, "info": 2}


def scan(x, accession: str) -> dict:
    s = FactStore(x, accession)
    findings = []
    for check in CHECKS:
        try:
            findings.extend(check(s))
        except Exception as e:
            findings.append(
                {
                    "id": f"error:{check.__name__}",
                    "category": "engine",
                    "severity": "info",
                    "title": f"{check.__name__} failed: {e}",
                    "evidence": [],
                    "metrics": {},
                }
            )
    findings.sort(key=lambda f: _SEV_ORDER.get(f["severity"], 3))
    _, ebit = s.get(EBIT)
    _, pretax = s.get(PRETAX)
    _, ni = s.get(NET_INCOME)
    _, shares = s.get(SHARES_D)
    return {
        "entity": x.entity_name,
        "accession_no": accession,
        "period_of_report": str(x.period_of_report),
        "reference_note": (
            "every evidence item is a tagged fact in this accession — verify"
            " any of them with explain_number(accession, concept)"
        ),
        "reported_baseline": {
            "operating_income": fmt_value(ebit),
            "pretax_income": fmt_value(pretax),
            "net_income": fmt_value(ni),
            "diluted_shares": fmt_value(shares),
        },
        "judgments_pending": [
            {
                "finding_id": f["id"],
                "question": f["judgment"]["question"],
                "options": [o["id"] for o in f["judgment"]["options"]],
                "default": f["judgment"]["default"],
            }
            for f in findings
            if f.get("judgment")
        ],
        "findings": findings,
    }


def apply_decisions(x, accession: str, decisions: dict[str, str]) -> dict:
    """Deterministic adjusted-earnings bridge from analyst decisions."""
    s = FactStore(x, accession)
    result = scan(x, accession)
    _, ebit = s.get(EBIT)
    _, pretax = s.get(PRETAX)
    _, taxexp = s.get(TAXEXP)
    _, shares = s.get(SHARES_D)

    etr = None
    if taxexp is not None and pretax:
        etr = taxexp / pretax
    tax_choice = decisions.get("tax:etr_swing", "company_etr")
    if tax_choice == "statutory_21" or etr is None or not (0 < etr < 0.45):
        tax_rate, tax_basis = 0.21, "statutory 21% (chosen or company ETR unusable)"
    else:
        tax_rate, tax_basis = etr, f"company ETR {round(etr*100,1)}%"

    ledger = []
    adj_ebit, adj_pretax = ebit or 0, pretax or 0
    unknown = [k for k in decisions if k not in {f["id"] for f in result["findings"]}]
    for f in result["findings"]:
        j = f.get("judgment")
        if not j:
            continue
        choice = decisions.get(f["id"], j["default"])
        opt = next((o for o in j["options"] if o["id"] == choice), None)
        if opt is None:
            opt = next(o for o in j["options"] if o["id"] == j["default"])
            choice = f"{j['default']} (requested option unknown, default used)"
        adj_ebit += opt.get("ebit_delta", 0) or 0
        adj_pretax += opt.get("pretax_delta", 0) or 0
        ledger.append(
            {
                "finding_id": f["id"],
                "title": f["title"],
                "decision": choice,
                "ebit_effect": fmt_value(opt.get("ebit_delta", 0)),
                "pretax_effect": fmt_value(opt.get("pretax_delta", 0)),
                "evidence": f["evidence"][:2],
            }
        )
    adj_ni = round(adj_pretax * (1 - tax_rate)) if pretax is not None else None
    return {
        "entity": result["entity"],
        "accession_no": accession,
        "decisions_applied": ledger,
        "unknown_decision_ids_ignored": unknown or None,
        "tax_rate_used": tax_basis,
        "bridge": {
            "operating_income_reported": fmt_value(ebit),
            "operating_income_adjusted": fmt_value(adj_ebit),
            "pretax_income_reported": fmt_value(pretax),
            "pretax_income_adjusted": fmt_value(adj_pretax),
            "net_income_adjusted_(pretax_adj_x_(1-t))": fmt_value(adj_ni),
            "adjusted_diluted_eps": round(adj_ni / shares, 2)
            if adj_ni is not None and shares
            else None,
        },
        "note": (
            "adjusted net income re-taxes the FULL adjusted pre-tax at one"
            " rate — a simplification; item-level tax effects (e.g."
            " non-deductible goodwill impairment) need manual treatment"
        ),
    }
