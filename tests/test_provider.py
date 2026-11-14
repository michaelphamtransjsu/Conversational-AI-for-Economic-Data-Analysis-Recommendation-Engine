"""Mocked structured-tool provider tests; no live requests or model claims."""
import json

import httpx
import pytest

from fred_chat.config import Settings
from fred_chat.demo import demo_snapshot
from fred_chat.analytics import latest
from fred_chat.provider import OpenAIProvider, ProviderError, build_tools

KEY = "synthetic-provider-secret-never-export"
MESSAGES = [{"role": "system", "content": "Use typed tools."}, {"role": "user", "content": "Latest GDP"}]


def message(name="latest_indicator", arguments='{"series":"GDP","metric":"level"}', content=None):
    return {"role": "assistant", "content": content, "tool_calls": [{
        "id": "call_fixture", "type": "function", "function": {"name": name, "arguments": arguments},
    }]}


def response(assistant=None):
    return {"choices": [{"message": message() if assistant is None else assistant}]}


def provider(handler, **overrides):
    return OpenAIProvider(Settings(llm_api_key=KEY, **overrides), transport=httpx.MockTransport(handler))


def test_strict_schemas_come_from_validated_public_tools():
    tools = build_tools()
    assert {row["function"]["name"] for row in tools} == {
        "latest_indicator", "analyze_history", "compare_indicators", "build_chart", "summarize_economy",
    }
    for row in tools:
        function = row["function"]
        schema = function["parameters"]
        assert function["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
    latest_schema = tools[0]["function"]["parameters"]
    assert {"type": "null"} in latest_schema["properties"]["metric"]["anyOf"]


def test_fixed_endpoint_authorization_and_bounded_structured_request():
    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        body = json.loads(request.content)
        assert KEY not in request.content.decode()
        assert body["model"] == "fixture-tool-model"
        assert body["max_tokens"] == 1200
        assert body["parallel_tool_calls"] is False
        assert body["temperature"] == 0
        assert body["tools"]
        return httpx.Response(200, json=response())
    result = provider(handler, llm_model="fixture-tool-model").complete(MESSAGES)
    assert result == message()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_provider_failures_are_safe_and_not_retried(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": f"Unsafe {KEY} {request.url}"}})
    with pytest.raises(ProviderError) as caught:
        provider(handler).complete(MESSAGES)
    assert len(calls) == 1
    assert KEY not in str(caught.value)
    assert "https://" not in str(caught.value)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_network_exceptions_cannot_expose_credentials(failure):
    def handler(request):
        raise failure(f"Unsafe key {KEY} at {request.url}", request=request)
    with pytest.raises(ProviderError) as caught:
        provider(handler).complete(MESSAGES)
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("settings", [Settings(), Settings(demo_mode=True, llm_api_key=KEY)])
def test_missing_key_or_demo_never_contacts_provider(settings):
    def handler(request):
        pytest.fail("No request is permitted for demo/missing key")
    client = OpenAIProvider(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        client.complete(MESSAGES)


@pytest.mark.parametrize("payload", [
    None, {}, {"choices": []}, {"choices": [{"message": None}]},
    response({"role": "user", "content": "bad"}),
    response(message(name="run_python", arguments='{"code":"print(1)"}')),
    response(message(arguments={})),
    response({"role": "assistant", "content": ["bad"]}),
    response({"role": "assistant", "tool_calls": [{"id": "../bad", "type": "function", "function": {"name": "latest_indicator", "arguments": "{}"}}]}),
])
def test_invalid_structured_responses_are_rejected(payload):
    with pytest.raises(ProviderError):
        provider(lambda request: httpx.Response(200, json=payload)).complete(MESSAGES)


def test_malformed_json_arguments_remain_available_for_one_orchestrator_repair():
    malformed = message(arguments="{invalid_json")
    result = provider(lambda request: httpx.Response(200, json=response(malformed))).complete(MESSAGES)
    assert result["tool_calls"][0]["function"]["arguments"] == "{invalid_json"


def test_oversized_response_is_bounded_before_json_parse():
    with pytest.raises(ProviderError, match="size limit"):
        provider(lambda request: httpx.Response(200, content=b"x" * 1_048_577)).complete(MESSAGES)


def test_key_echo_in_successful_payload_is_rejected_not_returned():
    with pytest.raises(ProviderError) as caught:
        provider(lambda request: httpx.Response(200, json=response(message(content=KEY)))).complete(MESSAGES)
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("messages", [[], MESSAGES * 17, [{"role": "admin", "content": "bad"}], [{"role": "user", "content": "x" * 270000}], [{"role": "user", "content": KEY}]])
def test_request_bounds_and_secret_material_block_before_network(messages):
    def handler(request):
        pytest.fail("Invalid request must be rejected before HTTP")
    with pytest.raises(ProviderError):
        provider(handler).complete(messages)


def test_notes_return_only_fixed_supported_text_and_ignore_model_prose():
    evidence = latest(demo_snapshot("GDP"), "level")
    def handler(request):
        payload = json.loads(request.content)
        assert payload["tool_choice"]["function"]["name"] == "select_evidence_notes"
        assert KEY not in request.content.decode()
        return httpx.Response(200, json=response(message("select_evidence_notes", '{"note_ids":["nominal_gdp","demo"]}', "GDP is 999999 and caused inflation.")))
    notes = provider(handler).select_notes(evidence)
    assert len(notes) == 2
    assert "nominal" in notes[0]
    assert "SYNTHETIC" in notes[1]
    assert "999999" not in " ".join(notes)
    assert "caused" not in " ".join(notes)


@pytest.mark.parametrize("arguments", ['{"note_ids":["invented"]}', '{"note_ids":["nominal_gdp"],"claim":"bad"}', '{"note_ids":"nominal_gdp"}', '{"note_ids":["periods","nominal_gdp","demo","periods","demo"]}'])
def test_unsupported_or_unbounded_explanation_selection_is_rejected(arguments):
    evidence = latest(demo_snapshot("GDP"), "level")
    with pytest.raises(ProviderError):
        provider(lambda request: httpx.Response(200, json=response(message("select_evidence_notes", arguments)))).select_notes(evidence)
