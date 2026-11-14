"""Deterministic economic transformations; all lags use a complete calendar."""
from __future__ import annotations

from datetime import date, timezone
from uuid import uuid4

import numpy as np
import pandas as pd

from .schemas import DateWindow, Evidence, Metric, SeriesID, SeriesRequest, Snapshot


class AnalyticsError(ValueError):
    """A useful, secret-free explanation of unavailable economic evidence."""


FORMULAS = {
    (SeriesID.GDP, Metric.level): "G[q]: nominal GDP level, billions of dollars at seasonally adjusted annual rate (SAAR)",
    (SeriesID.GDP, Metric.yoy): "100 * (G[q] / G[q-4] - 1)",
    (SeriesID.GDP, Metric.qoq): "100 * (G[q] / G[q-1] - 1)",
    (SeriesID.GDP, Metric.qoq_annualized): "100 * ((G[q] / G[q-1])**4 - 1)",
    (SeriesID.CPILFESL, Metric.level): "C[m]: seasonally adjusted Core CPI index, 1982–1984=100",
    (SeriesID.CPILFESL, Metric.yoy): "100 * (C[m] / C[m-12] - 1)",
    (SeriesID.CPILFESL, Metric.mom): "100 * (C[m] / C[m-1] - 1)",
}


def resolved_metric(series: SeriesID, metric: Metric | str | None) -> Metric:
    request = SeriesRequest(series=series, metric=metric)
    return request.metric or Metric.yoy


def native_series(snapshot: Snapshot) -> pd.Series:
    """Sort and reindex to calendar quarters/months, retaining null gaps."""
    freq = "Q-DEC" if snapshot.series == SeriesID.GDP else "M"
    if not snapshot.observations:
        raise AnalyticsError("The snapshot contains no observations.")
    dates = pd.DatetimeIndex([row.date for row in snapshot.observations])
    periods = dates.to_period(freq)
    if periods.has_duplicates:
        raise AnalyticsError("Duplicate calendar periods cannot be analyzed.")
    # FRED native observations are dated on the first day of their period.
    if any(stamp.date() != period.start_time.date() for stamp, period in zip(dates, periods)):
        raise AnalyticsError("Observation dates must be native calendar-period starts.")
    values = pd.Series([np.nan if row.value is None else row.value for row in snapshot.observations],
                       index=periods, dtype=float, name=snapshot.series.value).sort_index()
    return values.reindex(pd.period_range(values.index.min(), values.index.max(), freq=freq))


def calendar_change(values: pd.Series, lag: int, annualize: int = 1) -> pd.Series:
    """No fill; nonpositive denominators/levels produce unavailable rates."""
    baseline = values.shift(lag)
    valid = (baseline > 0) & (values > 0)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        rates = 100 * ((values / baseline) ** annualize - 1)
    return rates.where(valid & np.isfinite(rates))


def transform(snapshot: Snapshot, metric: Metric | str | None = None) -> pd.Series:
    metric = resolved_metric(snapshot.series, metric)
    values = native_series(snapshot)
    if metric == Metric.level:
        return values
    lag = 4 if snapshot.series == SeriesID.GDP and metric == Metric.yoy else 12 if metric == Metric.yoy else 1
    return calendar_change(values, lag, 4 if metric == Metric.qoq_annualized else 1)


def provenance(snapshot: Snapshot) -> dict:
    dates = sorted(row.date for row in snapshot.observations)
    return {
        "snapshot_id": snapshot.snapshot_id, "series": snapshot.series.value,
        "title": snapshot.metadata.title, "frequency": snapshot.metadata.frequency,
        "native_units": snapshot.metadata.units,
        "seasonal_adjustment": snapshot.metadata.seasonal_adjustment,
        "source_url": snapshot.metadata.source_url,
        "source_urls": snapshot.source_urls,
        "retrieved_at_utc": snapshot.retrieved_at.astimezone(timezone.utc).isoformat(),
        "source_last_updated": snapshot.metadata.last_updated,
        "observation_start": dates[0].isoformat() if dates else None,
        "observation_end": dates[-1].isoformat() if dates else None,
        "realtime_parameters": snapshot.realtime_parameters,
        "demo": snapshot.demo,
        "provenance_note": "Synthetic fixture timestamp; not a live retrieval" if snapshot.demo else "Observation periods are not publication dates; snapshot values may be revised.",
    }


def _warnings(snapshot: Snapshot, values: pd.Series) -> list[str]:
    warnings = []
    if snapshot.demo:
        warnings.append("DEMO / SYNTHETIC — not empirical economic findings. No live retrieval occurred.")
    if values.isna().any():
        warnings.append("Missing calendar observations remain missing; no interpolation or forward fill is used.")
    if (values.dropna() <= 0).any():
        warnings.append("Nonpositive index/level values cannot produce percentage-change rates; affected rates are unavailable.")
    warnings.append("GDP is nominal, at SAAR; it is not real GDP or the actual dollar total for a quarter." if snapshot.series == SeriesID.GDP else
                    "Core CPI changes use the seasonally adjusted CPILFESL index; they need not exactly match every published BLS headline figure.")
    return warnings


def metric_units(snapshot: Snapshot, metric: Metric) -> str:
    if metric == Metric.level:
        return "billions of dollars, seasonally adjusted annual rate (SAAR)" if snapshot.series == SeriesID.GDP else "index, 1982–1984=100, seasonally adjusted"
    return "percent, " + {Metric.yoy: "year-over-year", Metric.qoq: "quarter-over-quarter", Metric.qoq_annualized: "annualized quarter-over-quarter", Metric.mom: "month-over-month"}[metric]


def _scalar(value):
    return None if pd.isna(value) or not np.isfinite(value) else float(value)


def _records(values: pd.Series, levels: pd.Series, baseline: pd.Series | None = None) -> list[dict]:
    rows = []
    for period, value in values.items():
        record = {"date": period.start_time.date().isoformat(), "period": str(period), "value": _scalar(value), "level": _scalar(levels.loc[period])}
        if baseline is not None:
            record["baseline"] = _scalar(baseline.loc[period])
        rows.append(record)
    return rows


def _summary(values: pd.Series, metric: Metric) -> dict:
    valid = values.dropna()
    if valid.empty:
        raise AnalyticsError("No valid metric values are available. Check missing observations, baseline history and positive denominators.")
    first, last = float(valid.iloc[0]), float(valid.iloc[-1])
    summary = {
        "first": first, "last": last, "first_period": str(valid.index[0]),
        "last_period": str(valid.index[-1]), "n": int(valid.size),
        "min": float(valid.min()), "max": float(valid.max()),
        "min_period": str(valid.idxmin()), "max_period": str(valid.idxmax()),
        "direction": "increased" if last > first else "decreased" if last < first else "unchanged",
    }
    if metric == Metric.level:
        summary.update(absolute_change=last-first, percent_change=100*(last/first-1) if first > 0 else None)
    else:
        summary["change_pp"] = last-first
    return summary


def _baseline(levels: pd.Series, series: SeriesID, metric: Metric) -> pd.Series | None:
    if metric == Metric.level:
        return None
    lag = 4 if metric == Metric.yoy and series == SeriesID.GDP else 12 if metric == Metric.yoy else 1
    return levels.shift(lag)


def history(snapshot: Snapshot, metric: Metric | str | None, start: date | str, end: date | str) -> Evidence:
    window = DateWindow(start=start, end=end)
    resolved = resolved_metric(snapshot.series, metric)
    levels = native_series(snapshot)
    full = transform(snapshot, resolved)
    mask = (full.index.to_timestamp().date >= window.start) & (full.index.to_timestamp().date <= window.end)
    values = full.loc[mask]
    if values.empty:
        raise AnalyticsError("The requested date range contains no observation periods.")
    summary = _summary(values, resolved)
    summary.update(requested_start=window.start.isoformat(), requested_end=window.end.isoformat(), missing_periods=int(values.isna().sum()))
    formulas = [FORMULAS[(snapshot.series, resolved)]]
    formulas.append("Interval level change = last - first; percent change = 100 * (last / first - 1)" if resolved == Metric.level else "Rate change in percentage points = last rate - first rate")
    warnings = _warnings(snapshot, levels)
    if values.isna().any():
        warnings.append("Some requested metric periods are unavailable because current observations or required calendar baselines are missing; chart gaps are preserved.")
    return Evidence(result_id="result_"+uuid4().hex, operation="analyze_history", series=[snapshot.series], metric=resolved.value,
        units=metric_units(snapshot, resolved), start=window.start, end=window.end,
        records=_records(values, levels, _baseline(levels, snapshot.series, resolved)), summary=summary,
        formulas=formulas, provenance=[provenance(snapshot)], warnings=warnings,
        defaults=["Unspecified growth/inflation defaults to YoY."] if metric is None else [])


def latest(snapshot: Snapshot, metric: Metric | str | None = None) -> Evidence:
    resolved = resolved_metric(snapshot.series, metric)
    levels = native_series(snapshot)
    values = transform(snapshot, resolved)
    valid = values.dropna()
    if valid.empty:
        raise AnalyticsError("No valid latest metric is available; observations or required baseline history are missing.")
    period, value = valid.index[-1], float(valid.iloc[-1])
    warnings = _warnings(snapshot, levels)
    if pd.isna(levels.iloc[-1]):
        warnings.append(f"Newest dated row {levels.index[-1]} is missing; the displayed valid period is earlier.")
    if period != levels.index[-1]:
        warnings.append(f"Latest computable {resolved.value} is {period}; newest dated observation is {levels.index[-1]}. Missing current values or baselines prevent a later rate.")
    summary = {"value": value, "period": str(period), "n": 1, "newest_dated_period": str(levels.index[-1]),
               "latest_valid_level_period": str(levels.dropna().index[-1]) if not levels.dropna().empty else None,
               "observation_date": period.start_time.date().isoformat()}
    formulas = [FORMULAS[(snapshot.series, resolved)]]
    if snapshot.series == SeriesID.CPILFESL and resolved == Metric.yoy:
        mom = transform(snapshot, Metric.mom)
        summary.update(mom_value=_scalar(mom.loc[period]), mom_period=str(period))
        formulas.append(FORMULAS[(snapshot.series, Metric.mom)])
        if summary["mom_value"] is None:
            warnings.append("Secondary MoM is unavailable at the displayed YoY period because its preceding month is missing or nonpositive.")
    return Evidence(result_id="result_"+uuid4().hex, operation="latest_indicator", series=[snapshot.series], metric=resolved.value,
        units=metric_units(snapshot, resolved), start=period.start_time.date(), end=period.start_time.date(),
        records=_records(values.loc[[period]], levels, _baseline(levels, snapshot.series, resolved)), summary=summary,
        formulas=formulas, provenance=[provenance(snapshot)], warnings=warnings,
        defaults=["Unspecified growth/inflation defaults to YoY."] if metric is None else [])
