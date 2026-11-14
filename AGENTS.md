# FRED application: non-negotiable rules

This repository is one modular Python/Streamlit application, with `app.py` at its root and modules under `src/fred_chat/`. The destination is [Conversational-AI-for-Economic-Data-Analysis-Recommendation-Engine](https://github.com/michaelphamtransjsu/Conversational-AI-for-Economic-Data-Analysis-Recommendation-Engine). `docs/implementation_spec.md` is the supplied specification, not evidence of completed work.

## Economics

- Allow only GDP and CPILFESL. GDP is **nominal**, quarterly, billions of dollars at a seasonally adjusted annual rate (SAAR), not a quarterly dollar total or real GDP. CPILFESL is monthly, seasonally adjusted, index 1982–1984=100; an index level is not an inflation rate.
- Python owns all arithmetic. Default GDP growth and Core CPI inflation to YoY. GDP YoY uses q−4; QoQ q−1; annualized QoQ raises the ratio to 4. CPI YoY uses m−12 and MoM m−1. Rate differences are percentage points. Round only for presentation.
- Reindex to the complete native calendar before lagging. Never fill missing observations. Compute with pre-window history, then trim. Preserve nulls and distinguish unavailable history, zero denominators and empty ranges.
- Compare quarterly GDP YoY with YoY change of the arithmetic mean of **three valid monthly CPI levels** in each quarter. Inner-join valid quarters. Never average monthly inflation rates for this comparison or interpolate/repeat GDP monthly.
- Pearson correlation needs at least eight paired quarters and nonconstant inputs. State coefficient, count, exact common quarters and definition. Correlation establishes neither causality nor predictive power; nominal GDP contains price effects.
- Real GDP, CPI-deflated GDP, forecasts and historical-as-of vintages are outside default scope; explain rather than inventing a substitute.

## Evidence and application safety

- Preserve observation periods, metric, units, exact window, formulas, source URLs, retrieval UTC timestamp, source update fields and snapshot IDs in every numeric result. Observation dates are not publication dates.
- Immutable snapshots reproduce retrieved values, not arbitrary historical vintages. Retain older snapshots, checksums, raw responses without keys, and manifest metadata. Pin each series snapshot throughout an answer.
- Latest/current attempts refresh. Failed refresh may use a prior snapshot only with an explicit stale-data notice and original retrieval time. Never silently call fixture data live/current. Disclose a missing newest row when displaying an earlier valid period.
- Every fixture, transcript, chart, table and report using synthetic observations must say **DEMO / SYNTHETIC, not empirical findings**. Record only executed test and evaluation outcomes. Do not claim live API/model validation without running it.
- Typed allowlisted tools only. Never execute generated Python, SQL, shell, URLs or expressions from prompts. Reject invalid/incompatible metrics, series, dates and tool arguments. Bound model calls, tool calls, repairs, history and response sizes.
- Deterministic numeric tables/templates are authoritative. LLM text must be grounded in returned evidence; prevent unsupported numeric and causal claims. On model failure retain deterministic tools/dashboard with a clear notice.
- Secrets stay server-side in environment or ignored local configuration. Never log response/request URLs containing keys or include secrets in prompts, exports, snapshots, exceptions or version control.
- Use bounded HTTP timeouts and at most three attempts for transient failures; respect bounded Retry-After. Preserve existing snapshots when refresh fails.
- Maintain session-local context and evidence. Export only that session's conversation. Preserve source captions in CSV metadata/HTML/PNG and keep chart rows identical to correlation rows.

## Workflow

Implement and verify phases in order: foundation → data → economics → dashboard → conversation → submission. Run meaningful offline formula, missing-period, alignment, mocked-API, cache and conversation tests. Live smoke tests are explicit opt-in. For this implementation the user explicitly authorized committing changes and pushing to `main` of the destination repository above. Public deployment, merges and unrelated paid service use still require separate authorization.
