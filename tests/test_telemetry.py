from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import ClassVar

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)

from meta_research.telemetry import OtlpHttpTelemetryExporter


class _OtlpReceiver(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append((self.path, dict(self.headers.items()), body))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        del args


@pytest.fixture
def otlp_receiver(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, type[_OtlpReceiver]]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    _OtlpReceiver.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OtlpReceiver)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1/logs", _OtlpReceiver
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


def _attributes(request: ExportLogsServiceRequest) -> dict[str, object]:
    record = request.resource_logs[0].scope_logs[0].log_records[0]
    result: dict[str, object] = {}
    for attribute in record.attributes:
        value = attribute.value
        if value.HasField("string_value"):
            result[attribute.key] = value.string_value
        elif value.HasField("int_value"):
            result[attribute.key] = value.int_value
    return result


def test_real_otlp_http_export_is_sanitized_and_correlated(
    otlp_receiver: tuple[str, type[_OtlpReceiver]],
) -> None:
    endpoint, receiver = otlp_receiver
    exporter = OtlpHttpTelemetryExporter(
        endpoint=endpoint,
        headers={"Authorization": "Bearer must-never-be-an-event"},
        timeout_seconds=2,
    )

    exporter.export(
        {
            "schema_ref": "meta-research/runtime-event/v1",
            "recorded_at": 1_800_000_000.25,
            "level": "warning",
            "component": "runtime.protection",
            "event_code": "runtime.effect.acquire_failed",
            "status": "waiting",
            "reason_code": "runtime_inhibitor_unavailable",
            "active_count": 3,
            "correlation": {
                "responsibility_ref": "rr-123",
                "run_ref": "run-123",
                "operation_ref": "/home/alice/private/result.json",
                "seal_key": "forbidden",
            },
            "prompt": "private research prompt",
            "raw_stdout": "provider secret stdout",
            "endpoint": "https://collector.example/secret-path",
            "headers": {"X-Api-Key": "secret"},
        }
    )
    exporter.close()

    assert exporter.provider == "otlp_http"
    assert len(receiver.requests) == 1
    path, headers, payload = receiver.requests[0]
    assert path == "/v1/logs"
    assert headers["Content-Type"] == "application/x-protobuf"

    request = ExportLogsServiceRequest.FromString(payload)
    record = request.resource_logs[0].scope_logs[0].log_records[0]
    attributes = _attributes(request)
    assert record.body.string_value == "runtime.effect.acquire_failed"
    assert record.time_unix_nano == 1_800_000_000_250_000_000
    assert attributes == {
        "meta_research.schema_ref": "meta-research/runtime-event/v1",
        "meta_research.component": "runtime.protection",
        "meta_research.event_code": "runtime.effect.acquire_failed",
        "meta_research.status": "waiting",
        "meta_research.reason_code": "runtime_inhibitor_unavailable",
        "meta_research.active_count": 3,
        "meta_research.responsibility_ref": "rr-123",
        "meta_research.run_ref": "run-123",
    }
    decoded = payload.decode("utf-8", errors="ignore")
    for forbidden in (
        "must-never-be-an-event",
        "private research prompt",
        "provider secret stdout",
        "collector.example",
        "/home/alice",
        "forbidden",
    ):
        assert forbidden not in decoded


def test_close_is_idempotent_and_stops_future_exports(
    otlp_receiver: tuple[str, type[_OtlpReceiver]],
) -> None:
    endpoint, receiver = otlp_receiver
    exporter = OtlpHttpTelemetryExporter(endpoint=endpoint, timeout_seconds=2)
    event = {
        "schema_ref": "meta-research/runtime-event/v1",
        "recorded_at": 1_800_000_000.0,
        "level": "info",
        "component": "runtime.protection",
        "event_code": "runtime.telemetry.enabled",
        "status": "enabled",
    }

    exporter.export(event)
    exporter.close()
    exporter.close()
    exporter.export(event)

    assert len(receiver.requests) == 1


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///tmp/telemetry",
        "http://operator:password@collector.example/v1/logs",
        "https://collector.example/v1/logs#secret-fragment",
    ],
)
def test_endpoint_must_be_http_without_embedded_credentials(endpoint: str) -> None:
    with pytest.raises(ValueError, match="otlp_endpoint_invalid"):
        OtlpHttpTelemetryExporter(endpoint=endpoint)
