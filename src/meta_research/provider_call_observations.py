"""Measured provider inputs and reported usage; no character-to-token guesses."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

_LINE_LIMIT = 8 * 1024 * 1024
_NATIVE_PREFIX_LIMIT = 64 * 1024 * 1024


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def prompt_observation(prompt: str) -> dict[str, object]:
    raw = prompt.encode("utf-8")
    sections: dict[str, int] = {}
    marker = "Exact Owner context:\n"
    if marker in prompt:
        prefix, document = prompt.rsplit(marker, 1)
        try:
            material = json.loads(document)
        except (ValueError, RecursionError):
            material = None
        if isinstance(material, dict):
            sections = {name: len(_encoded(value)) for name, value in material.items()}
            sections["instructions"] = len((prefix + marker).encode("utf-8"))
    if not sections:
        for line in prompt.splitlines():
            match = re.match(r"([a-zA-Z_][a-zA-Z0-9_]*)=(\{|\[)", line)
            if match is None:
                continue
            try:
                value = json.loads(line.partition("=")[2])
            except (ValueError, RecursionError):
                continue
            sections[match[1]] = len(_encoded(value))
    return {"prompt_utf8_bytes": len(raw),
            "prompt_sha256": hashlib.sha256(raw).hexdigest(),
            "section_value_utf8_bytes": sections,
            "section_measurement": "canonical JSON values; key and separator bytes excluded",
            "input_tokens": None,
            "native_history_included_in_prompt_bytes": False}


def reported_usage(event: dict) -> dict[str, object] | None:
    """Keep the reporting scope explicit; a CLI turn can contain many calls."""
    usage, scope = event.get("usage"), "provider_turn_reported"
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        usage, scope = message["usage"], "provider_message_reported"
    payload = event.get("payload", event)
    info = payload.get("info") if isinstance(payload, dict) else None
    if isinstance(info, dict) and isinstance(info.get("last_token_usage"), dict):
        usage, scope = info["last_token_usage"], "provider_last_call_reported"
    if not isinstance(usage, dict):
        return None
    fields = ("input_tokens", "output_tokens", "cached_input_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens", "cache_write_input_tokens",
              "reasoning_output_tokens", "total_tokens")
    values = {key: usage[key] for key in fields
              if type(usage.get(key)) is int and usage[key] >= 0}
    if not values:
        return None
    return {"scope": scope,
            "input_tokens": values.get("input_tokens"),
            "output_tokens": values.get("output_tokens"),
            "cached_input_tokens": values.get("cached_input_tokens", values.get("cache_read_input_tokens")),
            "raw_reported": values,
            "cache_semantics": "cached tokens still occupy context; raw provider accounting preserved"}


def compaction_observation(event: dict) -> dict[str, object] | None:
    payload = event.get("payload", event)
    kind = str(event.get("type", ""))
    subtype = str(event.get("subtype", ""))
    payload_type = str(payload.get("type", "")) if isinstance(payload, dict) else ""
    if not any("compact" in value.lower() for value in (kind, subtype, payload_type)):
        return None
    raw = _encoded(event)
    return {"kind": kind, "subtype": subtype, "payload_type": payload_type,
            "event_content_sha256": hashlib.sha256(raw).hexdigest(),
            "event_utf8_bytes": len(raw),
            "exact_content_source": "stdout.jsonl event at source_byte_offset"}


def _native_session_observation(codex_home: Path, session_id: str) -> dict[str, object]:
    """Sample one authenticated session prefix; never treat it as this turn's total."""
    identifier = UUID(session_id)
    if str(identifier) != session_id or identifier.version != 7:
        return {"status": "unavailable", "reason": "unsupported_native_session_id"}
    # UUIDv7 embeds the session creation time. Probe only that session's named
    # rollout, allowing a local-date offset, without opening unrelated logs.
    day = datetime.fromtimestamp((identifier.int >> 80) / 1000, timezone.utc)
    root = codex_home.resolve()
    matches = []
    for delta in (-1, 0, 1):
        directory = root / "sessions" / (day + timedelta(days=delta)).strftime("%Y/%m/%d")
        if directory.is_dir():
            matches.extend(directory.glob(f"rollout-*-{session_id}.jsonl"))
    if len(matches) != 1:
        return {"status": "unavailable", "reason": "native_session_missing_or_ambiguous"}
    path = matches[0].resolve()
    if not path.is_relative_to(root):
        return {"status": "unavailable", "reason": "native_session_outside_home"}
    sample = {"status": "available", "session_id": session_id,
              "scope": "native_session_prefix_at_exit; not current invocation usage",
              "source_path": str(path), "sampled_at_utc": datetime.now(timezone.utc).isoformat(),
              "model_responses": [], "last_token_usage_snapshots": [], "compactions": [],
              "model_context_windows": [], "duplicate_response_records": 0,
              "duplicate_token_snapshots": 0, "conflicting_response_records": 0,
              "limited": False,
              "response_deduplication": "response_id within this sampled prefix; snapshots are not additional calls",
              "snapshot_deduplication": "identical last/total token usage and context window within this prefix",
              "limits": {"prefix_bytes": _NATIVE_PREFIX_LIMIT, "line_bytes": _LINE_LIMIT,
                         "model_responses": 512, "token_snapshots": 512, "compactions": 128}}
    digest = hashlib.sha256()
    offset, sequence = 0, 0
    responses, snapshots = {}, set()
    metadata_verified = False
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        sample.update(source_size_at_start=stat.st_size, source_mtime_ns_at_start=stat.st_mtime_ns)
        cutoff = min(stat.st_size, _NATIVE_PREFIX_LIMIT)
        sample["limited"] = stat.st_size > cutoff
        while offset < cutoff:
            line = stream.readline(min(_LINE_LIMIT + 1, cutoff - offset))
            if not line:
                sample["limited"] = True
                break
            start, offset, sequence = offset, offset + len(line), sequence + 1
            digest.update(line)
            if len(line) > _LINE_LIMIT or not line.endswith(b"\n"):
                sample["limited"] = True
                break
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                sample["limited"] = True
                if sequence == 1:
                    return {"status": "unavailable", "reason": "native_session_metadata_invalid"}
                continue
            if not isinstance(event, dict):
                if sequence == 1:
                    return {"status": "unavailable", "reason": "native_session_metadata_invalid"}
                sample["limited"] = True
                continue
            payload = event.get("payload")
            if sequence == 1 and (event.get("type") != "session_meta"
                    or not isinstance(payload, dict) or payload.get("id") != session_id):
                return {"status": "unavailable", "reason": "native_session_metadata_mismatch"}
            if sequence == 1:
                metadata_verified = True
            source = {"source_byte_offset": start, "source_line_bytes": len(line),
                      "source_sequence": sequence, "timestamp": event.get("timestamp")}
            if event.get("type") == "token_usage_record" and isinstance(payload, dict):
                if payload.get("thread_id") != session_id or payload.get("session_id") != session_id:
                    sample["limited"] = True
                    continue
                response_id = payload.get("response_id")
                usage = reported_usage({"usage": payload.get("usage")})
                if not isinstance(response_id, str) or not response_id or usage is None:
                    sample["limited"] = True
                    continue
                fingerprint = hashlib.sha256(_encoded(payload)).hexdigest()
                if response_id in responses:
                    sample["duplicate_response_records"] += 1
                    if responses[response_id] != fingerprint:
                        sample["conflicting_response_records"] += 1
                        sample["limited"] = True
                elif len(responses) < 512:
                    responses[response_id] = fingerprint
                    sample["model_responses"].append({**source, **usage,
                        "scope": "native_response_reported", "response_id": response_id,
                        "turn_id": payload.get("turn_id"), "root_turn_id": payload.get("root_turn_id"),
                        "record_content_sha256": hashlib.sha256(_encoded(event)).hexdigest()})
                else:
                    sample["limited"] = True
            info = payload.get("info") if isinstance(payload, dict) else None
            if isinstance(payload, dict) and payload.get("type") == "token_count" and isinstance(info, dict):
                usage = reported_usage(event)
                if usage is not None:
                    facts = {key: info.get(key) for key in ("last_token_usage", "total_token_usage", "model_context_window")}
                    fingerprint = hashlib.sha256(_encoded(facts)).hexdigest()
                    if fingerprint in snapshots:
                        sample["duplicate_token_snapshots"] += 1
                    elif len(snapshots) < 512:
                        snapshots.add(fingerprint)
                        sample["last_token_usage_snapshots"].append({**source, **usage,
                            "scope": "native_last_usage_snapshot; not independent call count", **facts})
                    else:
                        sample["limited"] = True
                    window = info.get("model_context_window")
                    if type(window) is int and window > 0 and window not in sample["model_context_windows"]:
                        if len(sample["model_context_windows"]) < 16:
                            sample["model_context_windows"].append(window)
                        else:
                            sample["limited"] = True
            compacted = compaction_observation(event)
            if compacted is not None:
                if len(sample["compactions"]) < 128:
                    sample["compactions"].append({**source, **compacted, "event": event,
                        "exact_content_source": "signed native_session compaction event; sampled prefix byte offset"})
                else:
                    sample["limited"] = True
    if not metadata_verified:
        return {"status": "unavailable", "reason": "native_session_metadata_incomplete"}
    sample.update(observed_prefix_bytes=offset, observed_prefix_sha256=digest.hexdigest(),
                  observed_source_line_count=sequence)
    return sample


def observe_provider_files(prompt_path: Path, stdout_path: Path, *, codex_home: Path | None = None) -> dict[str, object]:
    result = {"schema_ref": "meta-research/provider-call-observation/v1",
              **prompt_observation(prompt_path.read_bytes().decode("utf-8")),
              "usage_events": [], "compactions": [], "limited": False,
              "model_call_usage_available": False}
    offset, sequence = 0, 0
    session_ids = set()
    with stdout_path.open("rb") as stream:
        while line := stream.readline(8 * 1024 * 1024 + 1):
            if len(line) > 8 * 1024 * 1024:
                result["limited"] = True
                break
            start, offset, sequence = offset, offset + len(line), sequence + 1
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                session_ids.add(event["thread_id"])
            source = {"source_byte_offset": start, "source_line_bytes": len(line),
                      "source_sequence": sequence}
            usage = reported_usage(event)
            if usage is not None:
                result["model_call_usage_available"] |= usage["scope"] == "provider_last_call_reported"
                if len(result["usage_events"]) < 512:
                    result["usage_events"].append({**source, **usage})
                else:
                    result["limited"] = True
            try:
                compacted = compaction_observation(event)
            except (ValueError, UnicodeError, RecursionError):
                result["limited"] = True
                continue
            if compacted is not None:
                if len(result["compactions"]) < 128:
                    # This signed sidecar outlives temporary Provider stdout.
                    # Keep the observed event here, outside Harness summaries.
                    result["compactions"].append({**source, **compacted, "event": event})
                else:
                    result["limited"] = True
    result["observed_stdout_bytes"] = offset
    native = {"status": "unavailable", "reason": "trusted_home_or_unique_native_session_not_provided"}
    if codex_home is not None and len(session_ids) == 1:
        try:
            native = _native_session_observation(codex_home, next(iter(session_ids)))
        except (OSError, ValueError, UnicodeError, OverflowError, RecursionError):
            native = {"status": "unavailable", "reason": "native_session_sampling_failed"}
    result["native_session"] = native
    result["model_call_usage_available"] |= bool(native.get("model_responses") or native.get("last_token_usage_snapshots"))
    result["limited"] |= native.get("limited", False)
    return result
