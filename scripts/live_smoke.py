"""Explicit, secret-safe live retrieval check; never fabricates a successful call."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from fred_chat.config import load_settings
from fred_chat.schemas import SeriesID


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true", help="Make real FRED HTTP requests")
    args = parser.parse_args()
    settings = load_settings()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "live": True, "series": {}}
    if not args.confirm_live:
        report.update(status="NOT_RUN", reason="Pass --confirm-live to opt into real requests.")
    elif settings.demo_mode:
        report.update(status="NOT_RUN", reason="Set DEMO_MODE=false for live validation.")
    elif not settings.fred_api_key:
        report.update(status="BLOCKED", reason="FRED_API_KEY is missing; no live requests made.")
    else:
        from fred_chat.fred_client import FredClient
        client = FredClient(settings.fred_api_key, timeout=settings.request_timeout_seconds)
        for series in SeriesID:
            try:
                snapshot = client.fetch(series)
                valid = [row for row in snapshot.observations if row.value is not None]
                report["series"][series.value] = {
                    "status": "PASS", "snapshot_id": snapshot.snapshot_id,
                    "retrieved_at": snapshot.retrieved_at.isoformat(),
                    "last_period": valid[-1].date.isoformat() if valid else None,
                    "observations": len(snapshot.observations),
                    "source_last_updated": snapshot.metadata.last_updated,
                }
            except Exception:
                # Never stringify an exception that could contain key-bearing URLs.
                report["series"][series.value] = {"status": "FAIL", "reason": "Live retrieval failed; inspect configuration and network safely."}
        report["status"] = "PASS" if all(x["status"] == "PASS" for x in report["series"].values()) else "FAIL"
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
