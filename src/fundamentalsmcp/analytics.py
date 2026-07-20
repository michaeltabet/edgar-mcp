"""Statistical / ML analysis over the fact store.

Three genuinely different sample regimes, and this module is explicit about
which one you are in — because that determines whether a number means
anything:

  * `trend`      — one company, one concept, N annual points. N is small
                   (5-ish). OLS slope and R² are DESCRIPTIVE here, not
                   inferential; every result carries n and a reliability
                   note. No p-values are reported, because with n=5 they
                   would be noise dressed as significance.
  * `anomaly`    — one company, many metrics x years. Uses robust z-scores
                   (median / MAD) rather than mean/sd so a single blowout
                   year doesn't hide itself by inflating the deviation.
  * `peer_scan`  — MANY companies, one period. This is the regime where
                   real multivariate methods earn their keep: standardize,
                   then z-score and cluster.

Everything reads from the DuckDB fact store, so warm the companies first.
Nothing is imputed: a missing metric stays missing and is reported as such.
"""

from __future__ import annotations

import math

from . import dossier

# Metrics used for cross-sectional work, pulled from the dossier ratio suite.
PEER_METRICS = [
    "gross_margin", "operating_margin", "net_margin", "roe", "roa",
    "current_ratio", "debt_to_equity", "interest_coverage",
    "fcf_margin", "cash_conversion_cfo_ni", "accruals_ratio",
]


def _reliability(n: int) -> str:
    if n >= 12:
        return "adequate sample for inferential statistics"
    if n >= 8:
        return "marginal sample — treat as indicative, not conclusive"
    return (
        f"SMALL SAMPLE (n={n}) — descriptive only. Slope/R² summarize these "
        "points; they do not support inference about the future or "
        "significance testing."
    )


def _ols(xs: list[float], ys: list[float]) -> dict:
    """Plain least squares with R². No p-values by design (see module docstring)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    if sxx == 0:
        return {"slope": None, "intercept": None, "r_squared": None}
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = None if ss_tot == 0 else 1 - ss_res / ss_tot
    return {"slope": slope, "intercept": intercept, "r_squared": r2}


def trend(company: str, concept: str) -> dict:
    """Fit a trend to one concept's annual series for one company.

    Returns slope (units/year), R², CAGR, the fitted residual per year (which
    years break the trend), and an explicit reliability note tied to n.
    """
    matrix = dossier._fact_matrix(company)
    series = matrix.get(concept) or matrix.get(concept.replace("_", ":", 1))
    if not series:
        return {
            "company": company, "concept": concept, "error": "concept not in store",
            "hint": "warm the company first, or check the exact concept string "
                    "via query_fact_store",
        }
    years = sorted(series)
    ys = [series[y] for y in years]
    fit = _ols([float(y) for y in years], ys)
    resid = None
    if fit["slope"] is not None:
        resid = {
            y: round(v - (fit["intercept"] + fit["slope"] * y), 4)
            for y, v in zip(years, ys, strict=True)
        }
    cagr = None
    if len(ys) >= 2 and ys[0] and ys[-1] and ys[0] > 0 and ys[-1] > 0:
        span = years[-1] - years[0]
        if span:
            cagr = (ys[-1] / ys[0]) ** (1 / span) - 1
    return {
        "company": company,
        "concept": concept,
        "n_observations": len(ys),
        "years": years,
        "values": ys,
        "slope_per_year": fit["slope"],
        "r_squared": fit["r_squared"],
        "cagr": cagr,
        "residuals": resid,
        "reliability": _reliability(len(ys)),
    }


MIN_N_FOR_Z = 5
DISPERSION_FLOOR = 0.05  # scale never below 5% of |median|
Z_CAP = 10.0


def _robust_z(values: list[float]) -> list[float | None]:
    """Median/MAD z-scores, guarded against small-sample pathology.

    Plain MAD explodes when a few points are nearly identical: with
    [41.2, 40.75, 29.1] the MAD is 0.44, so a 28% difference scores as
    -18 sigma — precise-looking nonsense. Three guards:
      * need at least MIN_N_FOR_Z real observations (below that, dispersion
        is not estimable and we return None rather than guess);
      * the scale is floored at DISPERSION_FLOOR x |median|, i.e. we never
        claim to resolve differences finer than 5% of the level;
      * scores are capped at +/-Z_CAP so no single artifact dominates a sort.
    """
    vals = [v for v in values if v is not None]
    if len(vals) < MIN_N_FOR_Z:
        return [None] * len(values)
    s = sorted(vals)
    mid = len(s) // 2
    median = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
    devs = sorted(abs(v - median) for v in vals)
    m = len(devs) // 2
    mad = devs[m] if len(devs) % 2 else (devs[m - 1] + devs[m]) / 2
    scale = mad * 1.4826  # MAD -> sd-equivalent for normal data
    scale = max(scale, DISPERSION_FLOOR * abs(median))
    if scale == 0:
        return [None] * len(values)
    return [
        None if v is None
        else max(-Z_CAP, min(Z_CAP, (v - median) / scale))
        for v in values
    ]


def anomaly_scan(company: str, years: int = 6, threshold: float = 2.5) -> dict:
    """Flag unusual years in a company's own ratio history.

    Robust z-score per metric across its years; anything beyond `threshold`
    absolute deviations is surfaced with the value and the year. This is a
    within-company screen — it says "this year is unlike this company's other
    years", not "this is fraud".
    """
    d = dossier.build(company, years=years, warm=True)
    if d.get("error"):
        return d
    ratios, yrs = d["ratios"], d["years"]
    flags, coverage = [], {}
    for metric in PEER_METRICS:
        vals = [ratios[y].get(metric) for y in yrs]
        present = sum(1 for v in vals if v is not None)
        coverage[metric] = present
        if present < MIN_N_FOR_Z:
            continue
        for y, v, z in zip(yrs, vals, _robust_z(vals), strict=True):
            if z is not None and abs(z) >= threshold:
                flags.append({
                    "year": y, "metric": metric, "value": v,
                    "robust_z": round(z, 2),
                    "direction": "high" if z > 0 else "low",
                })
    flags.sort(key=lambda f: abs(f["robust_z"]), reverse=True)
    return {
        "company": d["company"],
        "years": yrs,
        "n_years": len(yrs),
        "threshold": threshold,
        "flag_count": len(flags),
        "flags": flags,
        "metric_coverage": coverage,
        "method": (f"robust z-score (median/MAD x1.4826); needs n>={MIN_N_FOR_Z}, "
                   f"scale floored at {DISPERSION_FLOOR:.0%} of |median|, capped +/-{Z_CAP:.0f}"),
        "reliability": _reliability(len(yrs)),
        "caveat": ("a flag means the year is statistically unlike the company's "
                   "other years — investigate it with forensic_scan / "
                   "explain_number, do not treat it as a conclusion"),
    }


def peer_scan(companies: list[str], year: int | None = None,
              n_clusters: int = 0) -> dict:
    """Cross-sectional comparison across companies — the regime where
    multivariate methods actually apply.

    Builds each company's ratio vector for the chosen year, reports z-scores
    per metric across the peer set (who is the outlier on what), and
    optionally KMeans-clusters the standardized vectors.
    """
    if len(companies) < 3:
        return {"error": "need at least 3 companies for a cross-sectional scan"}
    rows, skipped = {}, []
    for c in companies:
        d = dossier.build(c, years=6, warm=True)
        if d.get("error") or not d.get("years"):
            skipped.append({"company": c, "reason": d.get("error", "no data")})
            continue
        y = year if (year and year in d["years"]) else d["years"][-1]
        rows[d["company"]] = {"year": y, **{m: d["ratios"][y].get(m)
                                            for m in PEER_METRICS}}
    if len(rows) < 3:
        return {"error": "fewer than 3 companies resolved", "skipped": skipped}

    names = list(rows)
    zs: dict[str, dict] = {n: {} for n in names}
    for metric in PEER_METRICS:
        vals = [rows[n].get(metric) for n in names]
        for n, z in zip(names, _robust_z(vals), strict=True):
            zs[n][metric] = None if z is None else round(z, 2)

    outliers = sorted(
        ({"company": n, "metric": m, "value": rows[n][m], "robust_z": z}
         for n in names for m, z in zs[n].items()
         if z is not None and abs(z) >= 2.0),
        key=lambda r: abs(r["robust_z"]), reverse=True,
    )
    # An empty outlier list must not read as "everyone is normal" when the
    # truth is "too few peers to estimate dispersion at all".
    scored = any(z is not None for n in names for z in zs[n].values())
    z_note = None if scored else (
        f"z-SCORING SKIPPED: only {len(names)} peers resolved; at least "
        f"{MIN_N_FOR_Z} are needed to estimate cross-sectional dispersion. "
        "The empty `outliers` list means NOT MEASURED, not 'no outliers'. "
        "Compare the raw `values` directly, or add more peers."
    )

    clusters = None
    if n_clusters and n_clusters >= 2 and len(names) >= n_clusters:
        usable = [m for m in PEER_METRICS
                  if all(rows[n].get(m) is not None for n in names)]
        if len(usable) >= 2:
            from sklearn.cluster import KMeans
            from sklearn.preprocessing import StandardScaler

            X = [[rows[n][m] for m in usable] for n in names]
            Xs = StandardScaler().fit_transform(X)
            km = KMeans(n_clusters=n_clusters, n_init=10, random_state=0).fit(Xs)
            clusters = {
                "metrics_used": usable,
                "assignment": dict(zip(names, (int(c) for c in km.labels_),
                                       strict=True)),
                "note": "KMeans on standardized ratios; random_state fixed so "
                        "the same peer set reproduces the same clusters",
            }
        else:
            clusters = {"error": "not enough metrics present across all peers"}

    return {
        "companies": names,
        "skipped": skipped or None,
        "metrics": PEER_METRICS,
        "values": rows,
        "robust_z_scores": zs if scored else None,
        "outliers": outliers,
        "z_scoring_note": z_note,
        "clusters": clusters,
        "method": "robust z-score across peers (median/MAD); optional KMeans",
        "caveat": ("peers must be genuinely comparable — a z-score against an "
                   "ill-chosen peer set is precise and meaningless"),
    }


def _finite(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None
