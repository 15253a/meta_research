from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.power_inhibitors import ProductionPowerInhibitor
from meta_research.web import create_app


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_production_import_does_not_load_retired_execution_services() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import meta_research.composition; "
                "assert 'meta_research.target_execution_port' not in sys.modules; "
                "assert 'meta_research.target_execution_supervisor' not in sys.modules; "
                "assert 'meta_research.target_run_worker' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def test_target_root_is_ready_without_a_second_execution_service(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "target-root-ready")
    )
    try:
        assert runtime.query_target_root_readiness() == {
            "name": "target_root_lifecycle",
            "status": "ready",
        }
        assert not hasattr(runtime, "target_execution_port")
        assert not (runtime.data_root.run / "target-execution").exists()
    finally:
        runtime.close()


def test_operator_attested_development_host_reports_runtime_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("META_RESEARCH_ASSUME_ALWAYS_ON", "1")
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "operator-attested-development-host"),
        startup_harness_diagnostics=False,
    )
    try:
        evidence = runtime.query_runtime_observability()

        assert evidence["status"] == "ready"
        assert evidence["durable_waiting_count"] == 0
        assert evidence["inhibitor"]["backend"] == "operator_attested_always_on"
        assert evidence["inhibitor"]["capability"] == {
            "status": "ready",
            "backend": "operator_attested_always_on",
            "scope": "sleep",
            "reason": None,
            "probed_at": evidence["inhibitor"]["capability"]["probed_at"],
        }
    finally:
        runtime.close()


def test_internal_readiness_and_doctor_report_the_light_root_driver(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "target-root-web-gate")
    )
    runtime.harnesses.query_status = lambda: {"status": "ready"}  # type: ignore[method-assign]
    client = TestClient(
        create_app(
            runtime,
            base_url="http://testserver",
            control_key="target-root-control",
        ),
        base_url="http://testserver",
    )
    control = {"X-Meta-Research-Control": "target-root-control"}
    try:
        with client:
            readiness = client.get("/internal/readiness", headers=control)
            assert readiness.status_code == 200
            assert readiness.json()["target_root"] == (
                runtime.query_target_root_readiness()
            )
            assert "target_execution" not in readiness.json()

            doctor = client.get("/internal/doctor", headers=control)
            assert doctor.status_code == 200
            assert doctor.json()["target_root"] == (
                runtime.query_target_root_readiness()
            )
            assert "target_execution" not in doctor.json()
    finally:
        runtime.close()


def test_doctor_reports_failed_real_power_probe_without_repeating_it_on_get(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_log = tmp_path / "systemd-inhibit-invocations"
    systemd_inhibit = _write_executable(
        tmp_path / "systemd-inhibit",
        """#!/usr/bin/env python3
import os
from pathlib import Path

with Path(os.environ["PROBE_INVOCATION_LOG"]).open("a", encoding="utf-8") as output:
    output.write("systemd bus unavailable\\n")
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PROBE_INVOCATION_LOG", str(invocation_log))
    data_root = prepare_data_root(tmp_path / "power-probe-doctor")
    runtime = build_production_runtime(
        data_root,
        power_inhibitor=ProductionPowerInhibitor(
            data_root.run / "power-inhibitor",
            platform="ubuntu",
            systemd_inhibit=systemd_inhibit,
            readiness_timeout_seconds=0.2,
        ),
        startup_power_probe=True,
    )
    runtime.harnesses.query_status = lambda: {"status": "ready"}  # type: ignore[method-assign]
    runtime.projection.query_snapshot()
    runtime.projection.query_snapshot()
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        "systemd bus unavailable"
    ]
    client = TestClient(
        create_app(
            runtime,
            base_url="http://testserver",
            control_key="power-probe-control",
        ),
        base_url="http://testserver",
    )
    control = {"X-Meta-Research-Control": "power-probe-control"}
    try:
        with client:
            first = client.get("/internal/doctor", headers=control)
            second = client.get("/internal/doctor", headers=control)

        assert first.status_code == second.status_code == 200
        assert first.json()["status"] == "unavailable"
        runtime_protection = first.json()["runtime_protection"]
        assert (
            runtime_protection["inhibitor"]["capability"]
            == second.json()["runtime_protection"]["inhibitor"]["capability"]
        )
        assert runtime_protection["inhibitor"]["capability"] == {
            "status": "unavailable",
            "backend": "ubuntu_logind",
            "scope": "sleep",
            "reason": {
                "code": "power_inhibitor_systemd_reconciliation_required"
            },
            "probed_at": runtime_protection["inhibitor"]["capability"][
                "probed_at"
            ],
        }
        assert runtime_protection["responsibilities"] == []
        assert invocation_log.read_text(encoding="utf-8").splitlines() == [
            "systemd bus unavailable"
        ]
        log_content = data_root.daemon_log.read_text(encoding="utf-8")
        assert "power_inhibitor_systemd_reconciliation_required" in log_content
        assert str(tmp_path) not in log_content
    finally:
        runtime.close()
