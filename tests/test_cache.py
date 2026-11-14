"""Immutable cache and fallback behavior on explicitly synthetic snapshots."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fred_chat.cache import DataService, SnapshotCache
from fred_chat.config import Settings
from fred_chat.fred_client import DataError
from fred_chat.schemas import Observation, SeriesMetadata, Snapshot


def snapshot(identifier="fixture-v1", value=100.0, *, retrieved_at=None):
    return Snapshot(
        snapshot_id=identifier,
        series="GDP",
        metadata=SeriesMetadata(id="GDP", title="DEMO / SYNTHETIC nominal GDP, not empirical findings", frequency="Quarterly", units="Billions of Dollars", seasonal_adjustment="Seasonally Adjusted Annual Rate", last_updated="2025-01-01", source_url="https://fred.stlouisfed.org/series/GDP"),
        observations=[Observation(date=date(2020, 1, 1), value=value)],
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        source_urls=["https://fred.stlouisfed.org/series/GDP"],
        realtime_parameters={"realtime_start": "2025-01-01", "realtime_end": "2025-01-01"},
        raw_responses={"fixture": "DEMO / SYNTHETIC, not empirical findings"},
    )


class StubClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def fetch(self, series):
        self.calls.append(series)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def settings(tmp_path, **kwargs):
    return Settings(cache_dir=tmp_path, fred_api_key="synthetic-key", **kwargs)


def test_save_roundtrip_and_manifest_has_provenance_checksums(tmp_path):
    cache = SnapshotCache(tmp_path)
    original = snapshot()
    manifest = cache.save(original)
    restored = cache.latest("GDP")
    assert restored == original
    assert isinstance(manifest, dict)
    text = str(manifest)
    assert original.snapshot_id in text
    assert "sha256" in text.lower() or "checksum" in text.lower()
    assert "GDP" in text
    assert "2020-01-01" in text
    assert "synthetic-key" not in text
    assert list(tmp_path.rglob("*.csv"))
    assert list(tmp_path.rglob("*.json"))


def test_cache_hit_does_not_call_http(tmp_path):
    cache = SnapshotCache(tmp_path)
    stored = snapshot()
    cache.save(stored)
    client = StubClient()
    result = DataService(settings(tmp_path), client=client, cache=cache).get("GDP")
    assert result.snapshot.snapshot_id == stored.snapshot_id
    assert not result.stale
    assert not client.calls


def test_force_refresh_retains_previous_snapshot_and_revised_observation(tmp_path):
    cache = SnapshotCache(tmp_path)
    original = snapshot()
    cache.save(original)
    revised = snapshot("fixture-v2", value=110.0, retrieved_at=original.retrieved_at + timedelta(seconds=1))
    client = StubClient(revised)
    result = DataService(settings(tmp_path), client=client, cache=cache).get("GDP", refresh=True)
    assert len(client.calls) == 1
    assert result.snapshot.snapshot_id == "fixture-v2"
    assert result.snapshot.observations[0].value == 110
    assert cache.latest("GDP").snapshot_id == "fixture-v2"
    assert any("fixture-v1" in p.as_posix() for p in tmp_path.rglob("*"))


def test_expired_cache_attempts_refresh(tmp_path):
    old = snapshot(retrieved_at=datetime.now(timezone.utc) - timedelta(hours=7))
    fresh = snapshot("fixture-v2")
    cache = SnapshotCache(tmp_path)
    cache.save(old)
    client = StubClient(fresh)
    result = DataService(settings(tmp_path), client=client, cache=cache).get("GDP")
    assert len(client.calls) == 1
    assert result.snapshot.snapshot_id == fresh.snapshot_id


def test_failed_forced_refresh_marks_stale_and_preserves_original_retrieval(tmp_path):
    cache = SnapshotCache(tmp_path)
    original = snapshot()
    cache.save(original)
    failure = DataError("Network unavailable", code="network")
    client = StubClient(failure)
    result = DataService(settings(tmp_path), client=client, cache=cache).get("GDP", refresh=True)
    assert result.stale
    assert result.snapshot.retrieved_at == original.retrieved_at
    assert result.snapshot.snapshot_id == original.snapshot_id
    assert "stale" in " ".join(result.warnings).lower()
    assert original.retrieved_at.isoformat() in " ".join(result.warnings)
    assert cache.latest("GDP").snapshot_id == original.snapshot_id


def test_no_cache_and_failed_fetch_returns_useful_error(tmp_path):
    client = StubClient(DataError("Network unavailable", code="network"))
    with pytest.raises(DataError):
        DataService(settings(tmp_path), client=client).get("GDP")


def test_demo_mode_never_fetches_and_is_labeled(tmp_path):
    client = StubClient()
    result = DataService(settings(tmp_path, demo_mode=True), client=client).get("GDP", refresh=True)
    assert not client.calls
    assert result.snapshot.demo
    assert result.snapshot.observations
    assert "synthetic" in " ".join(result.warnings).lower()


def test_cache_rejects_snapshot_payload_tampering(tmp_path):
    cache = SnapshotCache(tmp_path)
    original = snapshot()
    cache.save(original)
    changed = False
    for path in tmp_path.rglob("*.json"):
        content = path.read_text()
        if '"observations"' in content and '"value"' in content:
            path.write_text(content.replace('100.0', '999.0'))
            changed = True
            break
    assert changed, "Expected a persisted snapshot JSON payload"
    try:
        loaded = cache.latest("GDP")
    except DataError:
        loaded = None
    assert loaded is None, "Checksum mismatch must not silently return modified data"


def test_saving_same_snapshot_identifier_with_new_content_is_rejected(tmp_path):
    cache = SnapshotCache(tmp_path)
    cache.save(snapshot())
    with pytest.raises((DataError, ValueError, FileExistsError)):
        cache.save(snapshot(value=999.0))


def test_corrupt_newest_snapshot_preserves_valid_fallback_and_notice(tmp_path):
    cache = SnapshotCache(tmp_path)
    original = snapshot()
    newest = snapshot("fixture-v2", value=110, retrieved_at=original.retrieved_at + timedelta(seconds=1))
    cache.save(original)
    cache.save(newest)
    (tmp_path / "GDP" / newest.snapshot_id / "manifest.json").write_text("{invalid")
    client = StubClient(DataError("Network unavailable", code="network"))
    result = DataService(settings(tmp_path), client=client, cache=cache).get("GDP", refresh=True)
    assert result.snapshot.snapshot_id == original.snapshot_id
    assert result.stale
    assert "corrupt" in " ".join(result.warnings).lower()
    assert "STALE" in " ".join(result.warnings)


def test_corrupt_older_snapshot_does_not_hide_valid_latest(tmp_path):
    cache = SnapshotCache(tmp_path)
    original = snapshot()
    newest = snapshot("fixture-v2", retrieved_at=original.retrieved_at + timedelta(seconds=1))
    cache.save(original)
    cache.save(newest)
    (tmp_path / "GDP" / original.snapshot_id / "observations.csv").write_text("tampered")
    restored, warnings = cache.latest_with_warnings("GDP")
    assert restored.snapshot_id == newest.snapshot_id
    assert warnings


def test_newer_demo_cannot_hide_valid_live_snapshot(tmp_path):
    cache = SnapshotCache(tmp_path)
    original = snapshot()
    fixture = snapshot("newer-demo", retrieved_at=original.retrieved_at + timedelta(seconds=1)).model_copy(update={"demo": True})
    cache.save(original)
    cache.save(fixture)
    client = StubClient()
    result = DataService(settings(tmp_path), client=client, cache=cache).get("GDP")
    assert result.snapshot.snapshot_id == original.snapshot_id
    assert not result.snapshot.demo
    assert not client.calls


def test_snapshot_payloads_are_published_atomically(tmp_path, monkeypatch):
    import os
    real_link = os.link
    published = []
    def inspect_link(source, destination):
        assert Path(source).read_bytes(), "Only complete temporary payloads may be published"
        assert not Path(destination).exists()
        published.append(Path(destination).name)
        real_link(source, destination)
    monkeypatch.setattr(os, "link", inspect_link)
    SnapshotCache(tmp_path).save(snapshot())
    assert published[-1] == "manifest.json"
    assert not list(tmp_path.rglob(".snapshot-*"))
