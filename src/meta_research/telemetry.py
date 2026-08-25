"""Explicit, sanitized OpenTelemetry export for local runtime evidence.

The runtime event recorder owns the canonical local evidence.  This adapter is
only an optional transport: it creates an isolated OpenTelemetry logger and
accepts the same narrow, correlation-only event envelope.  Collector
configuration and credentials are deliberately never copied into a log
record.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Mapping
from decimal import Decimal
from urllib.parse import urlsplit

from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    LogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import Resource


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SCHEMA_REF = "meta-research/runtime-event/v1"
_CORRELATION_FIELDS = frozenset(
    {
        "responsibility_ref",
        "run_ref",
        "attempt_ref",
        "fence_ref",
        "operation_ref",
        "holder_ref",
        "checkpoint_ref",
    }
)
_LEVELS = {
    "info": SeverityNumber.INFO,
    "warning": SeverityNumber.WARN,
    "error": SeverityNumber.ERROR,
}


class OtlpHttpTelemetryExporter:
    """Send sanitized runtime events through an isolated OTLP/HTTP SDK.

    Constructing this object is an explicit opt-in.  It does not install a
    global OpenTelemetry provider, does not read event fields from environment
    variables, and has no implicit enable path.  RuntimeProtection supplies the
    asynchronous/revocation boundary around this synchronous transport.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 5.0,
        sdk_exporter: LogRecordExporter | None = None,
    ) -> None:
        validate_otlp_http_endpoint(endpoint)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("otlp_timeout_invalid")

        # Keep credentials scoped to the SDK exporter.  They are not retained
        # by this adapter and can never enter _sanitize_event attributes.
        transport = sdk_exporter or OTLPLogExporter(
            endpoint=endpoint,
            headers=dict(headers or {}),
            timeout=timeout_seconds,
        )
        provider = LoggerProvider(
            resource=Resource(
                {
                    "service.name": "meta-research",
                    "service.namespace": "vnext",
                }
            ),
            shutdown_on_exit=False,
        )
        provider.add_log_record_processor(SimpleLogRecordProcessor(transport))

        self._logger_provider = provider
        self._logger = provider.get_logger(
            "meta_research.runtime",
            version="1",
        )
        self._closed = False
        self._lock = threading.RLock()

    @property
    def provider(self) -> str:
        return "otlp_http"

    def export(self, event: dict[str, object]) -> None:
        """Export one allow-listed envelope; unsafe input fails before I/O."""

        sanitized = _sanitize_event(event)
        with self._lock:
            if self._closed:
                return
            self._logger.emit(
                timestamp=sanitized.timestamp_unix_nano,
                observed_timestamp=sanitized.timestamp_unix_nano,
                severity_number=sanitized.severity_number,
                severity_text=sanitized.severity_text,
                body=sanitized.event_code,
                attributes=sanitized.attributes,
                event_name=sanitized.event_code,
            )

    def close(self) -> None:
        """Permanently stop accepting events and close the private SDK."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._logger_provider.shutdown()


class _SanitizedEvent:
    __slots__ = (
        "attributes",
        "event_code",
        "severity_number",
        "severity_text",
        "timestamp_unix_nano",
    )

    def __init__(
        self,
        *,
        event_code: str,
        severity_number: SeverityNumber,
        severity_text: str,
        timestamp_unix_nano: int,
        attributes: dict[str, str | int],
    ) -> None:
        self.event_code = event_code
        self.severity_number = severity_number
        self.severity_text = severity_text
        self.timestamp_unix_nano = timestamp_unix_nano
        self.attributes = attributes


def _sanitize_event(event: Mapping[str, object]) -> _SanitizedEvent:
    schema_ref = event.get("schema_ref")
    if schema_ref != _SCHEMA_REF:
        raise ValueError("telemetry_schema_invalid")

    event_code = _required_identifier(event.get("event_code"), "event_code")
    component = _required_identifier(event.get("component"), "component")
    status = _required_identifier(event.get("status"), "status")
    level = event.get("level")
    if not isinstance(level, str) or level not in _LEVELS:
        raise ValueError("telemetry_level_invalid")
    recorded_at = event.get("recorded_at")
    if (
        isinstance(recorded_at, bool)
        or not isinstance(recorded_at, (int, float))
        or not math.isfinite(float(recorded_at))
        or recorded_at < 0
    ):
        raise ValueError("telemetry_recorded_at_invalid")

    attributes: dict[str, str | int] = {
        "meta_research.schema_ref": _SCHEMA_REF,
        "meta_research.component": component,
        "meta_research.event_code": event_code,
        "meta_research.status": status,
    }
    reason_code = event.get("reason_code")
    if reason_code is not None:
        attributes["meta_research.reason_code"] = _required_identifier(
            reason_code, "reason_code"
        )
    active_count = event.get("active_count")
    if active_count is not None:
        if (
            isinstance(active_count, bool)
            or not isinstance(active_count, int)
            or not 0 <= active_count <= 2_147_483_647
        ):
            raise ValueError("telemetry_active_count_invalid")
        attributes["meta_research.active_count"] = active_count

    correlation = event.get("correlation")
    if correlation is not None:
        if not isinstance(correlation, Mapping):
            raise ValueError("telemetry_correlation_invalid")
        for key in sorted(_CORRELATION_FIELDS):
            value = correlation.get(key)
            if value is None:
                continue
            # Correlation refs containing paths, whitespace, or unbounded user
            # material are omitted instead of being normalized into telemetry.
            if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
                attributes[f"meta_research.{key}"] = value

    return _SanitizedEvent(
        event_code=event_code,
        severity_number=_LEVELS[level],
        severity_text=level.upper(),
        timestamp_unix_nano=int(Decimal(str(recorded_at)) * 1_000_000_000),
        attributes=attributes,
    )


def _required_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"telemetry_{field}_invalid")
    return value


def validate_otlp_http_endpoint(endpoint: str) -> None:
    """Validate a non-secret OTLP/HTTP destination without opening transport."""

    if not isinstance(endpoint, str):
        raise ValueError("otlp_endpoint_invalid")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("otlp_endpoint_invalid")
