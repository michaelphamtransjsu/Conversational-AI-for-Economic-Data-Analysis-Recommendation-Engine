"""Bounded conversational control; only validated tools can produce evidence."""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timezone
import json
import re
from typing import Any

from .config import Settings
from .fred_client import DataError
from .narrative import render_answer
from .schemas import Evidence, SeriesID
from .windows import observation_end, trailing_window

RUBRIC_PROMPTS = [
    "What is the latest reported GDP?",
    "Show GDP over the last ten years.",
    "What is current Core CPI inflation?",
    "Show Core CPI inflation over the last five years.",
    "What is the relationship between GDP growth and Core CPI inflation?",
    "Visualize the comparison of GDP growth and Core CPI inflation.",
    "Summarize the key economic trends in both indicators.",
]


@dataclass
class Context:
    series: str | None = None
    metric: str | None = None
    start: str | None = None
    end: str | None = None
    chart_kind: str | None = None
    previous_result_id: str | None = None
    operation: str | None = None


@dataclass
class Turn:
    prompt: str
    answer: str
    evidence: Evidence | None = None
    chart_kind: str | None = None
    notices: list[str] = field(default_factory=list)


@dataclass
class Request:
    name: str
    args: dict[str, Any]
    chart_kind: str | None = None


class Clarification(ValueError):
    pass


class Conversation:
    MAX_TURNS = 30
    MAX_MODEL_CALLS = 8

    def __init__(self, dispatcher, settings: Settings, provider=None, default_window=None):
        self.dispatcher = dispatcher
        self.settings = settings
        self.provider = provider
        self.default_window = default_window
        self.context = Context()
        self.turns: list[Turn] = []
        self.last_tool_calls = 0
        self.last_model_calls = 0

    def _redact(self, text: str) -> str:
        for secret in (self.settings.fred_api_key, self.settings.llm_api_key):
            if secret:
                text = text.replace(secret.get_secret_value(), "[REDACTED]")
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
        text = re.sub(r"(?i)((?:api[_ -]?key|authorization|access[_ -]?token)\s*[:=]\s*)\S+", r"\1[REDACTED]", text)
        return text.replace("\x00", "")

    def _check_supported(self, text: str):
        has_gdp = bool(re.search(r"\bgdp\b|gross domestic", text))
        has_cpi = "cpi" in text or "inflation" in text
        comparison = bool(re.search(r"compar|relationship|correlation", text))
        if comparison and re.search(r"\bqoq\b|\bmom\b|quarter.over.quarter|month.over.month|annualiz", text):
            raise Clarification("Comparison supports shared quarterly YoY rates: GDP YoY and YoY change of complete quarterly-average Core CPI. Request GDP QoQ or CPI MoM separately for those native-frequency metrics.")
        if has_gdp and re.search(r"\bmom\b|month.over.month|monthly\s+(?:growth|data|gdp)", text):
            raise Clarification("GDP is quarterly. Monthly GDP growth and month-over-month GDP are unsupported; choose YoY, QoQ or annualized QoQ. GDP is never interpolated to monthly data.")
        if has_cpi and not comparison and re.search(r"\bqoq\b|quarter.over.quarter|quarterly[ -]+(?:growth|inflation|change|rate|average|mean)|annualiz", text):
            raise Clarification("Standalone CPILFESL tools support the monthly index, YoY or MoM. Use the quarterly comparison tool for the documented YoY change of complete quarterly-average CPI.")
        if re.search(r"\breal\s+(?:gdp|gross domestic product)\b|\bgdpc1\b|deflat\w*.*\bgdp\b", text):
            raise Clarification("GDP is nominal. Real GDP requires an additional series outside the default GDP/CPILFESL scope. Core CPI is not a substitute GDP deflator.")
        if re.search(r"\bas[- ]of\b|\bvintage\b|\bforecast\b|\bpredict(?:ion|\s+next)\b", text):
            raise Clarification("Historical-as-of vintages and forecasts are unsupported. Snapshots reproduce what this app retrieved; they do not reconstruct an arbitrary historical vintage.")
        if re.search(r"\b(?:run|execute)\b.*\b(?:code|python|sql|shell|command)\b|import\s+os|os\.system|subprocess|\beval\s*\(|\bexec\s*\(|rm\s+-rf|\bselect\s+.+\s+from\b", text):
            raise Clarification("Executable code, shell and SQL requests are unsupported. Choose a GDP/Core CPI question or a validated chart tool.")
        if re.search(r"\b(?:unrate|cpiaucsl|payems|pcepi|sp500|dgs10)\b", text):
            raise Clarification("Only GDP and CPILFESL are enabled. Choose nominal GDP or seasonally adjusted Core CPI.")

    def _window(self, text: str, series: str | None, default_years: int) -> tuple[str, str]:
        tokens = re.findall(r"\b\d{4}-\d{1,2}-\d{1,2}\b", text)
        if tokens:
            try:
                dates = [date.fromisoformat(token) for token in tokens]
                if any(day.isoformat() != token for day, token in zip(dates, tokens)):
                    raise ValueError
                if len(dates) != 2:
                    raise ValueError
                start, end = dates
            except ValueError:
                raise Clarification("Use two valid ISO dates, for example 2018-01-01 to 2025-12-31.") from None
            return start.isoformat(), end.isoformat()
        years = re.findall(r"\b(?:19|20)\d{2}\b", text)
        if years:
            if len(years) > 2:
                raise Clarification("Use one calendar year or a start and end year.")
            return f"{years[0]}-01-01", f"{years[-1]}-12-31"
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "twenty": 20}
        match = re.search(r"\b(\d{1,3}|one|two|three|four|five|ten|twenty)\s+years?\b", text)
        if not match and self.context.start and self.context.end and self.context.operation in {"analyze_history", "compare_indicators", "summarize_economy"}:
            return self.context.start, self.context.end
        if not match and self.default_window:
            return tuple(day.isoformat() if isinstance(day, date) else day for day in self.default_window)
        years_count = (int(match[1]) if match[1].isdigit() else words[match[1]]) if match else default_years
        # Full snapshots are small and service caching avoids redundant HTTP calls.
        ids = [SeriesID(series)] if series else list(SeriesID)
        ends = [observation_end(self.dispatcher.data_service.get(item).snapshot) for item in ids]
        try:
            start, end = trailing_window(min(ends), years_count)
        except ValueError:
            raise Clarification("Use a trailing window of one to one hundred years.") from None
        return start.isoformat(), end.isoformat()

    def _parse(self, text: str) -> Request:
        if re.search(r"\b(?:now\s+)?(?:graph|plot|chart)\s+(?:it|that|this)\b", text):
            if not self.context.previous_result_id:
                raise Clarification("Choose an indicator and date window first, then ask to graph it.")
            kind = "scatter" if "scatter" in text else "line"
            return Request("build_chart", {"result_id": self.context.previous_result_id, "kind": kind}, kind)
        followup = bool(re.search(r"(?:use|show|over|for)\s+(?:the\s+)?(?:last|past)\s+.+years?", text)) and not re.search(r"gdp|cpi|inflation|both|compar|relationship", text)
        if followup:
            if not self.context.operation:
                raise Clarification("Which indicator or comparison should use that date window?")
            start, end = self._window(text, self.context.series, 5)
            operation = self.context.operation
            if operation in {"latest_indicator", "build_chart"}:
                operation = "analyze_history" if self.context.series else "compare_indicators"
            args = {"start": start, "end": end}
            if operation == "analyze_history":
                args.update(series=self.context.series, metric=self.context.metric)
            return Request(operation, args, "line" if operation != "summarize_economy" else None)
        if any(word in text for word in ("compar", "relationship", "correlation", "associate", "cause")):
            start, end = self._window(text, None, 10)
            return Request("compare_indicators", {"start": start, "end": end}, "scatter" if "scatter" in text else "line")
        if "key" in text and "trend" in text or "summar" in text and any(word in text for word in ("econom", "both", "indicator", "trend")):
            start, end = self._window(text, None, 10)
            return Request("summarize_economy", {"start": start, "end": end})
        has_gdp = bool(re.search(r"\bgdp\b|gross domestic", text))
        has_cpi = "cpi" in text or "inflation" in text
        if not has_gdp and not has_cpi:
            raise Clarification("Choose nominal GDP, Core CPI inflation, a comparison, or a prior-result follow-up. The seven prompt shortcuts show supported questions.")
        series = "GDP" if has_gdp else "CPILFESL"
        if has_gdp and has_cpi:
            start, end = self._window(text, None, 10)
            return Request("compare_indicators", {"start": start, "end": end}, "line")
        metric = "yoy"
        if series == "GDP":
            metric = "qoq_annualized" if "annualiz" in text else "qoq" if "qoq" in text or "quarter-over-quarter" in text else "yoy" if "growth" in text or "yoy" in text or "year-over-year" in text else "level"
        elif "mom" in text or "month-over-month" in text:
            metric = "mom"
        elif "index" in text or "level" in text:
            metric = "level"
        historical = bool(re.search(r"\byears?\b|\b(?:19|20)\d{2}\b|histor|over\s+time", text))
        if not historical and any(word in text for word in ("latest", "current", "now", "reported")):
            return Request("latest_indicator", {"series": series, "metric": metric})
        start, end = self._window(text, series, 10 if series == "GDP" else 5)
        return Request("analyze_history", {"series": series, "metric": metric, "start": start, "end": end}, "line")

    def _provider_result(self, prompt: str) -> tuple[Evidence, list[str]]:
        from .prompts import SYSTEM_PROMPT
        from .provider import ProviderError
        from .tools import ToolError
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "system", "content": "Session context: " + json.dumps(asdict(self.context)) + "; dashboard default window: " + str(self.default_window)}]
        for turn in self.turns[-4:]:
            messages += [{"role": "user", "content": turn.prompt[:2000]},
                         {"role": "assistant", "content": turn.answer[:3000]}]
        messages.append({"role": "user", "content": prompt})
        repair = 0
        evidence = None
        notices = []
        for _ in range(7):
            if self.last_model_calls >= self.MAX_MODEL_CALLS - 1:
                raise ProviderError("Model-call budget exhausted; choose a deterministic prompt shortcut.")
            self.last_model_calls += 1
            response = self.provider.complete(messages)
            calls = response.get("tool_calls") or []
            if not calls:
                if evidence is not None:
                    return evidence, notices
                raise ProviderError("The model returned no validated economic tool request.")
            # Never render or store free-form provider prose as an answer.
            messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            for call in calls:
                self.last_tool_calls += 1
                if self.last_tool_calls > self.settings.max_tool_calls:
                    self.last_tool_calls = self.settings.max_tool_calls
                    raise ProviderError("Tool-call budget exhausted. Narrow the question or use a prompt shortcut.")
                try:
                    evidence = self.dispatcher.execute(call["function"]["name"], call["function"]["arguments"])
                    payload = evidence.model_dump(mode="json")
                    # Models need exact summary/provenance, not thousands of raw rows.
                    payload["records"] = payload["records"][:8]
                    if "components" in payload["summary"]:
                        payload["summary"]["components"] = [{"metric": c["metric"], "series": c["series"], "summary": c["summary"]} for c in payload["summary"]["components"]]
                    message = json.dumps(payload, allow_nan=False)
                except ToolError as exc:
                    repair += 1
                    if repair > 1:
                        raise ProviderError("The model's tool arguments remained invalid after one repair attempt.") from None
                    message = json.dumps({"error": str(exc), "repair": "Use the declared tool schemas; one repair is allowed."})
                    notices.append("One invalid model tool request was rejected and a schema repair was requested.")
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": message})
        raise ProviderError("The bounded orchestration loop ended without a final response.")

    def _update(self, evidence: Evidence, chart_kind: str | None):
        self.context.previous_result_id = evidence.result_id
        self.context.chart_kind = chart_kind
        self.context.operation = evidence.operation if evidence.operation != "build_chart" else (
            "compare_indicators" if evidence.metric == "quarterly_yoy_comparison" else
            "latest_indicator" if "value" in evidence.summary else "analyze_history")
        self.context.start = str(evidence.start) if evidence.start else None
        self.context.end = str(evidence.end) if evidence.end else None
        self.context.series = evidence.series[0].value if len(evidence.series) == 1 else None
        self.context.metric = evidence.metric
        # build_chart keeps the original analytic operation in its evidence.

    def respond(self, prompt: str) -> Turn:
        from .tools import ToolError
        from .provider import ProviderError
        clean = self._redact(str(prompt))
        self.last_tool_calls = 0
        self.last_model_calls = 0
        notices = []
        evidence = None
        kind = None
        try:
            if not clean.strip() or len(clean) > 2000:
                raise Clarification("Use a nonempty question of at most two thousand characters.")
            self._check_supported(clean.lower())
            request = None
            parse_error = None
            try:
                request = self._parse(clean.lower())
            except Clarification as exc:
                parse_error = exc
            if self.provider is not None:
                try:
                    evidence, notes = self._provider_result(clean)
                    notices.extend(notes)
                    kind = evidence.summary.get("chart_kind") or ("scatter" if "scatter" in clean.lower() else "line" if evidence.operation in {"analyze_history", "compare_indicators", "build_chart"} else None)
                except ProviderError as exc:
                    notices.append(f"LLM unavailable or bounded request rejected: {exc} Deterministic supported tools remain available.")
            else:
                notices.append("Deterministic interpretation; no live LLM call was made.")
            if evidence is None:
                if request is None:
                    raise parse_error or Clarification("Choose a supported indicator question.")
                if self.last_tool_calls >= self.settings.max_tool_calls:
                    raise Clarification("Tool-call budget exhausted; submit a narrower question.")
                self.last_tool_calls += 1
                evidence = self.dispatcher.execute(request.name, request.args)
                kind = request.chart_kind
            self._update(evidence, kind)
            answer = render_answer(evidence)
            if self.provider is not None and self.last_model_calls < self.MAX_MODEL_CALLS:
                self.last_model_calls += 1
                try:
                    notes = self.provider.select_notes(evidence)
                    if notes:
                        answer += "\n\nEvidence-grounded notes: " + " ".join(notes)
                except ProviderError:
                    notices.append("LLM note selection was unavailable; the deterministic numeric answer is complete.")
        except (Clarification, ToolError, DataError) as exc:
            answer = str(exc)
        for notice in dict.fromkeys(notices):
            answer += "\n\n" + notice
        turn = Turn(clean[:2000], answer, evidence, kind, notices)
        self.turns.append(turn)
        if len(self.turns) > self.MAX_TURNS:
            self.turns = self.turns[-self.MAX_TURNS:]
        return turn

    def export_markdown(self) -> str:
        mode = "DEMO / SYNTHETIC — not empirical economic findings" if self.settings.demo_mode else "FRED snapshot mode; inspect each answer's freshness"
        lines = ["# Executed FRED conversation", "", mode,
                 f"Exported UTC: {datetime.now(timezone.utc).isoformat()}",
                 f"Interpretation: {'OpenAI configured; see per-turn notices' if self.provider else 'deterministic offline/supported parser; no live model'}; model configuration: {self.settings.llm_model}.",
                 "At most the latest thirty session turns are retained. Numeric evidence tables and snapshots are authoritative."]
        for index, turn in enumerate(self.turns, 1):
            lines += ["", f"## Turn {index}", "", "User:", "", "> " + turn.prompt.replace("\n", "\n> "), "", "Application:", "", turn.answer]
            if turn.evidence:
                lines += ["", f"Result ID: `{turn.evidence.result_id}`. Chart: {turn.chart_kind or 'none'}.", "", "```json", turn.evidence.model_dump_json(indent=2), "```"]
        return self._redact("\n".join(lines) + "\n")
