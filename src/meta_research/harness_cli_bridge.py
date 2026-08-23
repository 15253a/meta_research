from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path


_STDERR_LIMIT = 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--family", choices=("codex", "claude"), required=True)
    parser.add_argument("--provider-argv", type=Path, required=True)
    parser.add_argument("--output-schema", type=Path, required=True)
    parser.add_argument("--output-last-message", type=Path, required=True)
    parser.add_argument("prompt_source", choices=("-",))
    try:
        arguments = parser.parse_args(argv)
        provider_argv = json.loads(
            arguments.provider_argv.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SystemExit):
        return 64
    if (
        not isinstance(provider_argv, list)
        or not provider_argv
        or not all(isinstance(value, str) and value for value in provider_argv)
    ):
        return 64

    child_environment = dict(os.environ)
    child_environment.pop("META_RESEARCH_HARNESS_FAMILY", None)
    child_environment.pop("META_RESEARCH_PROVIDER_OPERATION_REF", None)
    workspace_value = child_environment.pop(
        "META_RESEARCH_HARNESS_WORKSPACE", ""
    )
    workspace = Path(workspace_value)
    if not workspace.is_absolute() or not workspace.is_dir():
        return 64
    redactions = _sensitive_environment_values(child_environment)
    try:
        process = subprocess.Popen(
            provider_argv,
            stdin=sys.stdin.buffer,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_environment,
            cwd=workspace,
        )
    except OSError:
        _write_bridge_result(arguments.output_last_message, 127)
        return 127
    assert process.stdout is not None
    assert process.stderr is not None
    stderr = bytearray()
    stdout_drain = threading.Thread(
        target=_copy_redacted,
        args=(process.stdout, sys.stdout.buffer, redactions),
        daemon=True,
    )
    stderr_drain = threading.Thread(
        target=_drain_bounded,
        args=(process.stderr, stderr),
        daemon=True,
    )
    stdout_drain.start()
    stderr_drain.start()
    returncode = process.wait()
    stdout_drain.join(timeout=1.0)
    stderr_drain.join(timeout=1.0)
    if stdout_drain.is_alive() or stderr_drain.is_alive():
        return 70
    _write_bridge_result(arguments.output_last_message, returncode)
    if returncode != 0:
        error_kind = (
            "auth_revoked"
            if _looks_like_auth_failure(stderr.decode("utf-8", errors="replace"))
            else "provider_failed"
        )
        sys.stdout.write(
            "\n"
            + json.dumps(
                {
                    "type": "meta_research.provider_error",
                    "family": arguments.family,
                    "error_kind": error_kind,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        sys.stdout.flush()
    return returncode


def _drain_bounded(stream, destination: bytearray) -> None:
    try:
        while chunk := stream.read(64 * 1024):
            remaining = _STDERR_LIMIT - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
    finally:
        stream.close()


def _copy_redacted(stream, destination, redactions: tuple[bytes, ...]) -> None:
    try:
        if not redactions:
            while chunk := stream.read(64 * 1024):
                destination.write(chunk)
                destination.flush()
            return
        pending = bytearray()
        maximum_length = max(len(value) for value in redactions)
        while chunk := stream.read(64 * 1024):
            pending.extend(chunk)
            while len(pending) >= maximum_length:
                encoded = bytes(pending)
                matches = [
                    (encoded.find(secret), -len(secret), secret)
                    for secret in redactions
                    if encoded.find(secret) >= 0
                ]
                if matches:
                    offset, _negative_length, secret = min(matches)
                    destination.write(encoded[:offset])
                    destination.write(b"*" * len(secret))
                    del pending[: offset + len(secret)]
                    continue
                safe_length = len(pending) - maximum_length + 1
                destination.write(bytes(pending[:safe_length]))
                del pending[:safe_length]
                destination.flush()
        encoded = bytes(pending)
        for secret in redactions:
            encoded = encoded.replace(secret, b"*" * len(secret))
        destination.write(encoded)
        destination.flush()
    finally:
        stream.close()


def _sensitive_environment_values(
    environment: dict[str, str]
) -> tuple[bytes, ...]:
    markers = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTHORIZATION")
    values = {
        value.encode("utf-8")
        for name, value in environment.items()
        if any(marker in name.upper() for marker in markers)
        and 8 <= len(value.encode("utf-8")) <= 4096
    }
    return tuple(sorted(values, key=len, reverse=True))


def _write_bridge_result(path: Path, returncode: int) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_ref": "meta-research/harness-bridge-result/v1",
                "returncode": returncode,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _looks_like_auth_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        marker in lowered
        for marker in ("unauthorized", "authentication", "login required", "401")
    )


if __name__ == "__main__":
    raise SystemExit(main())
