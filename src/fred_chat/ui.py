"""Streamlit evidence rendering. No credentials or model text are rendered here."""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import streamlit as st

from .schemas import Evidence


def render_result(evidence: Evidence, key: str, *, show_chart: bool = True, kind: str = "line"):
    from .charts import build_figure, caption, data_csv, figure_html, figure_png
    for warning in dict.fromkeys(evidence.warnings):
        if "STALE" in warning or "DEMO" in warning:
            st.warning(warning)
        else:
            st.caption(warning)
    if evidence.defaults:
        st.caption("Applied defaults: " + " ".join(evidence.defaults))
    if show_chart and evidence.records:
        try:
            st.plotly_chart(build_figure(evidence, kind), width="stretch", key=f"plot_{key}")
            st.caption(caption(evidence))
        except ValueError:
            st.info("This evidence contains different series definitions. Inspect the numeric table or choose a comparison chart.")
    with st.expander("Numeric table and evidence", expanded=not show_chart):
        st.dataframe(pd.DataFrame(evidence.records), width="stretch", hide_index=True)
        st.write("Calculation basis", evidence.formulas)
        st.write("Summary", evidence.summary)
        for source in evidence.provenance:
            st.markdown(f"[{source.get('series', 'FRED')} source definition]({source.get('source_url', '')})")
            st.caption(f"Snapshot {source.get('snapshot_id')} · Retrieved UTC {source.get('retrieved_at_utc')} · Source last updated {source.get('source_last_updated')}. Observation dates are not publication dates.")
    left, right = st.columns(2)
    left.download_button("Download data CSV", data_csv(evidence), file_name=f"{key}.csv", mime="text/csv", key=f"csv_{key}")
    right.download_button("Download evidence JSON", evidence.model_dump_json(indent=2), file_name=f"{key}_evidence.json", mime="application/json", key=f"json_{key}")
    if show_chart:
        try:
            st.download_button("Download interactive chart HTML", figure_html(evidence, kind), file_name=f"{key}.html", mime="text/html", key=f"html_{key}")
            signature = export_signature(evidence, kind)
            pngs = st.session_state.setdefault("chart_pngs", {})
            if st.button("Prepare static PNG", key=f"prepare_{key}"):
                with st.spinner("Rendering static chart…"):
                    try:
                        pngs[signature] = figure_png(evidence, kind)
                        while len(pngs) > 12:
                            del pngs[next(iter(pngs))]
                    except Exception:
                        st.error("PNG export is unavailable. Install Chromium for Kaleido; HTML and CSV remain available.")
            if signature in pngs:
                st.download_button("Download static PNG", pngs[signature], file_name=f"{key}.png", mime="image/png", key=f"png_{key}")
        except ValueError:
            pass


def export_signature(evidence: Evidence, kind: str) -> str:
    """Invalidate cached PNGs whenever data, window, caption or freshness changes."""
    payload = evidence.model_dump(mode="json", exclude={"result_id"})
    return hashlib.sha256(json.dumps({"evidence": payload, "kind": kind}, sort_keys=True).encode()).hexdigest()
