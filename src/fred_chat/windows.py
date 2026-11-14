"""Calendar windows shared by the dashboard and conversational defaults."""
from datetime import date, timedelta

import pandas as pd

from .schemas import SeriesID, Snapshot


def observation_end(snapshot: Snapshot) -> date:
    """End of the newest dated observation period, never a publication date."""
    latest = max(row.date for row in snapshot.observations)
    frequency = "Q-DEC" if snapshot.series == SeriesID.GDP else "M"
    return pd.Period(latest, freq=frequency).end_time.date()


def trailing_window(end: date, years: int) -> tuple[date, date]:
    if not 1 <= years <= 100:
        raise ValueError("Trailing windows must be one to one hundred years.")
    try:
        preceding = end.replace(year=end.year - years)
    except ValueError:
        preceding = end.replace(year=end.year - years, day=28)
    return preceding + timedelta(days=1), end
