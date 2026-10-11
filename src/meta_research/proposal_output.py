"""Private, bounded stdout pages for the current proposal generation.

Understanding finishes before synthesis starts. Their authenticated spools form
one append-only logical stream, so the existing byte cursor survives that phase
transition. Provider output remains observation, never an accepted proposal.
"""

from dataclasses import dataclass
from pathlib import Path

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    read_transport_envelope,
    read_transport_key_for_operation,
)
from meta_research.stage_root_observations import _decode_raw_page


@dataclass(frozen=True)
class _OutputSource:
    path: Path
    size: int
    phase: str


def _operation_output(root: Path, job_ref: str, name: str, phase: str) -> _OutputSource | None:
    directory = root / canonical_hash({"job_ref": job_ref}) / name
    output = directory / "stdout.jsonl"
    invocation_path = directory / "invocation.json"
    # Never follow a path outside this generation's private spool, including a
    # missing symlink target or a redirected invocation/key.
    for path in (root, root / ".transport-seal.key", directory.parent, directory,
                 output, invocation_path):
        if path.is_symlink():
            raise ValueError("proposal_output_unavailable")
    if not invocation_path.exists() and not output.exists():
        return None
    if (not invocation_path.is_file() or invocation_path.stat().st_size > 131072
            or output.exists() and not output.is_file()):
        raise ValueError("proposal_output_unavailable")
    _, key = read_transport_key_for_operation(directory)
    invocation = read_transport_envelope(invocation_path, key)
    if (invocation.get("job_ref") != job_ref
            or invocation.get("operation_name") != name
            or invocation.get("schema_ref") != "meta-research/codex-provider-operation/v3"):
        raise ValueError("proposal_output_unavailable")
    return _OutputSource(output, output.stat().st_size if output.exists() else 0, phase)


def read_proposal_output(data_root: Path, generation: dict, after: int, limit: int) -> dict:
    if after < 0 or not 4 <= limit <= 262144:
        raise ValueError("proposal_output_cursor_invalid")
    ref = generation["ref"]
    job_ref = ref + ":proposal"
    root = data_root / "companion-provider" / "provider-operations"
    understanding = _operation_output(root, job_ref + ":understanding",
                                      "initialization-understanding", "understanding")
    synthesis = _operation_output(root, job_ref, "first-question-synthesis", "drafting")
    legacy = _operation_output(root, job_ref, "proposal-fork", "drafting")
    if synthesis is not None and legacy is not None:
        # Two distinct producer operations cannot share an ambiguous cursor.
        raise ValueError("proposal_output_unavailable")
    sources = [source for source in (understanding, synthesis or legacy) if source is not None]
    return _output_page(generation, sources, after, limit)


def read_preparation_output(data_root: Path, deepfetch: dict, after: int, limit: int) -> dict:
    """Read only the authorized request's pre-DeepFetch understanding operation."""
    if after < 0 or not 4 <= limit <= 262144:
        raise ValueError("proposal_output_cursor_invalid")
    request_ref = deepfetch["request_ref"]
    root = data_root / "companion-provider" / "provider-operations"
    source = _operation_output(root, request_ref + ":understanding",
                               "initialization-understanding", "understanding")
    status = {"accepting": "running"}.get(deepfetch["status"], deepfetch["status"])
    identity = {"ref": request_ref, "status": status, "failure": deepfetch.get("failure")}
    return _output_page(identity, [] if source is None else [source], after, limit)


def _output_page(generation: dict, sources: list[_OutputSource], after: int, limit: int) -> dict:
    status = generation["status"]
    page = dict(generation_ref=generation["ref"], status=status, text="", offset=after,
                next_offset=after, source_bytes=0, has_more=False,
                availability="waiting", failure=generation.get("failure"),
                phase=sources[-1].phase if sources else None)

    # Provider JSONL normally ends in a newline. Keep even a final unterminated
    # record distinct when the next operation starts, without rewriting either
    # spool or changing bytes already returned to the client.
    pieces: list[tuple[Path | None, int]] = []
    previous_terminated = True
    for source in sources:
        if not source.size:
            continue
        if pieces and not previous_terminated:
            pieces.append((None, 1))
        pieces.append((source.path, source.size))
        with source.path.open("rb") as stream:
            stream.seek(source.size - 1)
            previous_terminated = stream.read(1) == b"\n"
    size = sum(length for _, length in pieces)
    if after > size:
        raise ValueError("proposal_output_cursor_stale")
    if not size:
        if status not in {"queued", "running"}:
            page["availability"] = "unavailable"
        return page

    chunks = []
    skip = after
    remaining = min(limit, size - after)
    for path, length in pieces:
        if skip >= length:
            skip -= length
            continue
        count = min(remaining, length - skip)
        if path is None:
            chunk = b"\n"[skip:skip + count]
        else:
            with path.open("rb") as stream:
                stream.seek(skip)
                chunk = stream.read(count)
        if len(chunk) != count:
            raise ValueError("proposal_output_cursor_stale")
        chunks.append(chunk)
        remaining -= count
        skip = 0
        if not remaining:
            break
    text, consumed = _decode_raw_page(b"".join(chunks))
    page.update(text=text, next_offset=after + consumed, source_bytes=size,
                has_more=after + consumed < size, availability="ready")
    return page
