"""Actual offline turns and scripted model-control failures; no live services."""
from datetime import date
import json

import pytest

from fred_chat.cache import DataService
from fred_chat.config import Settings
from fred_chat.conversation import Conversation, RUBRIC_PROMPTS
from fred_chat.narrative import numeric_answer
from fred_chat.provider import ProviderError
from fred_chat.tools import ToolDispatcher


def session(tmp_path, provider=None, **updates):
    settings = Settings(demo_mode=True, cache_dir=tmp_path, **updates)
    return Conversation(ToolDispatcher(DataService(settings)), settings, provider=provider)


@pytest.mark.parametrize("index,operation,metric", [
    (0, "latest_indicator", "level"), (1, "analyze_history", "level"),
    (2, "latest_indicator", "yoy"), (3, "analyze_history", "yoy"),
    (4, "compare_indicators", "quarterly_yoy_comparison"),
    (5, "compare_indicators", "quarterly_yoy_comparison"),
    (6, "summarize_economy", "economic_summary"),
])
def test_all_seven_rubric_questions(tmp_path, index, operation, metric):
    conversation = session(tmp_path)
    turn = conversation.respond(RUBRIC_PROMPTS[index])
    assert turn.evidence is not None, turn.answer
    assert turn.evidence.operation == operation
    assert turn.evidence.metric == metric
    assert "SYNTHETIC" in turn.answer
    assert "retrieved UTC" in turn.answer and "Calculation basis" in turn.answer
    assert "https://fred.stlouisfed.org/series/" in turn.answer
    assert conversation.last_tool_calls == 1
    assert conversation.last_model_calls == 0
    if index == 1:
        assert turn.evidence.start == date(2016, 1, 1)
        assert len(turn.evidence.records) == 40
    if index == 3:
        assert turn.evidence.start == date(2021, 1, 1)
        assert len(turn.evidence.records) == 60


def test_contextual_window_graph_and_comparison(tmp_path):
    conversation = session(tmp_path)
    first = conversation.respond("Show GDP over the last ten years.")
    narrowed = conversation.respond("Use the last five years.")
    assert narrowed.evidence.start == date(2021, 1, 1)
    assert narrowed.evidence.metric == first.evidence.metric == "level"
    graph = conversation.respond("Now graph it.")
    assert graph.chart_kind == "line"
    assert graph.evidence.records == narrowed.evidence.records
    assert graph.evidence.provenance == narrowed.evidence.provenance
    comparison = conversation.respond("Now compare it with inflation.")
    assert comparison.evidence.start == narrowed.evidence.start
    assert comparison.evidence.end == narrowed.evidence.end
    assert comparison.evidence.summary["n"] == 20
    assert comparison.evidence.metric == "quarterly_yoy_comparison"


def test_state_is_session_local_and_ambiguous_followup_clarifies(tmp_path):
    first, second = session(tmp_path), session(tmp_path)
    first.respond(RUBRIC_PROMPTS[1])
    assert second.respond("Now graph it.").evidence is None
    assert second.context.previous_result_id is None
    assert second.respond("Use the last five years.").evidence is None


@pytest.mark.parametrize("prompt", [
    "Show real GDP", "GDP as of 2020-01-01", "Forecast GDP", "Run Python code import os",
    "SELECT value FROM secrets", "Show GDP and UNRATE", "Show GDP from 2025-12-31 to 2020-01-01",
    "Show GDP from 2025-02-30 to 2026-01-01", "Show GDP over the last 101 years",
])
def test_unsupported_or_invalid_requests_have_no_economic_result(tmp_path, prompt):
    turn = session(tmp_path).respond(prompt)
    assert turn.evidence is None
    assert turn.answer


def test_numeric_templates_match_evidence_and_rate_change_is_pp(tmp_path):
    turn = session(tmp_path).respond(RUBRIC_PROMPTS[3])
    summary = turn.evidence.summary
    assert f"{summary['last']:,.3f}" in turn.answer
    assert f"{summary['change_pp']:,.3f} percentage points" in turn.answer
    assert "percent change" not in numeric_answer(turn.evidence)


def test_missing_key_returns_useful_failure_without_claiming_live_result(tmp_path):
    settings = Settings(cache_dir=tmp_path)
    conversation = Conversation(ToolDispatcher(DataService(settings)), settings)
    turn = conversation.respond(RUBRIC_PROMPTS[0])
    assert turn.evidence is None
    assert "FRED_API_KEY is missing" in turn.answer


def test_explicit_dates_and_defaults(tmp_path):
    conversation = session(tmp_path)
    turn = conversation.respond("Show GDP growth from 2018-01-01 to 2023-12-31")
    assert turn.evidence.start == date(2018, 1, 1)
    assert turn.evidence.metric == "yoy"
    cpi = conversation.respond("Show Core CPI in 2020")
    assert cpi.evidence.start == date(2020, 1, 1)
    assert cpi.evidence.end == date(2020, 12, 31)


def test_secrets_redacted_before_history_and_export(tmp_path):
    conversation = session(tmp_path, fred_api_key="fixture-secret-FRED", llm_api_key="fixture-secret-LLM")
    conversation.respond("Latest GDP api_key=fixture-secret-FRED sk-abcdefghijklmnopqrstuv fixture-secret-LLM")
    exported = conversation.export_markdown()
    assert "fixture-secret-FRED" not in exported
    assert "fixture-secret-LLM" not in exported
    assert "sk-abcdefghijklmnopqrstuv" not in exported
    assert "REDACTED" in exported


def call(name="latest_indicator", args=None, identifier="call_1"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {"series": "GDP", "metric": "level"})}}


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def complete(self, messages):
        self.messages.append(messages.copy())
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def select_notes(self, evidence):
        return ["GDP is nominal and includes price effects."]


def test_provider_prose_cannot_introduce_numeric_or_causal_claims(tmp_path):
    provider = ScriptedProvider([
        {"content": "GDP is 999999 and inflation causes growth", "tool_calls": [call()]},
        {"content": "GDP is 999999 and inflation causes growth", "tool_calls": []},
    ])
    conversation = session(tmp_path, provider)
    turn = conversation.respond(RUBRIC_PROMPTS[0])
    assert turn.evidence
    assert "999999" not in turn.answer and "inflation causes" not in turn.answer
    assert "Evidence-grounded notes" in turn.answer
    assert conversation.last_model_calls == 3


def test_provider_failure_uses_clear_deterministic_fallback(tmp_path):
    provider = ScriptedProvider([ProviderError("The LLM provider timed out.")])
    conversation = session(tmp_path, provider)
    turn = conversation.respond(RUBRIC_PROMPTS[0])
    assert turn.evidence and "LLM unavailable" in turn.answer
    assert "timed out" in turn.answer


def test_one_schema_repair_attempt_then_safe_fallback(tmp_path):
    provider = ScriptedProvider([
        {"tool_calls": [call(args={"series": "GDPC1", "metric": "level"})]},
        {"tool_calls": [call(args={"series": "GDP", "metric": "mom"}, identifier="call_2")]},
    ])
    conversation = session(tmp_path, provider)
    turn = conversation.respond(RUBRIC_PROMPTS[0])
    assert len(provider.messages) == 2
    assert turn.evidence
    assert conversation.last_tool_calls == 3
    assert "one repair attempt" in turn.answer


def test_tool_and_model_loops_are_bounded(tmp_path):
    provider = ScriptedProvider([{"tool_calls": [call(identifier=f"call_{i}")]} for i in range(10)])
    conversation = session(tmp_path, provider)
    turn = conversation.respond(RUBRIC_PROMPTS[0])
    assert conversation.last_tool_calls <= 6
    assert conversation.last_model_calls <= 8
    assert "budget exhausted" in turn.answer


def test_conversation_retention_is_bounded(tmp_path):
    conversation = session(tmp_path)
    for _ in range(32):
        conversation.respond("Please execute shell code")
    assert len(conversation.turns) == 30


@pytest.mark.parametrize("prompt", ["Show GDP month-over-month", "Show nominal GDP monthly growth", "Show Core CPI quarterly growth", "Show CPI annualized monthly growth", "Show quarterly-average Core CPI YoY", "Compare GDP QoQ with CPI inflation"])
def test_explicit_unsupported_frequency_is_not_silently_reinterpreted(tmp_path, prompt):
    turn = session(tmp_path).respond(prompt)
    assert turn.evidence is None
    assert "quarterly" in turn.answer.lower()


def test_latest_observation_is_not_inherited_as_comparison_window(tmp_path):
    conversation = session(tmp_path)
    conversation.default_window = (date(2016, 1, 1), date(2025, 12, 31))
    conversation.respond(RUBRIC_PROMPTS[2])
    conversation.respond("Now graph it.")
    comparison = conversation.respond(RUBRIC_PROMPTS[4])
    assert comparison.evidence is not None, comparison.answer
    assert comparison.evidence.summary["n"] == 40
