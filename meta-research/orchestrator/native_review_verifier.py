"""Read-only durable verifier for native reviewer chains.

The runtime MCP writer owns review creation.  This module owns replay-time
verification so ContextPack compilation, pool admission, and exact reuse all
apply the same trust boundary: an agent-authored receipt is authority only
when its durable request, child input, child terminal result, and revised
subject can all be replayed.  A running owner uses its orchestrator-persisted
parsed JSONL prefix; a successful owner must instead use provider accounting
and the terminal guardian capture.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
import sqlite3
from typing import Any, Dict, Mapping, Optional

from .artifact_capability import (
    ArtifactCapabilityError,
    read_artifact_bytes,
)
from .native_review import (
    NativeReviewError,
    replay_native_review_execution,
    replay_native_review_live_snapshot,
)
from .provider_invocation import (
    ProviderInvocationError,
    load_provider_invocation_receipt,
)
from .runtime_mcp import RuntimeIngestService, RuntimeMCPError


@dataclass(frozen=True)
class ValidatedNativeReviewChain:
    """One exact-N chain rooted by round 1 with no prior receipt."""

    entries: tuple[tuple[int, Dict[str, Any]], ...]

    @property
    def terminal(self) -> tuple[int, Dict[str, Any]]:
        return self.entries[-1]


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _json_object(raw: Any, *, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(
            raw, parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {token}")))
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} JSON damaged") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_owner_input(
        request: Mapping[str, Any], decision_id: int) -> None:
    """Rebind round and subject identity to the bytes delivered to the child."""
    try:
        manifest_bytes = read_artifact_bytes(
            Path(request["candidate_manifest_ref"]),
            expected_hash=request["candidate_manifest_hash"],
            max_bytes=384 * 1024,
            label=f"native review request d{decision_id} candidate")
        brief_bytes = read_artifact_bytes(
            Path(request["reviewer_brief_ref"]),
            expected_hash=request["reviewer_brief_hash"],
            max_bytes=384 * 1024,
            label=f"native review request d{decision_id} brief")
        manifest = _json_object(
            manifest_bytes,
            label=f"native review request d{decision_id} manifest")
        brief = _json_object(
            brief_bytes,
            label=f"native review request d{decision_id} brief")
    except (KeyError, OSError, ValueError,
            ArtifactCapabilityError) as error:
        raise ValueError(
            f"native review request d{decision_id} owner input "
            f"cannot be replayed: {error}") from error
    identity = {
        "protocol": "native-review-brief-v1",
        "review_request_id": request["review_request_id"],
        "cycle_id": request["cycle_id"],
        "stage": request["stage"],
        "target_id": request["target_id"],
        "purpose": request["purpose"],
        "review_kind": request["review_kind"],
        "round_no": request["round_no"],
        "configured_rounds": request["configured_rounds"],
        "reviewed_subject_hash": request["reviewed_subject_hash"],
    }
    if (
            any(brief.get(key) != value for key, value in identity.items())
            or manifest.get("protocol") != "native-review-candidate-v1"
            or manifest.get("artifact_hash")
            != request["reviewed_subject_hash"]
            or brief.get("candidate_manifest") != manifest
            or brief.get("review_focus")
            != RuntimeIngestService._native_review_focus(
                request["review_kind"])
            or brief.get("required_result_protocol")
            != "native-review-result-v1"
            or not isinstance(brief.get("required_result_fields"), dict)):
        raise ValueError(
            f"native review request d{decision_id} reviewer brief "
            "does not match its round/subject owner input")


def _validate_revised_subject(
        payload: Mapping[str, Any], decision_id: int) -> None:
    try:
        raw = read_artifact_bytes(
            Path(payload["revised_candidate_manifest_ref"]),
            expected_hash=payload["revised_candidate_manifest_hash"],
            max_bytes=384 * 1024,
            label=f"native review d{decision_id} revised candidate")
        manifest = _json_object(
            raw, label=f"native review d{decision_id} revised candidate")
    except (KeyError, OSError, ValueError,
            ArtifactCapabilityError) as error:
        raise ValueError(
            f"native review d{decision_id} revised candidate "
            f"cannot be replayed: {error}") from error
    if (
            manifest.get("protocol") != "native-review-candidate-v1"
            or manifest.get("artifact_hash")
            != payload["resulting_subject_hash"]):
        raise ValueError(
            f"native review d{decision_id} resulting subject "
            "does not match the revised candidate")


def validate_native_review_chains(
        conn: sqlite3.Connection, *, cycle_id: int,
        ) -> list[ValidatedNativeReviewChain]:
    """Replay every native review in one cycle and return exact-N chains.

    A Bundle target may have multiple independent code/result chains after a
    repair.  Roots are therefore identified by ``round_no=1`` and
    ``prior_receipt_hash=null``; receipt lineage, not target/review kind alone,
    assigns later rounds to a root.
    """
    durable_replays: Dict[int, Any] = {}
    live_replays: Dict[int, Any] = {}

    def durable_replay(payload: Mapping[str, Any]):  # noqa: ANN202
        runner_call_id = payload["runner_call_id"]
        cached = durable_replays.get(runner_call_id)
        if cached is not None:
            return cached
        rows = conn.execute(
            "SELECT id,payload_json FROM decision "
            "WHERE cycle_id=? AND actor='orchestrator' "
            "AND type='provider_invocation_accounted' "
            "AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.runner_call_id')=? "
            "ORDER BY id",
            (cycle_id, runner_call_id)).fetchall()
        if len(rows) != 1:
            raise ValueError(
                f"native review runner_call {runner_call_id} lacks one "
                "durable provider/guardian accounting receipt")
        accounting = _json_object(
            rows[0][1], label=f"decision d{rows[0][0]}")
        if (
                accounting.get("protocol") != "provider-accounting-v1"
                or accounting.get("runner_call_id") != runner_call_id
                or accounting.get("runner_terminal_status") != "success"):
            raise ValueError(
                f"native review runner_call {runner_call_id} provider "
                "accounting identity is invalid")
        try:
            invocation = load_provider_invocation_receipt(
                Path(accounting["provider_receipt_ref"]),
                expected_runner_call_id=runner_call_id,
                expected_cycle_id=payload["cycle_id"],
                expected_phase=payload["stage"],
                expected_purpose=payload["purpose"],
                expected_execution_receipt_ref=accounting[
                    "execution_receipt_ref"])
        except (KeyError, OSError, ValueError,
                ProviderInvocationError) as error:
            raise ValueError(
                f"native review runner_call {runner_call_id} durable "
                f"provider receipt cannot be replayed: {error}") from error
        if (
                accounting.get("provider_receipt_ref")
                != invocation.receipt_ref
                or accounting.get("provider_receipt_sha256")
                != invocation.receipt_sha256
                or accounting.get("execution_receipt_ref")
                != invocation.execution_receipt_ref
                or accounting.get("execution_receipt_sha256")
                != invocation.execution_receipt_sha256
                or accounting.get("execution_operation_id")
                != invocation.execution_operation_id):
            raise ValueError(
                f"native review runner_call {runner_call_id} provider "
                "accounting conflicts with the guardian receipt")
        if (
                invocation.provider_invocation_id_kind != "thread_id"
                or invocation.provider_invocation_id
                != payload["parent_thread_id"]):
            raise ValueError(
                f"native review runner_call {runner_call_id} provider "
                "parent session conflicts with child events")
        try:
            replay = replay_native_review_execution(
                Path(invocation.execution_receipt_ref),
                expected_runner_call_id=runner_call_id,
                expected_cycle_id=payload["cycle_id"],
                expected_stage=payload["stage"],
                expected_purpose=payload["purpose"])
        except (OSError, ValueError, NativeReviewError) as error:
            raise ValueError(
                f"native review runner_call {runner_call_id} guardian "
                f"child events cannot be replayed: {error}") from error
        durable_replays[runner_call_id] = replay
        return replay

    def live_replay(
            payload: Mapping[str, Any], review_decision_id: int):  # noqa: ANN202
        cached = live_replays.get(review_decision_id)
        if cached is not None:
            return cached
        rows = conn.execute(
            "SELECT id,payload_json FROM decision "
            "WHERE cycle_id=? AND actor='orchestrator' "
            "AND type='native_review_live_owner_proof' "
            "AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.review_decision_id')=? "
            "ORDER BY id",
            (cycle_id, review_decision_id)).fetchall()
        if len(rows) != 1:
            raise ValueError(
                f"native review d{review_decision_id} lacks one live "
                "owner proof")
        proof_decision_id, raw = rows[0]
        proof = _json_object(
            raw, label=f"decision d{proof_decision_id}")
        required = {
            "protocol", "review_decision_id", "review_receipt_hash",
            "review_request_id", "cycle_id", "stage", "target_id",
            "purpose", "review_kind", "runner_call_id",
            "parent_thread_id", "parent_turn_id", "child_call_id",
            "child_thread_id", "child_turn_id", "snapshot_ref",
            "snapshot_sha256", "snapshot_bytes", "spawn_proof_mode",
        }
        if set(proof) != required:
            raise ValueError(
                f"native review d{review_decision_id} live owner proof "
                "field closure is invalid")
        expected = {
            "protocol": "native-review-live-owner-proof-v1",
            "review_decision_id": review_decision_id,
            "review_receipt_hash": payload["receipt_hash"],
            "review_request_id": payload["review_request_id"],
            "cycle_id": payload["cycle_id"],
            "stage": payload["stage"],
            "target_id": payload["target_id"],
            "purpose": payload["purpose"],
            "review_kind": payload["review_kind"],
            "runner_call_id": payload["runner_call_id"],
            "parent_thread_id": payload["parent_thread_id"],
            "parent_turn_id": payload["parent_turn_id"],
            "child_call_id": payload["child_call_id"],
            "child_thread_id": payload["child_thread_id"],
            "child_turn_id": payload["child_turn_id"],
        }
        if (
                proof_decision_id <= review_decision_id
                or any(proof.get(key) != value
                       for key, value in expected.items())
                or not isinstance(proof.get("snapshot_ref"), str)
                or not Path(proof["snapshot_ref"]).is_absolute()
                or _SHA256.fullmatch(
                    str(proof.get("snapshot_sha256") or "")) is None
                or isinstance(proof.get("snapshot_bytes"), bool)
                or not isinstance(proof.get("snapshot_bytes"), int)
                or proof["snapshot_bytes"] <= 0):
            raise ValueError(
                f"native review d{review_decision_id} live owner proof "
                "identity is invalid")
        try:
            replay = replay_native_review_live_snapshot(
                Path(proof["snapshot_ref"]),
                expected_snapshot_hash=proof["snapshot_sha256"],
                expected_snapshot_bytes=proof["snapshot_bytes"],
                expected_runner_call_id=payload["runner_call_id"],
                expected_cycle_id=payload["cycle_id"],
                expected_stage=payload["stage"],
                expected_purpose=payload["purpose"],
                expected_parent_thread_id=payload["parent_thread_id"],
                expected_parent_turn_id=payload["parent_turn_id"],
                expected_spawn_proof_mode=proof["spawn_proof_mode"])
        except (OSError, ValueError, NativeReviewError) as error:
            raise ValueError(
                f"native review d{review_decision_id} live owner snapshot "
                f"cannot be replayed: {error}") from error
        live_replays[review_decision_id] = replay
        return replay

    request_rows = conn.execute(
        "SELECT id,payload_json FROM decision "
        "WHERE cycle_id=? AND actor='agent' "
        "AND type='runtime_review_request' ORDER BY id",
        (cycle_id,)).fetchall()
    requests: Dict[str, tuple[int, Dict[str, Any]]] = {}
    for decision_id, raw in request_rows:
        request = _json_object(raw, label=f"decision d{decision_id}")
        try:
            RuntimeIngestService._validate_review_request(request)
        except RuntimeMCPError as error:
            raise ValueError(
                f"native review request d{decision_id} cannot be "
                f"replayed: {error}") from error
        request_id = request["review_request_id"]
        if request_id in requests:
            raise ValueError(
                f"duplicate native review request_id: {request_id}")
        requests[request_id] = (int(decision_id), request)

    rows = conn.execute(
        "SELECT id,question_id,payload_json FROM decision "
        "WHERE cycle_id=? AND actor='agent' AND type='runtime_review' "
        "ORDER BY id", (cycle_id,)).fetchall()
    consumed_requests = set()
    entries: list[tuple[int, Dict[str, Any]]] = []
    for decision_id, decision_question, raw in rows:
        payload = _json_object(raw, label=f"decision d{decision_id}")
        try:
            RuntimeIngestService._validate_native_receipt(payload)
        except RuntimeMCPError as error:
            raise ValueError(
                f"native review d{decision_id} cannot be replayed: "
                f"{error}") from error
        if payload["cycle_id"] != f"c{cycle_id}":
            raise ValueError(
                f"native review d{decision_id} cycle identity mismatch")
        request_match = requests.get(payload["review_request_id"])
        if request_match is None:
            raise ValueError(
                f"native review d{decision_id} lacks one durable request")
        if payload["review_request_id"] in consumed_requests:
            raise ValueError(
                "native review request consumed more than once: "
                f"{payload['review_request_id']}")
        request_decision_id, request = request_match
        _validate_owner_input(request, request_decision_id)
        receipt_request_fields = {
            "cycle_id": "cycle_id",
            "stage": "stage",
            "target_id": "target_id",
            "purpose": "purpose",
            "review_kind": "review_kind",
            "round_no": "round_no",
            "configured_rounds": "configured_rounds",
            "reviewed_subject_hash": "reviewed_subject_hash",
            "prior_receipt_hash": "prior_receipt_hash",
            "runner_call_id": "runner_call_id",
            "parent_thread_id": "parent_thread_id",
            "parent_turn_id": "parent_turn_id",
            "review_input_brief_hash": "reviewer_brief_hash",
            "review_input_candidate_manifest_hash":
                "candidate_manifest_hash",
        }
        if any(
                payload[receipt_field] != request[request_field]
                for receipt_field, request_field
                in receipt_request_fields.items()):
            raise ValueError(
                f"native review d{decision_id} conflicts with its "
                "durable request identity")
        consumed_requests.add(payload["review_request_id"])
        runner = conn.execute(
            "SELECT cycle_id,phase,purpose,status FROM runner_call "
            "WHERE id=?", (payload["runner_call_id"],)).fetchone()
        if (
                runner is None
                or tuple(runner[:3]) != (
                    cycle_id, payload["stage"], payload["purpose"])):
            raise ValueError(
                f"native review d{decision_id} parent runner ownership "
                "mismatch")
        runner_status = str(runner[3])
        if runner_status == "running":
            execution = live_replay(payload, int(decision_id))
        elif runner_status == "success":
            execution = durable_replay(payload)
        elif runner_status in {"failed", "aborted"}:
            # A later Bundle repair/retry may leave a structurally valid
            # review receipt owned by an invocation that ultimately failed.
            # It is non-authoritative, but it must not prevent replay of a
            # later successful review chain in the same cycle.
            continue
        else:
            raise ValueError(
                f"native review d{decision_id} parent runner is "
                f"{runner_status!r}; only running live proof or successful "
                "guardian replay is authoritative")
        try:
            RuntimeIngestService._durable_native_review_child_proof(
                payload, execution)
        except RuntimeMCPError as error:
            raise ValueError(
                f"native review d{decision_id} durable child event proof "
                f"cannot be replayed: {error}") from error
        _validate_revised_subject(payload, int(decision_id))
        target_id = payload["target_id"]
        if target_id is not None:
            try:
                target_number = int(target_id)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"native review d{decision_id} target_id invalid") from error
            target = conn.execute(
                "SELECT cycle_id,question_id FROM build_target WHERE id=?",
                (target_number,)).fetchone()
            if (
                    target is None
                    or target[0] != cycle_id
                    or (
                        decision_question is not None
                        and decision_question != target[1])):
                raise ValueError(
                    f"native review d{decision_id} target ownership mismatch")
        entries.append((int(decision_id), payload))

    scoped: Dict[tuple, list[tuple[int, Dict[str, Any]]]] = {}
    for decision_id, payload in entries:
        scope = (
            payload["stage"], payload["target_id"], payload["purpose"],
            payload["review_kind"], payload["runner_call_id"],
            payload["parent_thread_id"], payload["parent_turn_id"],
            payload["configured_rounds"],
        )
        scoped.setdefault(scope, []).append((decision_id, payload))

    chains: list[ValidatedNativeReviewChain] = []
    for scope in sorted(
            scoped,
            key=lambda item: tuple(
                "" if value is None else str(value) for value in item)):
        scoped_entries = scoped[scope]
        roots = sorted(
            (
                item for item in scoped_entries
                if item[1]["round_no"] == 1
                and item[1]["prior_receipt_hash"] is None
            ),
            key=lambda item: item[0])
        if not roots:
            raise ValueError(
                f"native review chain lacks a round-1 root: {scope}")
        by_hash: Dict[str, tuple[int, Dict[str, Any]]] = {}
        by_prior: Dict[str, list[tuple[int, Dict[str, Any]]]] = {}
        for item in scoped_entries:
            decision_id, payload = item
            receipt_hash = payload["receipt_hash"]
            if receipt_hash in by_hash:
                raise ValueError(
                    f"duplicate native review receipt hash: {receipt_hash}")
            by_hash[receipt_hash] = item
            prior = payload["prior_receipt_hash"]
            if payload["round_no"] == 1:
                if prior is not None:
                    raise ValueError(
                        f"native review round 1 has a prior receipt: "
                        f"d{decision_id}")
            elif prior is None:
                raise ValueError(
                    f"native review non-root lacks prior receipt: "
                    f"d{decision_id}")
            else:
                by_prior.setdefault(prior, []).append(item)

        consumed_decisions = set()
        for root in roots:
            configured = root[1]["configured_rounds"]
            current = root
            chain_entries = []
            for expected_round in range(1, configured + 1):
                decision_id, payload = current
                if (
                        decision_id in consumed_decisions
                        or payload["round_no"] != expected_round):
                    raise ValueError(
                        f"native review chain missing/duplicate round: "
                        f"{scope}")
                chain_entries.append(current)
                consumed_decisions.add(decision_id)
                children = by_prior.get(payload["receipt_hash"], [])
                if expected_round == configured:
                    if children:
                        raise ValueError(
                            f"native review chain exceeds configured rounds: "
                            f"{scope}")
                    continue
                matching = [
                    item for item in children
                    if (
                        item[1]["round_no"] == expected_round + 1
                        and item[1]["reviewed_subject_hash"]
                        == payload["resulting_subject_hash"])
                ]
                if len(matching) != 1:
                    raise ValueError(
                        f"native review chain missing/duplicate/conflicting "
                        f"round: {scope}")
                current = matching[0]
            chains.append(ValidatedNativeReviewChain(tuple(chain_entries)))
        if consumed_decisions != {
                decision_id for decision_id, _payload in scoped_entries}:
            raise ValueError(
                f"native review chain contains orphan/conflicting receipts: "
                f"{scope}")

    chains.sort(key=lambda chain: chain.terminal[0])
    return chains


def validate_native_reviews(
        conn: sqlite3.Connection, *, cycle_id: int,
        ) -> list[tuple[int, Dict[str, Any]]]:
    """Return all durable, exact-chain receipts ordered by decision id."""
    accepted = [
        entry
        for chain in validate_native_review_chains(conn, cycle_id=cycle_id)
        for entry in chain.entries
    ]
    accepted.sort(key=lambda item: item[0])
    return accepted


def select_authoritative_native_review(
        conn: sqlite3.Connection, *, cycle_id: int, stage: str,
        target_id: Optional[str], review_kind: str,
        resulting_subject_hash: Optional[str] = None,
        decision_id: Optional[int] = None,
        ) -> Optional[tuple[int, Dict[str, Any]]]:
    """Select the latest terminal chain matching a subject and decision."""
    candidates = []
    for chain in validate_native_review_chains(conn, cycle_id=cycle_id):
        terminal_id, payload = chain.terminal
        if (
                payload["stage"] != stage
                or payload["target_id"] != target_id
                or payload["review_kind"] != review_kind
                or (
                    resulting_subject_hash is not None
                    and payload["resulting_subject_hash"]
                    != resulting_subject_hash)):
            continue
        candidates.append((terminal_id, payload))
    if not candidates:
        return None
    selected = max(candidates, key=lambda item: item[0])
    if decision_id is not None and selected[0] != decision_id:
        return None
    return selected


def native_review_receipt_valid(
        conn: sqlite3.Connection, *, cycle_id: int, target_id: int,
        receipt: Any, expected_review_kind: str = "bundle_code",
        require_stage_submission: bool = True) -> bool:
    """Validate one scientific native-review reference against durable truth."""
    required = {
        "protocol", "decision_id", "review_kind", "review_scope",
        "subject_hash", "receipt_hash",
    }
    if (
            not isinstance(receipt, Mapping)
            or set(receipt) != required
            or receipt.get("protocol") != "native-review-receipt-v1"
            or receipt.get("review_kind") != expected_review_kind
            or receipt.get("review_scope") != "code_plan_data_boundary"):
        return False
    decision_id = receipt.get("decision_id")
    if (
            isinstance(decision_id, bool)
            or not isinstance(decision_id, int)
            or decision_id <= 0):
        return False
    subject_hash = receipt.get("subject_hash")
    receipt_hash = receipt.get("receipt_hash")
    if (
            not isinstance(subject_hash, str)
            or not isinstance(receipt_hash, str)):
        return False
    try:
        selected = select_authoritative_native_review(
            conn, cycle_id=cycle_id, stage="bundle",
            target_id=str(target_id), review_kind=expected_review_kind,
            resulting_subject_hash="sha256:" + subject_hash,
            decision_id=decision_id)
    except (KeyError, OSError, TypeError, ValueError):
        return False
    if selected is None:
        return False
    _selected_id, payload = selected
    if receipt_hash != payload["receipt_hash"].removeprefix("sha256:"):
        return False
    if not require_stage_submission:
        return True
    rows = conn.execute(
        "SELECT payload_json FROM decision WHERE cycle_id=? "
        "AND actor='agent' AND type='runtime_stage_submission' "
        "AND json_valid(payload_json) "
        "AND json_extract(payload_json,'$.stage')='bundle' "
        "AND CAST(json_extract(payload_json,'$.target_id') AS TEXT)=? "
        "AND json_extract(payload_json,'$.review_decision_id')=?",
        (cycle_id, str(target_id), decision_id)).fetchall()
    if len(rows) != 1:
        return False
    try:
        submission = _json_object(
            rows[0][0],
            label=f"native review d{decision_id} stage submission")
    except ValueError:
        return False
    return submission.get("artifact_hash") == "sha256:" + subject_hash
