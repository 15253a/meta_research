from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.web import create_app


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
