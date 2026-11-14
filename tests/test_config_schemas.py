from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from fred_chat.config import load_settings
from fred_chat.schemas import AnalyzeHistory, BuildChart, CompareIndicators, LatestIndicator, Observation, Snapshot


def test_config_is_explicit_and_secret_safe():
    settings = load_settings({"FRED_API_KEY": "fred-secret-value", "LLM_API_KEY": "llm-secret-value"})
    assert settings.demo_mode is False
    assert settings.cache_ttl_seconds == 21600
    assert "fred-secret-value" not in repr(settings)
    assert "llm-secret-value" not in settings.model_dump_json()
    assert settings.fred_api_key.get_secret_value() == "fred-secret-value"


def test_missing_keys_give_useful_messages_and_demo_requires_opt_in():
    settings = load_settings({})
    assert "FRED_API_KEY" in " ".join(settings.configuration_messages)
    assert "LLM_API_KEY" in " ".join(settings.configuration_messages)
    assert not settings.demo_mode
    assert load_settings({"DEMO_MODE": "true"}).demo_mode
    with pytest.raises(ValidationError):
        load_settings({"DEMO_MODE": "maybe"})


@pytest.mark.parametrize("data", [
    {"series": "GDPC1"},
    {"series": "GDP", "metric": "mom"},
    {"series": "CPILFESL", "metric": "qoq"},
    {"series": "GDP", "python": "__import__('os').system('ls')"},
])
def test_tool_rejects_unsupported_series_metrics_and_extra_code(data):
    with pytest.raises(ValidationError):
        LatestIndicator.model_validate(data)


def test_iso_ranges_and_chart_references_are_validated():
    valid = AnalyzeHistory(series="GDP", metric="yoy", start="2020-01-01", end="2025-12-31")
    assert valid.start.isoformat() == "2020-01-01"
    for start, end in [("2025-01-01", "2020-01-01"), ("20200101", "2025-01-01"), (0, 1)]:
        with pytest.raises(ValidationError):
            CompareIndicators(start=start, end=end)
    with pytest.raises(ValidationError):
        BuildChart(result_id="../../.env", kind="line")
    with pytest.raises(ValidationError):
        BuildChart(result_id="result_a", kind="shell")


def test_nonfinite_observations_and_duplicate_dates_are_rejected():
    with pytest.raises(ValidationError):
        Observation(date="2020-01-01", value=float("inf"))
    row = {"date": "2020-01-01", "value": 100}
    metadata = dict(id="GDP", title="GDP", frequency="Quarterly", units="Billions of Dollars", seasonal_adjustment="Seasonally Adjusted Annual Rate", source_url="https://fred.stlouisfed.org/series/GDP")
    with pytest.raises(ValidationError, match="Duplicate"):
        Snapshot(snapshot_id="test", series="GDP", metadata=metadata, observations=[row, row], retrieved_at=datetime.now(timezone.utc))
    with pytest.raises(ValidationError, match="UTC offset"):
        Snapshot(snapshot_id="test", series="GDP", metadata=metadata, observations=[row], retrieved_at=datetime(2020, 1, 1))
