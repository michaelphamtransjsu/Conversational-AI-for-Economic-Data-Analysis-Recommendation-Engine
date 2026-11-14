"""Allowlisted Plotly templates and self-contained, evidence-preserving exports."""
from __future__ import annotations

from html import escape
import json
import textwrap

import pandas as pd
import plotly.graph_objects as go

from .schemas import ChartKind, Evidence, SeriesID


class ChartError(ValueError):
    """The selected evidence cannot be represented by an approved chart."""


def _is_comparison(evidence: Evidence) -> bool:
    return evidence.metric == "quarterly_yoy_comparison"


def _demo(evidence: Evidence) -> bool:
    return any(p.get("demo", False) for p in evidence.provenance) or any("SYNTHETIC" in w.upper() for w in evidence.warnings)


def _stale(evidence: Evidence) -> bool:
    return any(p.get("stale", False) for p in evidence.provenance) or any("stale" in w.lower() for w in evidence.warnings)


def caption(evidence: Evidence) -> str:
    """Full numeric definition and provenance, retained in every export format."""
    mode = "DEMO / SYNTHETIC, not empirical findings" if _demo(evidence) else "FRED retrieved snapshot"
    if _stale(evidence):
        mode += "; STALE DATA: refresh failed or snapshot expired"
    valid = ([r for r in evidence.records if r.get("gdp_yoy") is not None and r.get("cpi_yoy") is not None]
             if _is_comparison(evidence) else [r for r in evidence.records if r.get("value") is not None])
    coverage = f"{valid[0].get('period', valid[0].get('date'))} to {valid[-1].get('period', valid[-1].get('date'))}" if valid else "no valid observations"
    parts = [mode, f"Metric: {evidence.metric}; units: {evidence.units}",
             f"Requested window: {evidence.start} to {evidence.end}; valid observation coverage: {coverage}; n={len(valid)}"]
    if _is_comparison(evidence):
        parts.append("Quarterly Core CPI is YoY change of complete three-month quarterly-average seasonally adjusted index levels, not average monthly YoY rates")
        if "correlation" in evidence.summary:
            parts.append(f"Pearson r={evidence.summary['correlation']:.6f}; paired quarters={evidence.summary.get('n', len(valid))}")
    parts.append("Formulas: " + "; ".join(evidence.formulas))
    for source in evidence.provenance:
        urls = source.get("source_urls") or [source.get("source_url", "unavailable")]
        if source.get("source_url") and source["source_url"] not in urls:
            urls = [source["source_url"], *urls]
        parts.append(f"Source {source.get('series', '')}: {', '.join(urls)}; snapshot {source.get('snapshot_id', 'unavailable')}; retrieved UTC {source.get('retrieved_at_utc', source.get('retrieved_at', 'unavailable'))}; source last updated {source.get('source_last_updated', 'unavailable')} (not a release date)")
    if evidence.warnings:
        parts.append("Limitations: " + " ".join(evidence.warnings))
    return ". ".join(parts) + "."


def _frame(evidence: Evidence) -> pd.DataFrame:
    if not evidence.records:
        raise ChartError("This result contains no observation rows to chart or export.")
    frame = pd.DataFrame(evidence.records)
    required = {"date", "period", "gdp_yoy", "cpi_yoy"} if _is_comparison(evidence) else {"date", "period", "value"}
    if not required.issubset(frame.columns):
        raise ChartError("This result combines metrics with different units. Chart each indicator separately.")
    return frame


def _title(evidence: Evidence) -> str:
    if _is_comparison(evidence):
        title = "Quarterly nominal GDP growth and Core CPI inflation · YoY (%)"
    elif evidence.series == [SeriesID.GDP]:
        labels = {"level": "Nominal GDP level · billions of dollars at SAAR", "yoy": "Nominal GDP year-over-year growth (%)",
                  "qoq": "Nominal GDP quarter-over-quarter growth (%)", "qoq_annualized": "Nominal GDP annualized quarter-over-quarter growth (%)"}
        title = labels.get(evidence.metric, "Nominal GDP")
    elif evidence.series == [SeriesID.CPILFESL]:
        labels = {"level": "Core CPI index · 1982–1984=100 · seasonally adjusted", "yoy": "Core CPI year-over-year inflation (%) · seasonally adjusted",
                  "mom": "Core CPI month-over-month inflation (%) · seasonally adjusted"}
        title = labels.get(evidence.metric, "Seasonally adjusted Core CPI")
    else:
        raise ChartError("Chart each indicator separately to preserve metric units.")
    return ("DEMO / SYNTHETIC — " if _demo(evidence) else "") + title


def build_figure(evidence: Evidence, kind: ChartKind | str = "line") -> go.Figure:
    """Render exact evidence rows. Null drawing rows preserve comparison gaps."""
    try:
        kind = ChartKind(kind)
    except ValueError as exc:
        raise ChartError("Only line and paired-rate scatter charts are supported.") from exc
    frame = _frame(evidence)
    comparison = _is_comparison(evidence)
    if kind == ChartKind.scatter and not comparison:
        raise ChartError("Scatter requires paired quarterly GDP and Core CPI rate evidence.")
    fig = go.Figure()
    if kind == ChartKind.scatter:
        fig.add_trace(go.Scatter(x=frame.gdp_yoy.tolist(), y=frame.cpi_yoy.tolist(),
            customdata=frame.period.tolist(), mode="markers", name="Paired valid quarters",
            marker={"color": "#07877d", "size": 9},
            hovertemplate="%{customdata}<br>Nominal GDP YoY: %{x:.3f}%<br>Core CPI YoY: %{y:.3f}%<extra></extra>"))
        fig.update_xaxes(title_text="Nominal GDP YoY growth (%)")
        fig.update_yaxes(title_text="Quarterly-average Core CPI YoY inflation (%)")
    else:
        if comparison:
            # Rates/correlation remain the original finite paired rows. Null rows
            # are drawing instructions only, preventing lines across excluded quarters.
            indexed = frame.assign(_quarter=pd.PeriodIndex(frame.period, freq="Q-DEC")).set_index("_quarter")
            drawing = indexed.reindex(pd.period_range(indexed.index.min(), indexed.index.max(), freq="Q-DEC"))
            x = [period.start_time.date().isoformat() for period in drawing.index]
            periods = drawing.index.astype(str).tolist()
            for column, name, color in [("gdp_yoy", "Nominal GDP YoY", "#1e5ca8"), ("cpi_yoy", "Quarterly-average Core CPI YoY", "#bc5c15")]:
                y = [None if pd.isna(v) else float(v) for v in drawing[column]]
                fig.add_trace(go.Scatter(x=x, y=y, customdata=periods, mode="lines+markers", name=name,
                    line={"color": color, "width": 2.4}, marker={"size": 4}, connectgaps=False,
                    hovertemplate="%{customdata}<br>%{y:.3f}%<extra>%{fullData.name}</extra>"))
            fig.update_yaxes(title_text="Year-over-year change (%)")
        else:
            y = [None if pd.isna(v) else float(v) for v in frame.value]
            fig.add_trace(go.Scatter(x=frame.date.tolist(), y=y, customdata=frame.period.tolist(),
                mode="lines+markers", name="Nominal GDP" if evidence.series == [SeriesID.GDP] else "Core CPI",
                line={"color": "#1e5ca8", "width": 2.4}, marker={"size": 4}, connectgaps=False,
                hovertemplate="%{customdata}<br>%{y:.4f}<extra>%{fullData.name}</extra>"))
            fig.update_yaxes(title_text=evidence.units)
        fig.update_xaxes(title_text="Observation period (not publication date)", type="date")
    fig.update_layout(title={"text": escape(_title(evidence)), "font": {"size": 19}},
        template="plotly_white", height=550, margin={"l": 70, "r": 35, "t": 100, "b": 75},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "x": 0}, hovermode="closest")
    return fig


def data_csv(evidence: Evidence) -> bytes:
    """Every observation row carries the result's reproducibility metadata."""
    frame = _frame(evidence).copy()
    frame["metric"] = evidence.metric
    frame["metric_units"] = evidence.units
    frame["requested_start"] = str(evidence.start)
    frame["requested_end"] = str(evidence.end)
    frame["formulas"] = "; ".join(evidence.formulas)
    frame["data_mode"] = "DEMO / SYNTHETIC, not empirical findings" if _demo(evidence) else "FRED retrieved snapshot"
    frame["stale"] = _stale(evidence)
    frame["provenance_json"] = json.dumps(evidence.provenance, ensure_ascii=False, sort_keys=True)
    frame["limitations"] = " ".join(evidence.warnings)
    return frame.to_csv(index=False).encode("utf-8")


def figure_html(evidence: Evidence, kind: ChartKind | str = "line") -> bytes:
    fig = build_figure(evidence, kind)
    chart = fig.to_html(full_html=False, include_plotlyjs=True, config={"displaylogo": False, "responsive": True})
    document = f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(_title(evidence))}</title><style>body{{font-family:system-ui,sans-serif;max-width:1300px;margin:2rem auto;padding:0 1rem}}figcaption{{font-size:14px;line-height:1.6;overflow-wrap:anywhere}}</style></head><body><figure>{chart}<figcaption>{escape(caption(evidence))}</figcaption></figure></body></html>'
    return document.encode("utf-8")


def figure_png(evidence: Evidence, kind: ChartKind | str = "line") -> bytes:
    """A static chart with readable, wrapped full evidence caption underneath."""
    fig = build_figure(evidence, kind)
    lines = textwrap.wrap(caption(evidence), width=145, break_long_words=True)
    # Reserve the axis-label gap as well as caption text. Without this extra
    # space shorter captions can be clipped even though longer ones fit.
    footer_height = 20 * len(lines) + 105
    height = 570 + footer_height
    fig.update_layout(width=1400, height=height, margin={"l": 80, "r": 45, "t": 105, "b": footer_height},
        annotations=[{"text": "<br>".join(escape(line) for line in lines), "xref": "paper", "yref": "paper",
            "x": 0, "y": 0, "xanchor": "left", "yanchor": "top", "yshift": -70,
            "showarrow": False, "align": "left", "font": {"size": 12, "color": "#2d3748"}}])
    try:
        return fig.to_image(format="png", scale=1)
    except Exception as exc:
        raise ChartError("PNG export is unavailable. Install the pinned Kaleido dependency and a compatible Chrome/Chromium browser; HTML and CSV exports remain available.") from exc
