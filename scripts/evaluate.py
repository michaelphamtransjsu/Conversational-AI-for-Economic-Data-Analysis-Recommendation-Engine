"""Execute the seven questions, follow-ups and failures; write honest evidence."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

import pandas as pd

from fred_chat.cache import DataService
from fred_chat.charts import build_figure
from fred_chat.config import Settings, load_settings
from fred_chat.conversation import Conversation, RUBRIC_PROMPTS
from fred_chat.provider import OpenAIProvider
from fred_chat.schemas import SeriesID
from fred_chat.tools import ToolDispatcher
from fred_chat.windows import trailing_window

ROOT = Path(__file__).resolve().parents[1]


def independent_pairs(gdp, cpi, start, end):
    quarters = {pd.Period(row.date, freq="Q-DEC"): row.value for row in gdp.observations}
    monthly = {pd.Period(row.date, freq="M"): row.value for row in cpi.observations}
    means = {}
    for quarter in {period.asfreq("Q-DEC") for period in monthly}:
        months = pd.period_range(quarter.asfreq("M", "start"), quarter.asfreq("M", "end"), freq="M")
        values = [monthly.get(month) for month in months]
        if len(values) == 3 and all(value is not None and value > 0 for value in values):
            means[quarter] = sum(values) / 3
    pairs = []
    for quarter, gdp_value in sorted(quarters.items()):
        baseline = quarters.get(quarter - 4)
        cpi_value, cpi_baseline = means.get(quarter), means.get(quarter - 4)
        if start <= quarter.start_time.date() <= end and all(value is not None and value > 0 for value in (gdp_value, baseline, cpi_value, cpi_baseline)):
            pairs.append((str(quarter), 100 * (gdp_value / baseline - 1), 100 * (cpi_value / cpi_baseline - 1)))
    return pairs


def validate(index, evidence, service):
    assert evidence is not None, "No validated evidence returned"
    assert evidence.provenance and evidence.formulas and evidence.records
    if index == 0:
        valid = [row for row in service.get("GDP").snapshot.observations if row.value is not None]
        assert evidence.metric == "level"
        assert evidence.summary["value"] == valid[-1].value
        assert evidence.records[-1]["date"] == valid[-1].date.isoformat()
    elif index == 1:
        assert evidence.metric == "level"
        s = evidence.summary
        assert math.isclose(s["percent_change"], 100 * (s["last"] / s["first"] - 1), abs_tol=1e-12)
    elif index in {2, 3}:
        assert evidence.metric == "yoy"
        monthly = {pd.Period(row.date, freq="M"): row.value for row in service.get("CPILFESL").snapshot.observations}
        for row in evidence.records:
            if row["value"] is not None:
                period = pd.Period(row["period"], freq="M")
                expected = 100 * (monthly[period] / monthly[period-12] - 1)
                assert math.isclose(row["value"], expected, abs_tol=1e-10)
    elif index in {4, 5}:
        assert evidence.metric == "quarterly_yoy_comparison"
        expected = independent_pairs(service.get("GDP").snapshot, service.get("CPILFESL").snapshot, evidence.start, evidence.end)
        assert len(expected) == evidence.summary["n"] >= 8
        for row, (quarter, gdp, cpi) in zip(evidence.records, expected):
            assert row["period"] == quarter
            assert math.isclose(row["gdp_yoy"], gdp, abs_tol=1e-10)
            assert math.isclose(row["cpi_yoy"], cpi, abs_tol=1e-10)
        xs, ys = [row[1] for row in expected], [row[2] for row in expected]
        xbar, ybar = sum(xs)/len(xs), sum(ys)/len(ys)
        numerator = sum((x-xbar)*(y-ybar) for x,y in zip(xs,ys))
        denominator = math.sqrt(sum((x-xbar)**2 for x in xs) * sum((y-ybar)**2 for y in ys))
        assert math.isclose(evidence.summary["correlation"], numerator/denominator, abs_tol=1e-12)
        figure = build_figure(evidence)
        assert len(figure.data) == 2 and not figure.layout.to_plotly_json().get("yaxis2")
        for trace, key in zip(figure.data, ("gdp_yoy", "cpi_yoy")):
            plotted = [(x,y) for x,y in zip(trace.x, trace.y) if y is not None]
            assert plotted == [(row["date"], row[key]) for row in evidence.records]
    else:
        assert evidence.metric == "economic_summary"
        assert len(evidence.summary["components"]) == 2
        assert {component["series"][0] for component in evidence.summary["components"]} == {"GDP", "CPILFESL"}
        assert all(component["metric"] == "yoy" for component in evidence.summary["components"])


def test_summary():
    path = ROOT / "artifacts/test_results.xml"
    if not path.exists():
        return {"status": "NOT_RECORDED", "note": "Run pytest with --junitxml=artifacts/test_results.xml"}
    suites = ET.parse(path).getroot()
    nodes = list(suites) if suites.tag == "testsuites" else [suites]
    totals = {name: sum(int(node.attrib.get(name, 0)) for node in nodes) for name in ("tests", "failures", "errors", "skipped")}
    totals["passed"] = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    totals["status"] = "PASS" if totals["failures"] == totals["errors"] == 0 else "FAIL"
    totals["source"] = "artifacts/test_results.xml"
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Opt in to real FRED requests")
    parser.add_argument("--with-llm", action="store_true", help="With --live, opt in to configured OpenAI calls")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    configured = load_settings()
    output = args.output_dir or (ROOT / "work/live_evaluation" if args.live else ROOT / "docs")
    output.mkdir(parents=True, exist_ok=True)
    meta = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "mode": "LIVE" if args.live else "DEMO / SYNTHETIC — not empirical economic findings",
            "llm_requested": args.with_llm, "provider": "openai", "model_configuration": configured.llm_model,
            "tests": test_summary(), "scenarios": []}
    if args.with_llm and not args.live:
        parser.error("--with-llm requires --live; DEMO never calls the model")
    missing = [key for key, value in (("FRED_API_KEY", configured.fred_api_key), ("LLM_API_KEY", configured.llm_api_key)) if not value]
    if args.live and (not configured.fred_api_key or args.with_llm and not configured.llm_api_key):
        meta.update(status="BLOCKED", reason="Missing configured credentials: " + ", ".join(missing), live_requests_made=False)
        (output / "evaluation_results.json").write_text(json.dumps(meta, indent=2) + "\n")
        print(json.dumps(meta, indent=2))
        return 2
    settings = configured.model_copy(update={"demo_mode": not args.live})
    service = DataService(settings)
    provider = OpenAIProvider(settings) if args.with_llm else None
    evidence_dir = ROOT / "artifacts/evaluation" if not args.live else output / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    transcript = ["# Actual executed sample conversations", "", meta["mode"],
                  f"Executed UTC: {meta['generated_at_utc']}",
                  f"Interpreter: {'configured OpenAI structured tools' if provider else 'deterministic supported parser; no model calls'}. Model configuration: {settings.llm_model}.",
                  "These are application outputs captured by scripts/evaluate.py. Fixture timestamps identify generated snapshots, not live retrievals. Every numeric answer links to full evidence JSON."]

    def capture(label, conversation, prompt, assertion):
        turn = conversation.respond(prompt)
        passed, detail = True, "Validated against returned evidence and independent calendar calculations."
        try:
            assertion(turn)
        except Exception as exc:
            passed, detail = False, f"Validation failed ({type(exc).__name__}); inspect this scenario's evidence."
        filename = re_safe(label) + ".json"
        if turn.evidence:
            (evidence_dir / filename).write_text(turn.evidence.model_dump_json(indent=2) + "\n")
        result = {"scenario": label, "prompt": prompt, "status": "PASS" if passed else "FAIL", "detail": detail,
                  "result_id": turn.evidence.result_id if turn.evidence else None,
                  "snapshot_ids": [p["snapshot_id"] for p in turn.evidence.provenance] if turn.evidence else [],
                  "fresh_live_evidence": bool(turn.evidence and all(not p.get("demo") and not p.get("stale") for p in turn.evidence.provenance)),
                  "model_tools_succeeded": bool(provider and turn.evidence and not any("LLM unavailable" in notice for notice in turn.notices)),
                  "tool_calls": conversation.last_tool_calls, "model_calls": conversation.last_model_calls}
        meta["scenarios"].append(result)
        transcript.extend(["", "## " + label, "", "User: " + turn.prompt, "", "Application:", "", turn.answer,
                           "", "Automated scenario: **" + result["status"] + "**."])
        if turn.evidence:
            transcript.extend(["", f"Full evidence: `artifacts/evaluation/{filename}`; result `{turn.evidence.result_id}`."])
        return turn

    for index, prompt in enumerate(RUBRIC_PROMPTS):
        conversation = Conversation(ToolDispatcher(service), settings, provider=provider)
        capture(f"Rubric {index+1}", conversation, prompt, lambda turn, i=index: validate(i, turn.evidence, service))
    follow = Conversation(ToolDispatcher(service), settings, provider=provider)
    initial = follow.respond(RUBRIC_PROMPTS[1])
    # Include the actual initial turn so all follow-up context is visible.
    transcript.extend(["", "## Follow-up context", "", "User: " + initial.prompt, "", "Application:", "", initial.answer])
    narrowed = capture("Follow-up last five years", follow, "Use the last five years.",
                       lambda turn: require(turn.evidence is not None and turn.evidence.metric == "level" and turn.evidence.start == trailing_window(turn.evidence.end, 5)[0]))
    graph = capture("Follow-up graph", follow, "Now graph it.",
                    lambda turn: require(turn.evidence is not None and turn.evidence.records == narrowed.evidence.records and turn.evidence.provenance == narrowed.evidence.provenance))
    capture("Follow-up compare", follow, "Now compare it with inflation.",
            lambda turn: require(turn.evidence is not None and turn.evidence.metric == "quarterly_yoy_comparison" and turn.evidence.start == graph.evidence.start and turn.evidence.end == graph.evidence.end))
    capture("Unsupported real GDP", follow, "Show real GDP.", lambda turn: require(turn.evidence is None and "nominal" in turn.answer))
    (ROOT / "work").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "work") as unavailable:
        absent = Settings(cache_dir=Path(unavailable))
        failure = Conversation(ToolDispatcher(DataService(absent)), absent)
        capture("Missing credentials failure", failure, RUBRIC_PROMPTS[0], lambda turn: require(turn.evidence is None and "FRED_API_KEY is missing" in turn.answer))
    rubric = meta["scenarios"][:7]
    live_fred_validated = args.live and all(row["status"] == "PASS" and row["fresh_live_evidence"] for row in rubric)
    live_model_validated = bool(provider) and all(row["status"] == "PASS" and row["model_tools_succeeded"] for row in rubric)
    meta.update(status="PASS" if all(row["status"] == "PASS" for row in meta["scenarios"]) else "FAIL",
                live_fred_validated=live_fred_validated, live_model_validated=live_model_validated,
                snapshot_ids=sorted({identifier for row in meta["scenarios"] for identifier in row["snapshot_ids"]}))
    (output / "sample_conversations.md").write_text("\n".join(transcript) + "\n")
    (output / "evaluation_results.json").write_text(json.dumps(meta, indent=2) + "\n")
    rows = ["# Evaluation results", "", meta["mode"], "", f"Generated UTC: {meta['generated_at_utc']}",
            f"Evaluation status: **{meta['status']}**. Provider/model: openai / {settings.llm_model}; model calls {'enabled' if provider else 'not made'}. Live FRED validated: {live_fred_validated}. Live LLM validated: {live_model_validated}.",
            "", "## Executed tests", "", "```json", json.dumps(meta["tests"], indent=2), "```",
            "", "The final pytest console transcript is `artifacts/test_results.txt`. Known warnings concern the working pinned Kaleido 0.2.1 renderer. The opt-in live test is skipped without explicit enablement and credentials.",
            "", "## Seven rubric questions and follow-ups", "", "| Scenario | Actual status | Tool calls | Model calls |", "|---|---|---:|---:|"]
    rows += [f"| {row['scenario']} | {row['status']} | {row['tool_calls']} | {row['model_calls']} |" for row in meta["scenarios"]]
    rows += ["", "Acceptance checks cover native calendar baselines, independently rebuilt complete-quarter CPI means and Pearson correlation, exact plotted pairs, chart reuse, window context, and useful unsupported/missing-key failures. They do not measure unconstrained model quality or establish empirical economic findings.",
             "", "Snapshot IDs: " + ", ".join(meta["snapshot_ids"]), "", "## Export validation", "",
             "`scripts/export_demo.py` generates and reads back the three required PNG/HTML/CSV/evidence sets and full snapshots. `artifacts/export_index.json` records file checksums. The PNGs were visually inspected for readable, unclipped captions. Portable HTML embeds Plotly JavaScript. All output is synthetic.",
             "", "## Exact remaining live validations", "",
             "1. Configure FRED_API_KEY and allow HTTPS to api.stlouisfed.org. Run `DEMO_MODE=false python scripts/live_smoke.py --confirm-live`; verify both native metadata definitions, observation coverage, source update fields and latest valid rows.",
             "2. Run `DEMO_MODE=false python scripts/evaluate.py --live` to execute the seven questions and follow-ups on real FRED snapshots. Compare displayed GDP level, CPI YoY/MoM, calendar baselines, paired quarters and correlation with those saved raw responses.",
             "3. Configure LLM_API_KEY and a Chat Completions tool-capable LLM_MODEL; allow HTTPS to api.openai.com. Run `DEMO_MODE=false python scripts/evaluate.py --live --with-llm` and confirm the selected model accepts these strict tool schemas, retains follow-up context, and selects only supported evidence notes. This is the opt-in path that makes model service calls.",
             "4. In the live Streamlit UI, use Refresh twice and inspect distinct immutable snapshot IDs/checksums. Test a failed refresh after a successful live retrieval; verify STALE DATA, the unchanged prior retrieval timestamp, and regenerated stale-labeled exports. Do not use a synthetic snapshot as live fallback.",
             "5. Inspect/download live CSV, portable HTML, PNG and conversation Markdown; independently verify exact current snapshot values, definitions, periods and sources. No hard-coded current GDP/CPI value is a valid assertion.",
             "", "The implementation environment had neither FRED nor LLM credentials. No live service success or real economic finding is claimed. Live runs write under `work/live_evaluation/` by default, preserving this fixture-based submission record."]
    (output / "evaluation_results.md").write_text("\n".join(rows) + "\n")
    print(json.dumps({"status": meta["status"], "scenarios": len(meta["scenarios"]), "passed": sum(row["status"] == "PASS" for row in meta["scenarios"]), "mode": meta["mode"], "output": str(output)}, indent=2))
    return 0 if meta["status"] == "PASS" else 1


def require(condition):
    assert condition


def re_safe(label):
    return "_".join(label.lower().replace("-", " ").split())


if __name__ == "__main__":
    raise SystemExit(main())
