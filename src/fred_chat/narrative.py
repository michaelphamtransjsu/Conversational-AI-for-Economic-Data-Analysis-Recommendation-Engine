"""Authoritative deterministic numeric answers, also used in conversation exports."""
from __future__ import annotations

from .schemas import Evidence, SeriesID


def _number(value, digits=3) -> str:
    return "unavailable" if value is None else f"{value:,.{digits}f}"


def numeric_answer(evidence: Evidence) -> str:
    s = evidence.summary
    if evidence.operation == "summarize_economy" or "components" in s:
        return "\n\n".join(numeric_answer(Evidence.model_validate(component)) for component in s["components"])
    if evidence.metric == "quarterly_yoy_comparison":
        return (f"Pearson r = **{s['correlation']:.4f}** for **{s['n']} paired quarters**, "
                f"{s['first_period']} through {s['last_period']}. Nominal GDP YoY was {_number(s['gdp_first'])}% at the first paired quarter "
                f"and {_number(s['gdp_last'])}% at the last; quarterly-average Core CPI YoY was {_number(s['cpi_first'])}% and {_number(s['cpi_last'])}%. "
                "This is an association within the selected window; it establishes neither causality nor predictive power. "
                "Core CPI here is the YoY change of complete three-month quarterly-average index levels.")
    label = "Nominal GDP" if evidence.series == [SeriesID.GDP] else "Seasonally adjusted Core CPI"
    metric = {"level": "level", "yoy": "YoY growth" if evidence.series == [SeriesID.GDP] else "YoY inflation",
              "qoq": "QoQ growth", "qoq_annualized": "annualized QoQ growth", "mom": "MoM inflation"}.get(evidence.metric, evidence.metric)
    if "value" in s:
        answer = f"{label} {metric}: **{_number(s['value'])} {evidence.units}**, observation period **{s['period']}**."
        if "mom_value" in s:
            answer += (f" Secondary MoM: {_number(s['mom_value'])}% in {s['mom_period']}." if s['mom_value'] is not None else " Secondary MoM is unavailable at the displayed YoY period.")
        return answer
    answer = (f"{label} {metric} moved from **{_number(s['first'])}** in **{s['first_period']}** "
              f"to **{_number(s['last'])}** in **{s['last_period']}** ({evidence.units}; {s['n']} valid periods). ")
    if evidence.metric == "level":
        answer += f"The endpoint level change was {_number(s['absolute_change'])} {evidence.units}. "
        answer += f"The endpoint percentage change was {_number(s['percent_change'])}%. " if s['percent_change'] is not None else "The endpoint percentage change is unavailable because the initial level is nonpositive. "
    else:
        answer += f"The endpoint rate change was {_number(s['change_pp'])} percentage points. "
    answer += (f"The minimum was {_number(s['min'])} in {s['min_period']}; "
               f"the maximum was {_number(s['max'])} in {s['max_period']}. These describe the selected observations.")
    return answer


def render_answer(evidence: Evidence) -> str:
    parts = [numeric_answer(evidence),
             f"Metric: `{evidence.metric}`. Units: {evidence.units}. Requested observation-date window: {evidence.start} to {evidence.end}.",
             "Calculation basis: " + "; ".join(evidence.formulas) + "."]
    if evidence.defaults:
        parts.append("Applied defaults: " + " ".join(evidence.defaults))
    for source in evidence.provenance:
        parts.append(f"Source definition: [{source.get('series', 'FRED')}]({source.get('source_url', '')}). "
                     f"Snapshot `{source.get('snapshot_id')}`; retrieved UTC {source.get('retrieved_at_utc')}; "
                     f"source last updated {source.get('source_last_updated')} (not an observation period or release date).")
    if evidence.warnings:
        parts.append("Limitations: " + " ".join(dict.fromkeys(evidence.warnings)))
    return "\n\n".join(parts)
