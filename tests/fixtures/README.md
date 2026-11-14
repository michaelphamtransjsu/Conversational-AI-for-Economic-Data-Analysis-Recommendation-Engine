# Fixtures

**DEMO / SYNTHETIC — not empirical economic findings.**

`fred_chat.demo.demo_snapshot` deterministically generates 2010–2025 observations using a fixed starting level and sinusoidal variation in each period's multiplicative change. GDP has 64 quarterly observations; CPILFESL has 192 monthly observations. The fixture timestamp is fixed at 2026-01-15T12:00:00+00:00. Official series links document definitions only and do not claim these generated observations came from FRED.

Mock HTTP responses in `test_fred_client.py` use FRED's JSON shape but synthetic values. There are no frozen real FRED API responses in this build because live credentials were unavailable. `artifacts/sample_data/` deliberately stores generated snapshots and checksums for reproducibility.
