# FRED Economic Conversation

A single modular Streamlit application for **nominal GDP** and **seasonally adjusted Core CPI (CPILFESL)**. Python calculates every number. Typed tools and a bounded OpenAI structured-tool adapter interpret requests; deterministic evidence templates own the displayed answers.

Repository: [Conversational-AI-for-Economic-Data-Analysis-Recommendation-Engine](https://github.com/michaelphamtransjsu/Conversational-AI-for-Economic-Data-Analysis-Recommendation-Engine). The Streamlit entry point is [app.py at the repository root](https://github.com/michaelphamtransjsu/Conversational-AI-for-Economic-Data-Analysis-Recommendation-Engine/blob/main/app.py).

## Install and start

Python 3.11+ is required; the recorded offline implementation was validated on Python 3.12. Clone the repository and run installation from its root:

```bash
git clone https://github.com/michaelphamtransjsu/Conversational-AI-for-Economic-Data-Analysis-Recommendation-Engine.git
cd Conversational-AI-for-Economic-Data-Analysis-Recommendation-Engine
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-lock.txt
python3 -m pip install --no-deps -e .
cp .env.example .env
```

For offline use, set `DEMO_MODE=true` in `.env`, then:

```bash
python3 -m streamlit run app.py
```

Windows PowerShell: `.venv\Scripts\Activate.ps1`, then `Copy-Item .env.example .env`. All demo observations are **synthetic fixtures, not empirical economic findings**. No FRED or model request occurs in demo mode.

For live retrieval, set `DEMO_MODE=false` and `FRED_API_KEY` server-side. Configure `LLM_API_KEY`, `LLM_PROVIDER=openai`, and `LLM_MODEL` to enable model interpretation. Without a model key, the supported prompt shortcuts and deterministic request parser remain available with a clear notice. Keys are never browser inputs. `.env` is ignored; do not paste secrets into chat.

The sidebar can explicitly enable demo mode without editing `.env`. Use the seven shortcuts, then try “use the last five years,” “now graph it,” and “now compare it with inflation.” History metric/date controls are independent of session-local conversation context; a changed question displays its applied window. Inspect any prior conversation result with the evidence selector and download the session as Markdown.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FRED_API_KEY` | unset | Registered FRED API key |
| `LLM_API_KEY` | unset | OpenAI API key for structured tool calls |
| `LLM_PROVIDER` | `openai` | Implemented provider |
| `LLM_MODEL` | `gpt-4.1-mini` | Configurable tool-capable model |
| `DEMO_MODE` | `false` | Explicitly synthetic offline fixtures |
| `CACHE_TTL_SECONDS` | `21600` | Six-hour snapshot TTL |
| `CACHE_DIR` | `.cache/fred` | Ignored local immutable snapshots |

Live mode requires HTTPS access to `api.stlouisfed.org` and, for model assistance, `api.openai.com`. No public deployment or authentication is included; run locally for a trusted user.

`.streamlit/config.toml` binds the server to `127.0.0.1` and disables usage telemetry. The [implementation handoff](docs/implementation_handoff.md) lists files added, commands actually run, final test results and remaining blockers.

## Economic contract

GDP is quarterly **billions of dollars at SAAR**, not real GDP or a quarter's actual dollar total. CPILFESL is a monthly seasonally adjusted **index, 1982–1984=100**, not a percentage. GDP growth and CPI inflation default to YoY. Missing calendar periods stay missing; calculations use full history before trimming the requested window.

Comparison uses quarterly GDP YoY and YoY change of the **complete three-month quarterly-average CPI index**, then inner-joins valid quarters. Pearson correlation requires eight paired quarters and nonconstant inputs. It describes association, not causality or predictive power. GDP already contains price effects. No GDP interpolation or CPI-deflated substitute for real GDP is allowed.

Latest means the most recent valid observation in a snapshot, not today's economic state. Latest/current requests attempt refresh; failed refresh can use a previous snapshot only with a stale notice. Source update timestamps are not release dates. Snapshots reproduce retrievals, not arbitrary historical vintages. See [economic rules](AGENTS.md) and the [supplied implementation specification](docs/implementation_spec.md).

## Validation and artifacts

```bash
python3 -m pytest -q
python3 scripts/export_demo.py
python3 scripts/evaluate.py
```

To retain a machine-readable test record, run `python3 -m pytest -q --junitxml=artifacts/test_results.xml > artifacts/test_results.txt 2>&1` before evaluation. The recorded final run has **180 passed, 1 skipped**, with ten known PNG-renderer deprecation warnings; evaluation passed **12/12** scenarios. See the generated record for the actual execution timestamp.

Live checks are explicit opt-in:

```bash
DEMO_MODE=false python3 scripts/live_smoke.py --confirm-live
DEMO_MODE=false RUN_LIVE_TESTS=1 python3 -m pytest -q -m live
DEMO_MODE=false python3 scripts/evaluate.py --live
DEMO_MODE=false python3 scripts/evaluate.py --live --with-llm
```

The final command makes configured model calls. A compatible OpenAI Chat Completions model must accept strict function tools; model compatibility and live grounding have not been validated without credentials. Live evaluation defaults to `work/live_evaluation/` so it preserves the synthetic submission record.

PNG exports use the tested `kaleido==0.2.1` bundled renderer. Kaleido 1.2.0 failed against the available Chromium during implementation. The working version emits deprecation warnings and may need platform-specific replacement in future environments; HTML and CSV exports remain available. Plotly versions are pinned to preserve the tested combination. PNG creation happens only when explicitly requested in the UI.

The opt-in live smoke commands and actual executed results are recorded in [evaluation results](docs/evaluation_results.md). Demo exports and conversations do not validate live FRED access or model quality.

Submission artifacts: [architecture](docs/architecture.md), [diagram source](docs/architecture.mmd), [rendered diagram](docs/architecture.svg), [executed conversations](docs/sample_conversations.md), [technical report](docs/technical_report.md), [snapshot manifest](artifacts/snapshot_manifest.json), and charts/CSVs under `artifacts/`.

The application lives at this repository's root: `app.py` starts Streamlit, source modules live under `src/fred_chat/`, and tests live under `tests/`. There is no additional `fred-economic-chat/` directory to enter. The user authorized committing this implementation and pushing to this repository's `main` branch; public deployment is outside that authorization.

| Module | Responsibility |
|---|---|
| `config.py`, `schemas.py` | Server-side secrets, strict supported tools and evidence contracts |
| `fred_client.py`, `cache.py`, `demo.py` | Native HTTP retrieval, safe failures/logs, immutable snapshots and labeled fixtures |
| `analytics.py`, `alignment.py`, `windows.py` | Calendar arithmetic, complete-quarter comparison and date windows |
| `tools.py`, `conversation.py` | Session-local evidence, validated dispatch, follow-ups and bounded orchestration |
| `provider.py`, `prompts.py`, `narrative.py` | Structured OpenAI adapter, supported note choices and authoritative numeric templates |
| `charts.py`, `ui.py`, `app.py` | Chart templates, provenance-preserving exports and Streamlit UI |

See [phase gates](docs/phase_log.md) for the executed implementation sequence. Runtime caches and private work files are ignored; intentionally included synthetic snapshots are under `artifacts/sample_data/`.

Git attributes preserve exported artifacts byte-for-byte across platforms, including their original line endings, so the recorded SHA-256 checksums remain valid after checkout.
