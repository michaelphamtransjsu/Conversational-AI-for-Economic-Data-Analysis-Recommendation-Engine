"""Quarterly GDP/Core CPI comparison on one explicitly defined sample."""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import numpy as np
import pandas as pd

from .analytics import AnalyticsError, calendar_change, native_series, provenance, transform
from .schemas import DateWindow, Evidence, Metric, SeriesID, Snapshot


def quarterly_cpi_levels(snapshot: Snapshot) -> pd.Series:
    if snapshot.series != SeriesID.CPILFESL:
        raise AnalyticsError("Quarterly CPI aggregation requires CPILFESL.")
    monthly = native_series(snapshot)
    # Nonpositive economic index levels are invalid even if averaging would hide them.
    monthly = monthly.where(monthly > 0)
    groups = monthly.groupby(monthly.index.asfreq("Q-DEC"))
    # Exactly three finite monthly levels are required; skipna means are never accepted alone.
    means = groups.mean().where(groups.count() == 3)
    return means.reindex(pd.period_range(means.index.min(), means.index.max(), freq="Q-DEC"))


def compare(gdp: Snapshot, cpi: Snapshot, start: date | str, end: date | str) -> Evidence:
    if gdp.series != SeriesID.GDP or cpi.series != SeriesID.CPILFESL:
        raise AnalyticsError("Comparison requires quarterly GDP and monthly CPILFESL snapshots.")
    window = DateWindow(start=start, end=end)
    gdp_rates = transform(gdp, Metric.yoy).rename("gdp_yoy")
    quarterly_cpi = quarterly_cpi_levels(cpi)
    cpi_rates = calendar_change(quarterly_cpi, 4).rename("cpi_yoy")
    pairs = pd.concat([gdp_rates, cpi_rates], axis=1, join="inner").dropna()
    mask = (pairs.index.to_timestamp().date >= window.start) & (pairs.index.to_timestamp().date <= window.end)
    pairs = pairs.loc[mask]
    if len(pairs) < 8:
        raise AnalyticsError(f"Quarterly correlation needs at least eight valid paired quarters; this window has {len(pairs)}. Missing/incomplete CPI quarters and missing baselines are excluded.")
    if any(pairs[column].nunique() < 2 or np.isclose(float(pairs[column].std()), 0, rtol=0, atol=1e-12) for column in pairs):
        raise AnalyticsError("Correlation is undefined for a constant quarterly rate series.")
    coefficient = float(pairs["gdp_yoy"].corr(pairs["cpi_yoy"], method="pearson"))
    if not np.isfinite(coefficient):
        raise AnalyticsError("Correlation could not be calculated from finite nonconstant paired rates.")
    records = [{"date": period.start_time.date().isoformat(), "period": str(period),
                "gdp_yoy": float(row.gdp_yoy), "cpi_yoy": float(row.cpi_yoy)} for period, row in pairs.iterrows()]
    warnings = [
        "Association within this selected window does not establish causality or predictive power.",
        "Nominal GDP already contains price effects; results depend on the sample, revisions and extreme observations.",
        "Only complete three-valid-month CPI quarters and valid four-quarter baselines are used; GDP is never interpolated to monthly frequency.",
        "Quarterly CPI inflation is YoY change of quarterly-average CPI levels, not the average of monthly YoY inflation rates.",
    ]
    if gdp.demo or cpi.demo:
        warnings.insert(0, "DEMO / SYNTHETIC — not empirical economic findings. No live retrieval occurred.")
    return Evidence(result_id="result_"+uuid4().hex, operation="compare_indicators", series=[SeriesID.GDP, SeriesID.CPILFESL],
        metric="quarterly_yoy_comparison", units="percent, year-over-year, shared quarterly basis", start=window.start, end=window.end,
        records=records, summary={"correlation": coefficient, "method": "Pearson", "n": len(records),
            "first_period": str(pairs.index[0]), "last_period": str(pairs.index[-1]),
            "common_start": records[0]["date"], "common_end": records[-1]["date"],
            "requested_start": window.start.isoformat(), "requested_end": window.end.isoformat(),
            "gdp_first": float(pairs.gdp_yoy.iloc[0]), "gdp_last": float(pairs.gdp_yoy.iloc[-1]),
            "cpi_first": float(pairs.cpi_yoy.iloc[0]), "cpi_last": float(pairs.cpi_yoy.iloc[-1])},
        formulas=["GDP YoY = 100 * (G[q] / G[q-4] - 1)",
            "Cbar[q] = arithmetic mean of the three valid monthly seasonally adjusted CPI index levels in quarter q",
            "Quarterly Core CPI YoY = 100 * (Cbar[q] / Cbar[q-4] - 1)",
            "Pearson r computed only on the exact inner-joined paired quarterly rows shown in the chart/data"],
        provenance=[provenance(gdp), provenance(cpi)], warnings=warnings,
        defaults=["Comparison uses shared quarterly YoY rates and at least eight paired quarters."])
