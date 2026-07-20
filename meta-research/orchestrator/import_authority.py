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
QUESTION_REQUEST_BINDING_PROTOCOL = "question-request-binding-v1"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_QREF_RE = re.compile(r"^q[1-9][0-9]*$")
_REQUEST_REF_RE = re.compile(r"^db:(directive|decision):([1-9][0-9]*)$")
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


def build_question_request_binding(
        *, source_kind: str, request_ref: str, request_decision_id: int,
        reasoning_request_hash: str, question_id: int, goal_id: int,
        goal_ver: int, spawn_kind: str, parent_question_id: Optional[str],
        requested_text: str,
        source_authority_hash: Optional[str]) -> Dict[str, Any]:
    """Build the append-only join between a frozen request and its question.

    The request itself lives in an immutable consumed human decision or an
    ``import_trigger_completed`` decision.  This record is written only after
    StateStore has admitted the question, in the same transaction as that
    INSERT.  Keeping the join separate preserves ``decision`` append-only
    semantics: the earlier consuming/completion decision is never patched.
    """
    if source_kind not in ("console_directive", "import_trigger_completed"):
        raise ImportAuthorityError("question request binding source_kind 非法")
    if not isinstance(request_ref, str):
        raise ImportAuthorityError("question request binding request_ref 非法")
    match = _REQUEST_REF_RE.fullmatch(request_ref)
    expected_domain = (
        "directive" if source_kind == "console_directive" else "decision")
    if match is None or match.group(1) != expected_domain:
        raise ImportAuthorityError("question request binding request_ref/source 不一致")
    if spawn_kind not in ("followup", "import_reference"):
        raise ImportAuthorityError("question request binding spawn_kind 非法")
    if (parent_question_id is not None
            and (not isinstance(parent_question_id, str)
                 or _QREF_RE.fullmatch(parent_question_id) is None)):
        raise ImportAuthorityError(
            "question request binding parent_question_id 非法")
    if (not isinstance(reasoning_request_hash, str)
            or _SHA256_RE.fullmatch(reasoning_request_hash) is None):
        raise ImportAuthorityError(
            "question request binding reasoning_request_hash 非 sha256")
    if (source_authority_hash is not None
            and (not isinstance(source_authority_hash, str)
                 or _SHA256_RE.fullmatch(source_authority_hash) is None)):
        raise ImportAuthorityError(
            "question request binding source_authority_hash 非 sha256/null")
    core = {
        "protocol": QUESTION_REQUEST_BINDING_PROTOCOL,
        "source_kind": source_kind,
        "request_ref": request_ref,
        "request_decision_id": _positive_int(
            request_decision_id, field="request_decision_id"),
        "reasoning_request_hash": reasoning_request_hash,
        "question_id": _positive_int(question_id, field="question_id"),
        "goal_id": _positive_int(goal_id, field="goal_id"),
        "goal_ver": _positive_int(goal_ver, field="goal_ver"),
        "spawn_kind": spawn_kind,
        "parent_question_id": parent_question_id,
        "requested_text": _bounded_text(
            requested_text, field="requested_text", max_bytes=16_384),
        "source_authority_hash": source_authority_hash,
    }
    return {**core, "binding_hash": authority_hash(core)}


def validate_question_request_binding(value: Any) -> Dict[str, Any]:
    keys = {
        "protocol", "source_kind", "request_ref", "request_decision_id",
        "reasoning_request_hash", "question_id", "goal_id", "goal_ver",
        "spawn_kind", "parent_question_id", "requested_text",
        "source_authority_hash", "binding_hash",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ImportAuthorityError("question request binding 结构非法")
    expected = build_question_request_binding(
        source_kind=value["source_kind"], request_ref=value["request_ref"],
        request_decision_id=value["request_decision_id"],
        reasoning_request_hash=value["reasoning_request_hash"],
        question_id=value["question_id"], goal_id=value["goal_id"],
        goal_ver=value["goal_ver"], spawn_kind=value["spawn_kind"],
        parent_question_id=value["parent_question_id"],
        requested_text=value["requested_text"],
        source_authority_hash=value["source_authority_hash"])
    if value != expected:
        raise ImportAuthorityError("question request binding hash/身份不一致")
    return expected


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


def _question_request_bindings(conn, question_id: int):
    rows = conn.execute(
        "SELECT id,cycle_id,directive_id,payload_json FROM decision "
        "WHERE question_id=? AND actor='orchestrator' "
        "AND type='question_request_bound' ORDER BY id",
        (question_id,)).fetchall()
    result = []
    for decision_id, cycle_id, directive_id, payload_raw in rows:
        try:
            binding = validate_question_request_binding(json.loads(payload_raw))
        except (json.JSONDecodeError, ImportAuthorityError) as error:
            if isinstance(error, ImportAuthorityError):
                raise
            raise ImportAuthorityError(
                f"q{question_id} question_request_bound payload 损坏") from error
        if binding["question_id"] != question_id:
            raise ImportAuthorityError(
                f"q{question_id} question request binding 指向其他问题")
        if ((binding["source_kind"] == "console_directive")
                != (directive_id is not None)):
            raise ImportAuthorityError(
                f"q{question_id} question request binding directive provenance 非法")
        result.append((decision_id, cycle_id, directive_id, binding))
    if len(result) > 1:
        raise ImportAuthorityError(
            f"q{question_id} 存在多个 question request binding")
    return result


def load_question_import_authority(conn, *, question_id: int) -> Optional[Dict[str, Any]]:
    """Return the one exact trigger authority for a question, or fail loud.

    Free-form question text, model output, and an unconsumed directive are
    deliberately insufficient.  The joins below bind the JSON authority back
    to the append-only human/control facts which created it.
    """
    _positive_int(question_id, field="question_id")
    found = []
    bindings = _question_request_bindings(conn, question_id)
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

    # Two-phase question ownership cannot mutate the earlier consumed human
    # decision (``decision`` is append-only).  StateStore therefore writes a
    # separate binding plus this authority after admitting the question.  The
    # joins below still prove the exact consumed directive/message provenance.
    bound_human_rows = conn.execute(
        "SELECT id,cycle_id,directive_id,payload_json FROM decision "
        "WHERE question_id=? AND actor='orchestrator' "
        "AND type='human_named_import_authority' ORDER BY id",
        (question_id,)).fetchall()
    for authority_decision_id, authority_cycle_id, directive_id, payload_raw in bound_human_rows:
        try:
            authority = validate_human_named_authority(json.loads(payload_raw))
        except (json.JSONDecodeError, ImportAuthorityError) as error:
            if isinstance(error, ImportAuthorityError):
                raise
            raise ImportAuthorityError(
                f"q{question_id} human_named_import_authority payload 损坏") from error
        if len(bindings) != 1:
            raise ImportAuthorityError(
                f"q{question_id} human_named authority 缺唯一 request binding")
        _binding_decision_id, binding_cycle_id, binding_directive_id, binding = bindings[0]
        if (binding["source_kind"] != "console_directive"
                or binding["spawn_kind"] != "import_reference"
                or binding["request_ref"] != f"db:directive:{directive_id}"
                or binding["source_authority_hash"] != authority["authority_hash"]
                or binding_directive_id != directive_id
                or binding_cycle_id != authority_cycle_id
                or authority["directive_id"] != directive_id
                or authority["question_id"] != question_id):
            raise ImportAuthorityError(
                f"q{question_id} human_named authority/request binding 不一致")
        source = conn.execute(
            "SELECT d.status,d.kind,d.hardness,d.consumed_cycle,"
            "d.consumed_decision_id,d.source_interaction_message_id,"
            "x.cycle_id,x.question_id,x.directive_id,x.actor,x.type,x.payload_json,"
            "m.goal_id,m.goal_ver "
            "FROM directive d JOIN decision x ON x.id=d.consumed_decision_id "
            "JOIN interaction_message m ON m.id=d.source_interaction_message_id "
            "WHERE d.id=?", (directive_id,)).fetchone()
        if (source is None
                or source[:5] != (
                    "consumed", "inject_question", "hard", authority_cycle_id,
                    binding["request_decision_id"])
                or source[5] != authority["source_message_id"]
                or source[6:11] != (
                    authority_cycle_id, None, directive_id, "human",
                    "directive_inject_question")
                or source[12:14] != (
                    authority["goal_id"], authority["goal_ver"])):
            raise ImportAuthorityError(
                f"q{question_id} human_named consumed directive provenance 不一致")
        classified = conn.execute(
            "SELECT intent,directive_id FROM interaction_classification "
            "WHERE message_id=?", (authority["source_message_id"],)).fetchone()
        if classified != ("directive", directive_id):
            raise ImportAuthorityError(
                f"q{question_id} human_named classification provenance 不一致")
        try:
            consumed_payload = json.loads(source[11])
            effect = consumed_payload["effect"]
            request = effect["reasoning_question_request"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ImportAuthorityError(
                f"q{question_id} human_named consumed request 损坏") from error
        repo = request.get("human_named_repo") if isinstance(request, dict) else None
        if (not isinstance(effect, dict) or not isinstance(request, dict)
                or effect.get("applies_to_reasoning_cycle")
                != f"c{authority_cycle_id}"
                or authority_hash(request) != binding["reasoning_request_hash"]
                or request.get("request_ref") != binding["request_ref"]
                or request.get("requested_text") != binding["requested_text"]
                or request.get("parent_question_id")
                != binding["parent_question_id"]
                or request.get("suggested_kind") != "import_reference"
                or request.get("requires_reasoning_predicate") is not True
                or not isinstance(repo, dict)
                or repo.get("canonical_uri") != authority["canonical_uri"]
                or repo.get("requested_revision")
                != authority["requested_revision"]
                or request.get("need_summary") != authority["need_summary"]):
            raise ImportAuthorityError(
                f"q{question_id} human_named frozen request/authority 不一致")
        qlineage = conn.execute(
            "SELECT parent_id,goal_id,born_goal_ver,text,source,born_cycle "
            "FROM question WHERE id=?", (question_id,)).fetchone()
        expected_parent = (int(binding["parent_question_id"][1:])
                           if binding["parent_question_id"] is not None else None)
        if (qlineage is None
                or qlineage != (
                    expected_parent, authority["goal_id"], authority["goal_ver"],
                    binding["requested_text"], "human", authority_cycle_id)):
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
        if bindings:
            _binding_decision_id, binding_cycle_id, binding_directive_id, binding = bindings[0]
            completion = conn.execute(
                "SELECT cycle_id,question_id,actor,type,payload_json FROM decision "
                "WHERE id=?", (binding["request_decision_id"],)).fetchone()
            try:
                completion_payload = (
                    json.loads(completion[4]) if completion is not None else None)
            except json.JSONDecodeError as error:
                raise ImportAuthorityError(
                    f"q{question_id} import trigger completion payload 损坏") from error
            frozen_request = (completion_payload.get("reasoning_question_request")
                              if isinstance(completion_payload, dict) else None)
            if (binding["source_kind"] != "import_trigger_completed"
                    or binding["spawn_kind"] != "import_reference"
                    or binding["request_ref"]
                    != f"db:decision:{binding['request_decision_id']}"
                    or binding["source_authority_hash"] != authority["authority_hash"]
                    or binding_cycle_id != cycle_id
                    or binding_directive_id is not None
                    or completion is None
                    or completion[:4] != (
                        cycle_id, authority["origin_question_id"],
                        "orchestrator", "import_trigger_completed")
                    or not isinstance(frozen_request, dict)
                    or authority_hash(frozen_request)
                    != binding["reasoning_request_hash"]
                    or frozen_request.get("requested_text")
                    != binding["requested_text"]
                    or frozen_request.get("parent_question_id")
                    != binding["parent_question_id"]
                    or completion_payload.get("request_hash")
                    != authority["request_hash"]
                    or completion_payload.get("result_hash")
                    != authority["result_hash"]):
                raise ImportAuthorityError(
                    f"q{question_id} reference authority/request binding 不一致")
        found.append(authority)

    if len(found) > 1:
        raise ImportAuthorityError(
            f"q{question_id} 同时存在多个 import trigger authority")
    return found[0] if found else None
