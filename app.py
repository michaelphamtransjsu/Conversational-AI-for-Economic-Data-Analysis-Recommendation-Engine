"""One local Streamlit application for evidence-grounded FRED analysis."""
from __future__ import annotations

from datetime import date

import streamlit as st
from pydantic import ValidationError

from fred_chat import analytics
from fred_chat.alignment import compare
from fred_chat.cache import DataService
from fred_chat.config import load_settings
from fred_chat.conversation import Conversation, RUBRIC_PROMPTS
from fred_chat.fred_client import DataError
from fred_chat.schemas import Metric, SeriesID
from fred_chat.ui import render_result
from fred_chat.windows import observation_end, trailing_window
from fred_chat.tools import ToolDispatcher
from fred_chat.provider import OpenAIProvider

st.set_page_config(page_title="FRED Economic Conversation", page_icon="📈", layout="wide")
st.title("FRED Economic Conversation")
st.caption("Nominal GDP and seasonally adjusted Core CPI • dates, definitions and evidence in every result")

try:
    configured = load_settings()
except ValidationError:
    st.error("Invalid server configuration. Check the documented configuration names and bounds; credentials must stay server-side.")
    st.stop()

with st.sidebar:
    st.header("Analysis controls")
    demo = st.toggle("Use synthetic demo fixtures", value=configured.demo_mode)
    settings = configured.model_copy(update={"demo_mode": demo})
    if demo:
        st.warning("DEMO / SYNTHETIC — not empirical economic findings. No live API or model calls.")
    else:
        st.caption("FRED snapshots • six-hour default cache • latest requests attempt refresh")
    refresh = st.button("Refresh data snapshots", width="stretch")
    st.caption("Refresh preserves older snapshots. A failed refresh displays stale data only with an explicit notice.")

for message in settings.configuration_messages:
    if "DEMO" not in message:
        st.info(message)

# These objects are deliberately session-local; no globally cached chat/evidence state.
if st.session_state.get("runtime_settings") != settings:
    st.session_state["runtime_settings"] = settings
    st.session_state["data_service"] = DataService(settings)
    st.session_state.pop("conversation", None)
    st.session_state["chart_pngs"] = {}
service = st.session_state["data_service"]

snapshots = {}
data_results = {}
for series in SeriesID:
    try:
        result = service.get(series, refresh=refresh)
        data_results[series] = result
        snapshots[series] = result.snapshot
        for warning in result.warnings:
            if "STALE" in warning:
                st.warning(warning)
            elif "persisting" in warning or "integrity" in warning or "corrupt" in warning.lower():
                st.info(warning)
    except DataError as exc:
        st.error(f"{series.value}: {exc}")

if not snapshots:
    st.info("Configure FRED_API_KEY or enable the explicitly labeled synthetic demo in the sidebar to explore the application.")
    st.stop()

if demo:
    st.warning("DEMO / SYNTHETIC — all displayed values are generated fixtures, not empirical economic findings.")

left, right = st.columns(2)
for series, column in ((SeriesID.GDP, left), (SeriesID.CPILFESL, right)):
    if series not in snapshots:
        continue
    metric = Metric.level if series == SeriesID.GDP else Metric.yoy
    try:
        evidence = analytics.latest(snapshots[series], metric)
        with column:
            value = evidence.summary["value"]
            st.metric("Latest snapshot nominal GDP" if series == SeriesID.GDP else "Latest snapshot Core CPI YoY", f"{value:,.2f}" + ("%" if series == SeriesID.CPILFESL else ""))
            st.caption(f"{evidence.summary['period']} · {evidence.units}")
            st.caption(f"Snapshot {snapshots[series].snapshot_id} · retrieved UTC {snapshots[series].retrieved_at.isoformat()}")
            st.markdown(f"[FRED {series.value} definition]({snapshots[series].metadata.source_url})")
            st.caption(evidence.formulas[0])
            if series == SeriesID.CPILFESL and evidence.summary.get("mom_value") is not None:
                st.caption(f"Secondary MoM: {evidence.summary['mom_value']:.3f}% in {evidence.summary['mom_period']}.")
            if data_results[series].stale:
                st.warning("STALE DATA: the last refresh failed; this card uses the prior retrieval.")
            for warning in evidence.warnings:
                if "newest" in warning.lower() or "earlier" in warning.lower() or "computable" in warning.lower():
                    st.warning(warning)
    except analytics.AnalyticsError as exc:
        column.warning(str(exc))

# A window ends at the final observation's calendar-period end, never at a release date.
ends = [observation_end(snap) for snap in snapshots.values()]
anchor = min(ends)
start_default, _ = trailing_window(anchor, 10)
with st.sidebar:
    start = st.date_input("Window start (observation dates)", start_default, min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
    end = st.date_input("Window end (observation dates)", anchor, min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
    selected_series = st.selectbox("History series", [series.value for series in snapshots])
    choices = ["level", "yoy", "qoq", "qoq_annualized"] if selected_series == "GDP" else ["yoy", "mom", "level"]
    selected_metric = st.selectbox("History metric", choices)
    st.caption("GDP growth and Core CPI inflation default to YoY. GDP level charts use billions of dollars at SAAR. Rate changes use percentage points.")

if start > end:
    st.error("Window start must be on or before window end.")
    st.stop()

history_tab, comparison_tab = st.tabs(["History dashboard", "Quarterly comparison"])
with history_tab:
    try:
        evidence = analytics.history(snapshots[SeriesID(selected_series)], selected_metric, start, end)
        evidence.warnings += data_results[SeriesID(selected_series)].warnings
        render_result(evidence, "dashboard_history")
    except (analytics.AnalyticsError, ValidationError) as exc:
        st.warning(str(exc))
with comparison_tab:
    if len(snapshots) < 2:
        st.info("Both GDP and CPILFESL snapshots are needed for a comparison.")
    else:
        try:
            evidence = compare(snapshots[SeriesID.GDP], snapshots[SeriesID.CPILFESL], start, end)
            for result in data_results.values():
                evidence.warnings += result.warnings
            s = evidence.summary
            st.write(f"Pearson r = {s['correlation']:.4f} across {s['n']} paired quarters, {s['first_period']}–{s['last_period']}. Association does not establish causality.")
            scatter = st.checkbox("Show paired-rate scatter plot")
            render_result(evidence, "dashboard_comparison", kind="scatter" if scatter else "line")
        except (analytics.AnalyticsError, ValidationError) as exc:
            st.warning(str(exc))

st.divider()
st.subheader("Conversation")
if "conversation" not in st.session_state:
    provider = OpenAIProvider(settings) if settings.llm_api_key and not demo else None
    st.session_state["conversation"] = Conversation(ToolDispatcher(service), settings, provider=provider)
conversation = st.session_state["conversation"]
conversation.default_window = (start, end)
st.caption("Ask a question, then follow up with ‘use the last five years’, ‘now graph it’, or ‘now compare it with inflation’. Answers show applied defaults and snapshot provenance.")
prompt = None
with st.expander("Seven question shortcuts", expanded=not conversation.turns):
    columns = st.columns(2)
    for index, question in enumerate(RUBRIC_PROMPTS):
        if columns[index % 2].button(question, key=f"rubric_{index}", width="stretch"):
            prompt = question
for turn in conversation.turns:
    with st.chat_message("user"):
        st.write(turn.prompt)
    with st.chat_message("assistant"):
        st.markdown(turn.answer)
typed = st.chat_input("Ask about nominal GDP or Core CPI", max_chars=2000)
if typed:
    prompt = typed
if prompt:
    with st.spinner("Building validated economic evidence…"):
        conversation.respond(prompt)
    st.rerun()

available = [index for index, turn in enumerate(conversation.turns) if turn.evidence is not None]
if available:
    selected = st.selectbox("Inspect conversation evidence", available, index=len(available)-1,
                            format_func=lambda index: f"Turn {index+1}: {conversation.turns[index].prompt[:80]}")
    turn = conversation.turns[selected]
    render_result(turn.evidence, f"chat_{selected}_{turn.evidence.result_id}", show_chart=bool(turn.chart_kind), kind=turn.chart_kind or "line")
if conversation.turns:
    st.download_button("Download session conversation Markdown", conversation.export_markdown(), file_name="fred_conversation.md", mime="text/markdown")
    if st.button("Clear this session's conversation"):
        del st.session_state["conversation"]
        st.rerun()
