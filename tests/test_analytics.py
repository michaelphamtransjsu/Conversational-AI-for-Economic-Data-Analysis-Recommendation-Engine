from datetime import date

import pandas as pd
import pytest

from fred_chat.analytics import AnalyticsError, history, latest, native_series, transform
from fred_chat.demo import demo_snapshot
from fred_chat.schemas import Metric, Observation, SeriesID


def snapshot(series, values, start="2020-01-01", drop=()):
    base = demo_snapshot(series)
    dates = pd.date_range(start, periods=len(values), freq="QS" if series == SeriesID.GDP else "MS")
    return base.model_copy(update={"observations": [Observation(date=d.date(), value=v) for i, (d, v) in enumerate(zip(dates, values)) if i not in drop]})


def test_gdp_formulas_are_native_period_changes():
    s = snapshot(SeriesID.GDP, [100, 102, 105, 108, 110])
    assert transform(s, Metric.yoy).iloc[-1] == pytest.approx(10)
    assert transform(s, Metric.qoq).iloc[1] == pytest.approx(2)
    assert transform(s, Metric.qoq_annualized).iloc[1] == pytest.approx(8.243216)


def test_cpi_yoy_and_mom_use_exact_baselines():
    s = snapshot(SeriesID.CPILFESL, [200] * 11 + [205, 206])
    assert transform(s, Metric.yoy).iloc[-1] == pytest.approx(3)
    assert transform(s, Metric.mom).iloc[-1] == pytest.approx(0.48780487804878)
    e = latest(s)
    assert e.metric == "yoy"
    assert e.summary["mom_value"] == pytest.approx(0.48780487804878)
    assert "seasonally adjusted" in " ".join(e.warnings)


def test_missing_month_reindexed_before_lagging():
    s = snapshot(SeriesID.CPILFESL, [200 + i for i in range(15)], drop=(1,))
    values = transform(s, Metric.yoy)
    assert len(values) == 15
    assert pd.isna(values.loc["2021-02"])
    assert values.loc["2021-03"] == pytest.approx(100*(214/202-1))


def test_missing_quarter_does_not_shift_calendar_lag():
    s = snapshot(SeriesID.GDP, [100, 101, 102, 103, 110, 111], drop=(1,))
    values = transform(s, Metric.yoy)
    assert values.loc["2021Q1"] == pytest.approx(10)
    assert pd.isna(values.loc["2021Q2"])


def test_null_latest_discloses_earlier_observation_period():
    e = latest(snapshot(SeriesID.GDP, [100, 102, 103, 104, 110, None]), Metric.level)
    assert e.summary["period"] == "2021Q1"
    assert any("Newest dated row 2021Q2 is missing" in w for w in e.warnings)


def test_latest_metric_missing_baseline_discloses_fallback():
    e = latest(snapshot(SeriesID.GDP, [100, None, 102, 103, 110, 111]), Metric.yoy)
    assert e.summary["period"] == "2021Q1"
    assert e.summary["latest_valid_level_period"] == "2021Q2"
    assert any("Latest computable" in w for w in e.warnings)


def test_zero_denominator_is_gap_and_warning_not_infinity():
    s = snapshot(SeriesID.GDP, [0, 100, 105, 110, 115, 120])
    assert pd.isna(transform(s, Metric.yoy).iloc[4])
    assert transform(s, Metric.yoy).iloc[5] == pytest.approx(20)
    e = history(s, Metric.yoy, "2021-01-01", "2021-06-30")
    assert e.records[0]["value"] is None
    assert any("Nonpositive" in w for w in e.warnings)


def test_prewindow_baseline_calculated_before_trim():
    s = snapshot(SeriesID.CPILFESL, list(range(200, 225)))
    e = history(s, Metric.yoy, "2021-01-01", "2021-12-31")
    assert len(e.records) == 12
    assert e.records[0]["value"] == pytest.approx(6)
    assert e.records[0]["baseline"] == 200


def test_rate_difference_percentage_points_while_price_index_rises():
    s = snapshot(SeriesID.CPILFESL, [200] * 12 + [208, 209.04])
    # Make the second baseline higher so inflation falls from 4% to 3%.
    s.observations[1].value = 209.04 / 1.03
    e = history(s, Metric.yoy, "2021-01-01", "2021-02-01")
    assert e.summary["change_pp"] == pytest.approx(-1)
    assert e.records[1]["level"] > e.records[0]["level"]
    assert "percent_change" not in e.summary


def test_level_endpoint_changes_name_exact_periods():
    e = history(snapshot(SeriesID.GDP, [100, 110]), Metric.level, "2020-01-01", "2020-12-31")
    assert e.summary["absolute_change"] == 10
    assert e.summary["percent_change"] == pytest.approx(10)
    assert e.summary["first_period"] == "2020Q1"
    assert e.summary["last_period"] == "2020Q2"
    assert e.provenance[0]["demo"]
    assert e.provenance[0]["retrieved_at_utc"]


@pytest.mark.parametrize("values,metric,start,end,message", [
    ([100], Metric.yoy, "2020-01-01", "2020-12-31", "No valid metric"),
    ([None], Metric.level, "2020-01-01", "2020-12-31", "No valid metric"),
    ([100], Metric.level, "2022-01-01", "2022-12-31", "no observation periods"),
])
def test_unavailable_history_is_clear(values, metric, start, end, message):
    with pytest.raises(AnalyticsError, match=message):
        history(snapshot(SeriesID.GDP, values), metric, start, end)


def test_invalid_native_date_and_empty_snapshot():
    s = snapshot(SeriesID.GDP, [100])
    s.observations[0].date = date(2020, 2, 1)
    with pytest.raises(AnalyticsError, match="native calendar"):
        native_series(s)
    with pytest.raises(AnalyticsError, match="no observations"):
        native_series(s.model_copy(update={"observations": []}))


def test_metric_default_yoy_and_incompatible_metrics_rejected():
    assert latest(snapshot(SeriesID.GDP, [100, 101, 102, 103, 110])).summary["value"] == pytest.approx(10)
    with pytest.raises(ValueError, match="quarterly"):
        transform(snapshot(SeriesID.GDP, [100, 110]), Metric.mom)
