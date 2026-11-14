"""Small bounded FRED HTTP client. Exceptions intentionally never include URLs."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import re
import time
import traceback
from typing import Callable
from urllib.parse import quote, quote_plus
from uuid import uuid4

import httpx
from pydantic import SecretStr, ValidationError

from .schemas import Observation, SeriesID, SeriesMetadata, Snapshot

BASE_URL = "https://api.stlouisfed.org/fred"
SOURCE_URL = "https://fred.stlouisfed.org/series/"
EXPECTED = {
    SeriesID.GDP: ("Quarterly", "Billions of Dollars", "Seasonally Adjusted Annual Rate"),
    SeriesID.CPILFESL: ("Monthly", "Index 1982-1984=100", "Seasonally Adjusted"),
}

# HTTPX emits full URLs at INFO. Context-local redaction protects this client's
# key-bearing calls without changing levels or suppressing unrelated HTTP logs.
_LOG_SECRET: ContextVar[str | None] = ContextVar("fred_http_log_secret", default=None)


class _FREDLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        secret = _LOG_SECRET.get()
        if not secret:
            return True

        def redact(text: str) -> str:
            for value in {secret, quote(secret, safe=""), quote_plus(secret)}:
                text = text.replace(value, "[REDACTED]")
            return re.sub(r"([?&](?:api_key|access_token|token)=)[^&\s\"']*",
                          r"\1[REDACTED]", text, flags=re.I)

        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def _protect_http_logs() -> None:
    # Logger filters apply at the emitting logger, not its ancestors. These are
    # the emitting names in the pinned HTTPX/httpcore clients used by this app.
    for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11",
                 "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _FREDLogFilter) for item in logger.filters):
            logger.addFilter(_FREDLogFilter())


class DataError(Exception):
    """A safe public error: no request, response body or credentials retained."""
    def __init__(self, message: str, code: str = "data_unavailable"):
        super().__init__(message)
        self.code = code


def _safe_raw(value, secret: str):
    """Defensively redact unexpected sensitive fields from successful responses."""
    if isinstance(value, dict):
        return {k: _safe_raw(v, secret) for k, v in value.items()
                if not re.search(r"key|token|secret|authorization", k, re.I)}
    if isinstance(value, list):
        return [_safe_raw(v, secret) for v in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


class FredClient:
    def __init__(self, api_key: SecretStr | str | None, timeout: float = 15,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else (api_key or "")
        self._http = httpx.Client(timeout=httpx.Timeout(timeout), transport=transport,
                                  follow_redirects=False)
        self._sleep = sleep
        _protect_http_logs()

    def close(self):
        self._http.close()

    def _retry_after(self, response: httpx.Response, attempt: int) -> float | None:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                delay = float(raw)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 2 ** attempt
            if math.isfinite(delay):
                # Never retry sooner than server guidance. A longer requested
                # wait exceeds this synchronous application's bounded budget.
                return None if delay > 10.0 else max(0.0, delay)
        return float(2 ** attempt)

    def _request(self, endpoint: str, **params) -> dict:
        if not self._key:
            raise DataError("FRED_API_KEY is missing. Configure a key or explicitly enable DEMO mode.", "missing_key")
        query = {"api_key": self._key, "file_type": "json", **params}
        for attempt in range(3):
            try:
                log_token = _LOG_SECRET.set(self._key)
                try:
                    response = self._http.get(f"{BASE_URL}/{endpoint}", params=query)
                finally:
                    _LOG_SECRET.reset(log_token)
            except httpx.TimeoutException:
                if attempt < 2:
                    self._sleep(2 ** attempt)
                    continue
                raise DataError("FRED request timed out after three attempts.", "timeout") from None
            except httpx.RequestError:
                if attempt < 2:
                    self._sleep(2 ** attempt)
                    continue
                raise DataError("FRED network request failed after three attempts.", "network_error") from None
            status = response.status_code
            if status in {400, 401, 403}:
                raise DataError("FRED rejected the credentials or request. Check the registered API key.", "authentication_error")
            if status == 429 or 500 <= status <= 599:
                if attempt < 2:
                    delay = self._retry_after(response, attempt)
                    if delay is None:
                        raise DataError("FRED requested a retry delay beyond the bounded wait budget. Try refresh later.", "rate_limited")
                    self._sleep(delay)
                    continue
                raise DataError("FRED is temporarily unavailable or rate limited after three attempts.", "transient_error")
            if status != 200:
                raise DataError("FRED returned an unsuccessful HTTP status.", "http_error")
            try:
                payload = response.json()
            except ValueError:
                raise DataError("FRED returned invalid JSON.", "invalid_response") from None
            if not isinstance(payload, dict) or "error_code" in payload:
                raise DataError("FRED returned an invalid or error response.", "invalid_response")
            return _safe_raw(payload, self._key)
        raise DataError("FRED request exhausted its attempt budget.", "data_unavailable")

    def fetch(self, series: SeriesID | str) -> Snapshot:
        try:
            series = SeriesID(series)
        except ValueError:
            raise DataError("Only GDP and CPILFESL are supported.", "unsupported_series") from None
        meta_payload = self._request("series", series_id=series.value)
        try:
            rows = meta_payload["seriess"]
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError
            raw_meta = rows[0]
            expected = EXPECTED[series]
            observed = (raw_meta["frequency"], raw_meta["units"], raw_meta["seasonal_adjustment"])
            # FRED's CPI unit text sometimes includes spaces around the '='.
            if tuple(str(v).replace(" ", "") for v in observed) != tuple(v.replace(" ", "") for v in expected):
                raise ValueError
            if raw_meta["id"] != series.value or not raw_meta.get("title"):
                raise ValueError
            metadata = SeriesMetadata(id=series, title=raw_meta["title"], frequency=observed[0],
                                      units=observed[1], seasonal_adjustment=observed[2],
                                      last_updated=raw_meta.get("last_updated"),
                                      source_url=SOURCE_URL + series.value)
        except (KeyError, TypeError, ValueError, ValidationError):
            raise DataError("FRED metadata does not match the required native series definition.", "invalid_metadata") from None

        pages, observations = [], []
        offset, expected_count = 0, None
        realtime = {}
        while True:
            page = self._request("series/observations", series_id=series.value,
                                 units="lin", sort_order="asc", limit=100000, offset=offset)
            try:
                count = int(page["count"])
                page_offset = int(page.get("offset", offset))
                rows = page["observations"]
                if not isinstance(rows, list) or page_offset != offset or count < 0 or count > 100000:
                    raise ValueError
                if expected_count is not None and count != expected_count:
                    raise ValueError
                expected_count = count
                if not rows and offset < count:
                    raise ValueError
                for row in rows:
                    period = date.fromisoformat(row["date"])
                    if period.day != 1 or (series == SeriesID.GDP and period.month not in {1, 4, 7, 10}):
                        raise ValueError
                    raw_value = row["value"]
                    if raw_value in {".", None}:
                        value = None
                    else:
                        value = float(raw_value)
                        if not math.isfinite(value) or value <= 0:
                            raise ValueError
                    observations.append(Observation(date=period, value=value))
                page_realtime = {k: page[k] for k in ("realtime_start", "realtime_end") if k in page}
                for value in page_realtime.values():
                    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                if pages and page_realtime != realtime:
                    raise ValueError
                realtime = page_realtime
            except (KeyError, TypeError, ValueError, ValidationError):
                raise DataError("FRED observations are malformed, nonfinite, nonpositive or off the native calendar.", "invalid_response") from None
            pages.append(page)
            offset += len(rows)
            if offset >= count:
                if offset != count:
                    raise DataError("FRED pagination count is inconsistent.", "invalid_response")
                break
            if len(pages) >= 1000:
                raise DataError("FRED pagination exceeded the bounded page budget.", "invalid_response")
        if not observations:
            raise DataError("FRED returned no observations.", "empty_data")
        if len({row.date for row in observations}) != len(observations):
            raise DataError("FRED returned duplicate observation dates.", "duplicate_dates")
        observations.sort(key=lambda row: row.date)
        retrieved = datetime.now(timezone.utc)
        return Snapshot(snapshot_id=f"{series.value}-{retrieved.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:12]}",
                        series=series, metadata=metadata, observations=observations,
                        retrieved_at=retrieved, source_urls=[metadata.source_url,
                            BASE_URL + "/series", BASE_URL + "/series/observations"],
                        realtime_parameters={"request": "FRED default current realtime interval", **realtime},
                        raw_responses={"metadata": meta_payload, "observation_pages": pages})
