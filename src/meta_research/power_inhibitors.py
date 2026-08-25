"""Fail-closed production power-inhibitor adapters.

The runtime coordinator owns durable responsibility.  This module owns only the
host-specific hold and never treats a Linux process inside WSL as a Windows
power request.
"""

from __future__ import annotations

import base64
import hashlib
from importlib import resources
import os
import platform as platform_module
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Literal

from meta_research.runtime_protection import (
    InhibitorLease,
    RuntimeProtectionUnavailable,
)


PlatformKind = Literal["ubuntu", "wsl", "unsupported"]
_PLATFORMS = frozenset({"ubuntu", "wsl", "unsupported"})
_SCOPE = "sleep"
_LINUX_READY_PREFIX = "META_RESEARCH_INHIBITOR_READY:"


class ProductionPowerInhibitor:
    """Select the real inhibitor required by the supported host contract."""

    def __init__(
        self,
        state_directory: Path | None = None,
        *,
        platform: PlatformKind | None = None,
        systemd_inhibit: str | Path | None = None,
        powershell: str | Path | None = None,
        readiness_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        selected = platform or _detect_platform()
        if selected not in _PLATFORMS:
            raise ValueError("power_inhibitor_platform_invalid")
        if readiness_timeout_seconds <= 0:
            raise ValueError("power_inhibitor_readiness_timeout_invalid")
        self._state_directory = (
            state_directory.expanduser().resolve()
            if state_directory is not None
            else None
        )
        if self._state_directory is not None:
            self._state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                self._state_directory.chmod(0o700)
            except OSError:
                pass
        self._platform = selected
        self._systemd_inhibit = (
            str(systemd_inhibit)
            if systemd_inhibit is not None
            else shutil.which("systemd-inhibit")
        )
        self._powershell = (
            str(powershell)
            if powershell is not None
            else shutil.which("powershell.exe")
        )
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._clock = clock
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.RLock()

    @property
    def kind(self) -> str:
        return {
            "ubuntu": "ubuntu_logind",
            "wsl": "windows_power_request_guardian",
            "unsupported": "unsupported",
        }[self._platform]

    def acquire(self, *, holder_ref: str, reason: str) -> InhibitorLease:
        del reason  # User or research material must never enter a native command line.
        if self._platform == "unsupported":
            raise RuntimeProtectionUnavailable("power_inhibitor_platform_unsupported")
        if self._platform == "ubuntu":
            return self._acquire_ubuntu(holder_ref)
        return self._acquire_wsl(holder_ref)

    def is_confirmed(self, lease: InhibitorLease) -> bool:
        return self.query_hold(lease) == "confirmed"

    def query_hold(
        self, lease: InhibitorLease
    ) -> Literal["confirmed", "absent", "unknown"]:
        """Return proof of presence, proof of absence, or an unknown observation."""

        if lease.backend != self.kind or lease.scope != _SCOPE:
            return "unknown"
        if self._platform == "wsl":
            digest = _parse_windows_native_holder(lease.native_holder_ref)
            if digest is None:
                return "unknown"
            if digest != _holder_digest(lease.holder_ref):
                return "unknown"
            return self._probe_windows_guardian(digest)
        if self._platform != "ubuntu":
            return "unknown"
        digest = _holder_digest(lease.holder_ref)
        parsed = _parse_linux_native_holder(lease.native_holder_ref)
        if parsed is None or parsed[1] != digest:
            return "unknown"
        pid = parsed[0]
        if self._state_directory is not None:
            persisted_pid = self._read_linux_state(digest)
            if persisted_pid is not None and persisted_pid != pid:
                return "unknown"
        with self._lock:
            process = self._processes.get(lease.holder_ref)
            if process is not None:
                if process.poll() is not None:
                    return "absent"
        if not _linux_holder_matches(pid, digest):
            return "absent"
        return self._probe_ubuntu_logind(digest)

    def query_exact_hold(
        self,
        *,
        holder_ref: str,
    ) -> tuple[
        Literal["confirmed", "absent", "unknown"],
        InhibitorLease | None,
    ]:
        """Query one issued holder identity without creating a replacement."""

        digest = _holder_digest(holder_ref)
        if self._platform == "ubuntu":
            persisted_pid = self._read_linux_state(digest)
            if persisted_pid is None:
                status = self._probe_ubuntu_logind(digest)
                pending, launcher_group = self._read_linux_pending_issuance(
                    digest
                )
                if (
                    status == "absent"
                    and pending
                    and (
                        launcher_group is None
                        or _linux_process_group_status(launcher_group) != "absent"
                    )
                ):
                    status = "unknown"
                # Without the exact helper PID, even a positive logind row is
                # presence evidence rather than a releasable native identity.
                # Preserve reconciliation ownership until that exact digest is
                # provably absent instead of manufacturing a replacement.
                return ("absent", None) if status == "absent" else ("unknown", None)
            lease = InhibitorLease(
                holder_ref=holder_ref,
                backend=self.kind,
                scope=_SCOPE,
                acquired_at=self._clock(),
                native_holder_ref=f"pid:{persisted_pid}:holder:{digest}",
            )
            if not _linux_holder_matches(persisted_pid, digest):
                status = self._probe_ubuntu_logind(digest)
                pending, launcher_group = self._read_linux_pending_issuance(
                    digest
                )
                if (
                    status == "absent"
                    and pending
                    and (
                        launcher_group is None
                        or _linux_process_group_status(launcher_group) != "absent"
                    )
                ):
                    status = "unknown"
                return ("absent", lease) if status == "absent" else ("unknown", lease)
            return self._probe_ubuntu_logind(digest), lease
        if self._platform != "wsl":
            return "unknown", None
        lease = InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope=_SCOPE,
            acquired_at=self._clock(),
            native_holder_ref=f"windows-event:{digest}",
        )
        status = self._probe_windows_guardian(digest)
        if status == "absent" and self._state_directory is not None:
            try:
                marker = self._windows_state_path(digest).read_text(
                    encoding="ascii"
                ).strip()
            except FileNotFoundError:
                marker = None
            except OSError:
                return "unknown", lease
            if marker is not None and not marker.startswith("active:"):
                # A launcher that has not published readiness may still create
                # the named Windows hold after this instantaneous absence.
                return "unknown", lease
        return status, lease

    def release(self, lease: InhibitorLease) -> None:
        if lease.backend != self.kind or lease.scope != _SCOPE:
            return
        if self._platform == "wsl":
            digest = _parse_windows_native_holder(lease.native_holder_ref)
            if digest is None:
                return
            if digest != _holder_digest(lease.holder_ref):
                return
            self._release_windows_guardian(digest)
            if self._state_directory is not None:
                self._windows_state_path(digest).unlink(missing_ok=True)
            with self._lock:
                process = self._processes.pop(lease.holder_ref, None)
            if process is not None:
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    _stop_process_group(process)
                else:
                    _close_process_output(process)
            return
        if self._platform != "ubuntu":
            return
        parsed = _parse_linux_native_holder(lease.native_holder_ref)
        if parsed is None or parsed[1] != _holder_digest(lease.holder_ref):
            return
        pid, digest = parsed
        with self._lock:
            process = self._processes.pop(lease.holder_ref, None)
        if self._state_directory is not None:
            if self._read_linux_state(digest) == pid:
                _write_private_text(self._linux_stop_path(digest), "stop\n")
            if process is not None:
                try:
                    process.wait(timeout=self._readiness_timeout_seconds)
                except subprocess.TimeoutExpired as error:
                    _stop_process_group(process)
                    raise RuntimeProtectionUnavailable(
                        "power_inhibitor_systemd_release_failed"
                    ) from error
                else:
                    _close_process_output(process)
            deadline = time.monotonic() + self._readiness_timeout_seconds
            while _linux_holder_matches(pid, digest) or (
                self._probe_ubuntu_logind(digest) != "absent"
            ):
                if time.monotonic() >= deadline:
                    raise RuntimeProtectionUnavailable(
                        "power_inhibitor_systemd_release_failed"
                    )
                time.sleep(0.02)
            self._clear_linux_pending_after_release(digest, process)
            return
        if process is not None:
            _stop_process_group(process)
        elif _linux_holder_matches(pid, digest):
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_state_directory_required"
            )

    def _acquire_ubuntu(self, holder_ref: str) -> InhibitorLease:
        if self._state_directory is None:
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_state_directory_required"
            )
        if not self._systemd_inhibit:
            raise RuntimeProtectionUnavailable("power_inhibitor_systemd_unavailable")
        digest = _holder_digest(holder_ref)
        existing_pid = self._read_linux_state(digest)
        if existing_pid is not None and _linux_holder_matches(
            existing_pid,
            digest,
        ):
            if self._probe_ubuntu_logind(digest) == "confirmed":
                self._clear_linux_pending_issuance(digest)
                return InhibitorLease(
                    holder_ref=holder_ref,
                    backend=self.kind,
                    scope=_SCOPE,
                    acquired_at=self._clock(),
                    native_holder_ref=f"pid:{existing_pid}:holder:{digest}",
                )
            # A live exact helper plus an unreachable/negative logind query is
            # an unknown outcome.  Never unlink its state and start a second
            # native guardian under a different process.
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_reconciliation_required"
            )
        self._reconcile_linux_pending_issuance(digest)
        # A missing/stale marker is not absence proof: the exact digest remains
        # visible through logind even when its helper identity file was lost.
        # Only a successful negative list query permits a fresh Popen.
        if self._probe_ubuntu_logind(digest) != "absent":
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_reconciliation_required"
            )
        self._linux_state_path(digest).unlink(missing_ok=True)
        self._linux_stop_path(digest).unlink(missing_ok=True)
        _write_private_text(self._linux_pending_path(digest), "pending\n")
        command = [
            self._systemd_inhibit,
            "--what=sleep",
            "--mode=block",
            f"--who=meta-research-vnext:{digest}",
            f"--why=managed-operation:{digest}",
            sys.executable,
            "-m",
            "meta_research.power_inhibitors",
            "_linux-hold",
            digest,
            str(self._state_directory),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            try:
                self._linux_pending_path(digest).unlink(missing_ok=True)
            except OSError as cleanup_error:
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_systemd_reconciliation_required"
                ) from cleanup_error
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_unavailable"
            ) from error
        with self._lock:
            previous = self._processes.setdefault(holder_ref, process)
        if previous is not process:
            _stop_process_group(process)
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_reconciliation_required"
            )
        try:
            _write_private_text(
                self._linux_pending_path(digest),
                f"launched:{process.pid}\n",
            )
        except Exception as error:
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_reconciliation_required"
            ) from error
        line = _read_ready_line(process, self._readiness_timeout_seconds)
        helper_pid = _parse_linux_ready(line, digest)
        if helper_pid is None:
            exited = process.poll() is not None
            _stop_process_group(process)
            self._forget_process(holder_ref, process)
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_acquire_failed"
                if exited
                else "power_inhibitor_systemd_readiness_timeout"
            )
        if process.poll() is not None:
            _stop_process_group(process)
            self._forget_process(holder_ref, process)
            raise RuntimeProtectionUnavailable("power_inhibitor_systemd_acquire_failed")
        if self._probe_ubuntu_logind(digest) != "confirmed":
            _stop_process_group(process)
            self._forget_process(holder_ref, process)
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_confirmation_failed"
            )
        lease = InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope=_SCOPE,
            acquired_at=self._clock(),
            native_holder_ref=f"pid:{helper_pid}:holder:{digest}",
        )
        try:
            self._clear_linux_pending_issuance(digest)
        except RuntimeProtectionUnavailable:
            raise
        except Exception as error:
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_reconciliation_required"
            ) from error
        return lease

    def _linux_state_path(self, digest: str) -> Path:
        assert self._state_directory is not None
        return self._state_directory / f"ubuntu-{digest}.holder"

    def _linux_stop_path(self, digest: str) -> Path:
        assert self._state_directory is not None
        return self._state_directory / f"ubuntu-{digest}.stop"

    def _linux_pending_path(self, digest: str) -> Path:
        assert self._state_directory is not None
        return self._state_directory / f"ubuntu-{digest}.pending"

    def _read_linux_pending_issuance(
        self, digest: str
    ) -> tuple[bool, int | None]:
        if self._state_directory is None:
            return False, None
        try:
            marker = self._linux_pending_path(digest).read_text(
                encoding="ascii"
            ).strip()
        except FileNotFoundError:
            return False, None
        except OSError:
            return True, None
        if marker == "pending":
            return True, None
        if marker.startswith("launched:"):
            try:
                process_group = int(marker.removeprefix("launched:"))
            except ValueError:
                return True, None
            if process_group > 0:
                return True, process_group
        return True, None

    def _reconcile_linux_pending_issuance(self, digest: str) -> None:
        pending, process_group = self._read_linux_pending_issuance(digest)
        if not pending:
            return
        process_status = (
            "unknown"
            if process_group is None
            else _linux_process_group_status(process_group)
        )
        logind_status = self._probe_ubuntu_logind(digest)
        if process_status != "absent" or logind_status != "absent":
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_reconciliation_required"
            )
        self._clear_linux_pending_issuance(digest)

    def _clear_linux_pending_issuance(self, digest: str) -> None:
        try:
            self._linux_pending_path(digest).unlink(missing_ok=True)
        except OSError as error:
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_reconciliation_required"
            ) from error

    def _clear_linux_pending_after_release(
        self,
        digest: str,
        process: subprocess.Popen[str] | None,
    ) -> None:
        pending, process_group = self._read_linux_pending_issuance(digest)
        if not pending:
            return
        exact_group = process_group
        if exact_group is None and process is not None:
            exact_group = process.pid
        if (
            exact_group is None
            or _linux_process_group_status(exact_group) != "absent"
        ):
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_systemd_release_failed"
            )
        self._clear_linux_pending_issuance(digest)

    def _forget_process(
        self,
        holder_ref: str,
        process: subprocess.Popen[str],
    ) -> None:
        with self._lock:
            if self._processes.get(holder_ref) is process:
                self._processes.pop(holder_ref, None)

    def _read_linux_state(self, digest: str) -> int | None:
        if self._state_directory is None:
            return None
        try:
            pid = int(self._linux_state_path(digest).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None
        return pid if pid > 0 else None

    def _probe_ubuntu_logind(
        self, digest: str
    ) -> Literal["confirmed", "absent", "unknown"]:
        if not self._systemd_inhibit:
            return "unknown"
        try:
            result = subprocess.run(
                [
                    self._systemd_inhibit,
                    "--list",
                    "--no-pager",
                    "--no-legend",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=self._readiness_timeout_seconds,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        if result.returncode != 0:
            return "unknown"
        who = f"meta-research-vnext:{digest}"
        why = f"managed-operation:{digest}"
        confirmed = any(
            who in fields
            and why in fields
            and "sleep" in fields
            and fields[-1:] == ["block"]
            for fields in (line.split() for line in result.stdout.splitlines())
        )
        return "confirmed" if confirmed else "absent"

    def _acquire_wsl(self, holder_ref: str) -> InhibitorLease:
        if self._state_directory is None:
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_state_directory_required"
            )
        if not self._powershell:
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_windows_guardian_unavailable"
            )
        digest = _holder_digest(holder_ref)
        state_path = self._windows_state_path(digest)
        if state_path.exists() and self._query_windows_guardian(digest):
            return InhibitorLease(
                holder_ref=holder_ref,
                backend=self.kind,
                scope=_SCOPE,
                acquired_at=self._clock(),
                native_holder_ref=f"windows-event:{digest}",
            )
        if state_path.exists():
            self._settle_pending_windows_guardian(digest)
        _write_private_text(state_path, "pending\n")
        command = self._windows_command("Hold", digest)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            state_path.unlink(missing_ok=True)
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_windows_guardian_unavailable"
            ) from error
        ready_prefix = "META_RESEARCH_WINDOWS_GUARDIAN_READY:"
        line = _read_ready_line(process, self._readiness_timeout_seconds)
        parsed = _parse_windows_ready(line, digest, ready_prefix)
        if parsed is None:
            exited = process.poll() is not None
            reconciliation_error: RuntimeProtectionUnavailable | None = None
            try:
                self._release_windows_guardian(
                    digest,
                    allow_initial_absence=False,
                )
            except RuntimeProtectionUnavailable as error:
                reconciliation_error = error
            _stop_process_group(process)
            if reconciliation_error is not None:
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_windows_guardian_reconciliation_failed"
                ) from reconciliation_error
            state_path.unlink(missing_ok=True)
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_windows_guardian_acquire_failed"
                if exited
                else "power_inhibitor_windows_guardian_readiness_timeout"
            )
        windows_pid = parsed
        if process.poll() is not None:
            _stop_process_group(process)
            state_path.unlink(missing_ok=True)
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_windows_guardian_acquire_failed"
            )
        if not self._query_windows_guardian(digest):
            self._release_windows_guardian(digest)
            _stop_process_group(process)
            state_path.unlink(missing_ok=True)
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_windows_guardian_confirmation_failed"
            )
        try:
            _write_private_text(state_path, f"active:{windows_pid}\n")
            with self._lock:
                previous = self._processes.setdefault(holder_ref, process)
            if previous is not process:
                _stop_process_group(process)
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_windows_guardian_reconciliation_failed"
                )
            return InhibitorLease(
                holder_ref=holder_ref,
                backend=self.kind,
                scope=_SCOPE,
                acquired_at=self._clock(),
                native_holder_ref=f"windows-event:{digest}",
            )
        except RuntimeProtectionUnavailable:
            raise
        except Exception as error:
            # The named Windows power request was already confirmed.  Any local
            # bookkeeping failure after this point is an uncertain issuance,
            # never proof that it is safe to create another guardian.
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_windows_guardian_reconciliation_failed"
            ) from error

    def _windows_state_path(self, digest: str) -> Path:
        assert self._state_directory is not None
        return self._state_directory / f"windows-{digest}.holder"

    def _query_windows_guardian(self, digest: str) -> bool:
        return self._probe_windows_guardian(digest) == "confirmed"

    def _probe_windows_guardian(
        self, digest: str
    ) -> Literal["confirmed", "absent", "unknown"]:
        result = self._run_windows_command("Query", digest)
        if result is None:
            return "unknown"
        if result.returncode == 3:
            return "absent"
        if (
            result.returncode == 0
            and result.stdout.strip()
            == f"META_RESEARCH_WINDOWS_GUARDIAN_CONFIRMED:{digest}"
        ):
            return "confirmed"
        return "unknown"

    def _release_windows_guardian(
        self,
        digest: str,
        *,
        allow_initial_absence: bool = True,
    ) -> None:
        result = self._run_windows_command("Release", digest)
        if result is not None and result.returncode == 3:
            if allow_initial_absence:
                return
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_windows_guardian_release_failed"
            )
        if (
            result is None
            or result.returncode != 0
            or result.stdout.strip()
            != f"META_RESEARCH_WINDOWS_GUARDIAN_RELEASED:{digest}"
        ):
            raise RuntimeProtectionUnavailable(
                "power_inhibitor_windows_guardian_release_failed"
            )
        deadline = time.monotonic() + self._readiness_timeout_seconds
        while True:
            if self._probe_windows_guardian(digest) == "absent":
                return
            if time.monotonic() >= deadline:
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_windows_guardian_release_failed"
                )
            time.sleep(0.02)

    def _settle_pending_windows_guardian(self, digest: str) -> None:
        deadline = time.monotonic() + self._readiness_timeout_seconds
        while True:
            if self._query_windows_guardian(digest):
                self._release_windows_guardian(digest)
                return
            result = self._run_windows_command("Release", digest)
            if result is not None and result.returncode == 0:
                while time.monotonic() < deadline:
                    probe = self._run_windows_command("Release", digest)
                    if probe is not None and probe.returncode == 3:
                        return
                    if probe is not None and probe.returncode not in {0, 3}:
                        break
                    time.sleep(0.02)
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_windows_guardian_reconciliation_failed"
                )
            if result is not None and result.returncode not in {0, 3}:
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_windows_guardian_reconciliation_failed"
                )
            if time.monotonic() >= deadline:
                # A pending marker can mean the WSL launcher died immediately
                # before the native guardian published readiness.  Absence of
                # a Query result is not proof that the Windows process cannot
                # still acquire the named hold, so replacing it would permit
                # two native power requests for one durable responsibility.
                raise RuntimeProtectionUnavailable(
                    "power_inhibitor_windows_guardian_reconciliation_failed"
                )
            time.sleep(0.02)

    def _run_windows_command(
        self, mode: Literal["Query", "Release"], digest: str
    ) -> subprocess.CompletedProcess[str] | None:
        if not self._powershell:
            return None
        try:
            return subprocess.run(
                self._windows_command(mode, digest),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=self._readiness_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _windows_command(
        self,
        mode: Literal["Hold", "Query", "Release"],
        digest: str,
    ) -> list[str]:
        assert self._powershell is not None
        guardian = (
            resources.files("meta_research")
            .joinpath("windows_power_guardian.ps1")
            .read_text(encoding="utf-8")
        )
        script = (
            f"$MetaResearchMode = '{mode}'\n"
            f"$MetaResearchHolderToken = '{digest}'\n"
            f"{guardian}"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [
            self._powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]


def _detect_platform() -> PlatformKind:
    kernel_release = platform_module.release().lower()
    if (
        os.environ.get("WSL_INTEROP")
        or os.environ.get("WSL_DISTRO_NAME")
        or "microsoft" in kernel_release
        or "wsl" in kernel_release
    ):
        return "wsl"
    try:
        os_release = platform_module.freedesktop_os_release()
    except OSError:
        return "unsupported"
    identifiers = {
        str(os_release.get("ID", "")).lower(),
        *str(os_release.get("ID_LIKE", "")).lower().split(),
    }
    return "ubuntu" if "ubuntu" in identifiers else "unsupported"


def _holder_digest(holder_ref: str) -> str:
    return hashlib.sha256(holder_ref.encode("utf-8")).hexdigest()[:24]


def _read_ready_line(
    process: subprocess.Popen[str], timeout_seconds: float
) -> str | None:
    if process.stdout is None:
        return None
    deadline = time.monotonic() + timeout_seconds
    buffer = bytearray()
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while len(buffer) <= 4_096:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return None
            chunk = os.read(process.stdout.fileno(), 4_096 - len(buffer))
            if not chunk:
                return None
            buffer.extend(chunk)
            newline = buffer.find(b"\n")
            if newline >= 0:
                return bytes(buffer[:newline]).decode(
                    "utf-8", errors="strict"
                ).strip()
        return None
    finally:
        selector.close()


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2.0)
    if process.stdout is not None:
        process.stdout.close()


def _close_process_output(process: subprocess.Popen[str]) -> None:
    if process.stdout is not None:
        process.stdout.close()


def _parse_linux_native_holder(value: str) -> tuple[int, str] | None:
    parts = value.split(":")
    if len(parts) != 4 or parts[0] != "pid" or parts[2] != "holder":
        return None
    try:
        pid = int(parts[1])
    except ValueError:
        return None
    if pid <= 0 or len(parts[3]) != 24:
        return None
    return pid, parts[3]


def _parse_linux_ready(line: str | None, digest: str) -> int | None:
    if line is None or not line.startswith(_LINUX_READY_PREFIX):
        return None
    parts = line.removeprefix(_LINUX_READY_PREFIX).split(":")
    if len(parts) != 2 or parts[1] != digest:
        return None
    try:
        pid = int(parts[0])
    except ValueError:
        return None
    return pid if pid > 0 else None


def _parse_windows_native_holder(value: str) -> str | None:
    parts = value.split(":")
    if len(parts) != 2 or parts[0] != "windows-event":
        return None
    if len(parts[1]) != 24:
        return None
    return parts[1]


def _parse_windows_ready(
    line: str | None, digest: str, prefix: str
) -> int | None:
    if line is None or not line.startswith(prefix):
        return None
    parts = line.removeprefix(prefix).split(":")
    if len(parts) != 2 or parts[1] != digest:
        return None
    try:
        pid = int(parts[0])
    except ValueError:
        return None
    return pid if pid > 0 else None


def _linux_holder_matches(pid: int, digest: str) -> bool:
    try:
        command_line = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    rendered = [part.decode("utf-8", errors="replace") for part in command_line]
    return (
        "meta_research.power_inhibitors" in rendered
        and "_linux-hold" in rendered
        and digest in rendered
    )


def _linux_process_group_status(
    process_group: int,
) -> Literal["confirmed", "absent", "unknown"]:
    """Prove whether one start-new-session launcher group still exists."""

    try:
        processes = tuple(Path("/proc").iterdir())
    except OSError:
        return "unknown"
    observation_unknown = False
    for process_path in processes:
        if not process_path.name.isdigit():
            continue
        try:
            observed_group = os.getpgid(int(process_path.name))
        except ProcessLookupError:
            continue
        except OSError:
            observation_unknown = True
            continue
        if observed_group == process_group:
            return "confirmed"
    return "unknown" if observation_unknown else "absent"


def _write_private_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _linux_hold(holder_digest: str, state_directory: Path) -> int:
    def stop(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state_path = state_directory / f"ubuntu-{holder_digest}.holder"
    stop_path = state_directory / f"ubuntu-{holder_digest}.stop"
    stop_path.unlink(missing_ok=True)
    _write_private_text(state_path, f"{os.getpid()}\n")
    try:
        print(
            f"{_LINUX_READY_PREFIX}{os.getpid()}:{holder_digest}",
            flush=True,
        )
        while not stop_path.exists():
            time.sleep(0.05)
    finally:
        state_path.unlink(missing_ok=True)
        stop_path.unlink(missing_ok=True)
    return 0


def _main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "_linux-hold":
        return _linux_hold(sys.argv[2], Path(sys.argv[3]))
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
