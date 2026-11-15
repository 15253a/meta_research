"""The recorder's durable native Session and its observed, read-only source cut."""
from __future__ import annotations

import json
from pathlib import Path

from meta_research.owners.common import canonical_hash
from meta_research.quest_drafting import (
    CodexDraftingAdapter,
    DraftingUnavailable,
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


class TimelineSummaryBatchError(DraftingUnavailable):
    """Verified rows and actionable failures from one recorder observation."""

    def __init__(self, *, summaries, failures, native_session_ref, next_scan_seconds=300,
                 code="timeline_summary_output_invalid"):
        super().__init__(code, native_session_ref=native_session_ref)
        self.summaries = summaries
        self.failures = failures
        self.next_scan_seconds = next_scan_seconds


class _NodeValidationError(Exception):
    def __init__(self, code, message, source_refs=()):
        self.feedback = {"code": code, "source_refs": list(source_refs), "message": message}


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
            intro + f" 可按需要调用 {READ_SOURCE}，用 node_key、source_ref 阅读资料正文；"
            "若节点有 retry_feedback，按其中的原因修正结构或引用；只返回本次请求的节点。"
            "非空 next_offset 表示仍有后文，可继续读取。按已加载 Skill 的“每轮先讲本轮重点”要求，"
            "每个 Cycle 的第一句先说明当前 cycle_focus，再讲工作和结果。"
            "按 output_language 用自然连贯的人话讲清楚，让非专业读者一遍读懂；"
            "篇幅随内容需要调整，不固定句数，保留必要解释，避免重复和无关展开。\n"
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
        return self._result(raw, native, basis, native_session_ref)

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
        return self._result(raw, native, basis, job["native_session_ref"])

    def _result(self, raw, native, basis, expected_native):
        if (
            not isinstance(native, str)
            or not native.strip()
            or expected_native is not None
            and native != expected_native
        ):
            raise DraftingUnavailable("timeline_summary_session_invalid")
        rows = _validate(raw, basis, native)
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


def _validate(raw, basis, native):
    nodes = {node["node_key"]: node for node in basis["nodes"]}
    interval = raw.get("next_scan_seconds") if isinstance(raw, dict) else None
    interval = interval if type(interval) is int and 300 <= interval <= 7200 else 300
    if (not isinstance(raw, dict) or set(raw) != {"summaries", "next_scan_seconds"}
            or not isinstance(raw["summaries"], list)):
        raise TimelineSummaryBatchError(
            summaries=[], failures={key: {"code": "invalid_output", "source_refs": [],
                "message": "Return one summaries array and next_scan_seconds, with one row for each requested node."}
                for key in nodes}, native_session_ref=native, next_scan_seconds=interval)

    grouped = {key: [] for key in nodes}
    for row in raw["summaries"]:
        key = row.get("node_key") if isinstance(row, dict) else None
        # Unrequested or unidentifiable rows cannot supply a requested node.
        if isinstance(key, str) and key in grouped:
            grouped[key].append(row)

    summaries, failures = [], {}
    for key, node in nodes.items():
        rows = grouped[key]
        try:
            if not rows:
                raise _NodeValidationError("missing_node", "Return this requested node exactly once.")
            if len(rows) != 1:
                raise _NodeValidationError("duplicate_node", "Return one unambiguous row for this node; do not repeat its node_key.")
            summaries.append(_validate_node(rows[0], node, basis["output_language"]))
        except _NodeValidationError as error:
            failures[key] = error.feedback
    if failures:
        raise TimelineSummaryBatchError(summaries=summaries, failures=failures,
            native_session_ref=native, next_scan_seconds=interval)
    return summaries


def _validate_node(row, node, language):
    if set(row) != _OUTPUT_KEYS:
        raise _NodeValidationError("invalid_node", "Return all required summary fields without extra fields.")
    key = node["node_key"]
    kind, refs, sentence = row["summary_kind"], row["source_refs"], row["summary"]
    allowed = {s["ref"]: s["binding"] for s in node["sources"]}
    if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
        foreign = [ref for ref in refs if ref not in allowed]
        if foreign:
            raise _NodeValidationError("unallowed_source_refs",
                "Cite only source_refs listed for this requested node.", foreign)
    if (
        not refs and kind != "insufficient_evidence"
        or row["source_hash"] != node["source_hash"]
        or kind not in _SUMMARY_KINDS
        or not isinstance(refs, list)
        or any(not isinstance(ref, str) or ref not in allowed for ref in refs)
        or len(refs) != len(set(refs))
        or bool(allowed) and not refs
        or type(row["milestone"]) is not bool
        or row["milestone"] and kind not in {"tentative_finding", "accepted_conclusion"}
        or not isinstance(sentence, str)
        or not sentence.strip()
        or len(sentence.strip()) > _SUMMARY_LIMITS[language]
        or any(c in sentence for c in "\r\n\u2028\u2029")
    ):
        raise _NodeValidationError("invalid_node",
            "Match the requested source_hash, allowed source_refs, summary_kind and output schema.")
    # The source reader freezes these identities independently of model tool use.
    stage = allowed.get(key)
    if (node["kind"] == "stage" and stage and stage.get("kind") == "stage"
            and stage.get("stage") == node["stage"] and stage.get("artifacts") == []
            and "current" in stage and stage["current"] is None):
        return dict(row, summary=_EMPTY_SENTENCE[language], summary_kind="insufficient_evidence",
                    milestone=False, source_refs=[node["cycle_ref"]] if node["cycle_ref"] in allowed else [key])
    if sentence.strip() in _EMPTY_SENTENCE.values():
        raise _NodeValidationError("invalid_node", "The frozen stage metadata does not identify an empty stage.")
    if kind == "accepted_conclusion":
        eligible = node["kind"] == "cycle" or node["kind"] == "stage" and node["stage"] == "reasoning"
        if not eligible or not any(
            allowed[ref].get("kind") == "artifact" and allowed[ref].get("stage") == "reasoning"
            and allowed[ref].get("cycle_ref") == node["cycle_ref"]
            and allowed[ref].get("disposition") == "completed"
            and isinstance(allowed[ref].get("commit_ref"), str) and allowed[ref]["commit_ref"].strip()
            and isinstance(allowed[ref].get("outcome_ref"), str) and allowed[ref]["outcome_ref"] in refs
            for ref in refs
        ):
            raise _NodeValidationError("unsupported_conclusion",
                "Use accepted_conclusion only for this Cycle or Reasoning, citing its completed Reasoning outcome with a committed artifact identity.", refs)
    return dict(row, summary=sentence.strip())
