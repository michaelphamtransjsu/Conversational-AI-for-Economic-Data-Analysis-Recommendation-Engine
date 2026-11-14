"""FRED-shaped synthetic HTTP responses; no live service calls or real findings."""

from datetime import date
import logging

import httpx
import pytest

from fred_chat.fred_client import DataError, FredClient
from fred_chat.schemas import SeriesID


KEY = "test-only-secret-do-not-export"


def metadata(series="GDP", **overrides):
    row = {
        "id": series,
        "title": "Synthetic fixture nominal GDP" if series == "GDP" else "Synthetic fixture core CPI",
        "frequency": "Quarterly" if series == "GDP" else "Monthly",
        "units": "Billions of Dollars" if series == "GDP" else "Index 1982-1984=100",
        "seasonal_adjustment": "Seasonally Adjusted Annual Rate" if series == "GDP" else "Seasonally Adjusted",
        "last_updated": "2025-01-01 00:00:00-06",
    }
    row.update(overrides)
    return {"seriess": [row]}


def page(rows, count=None, offset=0):
    return {
        "count": len(rows) if count is None else count,
        "offset": offset,
        "limit": len(rows),
        "realtime_start": "2025-01-01",
        "realtime_end": "2025-01-01",
        "observations": [{"date": d, "value": v} for d, v in rows],
    }


def make_client(handler, sleep=lambda _: None):
    return FredClient(KEY, transport=httpx.MockTransport(handler), sleep=sleep)


def test_native_full_history_pagination_missing_marker_and_safe_provenance():
    requested = []

    def handler(request):
        requested.append(request)
        assert request.url.params["api_key"] == KEY
        assert request.url.params["series_id"] == "GDP"
        if request.url.path.endswith("/observations"):
            assert request.url.params.get("units", "lin") == "lin"
            assert "frequency" not in request.url.params
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                return httpx.Response(200, json=page([("2020-01-01", "100"), ("2020-04-01", ".")], count=3))
            assert offset == 2
            return httpx.Response(200, json=page([("2020-07-01", "104")], count=3, offset=2))
        return httpx.Response(200, json=metadata())

    snapshot = make_client(handler).fetch(SeriesID.GDP)
    assert [r.date for r in snapshot.observations] == [date(2020, 1, 1), date(2020, 4, 1), date(2020, 7, 1)]
    assert [r.value for r in snapshot.observations] == [100.0, None, 104.0]
    assert snapshot.metadata.frequency == "Quarterly"
    assert snapshot.metadata.seasonal_adjustment == "Seasonally Adjusted Annual Rate"
    assert snapshot.retrieved_at.utcoffset().total_seconds() == 0
    assert len(requested) == 3
    serialized = snapshot.model_dump_json()
    assert KEY not in serialized
    assert "api_key" not in serialized
    assert snapshot.source_urls
    assert snapshot.realtime_parameters


@pytest.mark.parametrize("status", [400, 401, 403])
def test_authentication_or_invalid_request_is_not_retried_and_secret_is_safe(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error_message": f"bad api_key={KEY}"})

    with pytest.raises(DataError) as caught:
        make_client(handler).fetch("GDP")
    assert len(calls) == 1
    assert KEY not in str(caught.value)
    assert "api_key=" not in str(caught.value)


def test_missing_key_never_attempts_network():
    def handler(request):
        pytest.fail("Missing credentials must not issue a request")

    with pytest.raises(DataError) as caught:
        FredClient(None, transport=httpx.MockTransport(handler)).fetch("GDP")
    assert "key" in str(caught.value).lower()


def test_timeout_is_bounded_to_three_attempts_and_safe_message():
    calls = []
    pauses = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(f"Request to {request.url} failed", request=request)

    with pytest.raises(DataError) as caught:
        make_client(handler, sleep=pauses.append).fetch("GDP")
    assert len(calls) == 3
    assert len(pauses) == 2
    assert KEY not in str(caught.value)


def test_retry_after_is_respected_without_unbounded_sleep():
    calls = []
    pauses = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "2"}, json={"error_message": "retry"})

    with pytest.raises(DataError):
        make_client(handler, sleep=pauses.append).fetch("GDP")
    assert len(calls) == 3
    assert pauses and all(2 <= pause <= 60 for pause in pauses)


def test_oversized_retry_after_refuses_or_bounds_wait():
    pauses = []
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "999999"})
    with pytest.raises(DataError) as caught:
        make_client(handler, sleep=pauses.append).fetch("GDP")
    assert caught.value.code == "rate_limited"
    assert len(calls) == 1
    assert not pauses, "Do not retry earlier than the server's guidance"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_transient_status_can_recover(status):
    attempts = 0

    def handler(request):
        nonlocal attempts
        if not request.url.path.endswith("/observations"):
            attempts += 1
            return httpx.Response(status if attempts < 3 else 200, json=metadata())
        return httpx.Response(200, json=page([("2020-01-01", "100")]))

    assert make_client(handler).fetch("GDP").observations[0].value == 100
    assert attempts == 3


@pytest.mark.parametrize("payload", [None, {}, {"seriess": []}, metadata(id="CPILFESL"), metadata(frequency="Monthly"), metadata(units="Percent"), metadata(seasonal_adjustment="Not Seasonally Adjusted")])
def test_invalid_metadata_is_rejected(payload):
    with pytest.raises(DataError):
        make_client(lambda request: httpx.Response(200, json=payload)).fetch("GDP")


@pytest.mark.parametrize("rows", [
    [("2020-01-01", "100"), ("2020-01-01", "101")],
    [("2020-01-01", "nan")],
    [("2020-01-01", "inf")],
    [("2020-01-01", "not-a-number")],
    [("2020-01-01", "0")],
    [("2020-01-01", "-1")],
    [("invalid-date", "100")],
    [("2020-02-01", "100")],
])
def test_duplicate_nonfinite_invalid_or_off_calendar_observations_rejected(rows):
    def handler(request):
        return httpx.Response(200, json=page(rows) if request.url.path.endswith("/observations") else metadata())

    with pytest.raises(DataError):
        make_client(handler).fetch("GDP")


def test_empty_observations_are_data_unavailable():
    def handler(request):
        return httpx.Response(200, json=page([]) if request.url.path.endswith("/observations") else metadata())

    with pytest.raises(DataError):
        make_client(handler).fetch("GDP")


def test_invalid_json_is_useful_safe_error():
    with pytest.raises(DataError) as caught:
        make_client(lambda request: httpx.Response(200, text=f"invalid {KEY}")).fetch("GDP")
    assert KEY not in str(caught.value)


def test_unsupported_series_rejected_before_network():
    def handler(request):
        pytest.fail("Unsupported series must be rejected before HTTP")

    with pytest.raises((DataError, ValueError)):
        make_client(handler).fetch("GDPC1")


def test_successful_raw_response_cannot_echo_secrets_into_snapshot():
    def handler(request):
        payload = page([("2020-01-01", "100")]) if request.url.path.endswith("/observations") else metadata()
        payload["nested"] = {"api_key": KEY, "note": f"unexpected echo {KEY}", "more": [{"authorization": KEY}]}
        return httpx.Response(200, json=payload)
    serialized = make_client(handler).fetch("GDP").model_dump_json()
    assert KEY not in serialized
    assert "api_key" not in serialized
    assert "authorization" not in serialized


def test_pagination_cannot_mix_realtime_intervals():
    def handler(request):
        if not request.url.path.endswith("/observations"):
            return httpx.Response(200, json=metadata())
        offset = int(request.url.params["offset"])
        payload = page([("2020-01-01" if offset == 0 else "2020-04-01", "100")], count=2, offset=offset)
        if offset:
            payload["realtime_end"] = "2025-01-02"
        return httpx.Response(200, json=payload)
    with pytest.raises(DataError):
        make_client(handler).fetch("GDP")


@pytest.mark.parametrize("invalid", [None, [], {}, "invalid", "2025-01-01T00:00:00"])
def test_malformed_realtime_fields_are_safe_data_errors(invalid):
    def handler(request):
        if not request.url.path.endswith("/observations"):
            return httpx.Response(200, json=metadata())
        payload = page([("2020-01-01", "100")])
        payload["realtime_end"] = invalid
        return httpx.Response(200, json=payload)
    with pytest.raises(DataError) as caught:
        make_client(handler).fetch("GDP")
    assert KEY not in str(caught.value)


def test_successful_http_info_logs_redact_fred_key_without_disabling_logging(caplog):
    def handler(request):
        # MockTransport bypasses httpcore internals, so also exercise an actual
        # emitting httpcore child logger while this FRED request is in scope.
        logging.getLogger("httpcore.http11").info("request target: %s", request.url)
        payload = page([("2020-01-01", "100")]) if request.url.path.endswith("/observations") else metadata()
        return httpx.Response(200, json=payload)
    with caplog.at_level(logging.INFO):
        snapshot = make_client(handler).fetch("GDP")
        logging.getLogger("httpx").info("unrelated HTTP log remains available")
    assert KEY not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "HTTP Request:" in caplog.text, "HTTPX logging must remain enabled"
    assert "request target:" in caplog.text, "httpcore logging must remain enabled"
    assert "unrelated HTTP log remains available" in caplog.text
    assert KEY not in snapshot.model_dump_json()
