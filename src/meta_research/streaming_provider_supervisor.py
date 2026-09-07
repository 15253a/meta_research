"""Launch the durable supervisor with an unbuffered provider stdout pipe.

Transport configuration stays outside the frozen provider adapter sources so
already-admitted runs can retain their exact input and result contracts.
"""

from pathlib import Path
import subprocess
import sys

from meta_research.provider_supervisor import (
    ProviderProcessPlatform,
    ProviderSupervisorError,
    supervise,
)


class StreamingProviderProcessPlatform(ProviderProcessPlatform):
    def provider_spawn_options(self) -> dict[str, object]:
        # BufferedReader.read(64 KiB) waits for a full block or EOF. FileIO.read
        # returns the bytes currently available while keeping the same bounded
        # drain, signed receipts, process isolation, and cancellation behavior.
        return {**super().provider_spawn_options(), "bufsize": 0}


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 64
    try:
        supervise(
            Path(arguments[0]), process_platform=StreamingProviderProcessPlatform()
        )
    except (OSError, ProviderSupervisorError, subprocess.SubprocessError):
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
