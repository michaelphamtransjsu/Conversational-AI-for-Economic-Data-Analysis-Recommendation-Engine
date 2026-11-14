"""No service is called unless RUN_LIVE_TESTS=1 and credentials are available."""
import os
import pytest


@pytest.mark.live
@pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="Opt-in live smoke; credentials/network required")
def test_live_native_series():
    from fred_chat.config import load_settings
    from fred_chat.fred_client import FredClient
    from fred_chat.schemas import SeriesID
    settings = load_settings()
    if settings.demo_mode or not settings.fred_api_key:
        pytest.skip("Live FRED key required and DEMO_MODE must be false")
    client = FredClient(settings.fred_api_key)
    for series in SeriesID:
        snapshot = client.fetch(series)
        assert not snapshot.demo
        assert any(x.value is not None for x in snapshot.observations)
        assert snapshot.metadata.id == series
