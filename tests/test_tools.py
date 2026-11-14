import json

import pytest

from fred_chat.cache import DataResult, DataService
from fred_chat.config import Settings
from fred_chat.demo import demo_snapshot
from fred_chat.fred_client import DataError
from fred_chat.schemas import Evidence, SeriesID
from fred_chat.tools import ToolDispatcher, ToolError


WINDOW = {"start": "2015-01-01", "end": "2024-12-31"}


class RecordingData:
    def __init__(self, stale=False):
        self.calls = []
        self.stale = stale

    def get(self, series, refresh=False):
        self.calls.append((SeriesID(series), refresh))
        return DataResult(demo_snapshot(series), ["STALE DATA: test fallback"] if self.stale else [], self.stale)


def test_latest_refreshes_and_history_uses_default_yoy():
    data = RecordingData()
    dispatcher = ToolDispatcher(data)
    latest = dispatcher.execute("latest_indicator", {"series": "GDP", "metric": "level"})
    assert latest.metric == "level"
    assert data.calls == [(SeriesID.GDP, True)]
    history = dispatcher.execute("analyze_history", {"series": "GDP", **WINDOW})
    assert history.metric == "yoy"
    assert history.defaults
    assert data.calls[-1] == (SeriesID.GDP, False)


def test_compare_pins_one_snapshot_each_and_retains_stale_provenance():
    data = RecordingData(stale=True)
    evidence = ToolDispatcher(data).execute("compare_indicators", WINDOW)
    assert data.calls == [(SeriesID.GDP, False), (SeriesID.CPILFESL, False)]
    assert all(source["stale"] for source in evidence.provenance)
    assert all(source["data_warnings"] for source in evidence.provenance)
    assert "STALE" in " ".join(evidence.warnings)
    assert evidence.summary["n"] == len(evidence.records)


def test_summary_components_are_complete_authoritative_evidence():
    data = RecordingData()
    summary = ToolDispatcher(data).execute("summarize_economy", WINDOW)
    components = [Evidence.model_validate(child) for child in summary.summary["components"]]
    assert len(data.calls) == 2
    assert [component.series for component in components] == [[SeriesID.GDP], [SeriesID.CPILFESL]]
    assert all(component.metric == "yoy" for component in components)
    assert all(component.formulas and component.provenance for component in components)
    assert summary.records[0]["row_metric"] == "yoy"
    assert "row_units" in summary.records[0]


def test_chart_reuses_exact_evidence_and_never_refetches():
    data = RecordingData()
    dispatcher = ToolDispatcher(data)
    original = dispatcher.execute("compare_indicators", WINDOW)
    chart = dispatcher.execute("build_chart", {"result_id": original.result_id, "kind": "scatter"})
    assert len(data.calls) == 2
    assert chart.records == original.records
    assert chart.provenance == original.provenance
    assert chart.formulas == original.formulas
    assert chart.summary["chart_kind"] == "scatter"
    assert chart.summary["source_result_id"] == original.result_id
    assert chart.result_id != original.result_id
    chart.records[0]["gdp_yoy"] = -999
    assert dispatcher.results[original.result_id].records[0]["gdp_yoy"] != -999


def test_chart_cannot_access_another_session_or_mixed_summary():
    first = ToolDispatcher(RecordingData())
    second = ToolDispatcher(RecordingData())
    prior = first.execute("analyze_history", {"series": "GDP", **WINDOW})
    with pytest.raises(ToolError, match="session"):
        second.execute("build_chart", {"result_id": prior.result_id})
    with pytest.raises(ToolError, match="Scatter"):
        first.execute("build_chart", {"result_id": prior.result_id, "kind": "scatter"})
    summary = first.execute("summarize_economy", WINDOW)
    with pytest.raises(ToolError, match="Choose GDP"):
        first.execute("build_chart", {"result_id": summary.result_id})


@pytest.mark.parametrize("name,args", [
    ("run_python", {"code": "import os"}),
    ("latest_indicator", {"series": "GDPC1"}),
    ("latest_indicator", {"series": "GDP", "metric": "mom"}),
    ("latest_indicator", {"series": "GDP", "code": "secret-payload"}),
    ("analyze_history", {"series": "GDP", "start": "2024-01-01", "end": "2020-01-01"}),
    ("latest_indicator", "not-json-secret-payload"),
    ("latest_indicator", ["GDP"]),
    ("latest_indicator", {"series": "GDP", "oversized": "X" * 17000}),
])
def test_invalid_tools_and_injection_never_reach_data(name, args):
    data = RecordingData()
    with pytest.raises(ToolError) as error:
        ToolDispatcher(data).execute(name, args)
    assert "secret-payload" not in str(error.value)
    assert not data.calls


def test_json_tool_call_and_bounded_result_registry():
    dispatcher = ToolDispatcher(RecordingData())
    for _ in range(32):
        evidence = dispatcher.execute("latest_indicator", json.dumps({"series": "GDP", "metric": "level"}))
    assert len(dispatcher.results) == 30
    assert evidence.result_id in dispatcher.results


def test_data_failure_is_safe_and_no_partial_result_saved():
    class BrokenData:
        def get(self, *args, **kwargs):
            raise DataError("sensitive-api-key-value", "network")
    dispatcher = ToolDispatcher(BrokenData())
    with pytest.raises(ToolError, match="could not be reached") as error:
        dispatcher.execute("latest_indicator", {"series": "GDP"})
    assert "sensitive" not in str(error.value)
    assert not dispatcher.results


def test_demo_data_service_is_supported_without_credentials():
    evidence = ToolDispatcher(DataService(Settings(demo_mode=True))).execute("latest_indicator", {"series": "CPILFESL"})
    assert evidence.metric == "yoy"
    assert any(source["demo"] for source in evidence.provenance)
    assert "SYNTHETIC" in " ".join(evidence.warnings)
