"""Offline Streamlit flows exercise the actual local application."""
from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def start(monkeypatch, demo=True):
    monkeypatch.setenv("DEMO_MODE", "true" if demo else "false")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    return AppTest.from_file(APP).run(timeout=30)


def test_missing_key_is_useful_and_does_not_crash(monkeypatch):
    app = start(monkeypatch, demo=False)
    assert not app.exception
    assert any("FRED_API_KEY" in element.value for element in app.info)
    assert any("GDP:" in element.value for element in app.error)


def test_demo_dashboard_has_distinct_periods_units_and_exports(monkeypatch):
    app = start(monkeypatch)
    assert not app.exception
    assert len(app.metric) == 2
    assert "nominal GDP" in app.metric[0].label
    assert "Core CPI YoY" in app.metric[1].label
    captions = " ".join(x.value for x in app.caption)
    assert "2025Q4" in captions and "2025-12" in captions
    assert "SAAR" in captions
    assert any("DEMO / SYNTHETIC" in x.value for x in app.warning)
    assert len(app.get("download_button")) >= 6


def test_changing_to_cpi_uses_monthly_yoy(monkeypatch):
    app = start(monkeypatch)
    app.selectbox[0].select("CPILFESL").run(timeout=30)
    assert not app.exception
    assert app.selectbox[1].value == "yoy"
    assert "qoq" not in app.selectbox[1].options
    assert any("month" in x.value.lower() or "C[m]" in x.value for x in app.caption)


def test_reversed_window_displays_error(monkeypatch):
    app = start(monkeypatch)
    app.date_input[0].set_value(date(2026, 1, 1)).run(timeout=30)
    assert not app.exception
    assert any("start must be on or before" in x.value.lower() for x in app.error)


def test_actual_chat_followups_and_download(monkeypatch):
    app = start(monkeypatch)
    app.button(key="rubric_1").click().run(timeout=30)
    app.chat_input[0].set_value("Use the last five years.").run(timeout=30)
    app.chat_input[0].set_value("Now graph it.").run(timeout=30)
    assert not app.exception
    assert len(app.chat_message) == 6
    markdown = " ".join(element.value for element in app.markdown)
    assert "2021Q1" in markdown and "2025Q4" in markdown
    assert any(element.label == "Download session conversation Markdown" for element in app.get("download_button"))


def test_png_cache_signature_retains_freshness_and_requested_window():
    from fred_chat.analytics import history
    from fred_chat.demo import demo_snapshot
    from fred_chat.ui import export_signature
    evidence = history(demo_snapshot("GDP"), "level", "2021-01-01", "2025-12-31")
    original = export_signature(evidence, "line")
    equivalent = evidence.model_copy(deep=True)
    equivalent.result_id = "another_id"
    assert export_signature(equivalent, "line") == original
    equivalent.warnings.append("STALE DATA: refresh failed")
    assert export_signature(equivalent, "line") != original
    equivalent = evidence.model_copy(deep=True)
    equivalent.end = date(2025, 12, 30)
    assert export_signature(equivalent, "line") != original
