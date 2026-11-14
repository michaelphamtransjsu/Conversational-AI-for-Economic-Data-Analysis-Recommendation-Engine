"""Bounded OpenAI structured-tool HTTP adapter; no prompt-generated execution."""
from __future__ import annotations

import copy
import json
import re
from typing import Any

import httpx

from .config import Settings
from .prompts import TOOL_DESCRIPTIONS, supported_notes
from .schemas import Evidence, TOOL_SCHEMAS

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MAX_RESPONSE_BYTES = 1_048_576
MAX_REQUEST_BYTES = 262_144


class ProviderError(Exception):
    """Public, secret-free error; underlying request/response details are omitted."""


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(schema)

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in list(node.values()):
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    return result


def build_tools() -> list[dict[str, Any]]:
    """OpenAI strict function schemas generated from the validated tool models."""
    return [
        {"type": "function", "function": {
            "name": name,
            "description": TOOL_DESCRIPTIONS[name],
            "strict": True,
            "parameters": _strict_schema(model.model_json_schema()),
        }} for name, model in TOOL_SCHEMAS.items()
    ]


class OpenAIProvider:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
        self._http = httpx.Client(timeout=httpx.Timeout(settings.request_timeout_seconds),
                                  transport=transport, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 tool_choice: str | dict = "auto") -> dict:
        """Return a validated assistant message; never execute its tool calls.

        Malformed JSON arguments remain strings for the orchestrator's one
        schema-repair attempt. Unknown/unoffered function names are rejected.
        Raw content is untrusted and must not be displayed by the application.
        """
        if self.settings.demo_mode:
            raise ProviderError("DEMO mode does not contact the LLM provider.")
        if not self._key:
            raise ProviderError("LLM_API_KEY is missing; deterministic tools remain available.")
        tools = build_tools() if tools is None else tools
        try:
            offered = {tool["function"]["name"] for tool in tools}
            if not offered or len(offered) > 6 or len(messages) > 32 or not messages:
                raise ValueError
            if any(not isinstance(row, dict) or row.get("role") not in {"system", "user", "assistant", "tool"} for row in messages):
                raise ValueError
            if isinstance(tool_choice, str) and tool_choice not in {"auto", "required", "none"}:
                raise ValueError
            payload = {"model": self.settings.llm_model, "messages": messages,
                       "tools": tools, "tool_choice": tool_choice,
                       "parallel_tool_calls": False, "temperature": 0, "max_tokens": 1200}
            encoded = json.dumps(payload, allow_nan=False).encode()
            if len(encoded) > MAX_REQUEST_BYTES or self._key in encoded.decode():
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ProviderError("The bounded model request could not be validated.") from None
        try:
            with self._http.stream("POST", ENDPOINT, content=encoded,
                                   headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}) as response:
                if response.status_code in {401, 403}:
                    raise ProviderError("The LLM provider rejected the configured credentials.")
                if response.status_code == 429:
                    raise ProviderError("The LLM provider is rate limited or has insufficient quota.")
                if response.status_code != 200:
                    raise ProviderError("The LLM provider returned an unsuccessful response.")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ProviderError("The LLM response exceeded its size limit.")
        except httpx.TimeoutException:
            raise ProviderError("The LLM provider timed out; deterministic tools remain available.") from None
        except httpx.RequestError:
            raise ProviderError("The LLM provider is unavailable; deterministic tools remain available.") from None
        try:
            decoded = bytes(body).decode()
            if self._key in decoded:
                raise ValueError
            response_data = json.loads(decoded)
            choices = response_data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            message = choices[0]["message"]
            if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
                raise ValueError
            content = message.get("content")
            if content is not None and (not isinstance(content, str) or len(content) > 8192):
                raise ValueError
            calls = message.get("tool_calls", [])
            if calls is None:
                calls = []
            if not isinstance(calls, list) or len(calls) > 6:
                raise ValueError
            valid_calls = []
            ids = set()
            for call in calls:
                identifier, function = call["id"], call["function"]
                if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier) or identifier in ids:
                    raise ValueError
                ids.add(identifier)
                if call.get("type") != "function" or function["name"] not in offered:
                    raise ValueError
                arguments = function["arguments"]
                if not isinstance(arguments, str) or len(arguments.encode()) > 16_384:
                    raise ValueError
                valid_calls.append({"id": identifier, "type": "function", "function": {
                    "name": function["name"], "arguments": arguments,
                }})
            return {"role": "assistant", "content": content, "tool_calls": valid_calls}
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ProviderError("The LLM provider returned an invalid structured response.") from None

    def select_notes(self, evidence: Evidence) -> list[str]:
        """Select fixed, supported caveats; discard all untrusted model prose."""
        notes = supported_notes(evidence)
        parameters = {"type": "object", "properties": {
            "note_ids": {"type": "array", "items": {"type": "string", "enum": list(notes)}}
        }, "required": ["note_ids"], "additionalProperties": False}
        tools = [{"type": "function", "function": {
            "name": "select_evidence_notes", "description": "Choose relevant notes already supported by this evidence.",
            "strict": True, "parameters": parameters,
        }}]
        # There are no credentials, raw API responses or user-provided instructions
        # in this bounded evidence summary.
        context = {"operation": evidence.operation, "series": [row.value for row in evidence.series],
                   "metric": evidence.metric, "available_notes": notes}
        result = self.complete([
            {"role": "system", "content": "Call select_evidence_notes with at most four relevant note IDs from available_notes. Do not write prose or add facts."},
            {"role": "user", "content": json.dumps(context)},
        ], tools=tools, tool_choice={"type": "function", "function": {"name": "select_evidence_notes"}})
        try:
            calls = result["tool_calls"]
            if len(calls) != 1:
                raise ValueError
            arguments = json.loads(calls[0]["function"]["arguments"])
            if not isinstance(arguments, dict) or set(arguments) != {"note_ids"}:
                raise ValueError
            ids = arguments["note_ids"]
            if not isinstance(ids, list) or len(ids) > 4 or any(not isinstance(identifier, str) or identifier not in notes for identifier in ids):
                raise ValueError
            return [notes[identifier] for identifier in dict.fromkeys(ids)]
        except (ValueError, TypeError, KeyError):
            raise ProviderError("The LLM explanation did not select supported evidence notes.") from None
