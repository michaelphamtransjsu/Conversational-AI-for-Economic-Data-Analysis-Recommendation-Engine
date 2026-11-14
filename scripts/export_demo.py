"""Export actual charts/data from fixed synthetic fixtures; never call live services.

The data and calculation windows are deterministic. Export timestamps and Plotly's
HTML element IDs can vary between executions. Checksums describe the actual files
written by this execution, not claims of live retrieval or empirical findings.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path

import pandas as pd
from PIL import Image

from fred_chat.alignment import compare
from fred_chat.analytics import history
from fred_chat.cache import SnapshotCache
from fred_chat.charts import caption, data_csv, figure_html, figure_png
from fred_chat.demo import DEMO_LABEL, FIXTURE_TIMESTAMP, demo_snapshot
from fred_chat.schemas import Evidence, Metric, SeriesID

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.external = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            values = dict(attrs)
            if values.get("src"):
                self.external.append(values["src"])


def validate_chart_exports(directory: Path, name: str, evidence: Evidence, expected_rows: int) -> dict:
    """Read back real PNG/HTML/CSV/JSON outputs and verify their contracts."""
    figure_dir = directory / "figures"
    sample_dir = directory / "sample_data"
    png_path = figure_dir / f"{name}.png"
    html_path = figure_dir / f"{name}.html"
    csv_path = sample_dir / f"{name}.csv"
    evidence_path = sample_dir / f"{name}.evidence.json"
    png = png_path.read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n"), "Invalid PNG signature"
    with Image.open(BytesIO(png)) as image:
        image.verify()
    with Image.open(BytesIO(png)).convert("RGB") as image:
        width, height = image.size
        assert width == 1400 and height > 600
        assert image.crop((0, height - 10, width, height)).getextrema() == ((255, 255), (255, 255), (255, 255)), "Caption may be clipped at the bottom edge"
    html = html_path.read_text(encoding="utf-8")
    scripts = _Scripts()
    scripts.feed(html)
    assert not scripts.external, "Portable HTML must embed its JavaScript"
    assert "plotly.js" in html and "SYNTHETIC" in html
    for source in evidence.provenance:
        assert source["snapshot_id"] in html
        assert source["retrieved_at_utc"] in html
        assert source["source_url"] in html
    csv = pd.read_csv(csv_path, float_precision="round_trip")
    assert len(csv) == len(evidence.records) == expected_rows
    assert csv.data_mode.str.contains("SYNTHETIC").all()
    assert csv.metric.eq(evidence.metric).all()
    assert csv.metric_units.eq(evidence.units).all()
    assert csv.formulas.eq("; ".join(evidence.formulas)).all()
    assert json.loads(csv.provenance_json.iloc[0]) == evidence.provenance
    for column in ("value", "gdp_yoy", "cpi_yoy"):
        if column in csv:
            assert csv[column].tolist() == [record[column] for record in evidence.records]
    loaded = Evidence.model_validate_json(evidence_path.read_text(encoding="utf-8"))
    assert loaded == evidence
    return {"status": "PASS", "rows": expected_rows, "png_size": [width, height],
            "verified": ["PNG signature/readability/footer margin", "portable embedded-JS HTML",
                         "CSV exact float round trip and full provenance", "Pydantic evidence JSON round trip"]}


def main() -> int:
    directory = PROJECT_ROOT / "artifacts"
    figures = directory / "figures"
    samples = directory / "sample_data"
    figures.mkdir(parents=True, exist_ok=True)
    samples.mkdir(parents=True, exist_ok=True)
    exported_at = datetime.now(timezone.utc).isoformat()
    snapshots = {series: demo_snapshot(series) for series in SeriesID}
    snapshot_root = samples / "snapshots"
    cache = SnapshotCache(snapshot_root)
    manifests = []
    for series, snapshot in snapshots.items():
        manifest = cache.save(snapshot)
        assert cache.latest(series) == snapshot
        manifest["directory"] = (snapshot_root / series.value / snapshot.snapshot_id).relative_to(directory).as_posix()
        manifests.append(manifest)
    gdp = snapshots[SeriesID.GDP]
    cpi = snapshots[SeriesID.CPILFESL]
    scenarios = [
        ("gdp_level_ten_years", history(gdp, Metric.level, "2016-01-01", "2025-12-31"), 40),
        ("core_cpi_yoy_five_years", history(cpi, Metric.yoy, "2021-01-01", "2025-12-31"), 60),
        ("quarterly_gdp_core_cpi_comparison", compare(gdp, cpi, "2016-01-01", "2025-12-31"), 40),
    ]
    validations = {}
    for name, evidence, expected_rows in scenarios:
        # Stable public result IDs make JSON/CSV results reproducible. No economic
        # data, formula or analytics implementation is modified for an export.
        evidence.result_id = f"demo_{name}"
        (figures / f"{name}.png").write_bytes(figure_png(evidence))
        (figures / f"{name}.html").write_bytes(figure_html(evidence))
        (samples / f"{name}.csv").write_bytes(data_csv(evidence))
        (samples / f"{name}.evidence.json").write_bytes(_json_bytes(evidence.model_dump(mode="json")))
        validations[name] = validate_chart_exports(directory, name, evidence, expected_rows)
    (samples / "README.md").write_text(
        "# DEMO / SYNTHETIC data — not empirical economic findings\n\n"
        "All observations in this directory are generated by `fred_chat.demo.demo_snapshot`, not downloaded from FRED. "
        "The fixed timestamp 2026-01-15T12:00:00+00:00 is fixture metadata, not a live retrieval or a publication date.\n\n"
        "The analytical CSVs repeat units, formulas, source URLs, snapshot IDs, requested windows, and provenance on every row. "
        "Their matching `.evidence.json` files contain complete computed evidence. Raw full-history snapshot CSVs under `snapshots/` "
        "must be read with their adjacent `manifest.json` and `snapshot.json`; these manifests label DEMO data and preserve source definitions and checksums.\n\n"
        "GDP chart: 2016Q1–2025Q4, 40 quarters. Core CPI chart: 2021-01–2025-12, 60 months, YoY calculated from pre-window history. "
        "Comparison: 2016Q1–2025Q4, 40 paired quarters, nominal GDP YoY versus YoY change of complete quarterly-average CPI. "
        "These are reproducibility examples, not findings about the U.S. economy.\n",
        encoding="utf-8")
    manifest = {
        "label": DEMO_LABEL,
        "mode": "DEMO / SYNTHETIC, not empirical findings",
        "series_ids": [series.value for series in SeriesID],
        "exported_at_utc": exported_at,
        "fixture_timestamp_utc": FIXTURE_TIMESTAMP.isoformat(),
        "fixture_timestamp_note": "Fixed synthetic provenance; no FRED retrieval, source update, or release occurred.",
        "generator": "fred_chat.demo.demo_snapshot v1",
        "generator_source_sha256": sha256((PROJECT_ROOT / "src/fred_chat/demo.py").read_bytes()).hexdigest(),
        "live_fred_validated": False,
        "llm_used": False,
        "snapshot_reproduction_note": "Snapshots reproduce these synthetic observations only. They are not historical FRED vintages.",
        "snapshots": manifests,
        "exports": [{"name": name, "result_id": evidence.result_id, "requested_start": str(evidence.start),
                     "requested_end": str(evidence.end), "rows": count, "caption": caption(evidence)}
                    for name, evidence, count in scenarios],
    }
    (directory / "snapshot_manifest.json").write_bytes(_json_bytes(manifest))
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "export_index.json":
            data = path.read_bytes()
            files.append({"path": path.relative_to(directory).as_posix(), "bytes": len(data), "sha256": sha256(data).hexdigest()})
    index = {"label": DEMO_LABEL, "exported_at_utc": exported_at,
             "checksums_note": "Checksums describe actual files. export_index.json excludes itself to avoid a circular checksum.",
             "validation": validations, "files": files}
    (directory / "export_index.json").write_bytes(_json_bytes(index))
    # Verify every recorded checksum against the actual deliverable bytes.
    for item in json.loads((directory / "export_index.json").read_text())["files"]:
        data = (directory / item["path"]).read_bytes()
        assert len(data) == item["bytes"] and sha256(data).hexdigest() == item["sha256"]
    print(json.dumps({"status": "PASS", "mode": DEMO_LABEL, "chart_sets": len(scenarios),
                      "files_checksummed": len(files), "validated": validations,
                      "manifest": "artifacts/snapshot_manifest.json", "index": "artifacts/export_index.json"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
