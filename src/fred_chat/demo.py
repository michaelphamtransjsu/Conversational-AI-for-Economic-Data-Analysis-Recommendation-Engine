"""Deterministic synthetic fixtures, never historical FRED observations.

The generated series spans 2010–2025 to exercise all required windows. Formulas
provide variable rates, not plausible claims about the actual U.S. economy.
"""
from datetime import date, datetime, timezone
import math

from .fred_client import EXPECTED, SOURCE_URL
from .schemas import Observation, SeriesID, SeriesMetadata, Snapshot

DEMO_LABEL = "DEMO / SYNTHETIC — not empirical economic findings"
FIXTURE_TIMESTAMP = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def demo_snapshot(series: SeriesID | str) -> Snapshot:
    series = SeriesID(series)
    frequency, units, adjustment = EXPECTED[series]
    observations = []
    value = 15000.0 if series == SeriesID.GDP else 210.0
    steps = 64 if series == SeriesID.GDP else 192
    for n in range(steps):
        step_month = n * 3 if series == SeriesID.GDP else n
        year, month_index = divmod(step_month, 12)
        period = date(2010 + year, month_index + 1, 1)
        if n:
            rate = (0.011 + 0.005 * math.sin(n / 3.0)) if series == SeriesID.GDP else (0.002 + 0.001 * math.sin(n / 11.0))
            value *= 1 + rate
        observations.append(Observation(date=period, value=value))
    return Snapshot(snapshot_id=f"DEMO-{series.value}-v1", series=series,
                    metadata=SeriesMetadata(id=series, title=f"{DEMO_LABEL}: {series.value}",
                        frequency=frequency, units=units, seasonal_adjustment=adjustment,
                        last_updated="SYNTHETIC fixture version 1; no source update or release date",
                        source_url=SOURCE_URL + series.value),
                    observations=observations, retrieved_at=FIXTURE_TIMESTAMP,
                    source_urls=[SOURCE_URL + series.value],
                    realtime_parameters={"request": "none; synthetic fixture"},
                    raw_responses={"fixture": "deterministic synthetic generator fred_chat.demo.demo_snapshot v1",
                                   "label": DEMO_LABEL}, demo=True)
