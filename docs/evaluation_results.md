# Evaluation results

DEMO / SYNTHETIC — not empirical economic findings

Generated UTC: 2026-11-14T18:03:06.702027+00:00
Evaluation status: **PASS**. Provider/model: openai / gpt-4.1-mini; model calls not made. Live FRED validated: False. Live LLM validated: False.

## Executed tests

```json
{
  "tests": 181,
  "failures": 0,
  "errors": 0,
  "skipped": 1,
  "passed": 180,
  "status": "PASS",
  "source": "artifacts/test_results.xml"
}
```

The final pytest console transcript is `artifacts/test_results.txt`. Known warnings concern the working pinned Kaleido 0.2.1 renderer. The opt-in live test is skipped without explicit enablement and credentials.

## Seven rubric questions and follow-ups

| Scenario | Actual status | Tool calls | Model calls |
|---|---|---:|---:|
| Rubric 1 | PASS | 1 | 0 |
| Rubric 2 | PASS | 1 | 0 |
| Rubric 3 | PASS | 1 | 0 |
| Rubric 4 | PASS | 1 | 0 |
| Rubric 5 | PASS | 1 | 0 |
| Rubric 6 | PASS | 1 | 0 |
| Rubric 7 | PASS | 1 | 0 |
| Follow-up last five years | PASS | 1 | 0 |
| Follow-up graph | PASS | 1 | 0 |
| Follow-up compare | PASS | 1 | 0 |
| Unsupported real GDP | PASS | 0 | 0 |
| Missing credentials failure | PASS | 1 | 0 |

Acceptance checks cover native calendar baselines, independently rebuilt complete-quarter CPI means and Pearson correlation, exact plotted pairs, chart reuse, window context, and useful unsupported/missing-key failures. They do not measure unconstrained model quality or establish empirical economic findings.

Snapshot IDs: DEMO-CPILFESL-v1, DEMO-GDP-v1

## Export validation

`scripts/export_demo.py` generates and reads back the three required PNG/HTML/CSV/evidence sets and full snapshots. `artifacts/export_index.json` records file checksums. The PNGs were visually inspected for readable, unclipped captions. Portable HTML embeds Plotly JavaScript. All output is synthetic.

## Exact remaining live validations

1. Configure FRED_API_KEY and allow HTTPS to api.stlouisfed.org. Run `DEMO_MODE=false python scripts/live_smoke.py --confirm-live`; verify both native metadata definitions, observation coverage, source update fields and latest valid rows.
2. Run `DEMO_MODE=false python scripts/evaluate.py --live` to execute the seven questions and follow-ups on real FRED snapshots. Compare displayed GDP level, CPI YoY/MoM, calendar baselines, paired quarters and correlation with those saved raw responses.
3. Configure LLM_API_KEY and a Chat Completions tool-capable LLM_MODEL; allow HTTPS to api.openai.com. Run `DEMO_MODE=false python scripts/evaluate.py --live --with-llm` and confirm the selected model accepts these strict tool schemas, retains follow-up context, and selects only supported evidence notes. This is the opt-in path that makes model service calls.
4. In the live Streamlit UI, use Refresh twice and inspect distinct immutable snapshot IDs/checksums. Test a failed refresh after a successful live retrieval; verify STALE DATA, the unchanged prior retrieval timestamp, and regenerated stale-labeled exports. Do not use a synthetic snapshot as live fallback.
5. Inspect/download live CSV, portable HTML, PNG and conversation Markdown; independently verify exact current snapshot values, definitions, periods and sources. No hard-coded current GDP/CPI value is a valid assertion.

The implementation environment had neither FRED nor LLM credentials. No live service success or real economic finding is claimed. Live runs write under `work/live_evaluation/` by default, preserving this fixture-based submission record.
