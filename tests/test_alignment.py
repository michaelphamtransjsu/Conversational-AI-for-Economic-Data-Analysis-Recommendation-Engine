import pandas as pd
import pytest

from fred_chat.alignment import compare, quarterly_cpi_levels
from fred_chat.analytics import AnalyticsError, transform
from fred_chat.demo import demo_snapshot
from fred_chat.schemas import Metric, Observation, SeriesID


def test_quarterly_cpi_requires_three_valid_months():
    s = demo_snapshot(SeriesID.CPILFESL)
    s.observations = s.observations[:8]
    s.observations[1].value = None
    means = quarterly_cpi_levels(s)
    assert pd.isna(means.loc["2010Q1"])
    assert means.loc["2010Q2"] == pytest.approx(sum(r.value for r in s.observations[3:6])/3)
    assert pd.isna(means.loc["2010Q3"])


def test_missing_month_quarter_and_yoy_baseline_are_excluded():
    gdp, cpi = demo_snapshot(SeriesID.GDP), demo_snapshot(SeriesID.CPILFESL)
    cpi.observations = [r for r in cpi.observations if r.date.isoformat() != "2020-02-01"]
    e = compare(gdp, cpi, "2019-01-01", "2023-12-31")
    periods = [r["period"] for r in e.records]
    assert "2020Q1" not in periods
    assert "2021Q1" not in periods
    assert e.summary["n"] == len(e.records) == 18


def test_comparison_uses_change_of_quarterly_means_not_mean_of_monthly_rates():
    gdp, cpi = demo_snapshot(SeriesID.GDP), demo_snapshot(SeriesID.CPILFESL)
    for i, value in zip([0, 1, 2, 12, 13, 14], [100, 200, 300, 200, 210, 330]):
        cpi.observations[i].value = value
    e = compare(gdp, cpi, "2011-01-01", "2025-12-31")
    quarterly = quarterly_cpi_levels(cpi)
    monthly_yoy = transform(cpi, Metric.yoy)
    row = e.records[0]
    q = pd.Period(row["period"], freq="Q-DEC")
    expected = 100*(quarterly.loc[q]/quarterly.loc[q-4]-1)
    monthly_average = monthly_yoy.loc[monthly_yoy.index.asfreq("Q-DEC") == q].mean()
    assert row["cpi_yoy"] == pytest.approx(expected)
    assert abs(row["cpi_yoy"] - monthly_average) > 1e-8
    assert expected == pytest.approx(100*(740/600-1))
    assert monthly_average == pytest.approx((100+5+10)/3)
    assert row["gdp_yoy"] == pytest.approx(transform(gdp, Metric.yoy).loc[q])


def test_correlation_sample_is_exactly_returned_chart_rows():
    e = compare(demo_snapshot(SeriesID.GDP), demo_snapshot(SeriesID.CPILFESL), "2015-01-01", "2025-12-31")
    rows = pd.DataFrame(e.records)
    assert e.summary["correlation"] == pytest.approx(rows.gdp_yoy.corr(rows.cpi_yoy))
    assert e.summary["n"] == len(rows)
    assert e.summary["first_period"] == rows.iloc[0].period
    assert e.summary["last_period"] == rows.iloc[-1].period
    assert "causality" in " ".join(e.warnings)


def test_short_empty_and_constant_samples_fail_clearly():
    gdp, cpi = demo_snapshot(SeriesID.GDP), demo_snapshot(SeriesID.CPILFESL)
    with pytest.raises(AnalyticsError, match="at least eight"):
        compare(gdp, cpi, "2025-01-01", "2025-12-31")
    with pytest.raises(AnalyticsError, match="this window has 0"):
        compare(gdp, cpi, "1900-01-01", "1901-01-01")
    gdp.observations = [Observation(date=r.date, value=100) for r in gdp.observations]
    with pytest.raises(AnalyticsError, match="constant"):
        compare(gdp, cpi, "2015-01-01", "2025-12-31")


def test_trailing_partial_cpi_quarter_does_not_enter_comparison():
    gdp, cpi = demo_snapshot(SeriesID.GDP), demo_snapshot(SeriesID.CPILFESL)
    cpi.observations = cpi.observations[:-1]
    e = compare(gdp, cpi, "2020-01-01", "2025-12-31")
    assert e.summary["last_period"] == "2025Q3"


def test_nonpositive_month_cannot_hide_inside_quarterly_average():
    cpi = demo_snapshot(SeriesID.CPILFESL)
    cpi.observations[1].value = 0
    means = quarterly_cpi_levels(cpi)
    assert pd.isna(means.loc["2010Q1"])
