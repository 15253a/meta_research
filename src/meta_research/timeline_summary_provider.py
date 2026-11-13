"""The recorder's durable native Session and its observed, read-only source cut."""
from __future__ import annotations

import json
from pathlib import Path

from meta_research.owners.common import canonical_hash
from meta_research.quest_drafting import (
    PROVIDER_STREAM_MAX_BYTES,
    CodexDraftingAdapter,
    DraftingUnavailable,
    _read_bounded_text,
    _read_drafting_invocation,
    _read_durable_job,
)
from meta_research.recorder_sources import READ_SOURCE, RecorderChannels
from meta_research.system_prompt import RESEARCH_SYSTEM_PROMPT

_SKILL_PATH = Path(__file__).with_name("skills") / "research-recorder" / "SKILL.md"
_SUMMARY_LIMITS = {"zh": 600, "en": 1800}
_SUMMARY_KINDS = ("process", "tentative_finding", "accepted_conclusion", "insufficient_evidence")
_OUTPUT_KEYS = {"node_key", "source_hash", "summary", "summary_kind", "source_refs", "milestone"}
_EMPTY_SENTENCE = {
    "zh": "暂时没有这个阶段的工作记录。",
    "en": "No work has been recorded for this stage yet.",
}


class CodexTimelineSummaryAdapter(CodexDraftingAdapter):
    def __init__(self, *args, channels: RecorderChannels, **kwargs):
        super().__init__(*args, **kwargs)
        self.channels = channels

    def _provider_argv(self, *args, **kwargs):
        argv = super()._provider_argv(*args, **kwargs)
        configs = (
            # Override the drafting catalog's base instructions for the recorder role.
            "model_instructions_file=" + json.dumps(str(_SKILL_PATH.resolve())),
            "developer_instructions=" + json.dumps(
                RESEARCH_SYSTEM_PROMPT + "\n\n当前角色是独立的只读研究记录员，按已加载的 research-recorder 合同总结。"
                "公共研究原则用于理解研究对象、阶段职责与证据含义；其中的执行、委派和交接由相应研究根承担。"
                "本角色只通过获授的 timeline.sources.read 阅读本次观察资料，按 output_language 输出摘要。",
                ensure_ascii=False,
            ),
            "mcp_servers.meta_research.url=" + json.dumps(self.channels.url),
            'mcp_servers.meta_research.bearer_token_env_var="META_RESEARCH_MCP_TOKEN"',
            "mcp_servers.meta_research.required=true",
            'mcp_servers.meta_research.default_tools_approval_mode="approve"',
        )
        position = argv.index("--sandbox")
        argv[position:position] = [part for config in configs for part in ("--config", config)]
        return argv

    def summarize(self, *, quest_ref, nodes, native_session_ref, job_ref, observed_at, output_language="zh"):
        basis = _basis(quest_ref, nodes, observed_at, output_language)
        if not job_ref or (native_session_ref is not None and not native_session_ref.strip()):
            raise DraftingUnavailable("timeline_summary_request_invalid")
        if self._durable_job_directory(job_ref).exists():
            return self.recover_summaries(dict(basis, native_session_ref=native_session_ref, job_ref=job_ref))
        if not _SKILL_PATH.is_file():
            raise DraftingUnavailable("timeline_summary_skill_unavailable")
        visible = [
            {
                **{k: v for k, v in n.items() if k != "sources"},
                "sources": [{"ref": s["ref"], "label": s.get("label", s["ref"])} for s in n["sources"]],
            }
            for n in nodes
        ]
        intro = "你是本 Quest 的研究记录员，为本批每个节点说明它做了什么。" if native_session_ref is None else "按当前记录员说明，更新本批节点的工作介绍。"
        prompt = (
            intro + f" 调用 {READ_SOURCE}，用 node_key、source_ref 阅读本次观察的必要正文；"
            "非空 next_offset 表示仍有后文，可继续读取。按 output_language 用易懂的一小段话讲清每个节点做了什么、得到什么结果，可以用几句话解释。\n"
            + json.dumps({**basis, "nodes": visible}, ensure_ascii=False, separators=(",", ":"))
        )
        with self.channels.grant(job_ref, basis, native_session_ref) as grant:
            raw, native = self._invoke(
                prompt,
                _output_schema(len(nodes), _SUMMARY_LIMITS[output_language]),
                native_session_ref=native_session_ref,
                ephemeral=False,
                job_ref=job_ref,
                request_hash=canonical_hash(basis),
                environment={"META_RESEARCH_MCP_TOKEN": grant.token},
            )
        return self._result(job_ref, raw, native, basis, native_session_ref)

    def recover_summaries(self, job):
        basis = _basis(job["quest_ref"], job["nodes"], job["observed_at"], job["output_language"])
        directory = self._durable_job_directory(job["job_ref"])
        invocation = _read_drafting_invocation(directory, job_ref=job["job_ref"])
        if (
            invocation.get("request_hash") != canonical_hash(basis)
            or invocation["native_session_ref"] != job["native_session_ref"]
            or invocation["ephemeral"]
        ):
            raise DraftingUnavailable("codex_job_spool_conflict")
        raw, native = _read_durable_job(directory, invocation)
        return self._result(job["job_ref"], raw, native, basis, job["native_session_ref"])

    def _result(self, job_ref, raw, native, basis, expected_native):
        if (
            not isinstance(native, str)
            or not native.strip()
            or expected_native is not None
            and native != expected_native
        ):
            raise DraftingUnavailable("timeline_summary_session_invalid")
        stdout = _read_bounded_text(
            self._durable_job_directory(job_ref) / "stdout.jsonl", PROVIDER_STREAM_MAX_BYTES
        )
        bodies = _read_bodies(stdout, basis, native)
        rows = _validate(raw, basis, bodies, native)
        interval = raw["next_scan_seconds"]
        return rows, native, interval if type(interval) is int and 300 <= interval <= 7200 else 300


def _basis(quest_ref, nodes, observed_at, output_language):
    if (
        not quest_ref
        or not isinstance(nodes, list)
        or not nodes
        or output_language not in _SUMMARY_LIMITS
        or not isinstance(observed_at, (int, float))
        or isinstance(observed_at, bool)
    ):
        raise DraftingUnavailable("timeline_summary_request_invalid")
    keys = set()
    for node in nodes:
        if (
            not isinstance(node, dict)
            or node.get("quest_ref") != quest_ref
            or "content" in node
            or not all(isinstance(node.get(k), str) and node[k] for k in ("node_key", "kind", "source_hash"))
            or node["node_key"] in keys
            or not isinstance(node.get("sources"), list)
            or any(
                not isinstance(s, dict)
                or not isinstance(s.get("ref"), str)
                or not s["ref"]
                or not isinstance(s.get("binding"), dict)
                for s in node["sources"]
            )
        ):
            raise DraftingUnavailable("timeline_summary_request_invalid")
        keys.add(node["node_key"])
    return {"quest_ref": quest_ref, "output_language": output_language, "observed_at": observed_at, "nodes": nodes}


def _output_schema(count, maximum_length):
    fields = {
        "node_key": {"type": "string", "minLength": 1},
        "source_hash": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1, "maxLength": maximum_length},
        "summary_kind": {"type": "string", "enum": list(_SUMMARY_KINDS)},
        "milestone": {"type": "boolean"},
        "source_refs": {"type": "array", "items": {"type": "string", "minLength": 1}},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summaries", "next_scan_seconds"],
        "properties": {
            "next_scan_seconds": {"type": "integer", "minimum": 300, "maximum": 7200},
            "summaries": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": fields,
                    "required": list(fields),
                },
            },
        },
    }


def _read_bodies(stdout, basis, native):
    # Use the original provider output, including on recovery. Only successful pages
    # of this root's reader count. The temporary spool already records those calls.
    nodes = {n["node_key"]: n for n in basis["nodes"]}
    pages, failed = {}, set()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if (
            not isinstance(item, dict)
            or item.get("type") != "mcp_tool_call"
            or item.get("server") != "meta_research"
            or item.get("tool") != READ_SOURCE
        ):
            continue
        args = item.get("arguments")
        if not isinstance(args, dict):
            continue
        key, ref = args.get("node_key"), args.get("source_ref")
        if not isinstance(key, str) or not isinstance(ref, str):
            continue
        node = nodes.get(key)
        if not node or not any(s["ref"] == ref for s in node["sources"]):
            continue
        origin = (key, ref)
        result = item.get("result") or {}
        if (
            item.get("status") != "completed"
            or result.get("isError")
            or result.get("is_error")
            or item.get("error")
        ):
            failed.add(origin)
            continue
        # Codex retains MCP text content even when the native event omits structuredContent.
        try:
            page = json.loads(result["content"][0]["text"])
            if (
                page["source_hash"] != node["source_hash"]
                or page["node_key"] != key
                or page["source_ref"] != ref
                or page["offset"] != args.get("offset", 0)
                or not isinstance(page["text"], str)
            ):
                continue
            pages.setdefault(origin, {})[page["offset"]] = page
        except (ValueError, KeyError, TypeError, IndexError):
            failed.add(origin)
    bodies = {}
    for origin, chunks in pages.items():
        # A segment may be sufficient for ordinary progress. Only a full body
        # can establish the empty-stage or accepted-claim qualifications.
        bodies[origin] = None
        offset, parts = 0, []
        while offset in chunks:
            page = chunks[offset]
            parts.append(page["text"])
            if page["next_offset"] is None:
                body = json.loads("".join(parts))
                if isinstance(body, dict):
                    bodies[origin] = body
                    failed.discard(origin)
                break
            if not page["text"] or page["next_offset"] != offset + len(page["text"]):
                break
            offset = page["next_offset"]
    if failed:
        raise DraftingUnavailable("timeline_summary_source_unavailable", native_session_ref=native)
    return bodies


def _validate(raw, basis, bodies, native):
    def invalid():
        return DraftingUnavailable("timeline_summary_output_invalid", native_session_ref=native)

    if not isinstance(raw, dict) or set(raw) != {"summaries", "next_scan_seconds"}:
        raise invalid()
    nodes = {node["node_key"]: node for node in basis["nodes"]}
    rows = raw["summaries"]
    if not isinstance(rows, list) or len(rows) != len(nodes):
        raise invalid()
    results = {}
    language = basis["output_language"]
    # The same source may legitimately support multiple levels of this batch.
    read = {}
    for (key, ref), body in bodies.items():
        source = next(s for s in nodes[key]["sources"] if s["ref"] == ref)
        identity = (ref, canonical_hash(source["binding"]))
        if identity not in read or body is not None:
            read[identity] = body
    read_refs = {ref for ref, _ in read}
    for row in rows:
        if not isinstance(row, dict) or set(row) != _OUTPUT_KEYS:
            raise invalid()
        key = row["node_key"]
        if not isinstance(key, str) or key not in nodes or key in results:
            raise invalid()
        node = nodes[key]
        kind, refs, sentence = row["summary_kind"], row["source_refs"], row["summary"]
        allowed = {s["ref"]: s["binding"] for s in node["sources"]}
        if (
            not refs
            and kind != "insufficient_evidence"
            or row["source_hash"] != node["source_hash"]
            or kind not in _SUMMARY_KINDS
            or not isinstance(refs, list)
            or any(not isinstance(ref, str) or ref not in allowed for ref in refs)
            or len(refs) != len(set(refs))
            or bool(allowed)
            and not refs
            or type(row["milestone"]) is not bool
            or row["milestone"]
            and kind not in {"tentative_finding", "accepted_conclusion"}
            or not isinstance(sentence, str)
            or not sentence.strip()
            or len(sentence.strip()) > _SUMMARY_LIMITS[language]
            or any(c in sentence for c in "\r\n\u2028\u2029")
        ):
            raise invalid()
        available = {
            ref: read[identity]
            for ref, binding in allowed.items()
            if ref in read_refs and (identity := (ref, canonical_hash(binding))) in read
        }
        stage = available.get(key)
        if (
            node["kind"] == "stage"
            and stage
            and allowed[key].get("kind") == "stage"
            and stage.get("stage") == node["stage"]
            and stage.get("artifacts") == []
            and "current" in stage
            and stage["current"] is None
        ):
            row = dict(
                row,
                summary=_EMPTY_SENTENCE[language],
                summary_kind="insufficient_evidence",
                milestone=False,
                source_refs=[node["cycle_ref"]] if node["cycle_ref"] in allowed else [key],
            )
        else:
            if not all(ref in available for ref in refs) or sentence.strip() in _EMPTY_SENTENCE.values():
                raise invalid()
            if kind == "accepted_conclusion":
                eligible = node["kind"] == "cycle" or node["kind"] == "stage" and node["stage"] == "reasoning"
                if not eligible or not any(
                    allowed[ref].get("kind") == "artifact"
                    and allowed[ref].get("stage") == "reasoning"
                    and allowed[ref].get("cycle_ref") == node["cycle_ref"]
                    and _supports_conclusion(available[ref] or {}, refs)
                    for ref in refs
                ):
                    raise invalid()
            row = dict(row, summary=sentence.strip())
        results[key] = row
    return [results[key] for key in nodes]


def _supports_conclusion(body, refs):
    judgment, source = body.get("content"), body.get("source")
    if not isinstance(judgment, dict) or not isinstance(source, dict):
        return False
    claim = judgment.get("claim")
    return (
        body.get("stage") == "reasoning"
        and body.get("status") == "accepted"
        and isinstance(claim, str)
        and claim.strip()
        and any(source.get(k) in refs for k in ("content_ref", "outcome_ref"))
    )
