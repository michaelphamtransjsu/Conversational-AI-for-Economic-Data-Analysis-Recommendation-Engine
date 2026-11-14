"""Validated public tools and durable evidence/snapshot data contracts."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from math import isfinite
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SeriesID(str, Enum):
    GDP = "GDP"
    CPILFESL = "CPILFESL"


class Metric(str, Enum):
    level = "level"
    yoy = "yoy"
    qoq = "qoq"
    qoq_annualized = "qoq_annualized"
    mom = "mom"


class ChartKind(str, Enum):
    line = "line"
    scatter = "scatter"


class SeriesRequest(StrictModel):
    series: SeriesID
    metric: Metric | None = None

    @model_validator(mode="after")
    def compatible_metric(self):
        if self.series == SeriesID.GDP and self.metric == Metric.mom:
            raise ValueError("GDP is quarterly; month-over-month is unsupported.")
        if self.series == SeriesID.CPILFESL and self.metric in {Metric.qoq, Metric.qoq_annualized}:
            raise ValueError("CPILFESL is monthly; use level, yoy, or mom. Quarterly comparison has a separate explicit definition.")
        return self


class DateWindow(StrictModel):
    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def explicit_calendar_date(cls, value):
        if isinstance(value, datetime):
            raise ValueError("Use a calendar date, not a timestamp.")
        if not isinstance(value, (str, date)):
            raise ValueError("Dates must use ISO YYYY-MM-DD format.")
        if isinstance(value, str):
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("Dates must use ISO YYYY-MM-DD format.") from exc
        return value

    @model_validator(mode="after")
    def valid_window(self):
        if self.start > self.end:
            raise ValueError("Start date must be on or before end date.")
        if self.start < date(1900, 1, 1) or self.end > date(2100, 12, 31):
            raise ValueError("Supported date range is 1900-01-01 through 2100-12-31.")
        return self


class LatestIndicator(SeriesRequest):
    pass


class AnalyzeHistory(SeriesRequest, DateWindow):
    pass


class CompareIndicators(DateWindow):
    pass


class SummarizeEconomy(DateWindow):
    pass


class BuildChart(StrictModel):
    result_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    kind: ChartKind = ChartKind.line


TOOL_SCHEMAS = {
    "latest_indicator": LatestIndicator,
    "analyze_history": AnalyzeHistory,
    "compare_indicators": CompareIndicators,
    "build_chart": BuildChart,
    "summarize_economy": SummarizeEconomy,
}


class Observation(StrictModel):
    date: date
    value: float | None

    @field_validator("value")
    @classmethod
    def finite_value(cls, value):
        if value is not None and not isfinite(value):
            raise ValueError("Observation values must be finite or null.")
        return value


class SeriesMetadata(StrictModel):
    id: SeriesID
    title: str
    frequency: str
    units: str
    seasonal_adjustment: str
    last_updated: str | None = None
    source_url: str


class Snapshot(StrictModel):
    snapshot_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    series: SeriesID
    metadata: SeriesMetadata
    observations: list[Observation]
    retrieved_at: datetime
    source_urls: list[str] = Field(default_factory=list)
    realtime_parameters: dict[str, str] = Field(default_factory=dict)
    raw_responses: dict[str, Any] = Field(default_factory=dict)
    demo: bool = False

    @field_validator("retrieved_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Retrieval timestamp must include its UTC offset.")
        return value

    @model_validator(mode="after")
    def matching_metadata_and_unique_dates(self):
        if self.metadata.id != self.series:
            raise ValueError("Snapshot metadata does not match its series.")
        dates = [row.date for row in self.observations]
        if len(dates) != len(set(dates)):
            raise ValueError("Duplicate observation dates are invalid.")
        return self


class Evidence(StrictModel):
    result_id: str
    operation: str
    series: list[SeriesID]
    metric: str
    units: str
    start: date | None = None
    end: date | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    formulas: list[str] = Field(default_factory=list)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    defaults: list[str] = Field(default_factory=list)
