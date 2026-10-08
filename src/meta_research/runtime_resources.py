"""Read-only observations of the execution machine, independent of Quest grants."""
from __future__ import annotations

import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Protocol

from meta_research.quest_drafting import HostComputeProbe, HostComputeResourceSnapshot


@dataclass(frozen=True)
class HostResourceSample:
    sampled_from: float
    sampled_to: float
    cpu_utilization_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    cpu_source: str = "unavailable"
    memory_source: str = "unavailable"
    cpu_reason_code: str | None = None
    memory_reason_code: str | None = None


class HostResourceProbe(Protocol):
    def observe(self) -> HostResourceSample: ...


def _read_system_file(path: str) -> str:
    return Path(path).read_text(encoding="ascii")


class SystemHostResourceProbe:
    """Sample Linux host totals, rather than the service process or a Quest."""
    def __init__(
        self, *, read_text: Callable[[str], str] = _read_system_file,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], object] = time.sleep,
        sample_interval_seconds: float = 0.1,
        platform: str = sys.platform,
    ):
        self._read_text = read_text
        self._clock = clock
        self._wait = wait
        self._sample_interval_seconds = sample_interval_seconds
        self._platform = platform

    def observe(self) -> HostResourceSample:
        started = self._clock()
        if self._platform != "linux":
            return HostResourceSample(
                sampled_from=started, sampled_to=self._clock(),
                cpu_reason_code="platform_unsupported", memory_reason_code="platform_unsupported",
            )
        cpu = None
        cpu_reason = None
        first = None
        try:
            first = self._cpu_ticks()
            self._wait(self._sample_interval_seconds)
        except (OSError, ValueError, IndexError):
            cpu_reason = "linux_cpu_sample_unavailable"

        total = None
        used = None
        memory_reason = None
        try:
            memory = {parts[0].rstrip(":"): int(parts[1]) * 1024
                      for line in self._read_text("/proc/meminfo").splitlines()
                      if len(parts := line.split()) == 3 and parts[2] == "kB"
                      and parts[0] in {"MemTotal:", "MemAvailable:"}}
            total = memory["MemTotal"]
            available = memory["MemAvailable"]
            if total <= 0 or available < 0 or available > total:
                total = None
                memory_reason = "linux_memory_sample_invalid"
            else:
                used = total - available
        except (OSError, ValueError, KeyError):
            total = None
            memory_reason = "linux_memory_sample_unavailable"

        if first is not None:
            try:
                second = self._cpu_ticks()
                elapsed = second[0] - first[0]
                idle = second[1] - first[1]
                if elapsed <= 0 or idle < 0 or idle > elapsed:
                    cpu_reason = "linux_cpu_sample_invalid"
                else:
                    cpu = 100.0 * (elapsed - idle) / elapsed
            except (OSError, ValueError, IndexError):
                cpu_reason = "linux_cpu_sample_unavailable"
        return HostResourceSample(
            sampled_from=started, sampled_to=self._clock(),
            cpu_utilization_percent=cpu,
            memory_used_bytes=used, memory_total_bytes=total,
            cpu_source="linux_proc_stat", memory_source="linux_proc_meminfo",
            cpu_reason_code=cpu_reason, memory_reason_code=memory_reason,
        )

    def _cpu_ticks(self) -> tuple[int, int]:
        row = self._read_text("/proc/stat").splitlines()[0].split()
        if row[0] != "cpu" or len(row) < 5:
            raise ValueError("aggregate CPU sample missing")
        # Linux includes guest time in user/nice already; summing it twice
        # would overstate the denominator. Idle includes IO wait.
        ticks = [int(value) for value in row[1:9]]
        return sum(ticks), ticks[3] + (ticks[4] if len(ticks) > 4 else 0)


class RuntimeResourceReader:
    def __init__(self, host_probe: HostResourceProbe, gpu_probe: HostComputeProbe):
        self._host_probe = host_probe
        self._gpu_probe = gpu_probe

    def query(self) -> dict[str, object]:
        host_started = time.time()
        try:
            host = self._host_probe.observe()
        except Exception:
            host = HostResourceSample(
                sampled_from=host_started, sampled_to=time.time(),
                cpu_source="host_resource_probe", memory_source="host_resource_probe",
                cpu_reason_code="host_sample_unavailable", memory_reason_code="host_sample_unavailable",
            )
        gpu_started = time.time()
        try:
            live_observation = getattr(self._gpu_probe, "observe_resources", None)
            gpu = live_observation() if callable(live_observation) else self._gpu_probe.observe()
        except Exception:
            gpu = HostComputeResourceSnapshot(
                status="unavailable", sampled_from=gpu_started, observed_at=time.time(),
                devices=(), adapter_kind=getattr(self._gpu_probe, "adapter_kind", "host_compute_probe"),
                reason_code="gpu_sample_unavailable",
            )
        gpu_started = getattr(gpu, "sampled_from", gpu.observed_at)
        return {
            "schema_ref": "meta-research/runtime-resources/v1",
            "scope": "execution_host",
            "host": {"hostname": socket.gethostname()},
            "observed_at": max(host.sampled_to, gpu.observed_at),
            "sampled_from": min(host.sampled_from, gpu_started),
            "sampled_to": max(host.sampled_to, gpu.observed_at),
            "refresh_interval_seconds": 5,
            "cpu": {
                "status": "ready" if host.cpu_utilization_percent is not None else "unavailable",
                "utilization_percent": host.cpu_utilization_percent,
                "source": host.cpu_source,
                "reason_code": host.cpu_reason_code,
                "sampled_from": host.sampled_from,
                "sampled_to": host.sampled_to,
            },
            "memory": {
                "status": "ready" if host.memory_used_bytes is not None and host.memory_total_bytes is not None else "unavailable",
                "used_bytes": host.memory_used_bytes,
                "total_bytes": host.memory_total_bytes,
                "source": host.memory_source,
                "reason_code": host.memory_reason_code,
                "observed_at": host.sampled_to,
            },
            "gpu": {
                "status": gpu.status if gpu.status == "no_devices" else "ready" if gpu.devices else "no_devices" if gpu.reason_code == "nvidia_device_not_found" else "unavailable",
                "source": gpu.adapter_kind,
                "reason_code": gpu.reason_code,
                "observed_at": gpu.observed_at,
                "sampled_from": gpu_started,
                "sampled_to": gpu.observed_at,
                "devices": [{
                    "uuid": device.uuid,
                    "name": device.name,
                    "utilization_percent": getattr(device, "utilization_percent", None),
                    "memory_used_mib": getattr(device, "memory_used_mib", None),
                    "memory_total_mib": device.memory_total_mib,
                    "reason_code": getattr(device, "reason_code", "gpu_live_metrics_unavailable"),
                } for device in gpu.devices],
            },
        }
