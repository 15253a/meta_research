"""Durable authorities for the non-default import trigger paths.

The frozen Appendix-A schema intentionally has no separate trigger-authority
table.  We therefore use append-only ``decision`` rows, but treat their JSON as
a strict protocol rather than as free-form rationale.  This module is the one
place which builds and validates those protocols.

``human_named`` authority comes from a consumed, confirmed human
``inject_question`` directive.  ``stuck``/``sota_reference`` authority comes
from a trusted survey receipt which spawned a *different* reference question.
Neither authority is evidence and neither skips license/materialization gates.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional


HUMAN_NAMED_PROTOCOL = "human-named-import-v1"
REFERENCE_AUTHORITY_PROTOCOL = "import-reference-authority-v1"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_URI_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


class ImportAuthorityError(RuntimeError):
    """A purported trigger authority is missing, ambiguous, or corrupt."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")


def authority_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ImportAuthorityError(f"{field} 须为正整数")
    return value


def _bounded_text(value: Any, *, field: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise ImportAuthorityError(f"{field} 须为非空字符串")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ImportAuthorityError(f"{field} 不是合法 UTF-8") from error
    if size > max_bytes:
        raise ImportAuthorityError(f"{field} 超过 {max_bytes} bytes")
    if any((ord(ch) < 0x20 and ch not in "\n\r\t") or ord(ch) == 0x7F
           for ch in value):
        raise ImportAuthorityError(f"{field} 含非法控制字符")
    return value


def validate_github_uri(value: Any) -> str:
    uri = _bounded_text(value, field="canonical_uri", max_bytes=512)
    if not _GITHUB_URI_RE.fullmatch(uri):
        raise ImportAuthorityError(
            "human_named canonical_uri 须为规范 https://github.com/<owner>/<repo>")
    return uri


def validate_optional_revision(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise ImportAuthorityError("requested_revision 须为空或 40 位小写 commit")
    return value


def build_human_named_authority(*, directive_id: int, source_message_id: int,
                                goal_id: int, goal_ver: int, question_id: int,
                                canonical_uri: str,
                                requested_revision: Optional[str],
                                need_summary: str) -> Dict[str, Any]:
    core = {
        "protocol": HUMAN_NAMED_PROTOCOL,
        "trigger_kind": "human_named",
        "directive_id": _positive_int(directive_id, field="directive_id"),
        "source_message_id": _positive_int(
            source_message_id, field="source_message_id"),
        "goal_id": _positive_int(goal_id, field="goal_id"),
        "goal_ver": _positive_int(goal_ver, field="goal_ver"),
        "question_id": _positive_int(question_id, field="question_id"),
        "canonical_uri": validate_github_uri(canonical_uri),
        "requested_revision": validate_optional_revision(requested_revision),
        "need_summary": _bounded_text(
            need_summary, field="need_summary", max_bytes=8192),
    }
    return {**core, "authority_hash": authority_hash(core)}


def validate_human_named_authority(value: Any) -> Dict[str, Any]:
    keys = {
        "protocol", "trigger_kind", "directive_id", "source_message_id",
        "goal_id", "goal_ver", "question_id", "canonical_uri",
        "requested_revision", "need_summary", "authority_hash",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ImportAuthorityError("human_named authority 结构非法")
    expected = build_human_named_authority(
        directive_id=value["directive_id"],
        source_message_id=value["source_message_id"],
        goal_id=value["goal_id"], goal_ver=value["goal_ver"],
        question_id=value["question_id"],
        canonical_uri=value["canonical_uri"],
        requested_revision=value["requested_revision"],
        need_summary=value["need_summary"])
    if value != expected:
        raise ImportAuthorityError("human_named authority hash/身份不一致")
    return expected


def build_reference_authority(*, trigger_kind: str, origin_cycle_id: int,
                              origin_question_id: int, child_question_id: int,
                              goal_id: int, goal_ver: int, request_hash: str,
                              trigger_context_hash: str, policy_hash: str,
                              runner_call_id: int, receipt_ref: str,
                              result_hash: str, need_summary: str,
                              reference_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if trigger_kind not in ("stuck", "sota_reference"):
        raise ImportAuthorityError("reference authority trigger_kind 非法")
    for field, digest in (
            ("request_hash", request_hash),
            ("trigger_context_hash", trigger_context_hash),
            ("policy_hash", policy_hash), ("result_hash", result_hash)):
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ImportAuthorityError(f"{field} 非 sha256")
    receipt_ref = _bounded_text(
        receipt_ref, field="receipt_ref", max_bytes=4096)
    if reference_snapshot is not None:
        if not isinstance(reference_snapshot, dict):
            raise ImportAuthorityError("reference_snapshot 须为 object/null")
        # Full source-snapshot validation belongs to the trusted service; the
        # authority binds it byte-for-byte here so later activation cannot
        # substitute another reference.
        canonical_bytes(reference_snapshot)
    core = {
        "protocol": REFERENCE_AUTHORITY_PROTOCOL,
        "trigger_kind": trigger_kind,
        "origin_cycle_id": _positive_int(
            origin_cycle_id, field="origin_cycle_id"),
        "origin_question_id": _positive_int(
            origin_question_id, field="origin_question_id"),
        "child_question_id": _positive_int(
            child_question_id, field="child_question_id"),
        "goal_id": _positive_int(goal_id, field="goal_id"),
        "goal_ver": _positive_int(goal_ver, field="goal_ver"),
        "request_hash": request_hash,
        "trigger_context_hash": trigger_context_hash,
        "policy_hash": policy_hash,
        "runner_call_id": _positive_int(
            runner_call_id, field="runner_call_id"),
        "receipt_ref": receipt_ref,
        "result_hash": result_hash,
        "need_summary": _bounded_text(
            need_summary, field="need_summary", max_bytes=8192),
        "reference_snapshot": reference_snapshot,
    }
    return {**core, "authority_hash": authority_hash(core)}


def validate_reference_authority(value: Any) -> Dict[str, Any]:
    keys = {
        "protocol", "trigger_kind", "origin_cycle_id",
        "origin_question_id", "child_question_id", "goal_id", "goal_ver",
        "request_hash", "trigger_context_hash", "policy_hash",
        "runner_call_id", "receipt_ref", "result_hash",
        "need_summary", "reference_snapshot", "authority_hash",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ImportAuthorityError("reference authority 结构非法")
    expected = build_reference_authority(
        trigger_kind=value["trigger_kind"],
        origin_cycle_id=value["origin_cycle_id"],
        origin_question_id=value["origin_question_id"],
        child_question_id=value["child_question_id"],
        goal_id=value["goal_id"], goal_ver=value["goal_ver"],
        request_hash=value["request_hash"],
        trigger_context_hash=value["trigger_context_hash"],
        policy_hash=value["policy_hash"],
        runner_call_id=value["runner_call_id"],
        receipt_ref=value["receipt_ref"], result_hash=value["result_hash"],
        need_summary=value["need_summary"],
        reference_snapshot=value["reference_snapshot"])
    if value != expected:
        raise ImportAuthorityError("reference authority hash/身份不一致")
    return expected


def load_question_import_authority(conn, *, question_id: int) -> Optional[Dict[str, Any]]:
    """Return the one exact trigger authority for a question, or fail loud.

    Free-form question text, model output, and an unconsumed directive are
    deliberately insufficient.  The joins below bind the JSON authority back
    to the append-only human/control facts which created it.
    """
    _positive_int(question_id, field="question_id")
    found = []
    human_rows = conn.execute(
        "SELECT x.id,x.directive_id,x.payload_json,d.status,d.kind,"
        "d.source_interaction_message_id,d.consumed_decision_id,m.goal_id,m.goal_ver "
        "FROM decision x JOIN directive d ON d.id=x.directive_id "
        "JOIN interaction_message m ON m.id=d.source_interaction_message_id "
        "WHERE x.question_id=? AND x.actor='human' "
        "AND x.type='directive_inject_question' ORDER BY x.id",
        (question_id,)).fetchall()
    for (decision_id, directive_id, payload_raw, status, kind, source_message_id,
         consumed_decision_id, message_goal_id, message_goal_ver) in human_rows:
        if (status != "consumed" or kind != "inject_question"
                or consumed_decision_id != decision_id):
            raise ImportAuthorityError(
                f"q{question_id} human_named directive/decision 终态不一致")
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError as error:
            raise ImportAuthorityError(
                f"q{question_id} directive_inject_question payload 损坏") from error
        effect = payload.get("effect") if isinstance(payload, dict) else None
        authority = (effect.get("human_named_authority")
                     if isinstance(effect, dict) else None)
        if authority is None:
            continue                         # ordinary injected question
        authority = validate_human_named_authority(authority)
        if (authority["directive_id"] != directive_id
                or authority["source_message_id"] != source_message_id
                or authority["question_id"] != question_id
                or (authority["goal_id"], authority["goal_ver"])
                != (message_goal_id, message_goal_ver)):
            raise ImportAuthorityError(
                f"q{question_id} human_named authority 与来源行不一致")
        qlineage = conn.execute(
            "SELECT goal_id,born_goal_ver,source FROM question WHERE id=?",
            (question_id,)).fetchone()
        if (qlineage is None
                or tuple(qlineage[:2]) != (
                    authority["goal_id"], authority["goal_ver"])
                or qlineage[2] != "human"):
            raise ImportAuthorityError(
                f"q{question_id} human_named authority 与出生 lineage 不一致")
        found.append(authority)

    reference_rows = conn.execute(
        "SELECT id,payload_json,cycle_id FROM decision WHERE question_id=? "
        "AND actor='orchestrator' AND type='import_reference_authority' ORDER BY id",
        (question_id,)).fetchall()
    for _decision_id, payload_raw, cycle_id in reference_rows:
        try:
            authority = validate_reference_authority(json.loads(payload_raw))
        except (json.JSONDecodeError, ImportAuthorityError) as error:
            if isinstance(error, ImportAuthorityError):
                raise
            raise ImportAuthorityError(
                f"q{question_id} import_reference_authority payload 损坏") from error
        qrow = conn.execute(
            "SELECT parent_id,goal_id,born_goal_ver,born_cycle FROM question WHERE id=?",
            (question_id,)).fetchone()
        if (qrow is None
                or authority["child_question_id"] != question_id
                or authority["origin_question_id"] != qrow[0]
                or (authority["goal_id"], authority["goal_ver"]) != tuple(qrow[1:3])
                or authority["origin_cycle_id"] != qrow[3]
                or authority["origin_cycle_id"] != cycle_id):
            raise ImportAuthorityError(
                f"q{question_id} reference authority 与 question lineage 不一致")
        found.append(authority)

    if len(found) > 1:
        raise ImportAuthorityError(
            f"q{question_id} 同时存在多个 import trigger authority")
    return found[0] if found else None
