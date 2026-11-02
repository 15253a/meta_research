"""One job-scoped grant for the recorder, over the resident Owner gateway."""
from __future__ import annotations

import json
from contextlib import contextmanager
from secrets import compare_digest

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation

READ_SOURCE = "timeline.sources.read"
PAGE_CHARS = 3500


class RecorderChannels:
    def __init__(self):
        self.gateway = self.reader = self.url = None
        self._active = None

    @property
    def operation(self):
        return SemanticOperation(
            READ_SOURCE,
            "research_recorder",
            "Read a page of the requested node's original JSON source at its observed version. "
            "Continue with next_offset until null. A missing or changed source is not an empty stage.",
            self._read,
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "node_key": {"type": "string", "minLength": 1},
                    "source_ref": {"type": "string", "minLength": 1},
                    "offset": {"type": "integer", "minimum": 0},
                },
                "required": ["node_key", "source_ref"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "node_key": {"type": "string"},
                    "source_ref": {"type": "string"},
                    "source_hash": {"type": "string"},
                    "text": {"type": "string"},
                    "offset": {"type": "integer"},
                    "next_offset": {"type": ["integer", "null"]},
                },
                "required": ["node_key", "source_ref", "source_hash", "text", "offset", "next_offset"],
            },
            unavailable_reason=self._unavailable,
        )

    @contextmanager
    def grant(self, job_ref, basis, native):
        if self.url is None or self.gateway is None or self.reader is None:
            raise SemanticMcpError("timeline_summary_reader_unavailable")
        # The recorder service already serializes provider calls.
        if self._active is not None:
            raise SemanticMcpError("timeline_summary_reader_busy")
        connection, _ = self.gateway.issue_channel(
            run_ref=job_ref,
            attempt_ref=job_ref,
            root_session_ref=native or job_ref,
            fence_ref=job_ref,
            capability_binding_hash=canonical_hash(basis),
            operation_ids=(READ_SOURCE,),
            root_kind=None,
            phase="research_recorder",
        )
        self._active = (connection, basis)
        try:
            yield connection
        finally:
            self.gateway.revoke_channel(connection.token)
            self._active = None

    def is_current(self, token):
        active = self._active
        return bool(active and token and compare_digest(active[0].token.encode(), token.encode()))

    def _unavailable(self, context):
        active = self._active
        return None if active and context.channel_grant_ref == active[0].grant_ref else "not_granted"

    def _read(self, context, arguments):
        active = self._active
        if not active or active[0].grant_ref != context.channel_grant_ref:
            raise SemanticMcpError("capability_unavailable")
        basis = active[1]
        ref = arguments["source_ref"]
        node = next((node for node in basis["nodes"] if node["node_key"] == arguments["node_key"]), None)
        if node is None or not any(source["ref"] == ref for source in node["sources"]):
            raise SemanticMcpError("capability_unavailable")
        try:
            body = self.reader.read_source(basis["quest_ref"], node, ref)
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error
        serialized = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        offset = arguments.get("offset", 0)
        if offset >= len(serialized):
            raise SemanticMcpError("content_offset_invalid")
        end = min(offset + PAGE_CHARS, len(serialized))
        return {
            "node_key": node["node_key"],
            "source_hash": node["source_hash"],
            "source_ref": ref,
            "text": serialized[offset:end],
            "offset": offset,
            "next_offset": None if end == len(serialized) else end,
        }
