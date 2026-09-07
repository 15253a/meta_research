"""Private, bounded stdout pages for the current proposal generation."""

from pathlib import Path

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    read_transport_envelope,
    read_transport_key_for_operation,
)
from meta_research.stage_root_observations import _decode_raw_page


def read_proposal_output(data_root: Path, generation: dict, after: int, limit: int) -> dict:
    if after < 0 or not 4 <= limit <= 262144:
        raise ValueError("proposal_output_cursor_invalid")
    ref = generation["ref"]
    job_ref = ref + ":proposal"
    root = data_root / "companion-provider" / "provider-operations"
    directory = root / canonical_hash({"job_ref": job_ref}) / "proposal-fork"
    # Never follow an output path outside this generation's private spool.
    for path in (root, directory.parent, directory):
        if path.is_symlink():
            raise ValueError("proposal_output_unavailable")
    output = directory / "stdout.jsonl"
    status = generation["status"]
    page = dict(generation_ref=ref, status=status, text="", offset=after,
                next_offset=after, source_bytes=0, has_more=False,
                availability="waiting", failure=generation.get("failure"))
    if not output.exists():
        if after:
            raise ValueError("proposal_output_cursor_stale")
        if status not in {"queued", "running"}:
            page["availability"] = "unavailable"
        return page
    invocation_path = directory / "invocation.json"
    if output.is_symlink() or invocation_path.is_symlink() or not output.is_file():
        raise ValueError("proposal_output_unavailable")
    if invocation_path.stat().st_size > 131072:
        raise ValueError("proposal_output_unavailable")
    _, key = read_transport_key_for_operation(directory)
    invocation = read_transport_envelope(invocation_path, key)
    if (invocation.get("job_ref") != job_ref
            or invocation.get("operation_name") != "proposal-fork"
            or invocation.get("schema_ref") != "meta-research/codex-provider-operation/v3"):
        raise ValueError("proposal_output_unavailable")
    with output.open("rb") as stream:
        size = stream.seek(0, 2)
        if after > size:
            raise ValueError("proposal_output_cursor_stale")
        stream.seek(after)
        raw = stream.read(min(limit, size - after))
    text, consumed = _decode_raw_page(raw)
    page.update(text=text, next_offset=after + consumed, source_bytes=size,
                has_more=after + consumed < size, availability="ready")
    return page
