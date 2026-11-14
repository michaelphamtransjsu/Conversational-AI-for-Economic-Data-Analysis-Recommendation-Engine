"""Chart/data/export checks on explicitly synthetic evidence, without live APIs."""
from io import BytesIO
import json

import pandas as pd
import pytest

from fred_chat.analytics import history
from fred_chat.alignment import compare
from fred_chat.charts import ChartError, build_figure, caption, data_csv, figure_html, figure_png
from fred_chat.demo import demo_snapshot
from fred_chat.schemas import Metric, SeriesID


@pytest.fixture
def gdp():
    return history(demo_snapshot(SeriesID.GDP), Metric.level, "2015-01-01", "2025-12-31")


@pytest.fixture
def comparison():
    return compare(demo_snapshot(SeriesID.GDP), demo_snapshot(SeriesID.CPILFESL), "2015-01-01", "2025-12-31")


def test_gdp_level_units_and_missing_gap(gdp):
    gdp.records[3]["value"] = None
    fig = build_figure(gdp)
    assert "Nominal GDP level" in fig.layout.title.text
    assert "SAAR" in fig.layout.yaxis.title.text
    assert fig.data[0].connectgaps is False
    assert fig.data[0].y[3] is None
    assert len(fig.data[0].x) == len(gdp.records)


def test_comparison_uses_one_percentage_axis_exact_pairs_and_gaps(comparison):
    comparison.records.pop(4)
    fig = build_figure(comparison)
    assert len(fig.data) == 2
    assert all(trace.yaxis in (None, "y") for trace in fig.data)
    assert fig.layout.yaxis.title.text == "Year-over-year change (%)"
    plotted = [(x, g, c) for x, g, c in zip(fig.data[0].x, fig.data[0].y, fig.data[1].y) if g is not None and c is not None]
    expected = [(r["date"], r["gdp_yoy"], r["cpi_yoy"]) for r in comparison.records]
    assert plotted == expected
    assert any(y is None for y in fig.data[0].y)
    assert all(trace.connectgaps is False for trace in fig.data)


def test_scatter_uses_exact_pairs(comparison):
    fig = build_figure(comparison, "scatter")
    assert list(fig.data[0].x) == [r["gdp_yoy"] for r in comparison.records]
    assert list(fig.data[0].y) == [r["cpi_yoy"] for r in comparison.records]
    assert "GDP" in fig.layout.xaxis.title.text
    assert "CPI" in fig.layout.yaxis.title.text


def test_csv_preserves_rows_and_provenance(gdp):
    csv = pd.read_csv(BytesIO(data_csv(gdp)), float_precision="round_trip")
    assert len(csv) == len(gdp.records)
    assert csv.value.tolist() == [r["value"] for r in gdp.records]
    assert set(csv.metric) == {"level"}
    assert csv.data_mode.str.contains("SYNTHETIC").all()
    provenance = json.loads(csv.provenance_json.iloc[0])
    assert provenance == gdp.provenance
    assert "retrieved_at_utc" in provenance[0]
    assert csv.formulas.iloc[0] == "; ".join(gdp.formulas)


def test_html_portable_and_full_caption(comparison):
    document = figure_html(comparison).decode()
    assert "plotly.js" in document
    assert '<script src="' not in document
    assert "SYNTHETIC" in document
    assert "quarterly-average" in document
    for source in comparison.provenance:
        assert source["snapshot_id"] in document
        assert source["retrieved_at_utc"] in document
        assert source["source_url"] in document


def test_caption_discloses_stale(gdp):
    gdp.warnings.append("STALE snapshot: refresh failed.")
    text = caption(gdp)
    assert "STALE DATA" in text
    assert "not a release date" in text
    assert "2015-01-01 to 2025-12-31" in text
    assert "SAAR" in text
    assert "not empirical findings" in text


def test_actual_png_export_has_png_signature(gdp):
    png = figure_png(gdp)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 20_000
    from PIL import Image
    image = Image.open(BytesIO(png)).convert("RGB")
    # The footer must end above the bottom edge, not be truncated at it.
    assert image.crop((0, image.height - 10, image.width, image.height)).getextrema() == ((255, 255), (255, 255), (255, 255))


def test_export_failures_are_clear(gdp, monkeypatch):
    import plotly.graph_objects as go
    monkeypatch.setattr(go.Figure, "to_image", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("private underlying error")))
    with pytest.raises(ChartError, match="PNG export is unavailable") as exc:
        figure_png(gdp)
    assert "private underlying error" not in str(exc.value)


def test_reject_unsupported_chart_or_mixed_units(gdp):
    with pytest.raises(ChartError, match="Only line"):
        build_figure(gdp, "python")
    with pytest.raises(ChartError, match="Scatter requires"):
        build_figure(gdp, "scatter")
    gdp.metric = "mixed"
    gdp.series = [SeriesID.GDP, SeriesID.CPILFESL]
    with pytest.raises(ChartError, match="separately"):
        build_figure(gdp)


def test_cpi_rate_label_distinguishes_index():
    cpi = history(demo_snapshot(SeriesID.CPILFESL), Metric.yoy, "2020-01-01", "2025-12-31")
    fig = build_figure(cpi)
    assert "inflation (%)" in fig.layout.title.text
    assert "seasonally adjusted" in fig.layout.title.text
    assert "percent" in fig.layout.yaxis.title.text
