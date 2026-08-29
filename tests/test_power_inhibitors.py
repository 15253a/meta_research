from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time

import pytest

from meta_research.power_inhibitors import (
    OperatorAttestedPowerInhibitor,
    ProductionPowerInhibitor,
)
from meta_research.runtime_protection import InhibitorLease, RuntimeProtectionUnavailable


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_unsupported_host_fails_closed_with_typed_reason() -> None:
    inhibitor = ProductionPowerInhibitor(platform="unsupported")

    assert inhibitor.kind == "unsupported"
    with pytest.raises(RuntimeProtectionUnavailable) as raised:
        inhibitor.acquire(holder_ref="holder:unsupported", reason="managed work")

    assert raised.value.code == "power_inhibitor_platform_unsupported"


def test_operator_attested_host_confirms_exact_holder_across_restart() -> None:
    holder_ref = "holder:operator-attested-development-host"
    first = OperatorAttestedPowerInhibitor(clock=lambda: 1_720_000_000.0)

    lease = first.acquire(holder_ref=holder_ref, reason="managed work")

    assert first.kind == "operator_attested_always_on"
    assert lease == InhibitorLease(
        holder_ref=holder_ref,
        backend="operator_attested_always_on",
        scope="sleep",
        acquired_at=1_720_000_000.0,
        native_holder_ref=(
            "operator-attestation:"
            + hashlib.sha256(holder_ref.encode("utf-8")).hexdigest()[:24]
        ),
    )
    assert first.is_confirmed(lease)
    assert first.query_hold(lease) == "confirmed"

    restarted = OperatorAttestedPowerInhibitor(clock=lambda: 1_720_000_100.0)
    exact_status, exact_lease = restarted.query_exact_hold(holder_ref=holder_ref)

    assert exact_status == "confirmed"
    assert exact_lease == InhibitorLease(
        holder_ref=holder_ref,
        backend="operator_attested_always_on",
        scope="sleep",
        acquired_at=1_720_000_100.0,
        native_holder_ref=lease.native_holder_ref,
    )
    restarted.release(exact_lease)

    with pytest.raises(RuntimeProtectionUnavailable) as foreign_release:
        restarted.release(
            InhibitorLease(
                holder_ref=holder_ref,
                backend="ubuntu_logind",
                scope="sleep",
                acquired_at=1_720_000_000.0,
                native_holder_ref="pid:123:holder:legacy",
            )
        )
    assert foreign_release.value.code == (
        "power_inhibitor_operator_attestation_invalid"
    )


def test_wsl_detection_takes_precedence_over_linux_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/interop")

    inhibitor = ProductionPowerInhibitor(
        tmp_path / "auto-platform",
        powershell="",
    )

    assert inhibitor.kind == "windows_power_request_guardian"
    with pytest.raises(RuntimeProtectionUnavailable) as raised:
        inhibitor.acquire(holder_ref="holder:auto-wsl", reason="managed work")
    assert raised.value.code == "power_inhibitor_windows_guardian_unavailable"


def test_wsl_hold_query_distinguishes_unknown_from_exact_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    powershell = _write_executable(
        tmp_path / "powershell.exe",
        """#!/usr/bin/env python3
import base64
import os
import re
import sys

arguments = sys.argv[1:]
encoded = arguments[arguments.index("-EncodedCommand") + 1]
command = base64.b64decode(encoded).decode("utf-16-le")
mode = re.search(r"\\$MetaResearchMode = '([A-Za-z]+)'", command).group(1)
token = re.search(r"\\$MetaResearchHolderToken = '([0-9a-f]+)'", command).group(1)
if mode != "Query":
    raise SystemExit(51)
status = os.environ["FAKE_GUARDIAN_QUERY_STATUS"]
if status == "confirmed":
    print(f"META_RESEARCH_WINDOWS_GUARDIAN_CONFIRMED:{token}")
elif status == "absent":
    raise SystemExit(3)
else:
    raise SystemExit(8)
""",
    )
    state_directory = tmp_path / "power-state"
    state_directory.mkdir()
    holder_ref = "holder:wsl-query-status"
    digest = hashlib.sha256(holder_ref.encode("utf-8")).hexdigest()[:24]
    (state_directory / f"windows-{digest}.holder").write_text(
        "active:12345\n",
        encoding="ascii",
    )
    inhibitor = ProductionPowerInhibitor(
        state_directory,
        platform="wsl",
        powershell=powershell,
        readiness_timeout_seconds=0.1,
        clock=lambda: 1_720_000_000.0,
    )
    lease = InhibitorLease(
        holder_ref=holder_ref,
        backend="windows_power_request_guardian",
        scope="sleep",
        acquired_at=1_720_000_000.0,
        native_holder_ref=f"windows-event:{digest}",
    )

    monkeypatch.setenv("FAKE_GUARDIAN_QUERY_STATUS", "confirmed")
    assert inhibitor.query_hold(lease) == "confirmed"
    exact_status, exact_lease = inhibitor.query_exact_hold(holder_ref=holder_ref)
    assert exact_status == "confirmed"
    assert exact_lease == lease
    monkeypatch.setenv("FAKE_GUARDIAN_QUERY_STATUS", "unknown")
    assert inhibitor.query_hold(lease) == "unknown"
    assert inhibitor.query_exact_hold(holder_ref=holder_ref) == ("unknown", lease)
    monkeypatch.setenv("FAKE_GUARDIAN_QUERY_STATUS", "absent")
    assert inhibitor.query_hold(lease) == "absent"
    assert inhibitor.query_exact_hold(holder_ref=holder_ref) == ("absent", lease)


def test_ubuntu_acquires_confirmed_systemd_inhibitor_until_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments_path = tmp_path / "systemd-inhibit-arguments.json"
    registry_path = tmp_path / "systemd-inhibit-registry"
    systemd_inhibit = _write_executable(
        tmp_path / "systemd-inhibit",
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys

arguments = sys.argv[1:]
registry = Path(os.environ["FAKE_INHIBIT_REGISTRY"])
if "--list" in arguments:
    if os.environ.get("FAKE_INHIBIT_QUERY_UNKNOWN"):
        raise SystemExit(8)
    if registry.exists():
        print(registry.read_text(encoding="utf-8"), end="")
    raise SystemExit(0)
with open(os.environ["FAKE_INHIBIT_ARGUMENTS"], "w", encoding="utf-8") as output:
    json.dump(arguments, output)
required = {"--what=sleep", "--mode=block"}
if not required.issubset(arguments):
    raise SystemExit(41)
command_index = next(
    index for index, argument in enumerate(arguments) if not argument.startswith("--")
)
who = next(argument.removeprefix("--who=") for argument in arguments if argument.startswith("--who="))
why = next(argument.removeprefix("--why=") for argument in arguments if argument.startswith("--why="))
registry.write_text(
    f"{who} 1000 tester {os.getpid()} python sleep {why} block\\n",
    encoding="utf-8",
)
try:
    raise SystemExit(subprocess.call(arguments[command_index:]))
finally:
    registry.unlink(missing_ok=True)
""",
    )
    monkeypatch.setenv("FAKE_INHIBIT_ARGUMENTS", str(arguments_path))
    monkeypatch.setenv("FAKE_INHIBIT_REGISTRY", str(registry_path))
    state_directory = tmp_path / "power-state"
    inhibitor = ProductionPowerInhibitor(
        state_directory,
        platform="ubuntu",
        systemd_inhibit=systemd_inhibit,
        readiness_timeout_seconds=2.0,
        clock=lambda: 1_720_000_000.0,
    )
    lease = inhibitor.acquire(
        holder_ref="holder:ubuntu",
        reason="prompt=TOP-SECRET /home/alice/private.txt",
    )

    try:
        assert inhibitor.kind == "ubuntu_logind"
        assert lease.backend == "ubuntu_logind"
        assert lease.scope == "sleep"
        assert inhibitor.is_confirmed(lease)
        assert inhibitor.query_exact_hold(holder_ref=lease.holder_ref) == (
            "confirmed",
            lease,
        )
        monkeypatch.setenv("FAKE_INHIBIT_QUERY_UNKNOWN", "1")
        assert inhibitor.query_hold(lease) == "unknown"
        assert inhibitor.query_exact_hold(holder_ref=lease.holder_ref) == (
            "unknown",
            lease,
        )
        monkeypatch.delenv("FAKE_INHIBIT_QUERY_UNKNOWN")
        assert inhibitor.query_hold(lease) == "confirmed"
        arguments = json.loads(arguments_path.read_text(encoding="utf-8"))
        assert "TOP-SECRET" not in " ".join(arguments)
        assert "/home/alice/private.txt" not in " ".join(arguments)
        restarted = ProductionPowerInhibitor(
            state_directory,
            platform="ubuntu",
            systemd_inhibit=systemd_inhibit,
            readiness_timeout_seconds=2.0,
            clock=lambda: 1_720_000_000.0,
        )
        assert restarted.is_confirmed(lease)
        adopted = restarted.acquire(
            holder_ref="holder:ubuntu",
            reason="managed work after daemon restart",
        )
        try:
            assert adopted.native_holder_ref == lease.native_holder_ref
        finally:
            restarted.release(adopted)
    finally:
        inhibitor.release(lease)

    assert not inhibitor.is_confirmed(lease)
    assert inhibitor.query_exact_hold(holder_ref=lease.holder_ref) == (
        "absent",
        None,
    )


@pytest.mark.parametrize("marker_pid", [None, 999_999_999])
def test_ubuntu_requires_digest_level_absence_before_replacing_missing_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_pid: int | None,
) -> None:
    launches_path = tmp_path / "systemd-inhibit-launches"
    systemd_inhibit = _write_executable(
        tmp_path / "systemd-inhibit",
        """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
if "--list" in arguments:
    status = os.environ["FAKE_INHIBIT_QUERY_STATUS"]
    if status == "unknown":
        raise SystemExit(8)
    if status == "confirmed":
        digest = os.environ["FAKE_INHIBIT_DIGEST"]
        print(
            f"meta-research-vnext:{digest} 1000 tester 42 python sleep "
            f"managed-operation:{digest} block"
        )
    raise SystemExit(0)
Path(os.environ["FAKE_INHIBIT_LAUNCHES"]).write_text("launch\\n", encoding="ascii")
raise SystemExit(19)
""",
    )
    holder_ref = f"holder:ubuntu-digest-recovery:{marker_pid}"
    digest = hashlib.sha256(holder_ref.encode("utf-8")).hexdigest()[:24]
    state_directory = tmp_path / "power-state"
    state_directory.mkdir()
    if marker_pid is not None:
        (state_directory / f"ubuntu-{digest}.holder").write_text(
            f"{marker_pid}\n",
            encoding="ascii",
        )
    monkeypatch.setenv("FAKE_INHIBIT_DIGEST", digest)
    monkeypatch.setenv("FAKE_INHIBIT_LAUNCHES", str(launches_path))
    inhibitor = ProductionPowerInhibitor(
        state_directory,
        platform="ubuntu",
        systemd_inhibit=systemd_inhibit,
        readiness_timeout_seconds=0.1,
    )

    for status in ("confirmed", "unknown"):
        monkeypatch.setenv("FAKE_INHIBIT_QUERY_STATUS", status)
        exact_status, _exact_lease = inhibitor.query_exact_hold(
            holder_ref=holder_ref
        )
        assert exact_status == "unknown"
        with pytest.raises(RuntimeProtectionUnavailable) as raised:
            inhibitor.acquire(holder_ref=holder_ref, reason="managed work")
        assert raised.value.code == (
            "power_inhibitor_systemd_reconciliation_required"
        )
        assert not launches_path.exists()

    monkeypatch.setenv("FAKE_INHIBIT_QUERY_STATUS", "absent")
    exact_status, _exact_lease = inhibitor.query_exact_hold(holder_ref=holder_ref)
    assert exact_status == "absent"
    with pytest.raises(RuntimeProtectionUnavailable) as raised:
        inhibitor.acquire(holder_ref=holder_ref, reason="managed work")
    assert raised.value.code == "power_inhibitor_systemd_acquire_failed"
    assert launches_path.read_text(encoding="ascii") == "launch\n"


def test_ubuntu_restart_never_replaces_a_delayed_pending_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches_path = tmp_path / "systemd-inhibit-launches"
    registry_path = tmp_path / "systemd-inhibit-registry"
    launch_gate = tmp_path / "allow-logind-acquisition"
    systemd_inhibit = _write_executable(
        tmp_path / "systemd-inhibit",
        """#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
import time

arguments = sys.argv[1:]
registry = Path(os.environ["FAKE_INHIBIT_REGISTRY"])
if "--list" in arguments:
    if registry.exists():
        print(registry.read_text(encoding="utf-8"), end="")
    raise SystemExit(0)
with open(os.environ["FAKE_INHIBIT_LAUNCHES"], "a", encoding="ascii") as output:
    output.write("launch\\n")
while not Path(os.environ["FAKE_INHIBIT_GATE"]).exists():
    time.sleep(0.01)
command_index = next(
    index for index, argument in enumerate(arguments) if not argument.startswith("--")
)
who = next(argument.removeprefix("--who=") for argument in arguments if argument.startswith("--who="))
why = next(argument.removeprefix("--why=") for argument in arguments if argument.startswith("--why="))
registry.write_text(
    f"{who} 1000 tester {os.getpid()} python sleep {why} block\\n",
    encoding="utf-8",
)
try:
    raise SystemExit(subprocess.call(arguments[command_index:]))
finally:
    registry.unlink(missing_ok=True)
""",
    )
    monkeypatch.setenv("FAKE_INHIBIT_LAUNCHES", str(launches_path))
    monkeypatch.setenv("FAKE_INHIBIT_REGISTRY", str(registry_path))
    monkeypatch.setenv("FAKE_INHIBIT_GATE", str(launch_gate))
    state_directory = tmp_path / "power-state"
    first = ProductionPowerInhibitor(
        state_directory,
        platform="ubuntu",
        systemd_inhibit=systemd_inhibit,
        readiness_timeout_seconds=2.0,
    )
    restarted = ProductionPowerInhibitor(
        state_directory,
        platform="ubuntu",
        systemd_inhibit=systemd_inhibit,
        readiness_timeout_seconds=2.0,
    )
    holder_ref = "holder:ubuntu-delayed-launcher"
    first_leases: list[InhibitorLease] = []
    first_errors: list[BaseException] = []
    restarted_errors: list[BaseException] = []

    def acquire_first() -> None:
        try:
            first_leases.append(first.acquire(holder_ref=holder_ref, reason="first"))
        except BaseException as error:
            first_errors.append(error)

    def acquire_after_restart() -> None:
        try:
            restarted.acquire(holder_ref=holder_ref, reason="restart")
        except BaseException as error:
            restarted_errors.append(error)

    first_worker = threading.Thread(target=acquire_first)
    restarted_worker = threading.Thread(target=acquire_after_restart)
    first_worker.start()
    deadline = time.monotonic() + 1
    while not launches_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert launches_path.exists()
    restarted_worker.start()
    try:
        restarted_worker.join(timeout=0.5)
        assert not restarted_worker.is_alive()
        assert len(restarted_errors) == 1
        assert isinstance(restarted_errors[0], RuntimeProtectionUnavailable)
        assert restarted_errors[0].code == (
            "power_inhibitor_systemd_reconciliation_required"
        )
        assert launches_path.read_text(encoding="ascii").splitlines() == ["launch"]
    finally:
        launch_gate.write_text("continue\n", encoding="ascii")
        first_worker.join(timeout=3)
        restarted_worker.join(timeout=3)
        for lease in first_leases:
            try:
                first.release(lease)
            except RuntimeProtectionUnavailable:
                pass
    assert not first_errors


def test_ubuntu_post_issuance_marker_failure_releases_exact_pending_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_path = tmp_path / "systemd-inhibit-registry"
    systemd_inhibit = _write_executable(
        tmp_path / "systemd-inhibit",
        """#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys

arguments = sys.argv[1:]
registry = Path(os.environ["FAKE_INHIBIT_REGISTRY"])
if "--list" in arguments:
    if registry.exists():
        print(registry.read_text(encoding="utf-8"), end="")
    raise SystemExit(0)
command_index = next(
    index for index, argument in enumerate(arguments) if not argument.startswith("--")
)
who = next(argument.removeprefix("--who=") for argument in arguments if argument.startswith("--who="))
why = next(argument.removeprefix("--why=") for argument in arguments if argument.startswith("--why="))
registry.write_text(
    f"{who} 1000 tester {os.getpid()} python sleep {why} block\\n",
    encoding="utf-8",
)
try:
    raise SystemExit(subprocess.call(arguments[command_index:]))
finally:
    registry.unlink(missing_ok=True)
""",
    )
    monkeypatch.setenv("FAKE_INHIBIT_REGISTRY", str(registry_path))
    state_directory = tmp_path / "power-state"
    holder_ref = "holder:ubuntu-post-issuance-marker"
    digest = hashlib.sha256(holder_ref.encode("utf-8")).hexdigest()[:24]
    pending_path = state_directory / f"ubuntu-{digest}.pending"
    original_replace = os.replace

    def fail_launched_marker(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if (
            Path(destination) == pending_path
            and source_path.read_text(encoding="ascii").startswith("launched:")
        ):
            raise OSError("injected pending launcher marker failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_launched_marker)
    inhibitor = ProductionPowerInhibitor(
        state_directory,
        platform="ubuntu",
        systemd_inhibit=systemd_inhibit,
        readiness_timeout_seconds=2.0,
    )
    with pytest.raises(RuntimeProtectionUnavailable) as raised:
        inhibitor.acquire(holder_ref=holder_ref, reason="managed work")
    assert raised.value.code == "power_inhibitor_systemd_reconciliation_required"
    assert pending_path.read_text(encoding="ascii") == "pending\n"
    assert list(state_directory.glob(".*.tmp")) == []

    monkeypatch.setattr(os, "replace", original_replace)
    deadline = time.monotonic() + 2
    exact_status, exact_lease = inhibitor.query_exact_hold(holder_ref=holder_ref)
    while exact_status != "confirmed" and time.monotonic() < deadline:
        time.sleep(0.01)
        exact_status, exact_lease = inhibitor.query_exact_hold(holder_ref=holder_ref)
    assert exact_status == "confirmed"
    assert exact_lease is not None
    inhibitor.release(exact_lease)
    assert not pending_path.exists()
    assert inhibitor.query_exact_hold(holder_ref=holder_ref)[0] == "absent"


def test_ubuntu_never_claims_success_without_readiness_handshake(
    tmp_path: Path,
) -> None:
    systemd_inhibit = _write_executable(
        tmp_path / "systemd-inhibit-no-ready",
        """#!/usr/bin/env python3
import sys
import time
if "--list" in sys.argv[1:]:
    raise SystemExit(0)
time.sleep(30)
""",
    )
    inhibitor = ProductionPowerInhibitor(
        tmp_path / "power-state",
        platform="ubuntu",
        systemd_inhibit=systemd_inhibit,
        readiness_timeout_seconds=0.05,
    )

    with pytest.raises(RuntimeProtectionUnavailable) as raised:
        inhibitor.acquire(holder_ref="holder:no-ready", reason="managed work")

    assert raised.value.code == "power_inhibitor_systemd_readiness_timeout"


def test_ubuntu_helper_cannot_impersonate_a_logind_hold(tmp_path: Path) -> None:
    systemd_inhibit = _write_executable(
        tmp_path / "systemd-inhibit-without-lock",
        """#!/usr/bin/env python3
import os
import subprocess
import sys

arguments = sys.argv[1:]
if "--list" in arguments:
    raise SystemExit(0)
command_index = next(
    index for index, argument in enumerate(arguments) if not argument.startswith("--")
)
raise SystemExit(subprocess.call(arguments[command_index:]))
""",
    )
    inhibitor = ProductionPowerInhibitor(
        tmp_path / "power-state",
        platform="ubuntu",
        systemd_inhibit=systemd_inhibit,
        readiness_timeout_seconds=0.5,
    )

    with pytest.raises(RuntimeProtectionUnavailable) as raised:
        inhibitor.acquire(holder_ref="holder:no-logind-lock", reason="managed work")

    assert raised.value.code == "power_inhibitor_systemd_confirmation_failed"


def test_ubuntu_partial_readiness_line_cannot_defeat_timeout(tmp_path: Path) -> None:
    systemd_inhibit = _write_executable(
        tmp_path / "systemd-inhibit-partial-ready",
        """#!/usr/bin/env python3
import sys
import time
if "--list" in sys.argv[1:]:
    raise SystemExit(0)
sys.stdout.write("META_RESEARCH_INHIBITOR_READY:")
sys.stdout.flush()
time.sleep(1)
""",
    )
    inhibitor = ProductionPowerInhibitor(
        tmp_path / "power-state",
        platform="ubuntu",
        systemd_inhibit=systemd_inhibit,
        readiness_timeout_seconds=0.05,
    )

    started_at = time.monotonic()
    with pytest.raises(RuntimeProtectionUnavailable) as raised:
        inhibitor.acquire(holder_ref="holder:partial-ready", reason="managed work")

    assert raised.value.code == "power_inhibitor_systemd_readiness_timeout"
    assert time.monotonic() - started_at < 0.5


def test_wsl_uses_confirmed_native_windows_power_request_guardian(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "windows-guardian.state"
    commands_path = tmp_path / "windows-guardian-commands.jsonl"
    powershell = _write_executable(
        tmp_path / "powershell.exe",
        """#!/usr/bin/env python3
import base64
import json
import os
from pathlib import Path
import re
import sys
import time

arguments = sys.argv[1:]
encoded = arguments[arguments.index("-EncodedCommand") + 1]
command = base64.b64decode(encoded).decode("utf-16-le")
with open(os.environ["FAKE_GUARDIAN_COMMANDS"], "a", encoding="utf-8") as output:
    output.write(json.dumps(command) + "\\n")
mode = re.search(r"\\$MetaResearchMode = '([A-Za-z]+)'", command).group(1)
token = re.search(r"\\$MetaResearchHolderToken = '([0-9a-f]+)'", command).group(1)
state = Path(os.environ["FAKE_GUARDIAN_STATE"])
if mode == "Hold":
    if (
        "PowerCreateRequest" not in command
        or "PowerSetRequest" not in command
        or "PowerRequestDisplayRequired = 0" not in command
        or "PowerRequestSystemRequired = 1" not in command
        or "new IntPtr(-1)" not in command
    ):
        raise SystemExit(51)
    markers = list(Path(os.environ["FAKE_ADAPTER_STATE_DIR"]).glob("windows-*.holder"))
    if len(markers) != 1 or markers[0].read_text(encoding="ascii") != "pending\\n":
        raise SystemExit(53)
    state.write_text(token, encoding="ascii")
    print(f"META_RESEARCH_WINDOWS_GUARDIAN_READY:{os.getpid()}:{token}", flush=True)
    while state.exists():
        time.sleep(0.02)
elif mode == "Query":
    if state.exists() and state.read_text(encoding="ascii") == token:
        print(f"META_RESEARCH_WINDOWS_GUARDIAN_CONFIRMED:{token}")
    else:
        raise SystemExit(3)
elif mode == "Release":
    if os.environ.get("FAKE_GUARDIAN_RELEASE_FAIL"):
        raise SystemExit(8)
    if state.exists() and state.read_text(encoding="ascii") == token:
        state.unlink()
    print(f"META_RESEARCH_WINDOWS_GUARDIAN_RELEASED:{token}")
else:
    raise SystemExit(52)
""",
    )
    monkeypatch.setenv("FAKE_GUARDIAN_STATE", str(state_path))
    monkeypatch.setenv("FAKE_GUARDIAN_COMMANDS", str(commands_path))
    state_directory = tmp_path / "power-state"
    monkeypatch.setenv("FAKE_ADAPTER_STATE_DIR", str(state_directory))
    inhibitor = ProductionPowerInhibitor(
        state_directory,
        platform="wsl",
        powershell=powershell,
        readiness_timeout_seconds=2.0,
    )
    lease = inhibitor.acquire(
        holder_ref="holder:wsl",
        reason="prompt=TOP-SECRET /home/alice/private.txt",
    )

    try:
        assert inhibitor.kind == "windows_power_request_guardian"
        assert lease.backend == "windows_power_request_guardian"
        assert lease.scope == "sleep"
        assert inhibitor.is_confirmed(lease)
        decoded_commands = [
            json.loads(line)
            for line in commands_path.read_text(encoding="utf-8").splitlines()
        ]
        assert any("PowerRequestSystemRequired" in command for command in decoded_commands)
        assert all("SetThreadExecutionState" not in command for command in decoded_commands)
        assert all("TOP-SECRET" not in command for command in decoded_commands)
        assert all("/home/alice/private.txt" not in command for command in decoded_commands)
        restarted = ProductionPowerInhibitor(
            state_directory,
            platform="wsl",
            powershell=powershell,
            readiness_timeout_seconds=2.0,
        )
        assert restarted.is_confirmed(lease)
        adopted = restarted.acquire(
            holder_ref="holder:wsl",
            reason="managed work after daemon restart",
        )
        assert adopted.native_holder_ref == lease.native_holder_ref
        monkeypatch.setenv("FAKE_GUARDIAN_RELEASE_FAIL", "1")
        with pytest.raises(RuntimeProtectionUnavailable) as raised:
            restarted.release(lease)
        assert raised.value.code == "power_inhibitor_windows_guardian_release_failed"
        assert restarted.is_confirmed(lease)
        monkeypatch.delenv("FAKE_GUARDIAN_RELEASE_FAIL")
        restarted.release(lease)
    finally:
        inhibitor.release(lease)

    assert not inhibitor.is_confirmed(lease)


def test_wsl_active_marker_failure_preserves_exact_guardian_for_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_state_path = tmp_path / "windows-guardian.state"
    hold_count_path = tmp_path / "windows-guardian-holds"
    powershell = _write_executable(
        tmp_path / "powershell.exe",
        """#!/usr/bin/env python3
import base64
import os
from pathlib import Path
import re
import sys
import time

arguments = sys.argv[1:]
encoded = arguments[arguments.index("-EncodedCommand") + 1]
command = base64.b64decode(encoded).decode("utf-16-le")
mode = re.search(r"\\$MetaResearchMode = '([A-Za-z]+)'", command).group(1)
token = re.search(r"\\$MetaResearchHolderToken = '([0-9a-f]+)'", command).group(1)
state = Path(os.environ["FAKE_GUARDIAN_STATE"])
if mode == "Hold":
    with open(os.environ["FAKE_GUARDIAN_HOLDS"], "a", encoding="ascii") as output:
        output.write("hold\\n")
    state.write_text(token, encoding="ascii")
    print(f"META_RESEARCH_WINDOWS_GUARDIAN_READY:{os.getpid()}:{token}", flush=True)
    while state.exists():
        time.sleep(0.01)
elif mode == "Query":
    if state.exists() and state.read_text(encoding="ascii") == token:
        print(f"META_RESEARCH_WINDOWS_GUARDIAN_CONFIRMED:{token}")
    else:
        raise SystemExit(3)
elif mode == "Release":
    if state.exists() and state.read_text(encoding="ascii") == token:
        state.unlink()
    print(f"META_RESEARCH_WINDOWS_GUARDIAN_RELEASED:{token}")
""",
    )
    monkeypatch.setenv("FAKE_GUARDIAN_STATE", str(native_state_path))
    monkeypatch.setenv("FAKE_GUARDIAN_HOLDS", str(hold_count_path))
    state_directory = tmp_path / "power-state"
    holder_ref = "holder:wsl-active-marker-failure"
    digest = hashlib.sha256(holder_ref.encode("utf-8")).hexdigest()[:24]
    marker_path = state_directory / f"windows-{digest}.holder"
    original_replace = os.replace

    def fail_active_marker(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        if (
            Path(destination) == marker_path
            and source_path.read_text(encoding="ascii").startswith("active:")
        ):
            raise OSError("injected active marker failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_active_marker)
    first = ProductionPowerInhibitor(
        state_directory,
        platform="wsl",
        powershell=powershell,
        readiness_timeout_seconds=2.0,
    )
    with pytest.raises(RuntimeProtectionUnavailable) as raised:
        first.acquire(holder_ref=holder_ref, reason="managed work")
    assert raised.value.code == (
        "power_inhibitor_windows_guardian_reconciliation_failed"
    )
    assert marker_path.read_text(encoding="ascii") == "pending\n"
    assert list(state_directory.glob(".*.tmp")) == []

    monkeypatch.setattr(os, "replace", original_replace)
    restarted = ProductionPowerInhibitor(
        state_directory,
        platform="wsl",
        powershell=powershell,
        readiness_timeout_seconds=2.0,
    )
    exact_status, exact_lease = restarted.query_exact_hold(holder_ref=holder_ref)
    assert exact_status == "confirmed"
    assert exact_lease is not None
    adopted = restarted.acquire(holder_ref=holder_ref, reason="restart")
    try:
        assert adopted.native_holder_ref == exact_lease.native_holder_ref
        assert hold_count_path.read_text(encoding="ascii").splitlines() == ["hold"]
    finally:
        restarted.release(adopted)


def test_wsl_pending_guardian_is_never_blindly_replaced_without_terminal_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands_path = tmp_path / "windows-guardian-commands.jsonl"
    powershell = _write_executable(
        tmp_path / "powershell.exe",
        """#!/usr/bin/env python3
import base64
import json
import os
import re
import sys

arguments = sys.argv[1:]
encoded = arguments[arguments.index("-EncodedCommand") + 1]
command = base64.b64decode(encoded).decode("utf-16-le")
mode = re.search(r"\\$MetaResearchMode = '([A-Za-z]+)'", command).group(1)
with open(os.environ["FAKE_GUARDIAN_COMMANDS"], "a", encoding="utf-8") as output:
    output.write(json.dumps(mode) + "\\n")
if mode in {"Query", "Release"}:
    raise SystemExit(3)
print("META_RESEARCH_WINDOWS_GUARDIAN_READY:12345:unexpected", flush=True)
""",
    )
    monkeypatch.setenv("FAKE_GUARDIAN_COMMANDS", str(commands_path))
    state_directory = tmp_path / "power-state"
    state_directory.mkdir()
    holder_ref = "holder:pending-wsl"
    digest = hashlib.sha256(holder_ref.encode("utf-8")).hexdigest()[:24]
    pending_path = state_directory / f"windows-{digest}.holder"
    pending_path.write_text("pending\n", encoding="ascii")
    inhibitor = ProductionPowerInhibitor(
        state_directory,
        platform="wsl",
        powershell=powershell,
        readiness_timeout_seconds=0.03,
    )

    exact_status, exact_lease = inhibitor.query_exact_hold(holder_ref=holder_ref)
    assert exact_status == "unknown"
    assert exact_lease is not None
    assert exact_lease.holder_ref == holder_ref
    with pytest.raises(RuntimeProtectionUnavailable) as raised:
        inhibitor.acquire(holder_ref=holder_ref, reason="managed work")

    assert raised.value.code == (
        "power_inhibitor_windows_guardian_reconciliation_failed"
    )
    assert pending_path.read_text(encoding="ascii") == "pending\n"
    modes = [
        json.loads(line)
        for line in commands_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "Hold" not in modes


@pytest.mark.parametrize("control_exit_code", [3, 8])
def test_wsl_readiness_cleanup_unknown_preserves_pending_guardian_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_exit_code: int,
) -> None:
    commands_path = tmp_path / "windows-guardian-commands.jsonl"
    powershell = _write_executable(
        tmp_path / "powershell.exe",
        """#!/usr/bin/env python3
import base64
import json
import os
import re
import sys
import time

arguments = sys.argv[1:]
encoded = arguments[arguments.index("-EncodedCommand") + 1]
command = base64.b64decode(encoded).decode("utf-16-le")
mode = re.search(r"\\$MetaResearchMode = '([A-Za-z]+)'", command).group(1)
with open(os.environ["FAKE_GUARDIAN_COMMANDS"], "a", encoding="utf-8") as output:
    output.write(json.dumps(mode) + "\\n")
if mode == "Hold":
    time.sleep(30)
raise SystemExit(int(os.environ["FAKE_GUARDIAN_CONTROL_EXIT_CODE"]))
""",
    )
    monkeypatch.setenv("FAKE_GUARDIAN_COMMANDS", str(commands_path))
    monkeypatch.setenv(
        "FAKE_GUARDIAN_CONTROL_EXIT_CODE", str(control_exit_code)
    )
    state_directory = tmp_path / "power-state"
    holder_ref = "holder:wsl-readiness-unknown"
    digest = hashlib.sha256(holder_ref.encode("utf-8")).hexdigest()[:24]
    pending_path = state_directory / f"windows-{digest}.holder"
    inhibitor = ProductionPowerInhibitor(
        state_directory,
        platform="wsl",
        powershell=powershell,
        readiness_timeout_seconds=0.03,
    )

    with pytest.raises(RuntimeProtectionUnavailable):
        inhibitor.acquire(holder_ref=holder_ref, reason="managed work")
    with pytest.raises(RuntimeProtectionUnavailable) as retried:
        inhibitor.acquire(holder_ref=holder_ref, reason="managed work")

    assert retried.value.code == (
        "power_inhibitor_windows_guardian_reconciliation_failed"
    )
    assert pending_path.read_text(encoding="ascii") == "pending\n"
    modes = [
        json.loads(line)
        for line in commands_path.read_text(encoding="utf-8").splitlines()
    ]
    assert modes.count("Hold") == 1


def test_wsl_release_query_unknown_preserves_guardian_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_state_path = tmp_path / "windows-guardian.state"
    release_marker_path = tmp_path / "windows-guardian.release"
    powershell = _write_executable(
        tmp_path / "powershell.exe",
        """#!/usr/bin/env python3
import base64
import os
from pathlib import Path
import re
import sys
import time

arguments = sys.argv[1:]
encoded = arguments[arguments.index("-EncodedCommand") + 1]
command = base64.b64decode(encoded).decode("utf-16-le")
mode = re.search(r"\\$MetaResearchMode = '([A-Za-z]+)'", command).group(1)
token = re.search(r"\\$MetaResearchHolderToken = '([0-9a-f]+)'", command).group(1)
state = Path(os.environ["FAKE_GUARDIAN_STATE"])
released = Path(os.environ["FAKE_GUARDIAN_RELEASE_MARKER"])
if mode == "Hold":
    state.write_text(token, encoding="ascii")
    print(f"META_RESEARCH_WINDOWS_GUARDIAN_READY:{os.getpid()}:{token}", flush=True)
    while state.exists():
        time.sleep(0.02)
elif mode == "Query":
    if os.environ.get("FAKE_GUARDIAN_QUERY_UNKNOWN") and released.exists():
        raise SystemExit(8)
    if state.exists() and state.read_text(encoding="ascii") == token:
        print(f"META_RESEARCH_WINDOWS_GUARDIAN_CONFIRMED:{token}")
    else:
        raise SystemExit(3)
elif mode == "Release":
    if not state.exists():
        raise SystemExit(3)
    state.unlink()
    released.write_text(token, encoding="ascii")
    print(f"META_RESEARCH_WINDOWS_GUARDIAN_RELEASED:{token}")
else:
    raise SystemExit(52)
""",
    )
    monkeypatch.setenv("FAKE_GUARDIAN_STATE", str(native_state_path))
    monkeypatch.setenv(
        "FAKE_GUARDIAN_RELEASE_MARKER", str(release_marker_path)
    )
    state_directory = tmp_path / "power-state"
    holder_ref = "holder:wsl-release-query-unknown"
    digest = hashlib.sha256(holder_ref.encode("utf-8")).hexdigest()[:24]
    pending_path = state_directory / f"windows-{digest}.holder"
    inhibitor = ProductionPowerInhibitor(
        state_directory,
        platform="wsl",
        powershell=powershell,
        readiness_timeout_seconds=0.1,
    )
    lease = inhibitor.acquire(holder_ref=holder_ref, reason="managed work")
    monkeypatch.setenv("FAKE_GUARDIAN_QUERY_UNKNOWN", "1")

    try:
        with pytest.raises(RuntimeProtectionUnavailable) as raised:
            inhibitor.release(lease)

        assert raised.value.code == (
            "power_inhibitor_windows_guardian_release_failed"
        )
        assert pending_path.exists()
    finally:
        monkeypatch.delenv("FAKE_GUARDIAN_QUERY_UNKNOWN", raising=False)
        inhibitor.release(lease)
