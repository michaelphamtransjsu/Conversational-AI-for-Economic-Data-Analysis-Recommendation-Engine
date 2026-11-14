"""Small validated dispatcher. User/model input can select tools, never code."""
from __future__ import annotations

import json
from uuid import uuid4

from pydantic import ValidationError

from . import alignment, analytics
from .cache import DataResult, DataService
from .fred_client import DataError
from .schemas import ChartKind, Evidence, Metric, SeriesID, TOOL_SCHEMAS


class ToolError(ValueError):
    """Safe user-facing failure; never includes raw model/provider payloads."""

    def __init__(self, message: str, code: str = "tool_error"):
        super().__init__(message)
        self.code = code


class ToolDispatcher:
    """One instance per conversation session; evidence and IDs never cross sessions."""

    MAX_ARGUMENT_BYTES = 16_384
    MAX_RESULTS = 30

    def __init__(self, data_service: DataService):
        self.data_service = data_service
        self.results: dict[str, Evidence] = {}

    def _arguments(self, name: str, arguments: dict | str):
        if not isinstance(name, str) or name not in TOOL_SCHEMAS:
            raise ToolError("Unsupported tool. Use one of the documented economic tools.", "unsupported_tool")
        try:
            if isinstance(arguments, str):
                if len(arguments.encode("utf-8")) > self.MAX_ARGUMENT_BYTES:
                    raise ToolError("Tool arguments exceed the allowed size.", "invalid_arguments")
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ToolError("Tool arguments must be a JSON object.", "invalid_arguments")
            encoded = json.dumps(arguments, allow_nan=False).encode("utf-8")
            if len(encoded) > self.MAX_ARGUMENT_BYTES:
                raise ToolError("Tool arguments exceed the allowed size.", "invalid_arguments")
            return TOOL_SCHEMAS[name].model_validate(arguments)
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise ToolError("Invalid tool arguments. Check the supported series, metric, ISO dates, date order, chart type and required fields.", "invalid_arguments") from None

    @staticmethod
    def _with_data(evidence: Evidence, *data: DataResult) -> Evidence:
        """Pin freshness to the same snapshots that produced the numeric result."""
        by_id = {item.snapshot.snapshot_id: item for item in data}
        for source in evidence.provenance:
            item = by_id.get(source.get("snapshot_id"))
            if item is not None:
                source["stale"] = item.stale
                source["data_warnings"] = list(item.warnings)
        for item in data:
            evidence.warnings.extend(warning for warning in item.warnings if warning not in evidence.warnings)
            if item.stale and not any("stale" in warning.lower() for warning in evidence.warnings):
                evidence.warnings.append("STALE DATA: the last successful snapshot is being used because refresh failed.")
        return evidence

    def _remember(self, evidence: Evidence) -> Evidence:
        self.results[evidence.result_id] = evidence.model_copy(deep=True)
        while len(self.results) > self.MAX_RESULTS:
            self.results.pop(next(iter(self.results)))
        return evidence

    def execute(self, name: str, arguments: dict | str) -> Evidence:
        request = self._arguments(name, arguments)
        try:
            if name == "latest_indicator":
                data = self.data_service.get(request.series, refresh=True)
                evidence = self._with_data(analytics.latest(data.snapshot, request.metric), data)
            elif name == "analyze_history":
                data = self.data_service.get(request.series)
                evidence = self._with_data(analytics.history(data.snapshot, request.metric, request.start, request.end), data)
            elif name == "compare_indicators":
                # Fetch exactly once per series within this answer. Metadata,
                # chart rows and correlation all use these pinned snapshots.
                gdp = self.data_service.get(SeriesID.GDP)
                cpi = self.data_service.get(SeriesID.CPILFESL)
                evidence = self._with_data(alignment.compare(gdp.snapshot, cpi.snapshot, request.start, request.end), gdp, cpi)
            elif name == "summarize_economy":
                gdp = self.data_service.get(SeriesID.GDP)
                cpi = self.data_service.get(SeriesID.CPILFESL)
                components = [self._with_data(analytics.history(item.snapshot, Metric.yoy, request.start, request.end), item)
                              for item in (gdp, cpi)]
                records = [{**row, "series": component.series[0].value,
                            "row_metric": component.metric, "row_units": component.units}
                           for component in components for row in component.records]
                warnings = list(dict.fromkeys(warning for component in components for warning in component.warnings))
                warnings.append("These two indicators do not provide a comprehensive assessment of the economy.")
                evidence = Evidence(result_id="result_" + uuid4().hex, operation=name,
                    series=[SeriesID.GDP, SeriesID.CPILFESL], metric="economic_summary",
                    units="mixed; see row_units and component evidence", start=request.start, end=request.end,
                    records=records, summary={"components": [component.model_dump(mode="json") for component in components],
                        "requested_start": request.start.isoformat(), "requested_end": request.end.isoformat(),
                        "n": len(records)},
                    formulas=list(dict.fromkeys(formula for component in components for formula in component.formulas)),
                    provenance=[source for component in components for source in component.provenance], warnings=warnings,
                    defaults=["Summary uses nominal GDP YoY growth and monthly seasonally adjusted Core CPI YoY inflation.",
                              "Monthly and quarterly observations remain at their native frequencies; use compare_indicators for aligned association."])
            elif name == "build_chart":
                original = self.results.get(request.result_id)
                if original is None:
                    raise ToolError("The prior result is unavailable in this session. Analyze an indicator or comparison first.", "unknown_result")
                if original.metric == "economic_summary":
                    raise ToolError("Choose GDP, Core CPI, or the quarterly comparison before building a chart. The summary combines different frequencies and units.", "unsupported_chart")
                if request.kind == ChartKind.scatter and original.metric != "quarterly_yoy_comparison":
                    raise ToolError("Scatter requires paired quarterly GDP and Core CPI rate evidence.", "unsupported_chart")
                evidence = original.model_copy(deep=True)
                evidence.result_id = "result_" + uuid4().hex
                evidence.operation = "build_chart"
                evidence.summary.update(source_result_id=original.result_id, source_operation=original.operation,
                                        chart_kind=request.kind.value)
                evidence.defaults.append("Chart uses exactly the prior result's data and pinned snapshots; no new retrieval occurred.")
            else:  # Defense if schema and dispatcher registration ever diverge.
                raise ToolError("Unsupported economic operation.", "unsupported_tool")
            return self._remember(evidence)
        except ToolError:
            raise
        except analytics.AnalyticsError as exc:
            # Analytics errors are fixed templates with controlled period/count
            # substitutions; they do not contain provider input or secrets.
            raise ToolError(str(exc), "insufficient_evidence") from None
        except DataError as exc:
            messages = {
                "missing_key": "FRED_API_KEY is missing. Configure the server-side key or explicitly enable DEMO mode.",
                "authentication": "FRED rejected the configured credential. Check the server-side API key.",
                "authentication_error": "FRED rejected the configured credential. Check the server-side API key.",
                "rate_limit": "FRED rate limiting prevented retrieval. Try again later or use an existing snapshot.",
                "rate_limited": "FRED rate limiting prevented retrieval. Try again later or use an existing snapshot.",
                "network": "FRED could not be reached. Check network access or use an existing snapshot.",
                "network_error": "FRED could not be reached. Check network access or use an existing snapshot.",
                "timeout": "FRED timed out. Try again later or use an existing snapshot.",
                "transient_error": "FRED is temporarily unavailable after bounded retries. Try again later or use an existing snapshot.",
                "invalid_response": "FRED returned invalid observation data. Refresh later; no result was calculated.",
                "invalid_metadata": "FRED metadata does not match the required native GDP/Core CPI definitions.",
                "empty_data": "FRED returned no observations. No economic result is available.",
                "cache_corrupt": "Cached snapshot integrity validation failed. Refresh or inspect the snapshot cache.",
            }
            raise ToolError(messages.get(exc.code, "Economic data is unavailable. Check configuration, connectivity and snapshot integrity."), "data_unavailable") from None
        except Exception:
            raise ToolError("The economic tool could not complete. Check the selected operation and data availability.", "tool_failure") from None
