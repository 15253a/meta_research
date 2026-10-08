from __future__ import annotations

import subprocess

from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from test_corrected_quest_initialization import (
    DeterministicDraftingAdapter,
    DeterministicProbe,
    _authenticated_client,
)
from meta_research.web import create_app


class HostSamples:
    def observe(self):
        from meta_research.runtime_resources import HostResourceSample

        return HostResourceSample(
            sampled_from=1_720_000_000.0,
            sampled_to=1_720_000_000.1,
            cpu_utilization_percent=37.5,
            memory_used_bytes=6_442_450_944,
            memory_total_bytes=17_179_869_184,
            cpu_source="linux_proc_stat",
            memory_source="linux_proc_meminfo",
        )


def test_resource_api_reports_execution_host_measurements_without_a_quest(tmp_path):
    drafting = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "resources"),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=DeterministicProbe(),
        host_resource_probe=HostSamples(),
    )
    try:
        client, _ = _authenticated_client(runtime)
        response = client.get("/api/v1/runtime/resources")
        assert response.status_code == 200, response.text
        sample = response.json()
        assert sample["schema_ref"] == "meta-research/runtime-resources/v1"
        assert sample["scope"] == "execution_host"
        assert sample["host"]["hostname"]
        assert (sample["sampled_from"], sample["sampled_to"]) == (1_720_000_000.0, 1_720_000_000.1)
        assert sample["cpu"] == {
            "status": "ready", "utilization_percent": 37.5,
            "source": "linux_proc_stat", "reason_code": None,
            "sampled_from": 1_720_000_000.0, "sampled_to": 1_720_000_000.1,
        }
        assert sample["memory"] == {
            "status": "ready", "used_bytes": 6_442_450_944,
            "total_bytes": 17_179_869_184, "source": "linux_proc_meminfo",
            "reason_code": None,
            "observed_at": 1_720_000_000.1,
        }
        assert sample["gpu"]["devices"] == [{
            "uuid": "GPU-test-1", "name": "Test GPU", "utilization_percent": None,
            "memory_used_mib": None, "memory_total_mib": 81920,
            "reason_code": "gpu_live_metrics_unavailable",
        }]
    finally:
        runtime.close()


def test_gpu_observation_keeps_each_uuid_and_real_zero_usage():
    from meta_research.quest_drafting import NvidiaSmiProbe
    from meta_research.runtime_resources import RuntimeResourceReader

    def run_gpu_query(argv, timeout):
        if "--query-gpu=uuid,name,memory.total,utilization.gpu,memory.used" in argv:
            return subprocess.CompletedProcess(argv, 0, "GPU-a, A100, 81920, 0, 0\nGPU-b, RTX, 24576, 75, 2048\n", "")
        return subprocess.CompletedProcess(argv, 0, "GPU-a, A100, 81920\nGPU-b, RTX, 24576\n", "")

    sample = RuntimeResourceReader(HostSamples(), NvidiaSmiProbe(command_runner=run_gpu_query)).query()
    assert sample["gpu"]["status"] == "ready"
    assert sample["gpu"]["devices"] == [
        {"uuid": "GPU-a", "name": "A100", "utilization_percent": 0.0,
         "memory_used_mib": 0, "memory_total_mib": 81920, "reason_code": None},
        {"uuid": "GPU-b", "name": "RTX", "utilization_percent": 75.0,
         "memory_used_mib": 2048, "memory_total_mib": 24576, "reason_code": None},
    ]


def test_linux_observation_measures_whole_host_cpu_delta_and_available_memory():
    from meta_research.runtime_resources import SystemHostResourceProbe

    cpu_rows = iter(["cpu 100 0 100 800 0 0 0 0 99 0\n",
                     "cpu 125 0 125 850 0 0 0 0 120 0\n"])
    sample_times = iter([100.0, 100.25])

    def read_system_file(path):
        if path == "/proc/stat":
            return next(cpu_rows)
        assert path == "/proc/meminfo"
        return "MemTotal: 16777216 kB\nMemAvailable: 10485760 kB\nMemFree: 2048 kB\n"

    sample = SystemHostResourceProbe(
        read_text=read_system_file, clock=lambda: next(sample_times), wait=lambda _: None,
    ).observe()
    assert (sample.sampled_from, sample.sampled_to) == (100.0, 100.25)
    assert sample.cpu_utilization_percent == 50.0
    assert (sample.memory_used_bytes, sample.memory_total_bytes) == (6_442_450_944, 17_179_869_184)
    assert (sample.cpu_source, sample.memory_source) == ("linux_proc_stat", "linux_proc_meminfo")


def test_gpu_sampling_recovers_and_refreshes_devices_without_filling_missing_values():
    from meta_research.quest_drafting import NvidiaSmiProbe
    from meta_research.runtime_resources import RuntimeResourceReader

    outcomes = iter([
        subprocess.TimeoutExpired("nvidia-smi", 5),
        (0, "GPU-a, A100, 81920, 0, 0\nGPU-b, RTX, 24576, 75, 2048\n"),
        (0, "GPU-b, RTX, 24576, N/A, 0\n"),
        (1, ""), (0, ""),
        (0, "GPU-new, New card, N/A, 0, N/A\n"),
        (0, "bad-uuid, Wrong identity, 123, 10, 20\n"),
        (0, "GPU-a, A100, 100, 1, 2\nGPU-a, Duplicate, 100, 1, 2\n"),
        FileNotFoundError("nvidia-smi"),
        OSError("driver unavailable"),
        (0, "GPU-new, New card, 4096, nan, -1\n"),
        (6, "No devices were found\n"),
        (0, "GPU-new, New card, 4096, 100, 4096\n"),
    ])

    def run_gpu_query(argv, timeout):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        code, stdout = outcome
        return subprocess.CompletedProcess(argv, code, stdout, "")

    reader = RuntimeResourceReader(HostSamples(), NvidiaSmiProbe(command_runner=run_gpu_query))
    observed = []
    for _ in range(13):
        sample = reader.query()
        gpu = sample["gpu"]
        assert sample["cpu"]["utilization_percent"] == 37.5
        assert gpu["source"] == "nvidia_smi"
        observed.append((gpu["status"], gpu["reason_code"], [
            (d["uuid"], d["utilization_percent"], d["memory_used_mib"], d["memory_total_mib"], d["reason_code"])
            for d in gpu["devices"]
        ]))
    assert observed == [
        ("unavailable", "nvidia_smi_timeout", []),
        ("ready", None, [("GPU-a", 0.0, 0, 81920, None), ("GPU-b", 75.0, 2048, 24576, None)]),
        ("ready", None, [("GPU-b", None, 0, 24576, "gpu_metrics_unavailable")]),
        ("unavailable", "nvidia_smi_failed", []),
        ("no_devices", "nvidia_device_not_found", []),
        ("ready", None, [("GPU-new", 0.0, None, None, "gpu_metrics_unavailable")]),
        ("unavailable", "nvidia_smi_output_invalid", []),
        ("unavailable", "nvidia_smi_output_invalid", []),
        ("unavailable", "nvidia_smi_unavailable", []),
        ("unavailable", "nvidia_smi_io_unavailable", []),
        ("ready", None, [("GPU-new", None, None, 4096, "gpu_metrics_invalid")]),
        ("no_devices", "nvidia_device_not_found", []),
        ("ready", None, [("GPU-new", 100.0, 4096, 4096, None)]),
    ]


def test_host_sources_fail_independently_and_recover_to_real_zero_samples():
    from meta_research.runtime_resources import RuntimeResourceReader, SystemHostResourceProbe

    initial = "cpu 100 0 100 800 0 0 0 0\n"
    busy = "cpu 200 0 100 800 0 0 0 0\n"
    idle = "cpu 100 0 100 900 0 0 0 0\n"
    half = "cpu 125 0 125 850 0 0 0 0\n"
    memory = "MemTotal: 16777216 kB\nMemAvailable: 10485760 kB\n"
    empty_memory = "MemTotal: 16777216 kB\nMemAvailable: 16777216 kB\n"
    cases = [
        ([FileNotFoundError("stat missing")], memory),
        ([initial, busy], OSError("meminfo unavailable")),
        ([initial, initial], empty_memory),
        ([busy, initial], memory),
        ([initial, idle], empty_memory),
        ([initial, half], "MemTotal: 16777216 kB\n"),
        ([initial, half], "MemTotal: 16 kB\nMemAvailable: 32 kB\n"),
        ([""], memory),
        ([initial, half], memory),
    ]
    current = {}

    def read_system_file(path):
        value = next(current["cpu"]) if path == "/proc/stat" else current["memory"]
        if isinstance(value, Exception):
            raise value
        return value

    probe = SystemHostResourceProbe(read_text=read_system_file, wait=lambda _: None)
    reader = RuntimeResourceReader(probe, DeterministicProbe())
    observed = []
    for cpu, mem in cases:
        current.update(cpu=iter(cpu), memory=mem)
        sample = reader.query()
        observed.append((sample["cpu"]["utilization_percent"], sample["cpu"]["reason_code"],
                         sample["memory"]["used_bytes"], sample["memory"]["reason_code"]))
    assert observed == [
        (None, "linux_cpu_sample_unavailable", 6_442_450_944, None),
        (100.0, None, None, "linux_memory_sample_unavailable"),
        (None, "linux_cpu_sample_invalid", 0, None),
        (None, "linux_cpu_sample_invalid", 6_442_450_944, None),
        (0.0, None, 0, None),
        (50.0, None, None, "linux_memory_sample_unavailable"),
        (50.0, None, None, "linux_memory_sample_invalid"),
        (None, "linux_cpu_sample_unavailable", 6_442_450_944, None),
        (50.0, None, 6_442_450_944, None),
    ]


def test_resource_reads_recover_without_changing_selected_devices_or_creation_grants(tmp_path):
    from meta_research.quest_drafting import NvidiaSmiProbe

    class FlakyHost(HostSamples):
        failed = False

        def observe(self):
            if not self.failed:
                self.failed = True
                raise RuntimeError("transient host adapter failure")
            return super().observe()

    dynamic_failures = iter([True, False])

    def run_gpu_query(argv, timeout):
        if "--query-gpu=uuid,name,memory.total" in argv:
            return subprocess.CompletedProcess(argv, 0, "GPU-selected, Selected card, 81920\n", "")
        if next(dynamic_failures):
            raise RuntimeError("transient GPU adapter failure")
        return subprocess.CompletedProcess(argv, 0, "GPU-new, New card, 24576, 0, 0\n", "")

    drafting = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "resources-read-only"),
        proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=NvidiaSmiProbe(command_runner=run_gpu_query),
        host_resource_probe=FlakyHost(),
    )
    try:
        client, headers = _authenticated_client(runtime)
        opened = client.post("/api/v1/quest-initializations", json={},
                             headers={**headers, "Idempotency-Key": "resources-open"})
        assert opened.status_code == 201, opened.text
        initialization = opened.json()["initialization_id"]
        configured = client.post(f"/api/v1/quest-initializations/{initialization}/compute-probe",
                                 json={"selected_device_uuids": ["GPU-selected"]},
                                 headers={**headers, "Idempotency-Key": "resources-selection"})
        assert configured.status_code == 200, configured.text
        before = client.get(f"/api/v1/quest-initializations/{initialization}").json()
        assert before["resource_envelope"]["selected_device_uuids"] == ["GPU-selected"]

        failed = client.get("/api/v1/runtime/resources")
        assert failed.status_code == 200, failed.text
        assert failed.headers["Cache-Control"] == "no-store"
        sample = failed.json()
        assert (sample["cpu"]["status"], sample["cpu"]["utilization_percent"], sample["cpu"]["reason_code"]) == (
            "unavailable", None, "host_sample_unavailable")
        assert (sample["memory"]["status"], sample["memory"]["used_bytes"]) == ("unavailable", None)
        assert (sample["gpu"]["status"], sample["gpu"]["reason_code"], sample["gpu"]["devices"]) == (
            "unavailable", "gpu_sample_unavailable", [])
        recovered = client.get("/api/v1/runtime/resources").json()
        assert recovered["cpu"]["utilization_percent"] == 37.5
        assert recovered["gpu"]["devices"][0]["uuid"] == "GPU-new"
        assert recovered["gpu"]["devices"][0]["utilization_percent"] == 0.0
        assert client.get(f"/api/v1/quest-initializations/{initialization}").json() == before
    finally:
        runtime.close()
