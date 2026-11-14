"""Immutable on-disk JSON/CSV snapshots with verified manifests and stale fallback."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile

from pydantic import ValidationError

from .config import Settings
from .demo import DEMO_LABEL, demo_snapshot
from .fred_client import DataError, FredClient
from .schemas import SeriesID, Snapshot


def _bytes_json(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


class SnapshotCache:
    def __init__(self, root: Path | str, ttl_seconds: int = 21600):
        self.root = Path(root)
        self.ttl_seconds = ttl_seconds

    def _write_once(self, path: Path, data: bytes):
        if path.exists():
            if path.read_bytes() != data:
                raise DataError("An immutable snapshot ID already contains different data.", "snapshot_conflict")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish a complete file atomically without replacing immutable content.
        # The manifest is published last, after every payload is complete.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".snapshot-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise DataError("Concurrent snapshot content conflict.", "snapshot_conflict") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save(self, snapshot: Snapshot) -> dict:
        directory = self.root / snapshot.series.value / snapshot.snapshot_id
        payload = snapshot.model_dump(mode="json")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["date", "value"])
        writer.writerows((row.date.isoformat(), "" if row.value is None else repr(row.value)) for row in snapshot.observations)
        files = {"snapshot.json": _bytes_json(payload), "observations.csv": output.getvalue().encode(),
                 "raw.json": _bytes_json(snapshot.raw_responses)}
        for name, data in files.items():
            self._write_once(directory / name, data)
        manifest = {
            "snapshot_id": snapshot.snapshot_id,
            "series_ids": [snapshot.series.value],
            "source_urls": snapshot.source_urls,
            "retrieved_at_utc": snapshot.retrieved_at.astimezone(timezone.utc).isoformat(),
            "source_last_updated": snapshot.metadata.last_updated,
            "observation_coverage": {"start": min(row.date for row in snapshot.observations).isoformat() if snapshot.observations else None,
                                     "end": max(row.date for row in snapshot.observations).isoformat() if snapshot.observations else None,
                                     "count": len(snapshot.observations)},
            "realtime_parameters": snapshot.realtime_parameters,
            "demo": snapshot.demo,
            "label": DEMO_LABEL if snapshot.demo else "Retrieved FRED snapshot; revisions may occur",
            "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in files.items()},
        }
        self._write_once(directory / "manifest.json", _bytes_json(manifest))
        return manifest

    def latest(self, series: SeriesID | str, include_demo: bool = True) -> Snapshot | None:
        snapshot, _ = self.latest_with_warnings(series, include_demo=include_demo)
        return snapshot

    def latest_with_warnings(self, series: SeriesID | str, include_demo: bool = True) -> tuple[Snapshot | None, list[str]]:
        """Select newest valid snapshot while retaining useful corruption notices."""
        series = SeriesID(series)
        paths = list((self.root / series.value).glob("*/manifest.json"))
        if not paths:
            return None, []
        snapshots, corrupt_count = [], 0
        for path in paths:
            try:
                manifest = json.loads(path.read_text())
                # File names are fixed, never use untrusted manifest paths for reads.
                for name in ("snapshot.json", "observations.csv", "raw.json"):
                    data = (path.parent / name).read_bytes()
                    if hashlib.sha256(data).hexdigest() != manifest["files"][name]["sha256"]:
                        raise ValueError
                snapshot = Snapshot.model_validate_json((path.parent / "snapshot.json").read_text())
                if snapshot.series != series or snapshot.snapshot_id != path.parent.name:
                    raise ValueError
                if include_demo or not snapshot.demo:
                    snapshots.append(snapshot)
            except (OSError, ValueError, KeyError, TypeError, ValidationError):
                corrupt_count += 1
        if not snapshots and corrupt_count:
            raise DataError("No valid cached snapshot remains after integrity validation; refresh or inspect the cache.", "cache_corrupt")
        warnings = ([f"Skipped {corrupt_count} corrupt cached snapshot(s); selected the newest valid eligible snapshot."]
                    if corrupt_count else [])
        return (max(snapshots, key=lambda snapshot: snapshot.retrieved_at) if snapshots else None), warnings

    def is_fresh(self, snapshot: Snapshot) -> bool:
        age = (datetime.now(timezone.utc) - snapshot.retrieved_at).total_seconds()
        return 0 <= age < self.ttl_seconds


@dataclass
class DataResult:
    snapshot: Snapshot
    warnings: list[str] = field(default_factory=list)
    stale: bool = False


class DataService:
    def __init__(self, settings: Settings, client: FredClient | None = None,
                 cache: SnapshotCache | None = None):
        self.settings = settings
        self.client = client or FredClient(settings.fred_api_key, settings.request_timeout_seconds)
        self.cache = cache or SnapshotCache(settings.cache_dir, settings.cache_ttl_seconds)

    def get(self, series: SeriesID | str, refresh: bool = False) -> DataResult:
        try:
            series = SeriesID(series)
        except ValueError:
            raise DataError("Only GDP and CPILFESL are supported.", "unsupported_series") from None
        if self.settings.demo_mode:
            snapshot = demo_snapshot(series)
            return DataResult(snapshot, [DEMO_LABEL, "Fixture timestamp is provenance, not a live retrieval. Refresh does not contact FRED in DEMO mode."])
        cached, cache_warning = None, []
        try:
            # Exclude fixtures before selecting: a newer fixture cannot hide an
            # older valid live snapshot. Corrupt entries do not erase fallback.
            cached, cache_warning = self.cache.latest_with_warnings(series, include_demo=False)
        except DataError:
            cache_warning = ["Cached snapshot failed integrity validation; attempting a fresh FRED retrieval."]
        if cached and not refresh and self.cache.is_fresh(cached):
            return DataResult(cached, cache_warning)
        try:
            snapshot = self.client.fetch(series)
        except DataError as exc:
            if cached:
                return DataResult(cached, cache_warning + [
                    f"STALE DATA: refresh failed ({exc.code}); using snapshot retrieved {cached.retrieved_at.isoformat()}. {exc}"], True)
            raise DataError(f"Data unavailable ({exc.code}). {exc}", exc.code) from None
        try:
            self.cache.save(snapshot)
        except (DataError, OSError):
            return DataResult(snapshot, cache_warning + ["Fresh FRED data was retrieved, but persisting its snapshot failed. This result remains pinned in memory."])
        return DataResult(snapshot, cache_warning)
