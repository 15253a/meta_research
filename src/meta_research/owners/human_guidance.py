from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import text

from meta_research.human_guidance import (
    FrozenGuidanceBinding, FrozenGuidanceCut, FrozenGuidanceDelivery,
    GuidanceOperationIdentity, GuidanceRuntimeScope, StageGuidanceOperation,
    TargetGuidanceOperation, GUIDANCE_DOCUMENT_MAX_BYTES,
    normalize_guidance_scope,
)
from meta_research.owners.common import (
    OwnerConflict, canonical_hash, canonical_json, decoded_object, new_ref,
)
from meta_research.owners.human_collaboration_ladder import guidance_binding_from_row
from meta_research.quest_goal import ResearchRoot
from meta_research.runtime_conditions import read_runtime_conditions


class HumanGuidanceMixin:
    def bind_reasoning_completion_conditions(
        self, *, operation: StageGuidanceOperation, submission_ref: str,
        output: dict[str, object], payload_hash: str,
    ) -> None:
        if operation.scope.root_kind != "reasoning" or not submission_ref:
            raise OwnerConflict("reasoning_completion_conditions_invalid")
        cut = self.freeze_operation_guidance(operation)
        if cut.direction_cut is None:
            raise OwnerConflict("goal_direction_cut_missing")
        binding = {
            "scope": operation.scope.as_dict(),
            "guidance_binding": cut.binding.as_dict(),
            "output_hash": canonical_hash(output),
            "payload_hash": payload_hash,
            "conditions": cut.direction_cut["conditions"],
        }
        with self._database.fenced_write() as connection:
            self._agent_runtime.verify_stage_guidance_operation(operation)
            row = connection.execute(text(
                "SELECT * FROM hc_reasoning_completion_conditions "
                "WHERE submission_ref=:submission_ref"
            ), {"submission_ref": submission_ref}).first()
            if row is not None:
                if (decoded_object(row.binding_json) != binding
                        or row.binding_hash != canonical_hash(binding)):
                    raise OwnerConflict("reasoning_completion_conditions_conflict")
                return
            connection.execute(text(
                "INSERT INTO hc_reasoning_completion_conditions "
                "(submission_ref,binding_json,binding_hash) VALUES "
                "(:submission_ref,:binding_json,:binding_hash)"
            ), {"submission_ref": submission_ref,
                "binding_json": canonical_json(binding),
                "binding_hash": canonical_hash(binding)})

    def submit_human_guidance(
        self, *, quest_ref: str, original_text: str, strength: int = 3,
        idempotency_key: str, work_materials=None, proposal_ref: str | None = None,
        expected_proposal_hash: str | None = None,
    ) -> dict[str, object]:
        if proposal_ref is not None or expected_proposal_hash is not None:
            if proposal_ref is None or expected_proposal_hash is None:
                raise OwnerConflict("guidance_confirmation_required")
            proposal = self._collaboration_ladder._query_agent_proposal(proposal_ref)
            document = proposal["proposal"]
            if (document.get("text") != original_text
                    or document.get("strength") != strength
                    or document.get("work_materials") != work_materials):
                raise OwnerConflict("guidance_confirmation_stale")
            converted = self.convert_agent_proposal_to_soft_constraint(
                proposal_ref, expected_scope_ref="quest:" + quest_ref,
                expected_proposal_hash=expected_proposal_hash,
                idempotency_key=idempotency_key, strength=strength)
            return converted["soft_constraint"]
        commit = self._guidance_material_committer(quest_ref, work_materials, idempotency_key)
        return self._collaboration_ladder.submit_human_guidance(
            quest_ref=quest_ref, original_text=original_text, strength=strength,
            idempotency_key=idempotency_key, work_materials=work_materials, material_committer=commit,
        )

    def _guidance_material_committer(self, quest_ref, work_materials, idempotency_key):
        commit = None
        if work_materials is not None:
            from meta_research.work_materials import MaterialSubmission
            submission = MaterialSubmission.parse(work_materials)
            if submission.receiver.get("kind") != "current" or submission.receiver.get("quest_ref") != quest_ref:
                raise OwnerConflict("material_receiver_invalid")
            with self._database.read() as connection:
                replay = self._material_replay(connection, idempotency_key, work_materials)
            if replay is None:
                self._prepare_material_submission(work_materials)
            def commit(connection, constraint_ref):
                self._insert_material_submission(connection, command=work_materials, key=idempotency_key,
                    anchor_kind="guidance", anchor_ref=constraint_ref)
        return commit

    def freeze_operation_guidance(
        self, operation: StageGuidanceOperation | TargetGuidanceOperation,
    ) -> FrozenGuidanceCut:
        identity = operation.identity
        verifier = (
            self._agent_runtime.verify_stage_guidance_operation
            if isinstance(operation, StageGuidanceOperation)
            else self._agent_runtime.verify_target_guidance_preparation
        )
        prepared_work = verifier(operation)
        read_runtime_conditions(
            Path(self._database.path).parent, prepared_work.quest_ref
        )
        with self._database.fenced_write() as connection:
            work = verifier(operation)
            row = connection.execute(text(
                "SELECT * FROM hc_guidance_snapshots WHERE root_kind=:root_kind "
                "AND run_ref=:run_ref AND operation_ref=:operation_ref"
            ), vars(identity)).first()
            if row is not None:
                cut = _cut(row)
                if cut.binding.quest_ref != work.quest_ref:
                    raise OwnerConflict("guidance_snapshot_unbound")
                return cut
            author = ResearchRoot(
                kind=operation.scope.root_kind,
                run_ref=operation.scope.run_ref,
                attempt_ref=operation.scope.attempt_ref,
                root_session_ref=operation.scope.root_session_ref,
                fence_ref=operation.scope.fence_ref,
                runtime_binding_hash=operation.scope.runtime_binding_hash,
                operation_ref=identity.operation_ref,
            )
            direction_cut = self._research_graph.freeze_quest_direction(
                connection=connection,
                quest_ref=work.quest_ref,
                author=author,
            )
            rows = connection.execute(text(
                "SELECT * FROM hc_soft_constraints WHERE scope_ref=:scope_ref "
                "AND status='active' ORDER BY constraint_ref, revision"
            ), {"scope_ref": "quest:" + work.quest_ref}).all()
            deliveries = []
            for guide_row in rows:
                guide = guidance_binding_from_row(guide_row)
                scope_value = guide["guidance"].get("semantic_scope")
                applies = scope_value is None or guidance_applies_to_work(connection, scope_value, work)
                prior = connection.execute(text(
                    "SELECT feedback_json FROM hc_guidance_treatments WHERE "
                    "root_kind=:root_kind AND run_ref=:run_ref AND "
                    "constraint_ref=:constraint_ref AND revision=:revision "
                    "AND guidance_hash=:guidance_hash"
                ), {"root_kind": work.root_kind, "run_ref": work.run_ref,
                    "constraint_ref": guide_row.constraint_ref,
                    "revision": guide_row.revision, "guidance_hash": guide_row.guidance_hash,
                }).first()
                deliveries.append(FrozenGuidanceDelivery(
                    new_ref("guidance_delivery"), canonical_json(guide), applies and prior is None,
                    None if prior is None else prior.feedback_json,
                    applies, "legacy_unconfirmed" if scope_value is None else "confirmed",
                ))
            snapshot_ref = new_ref("guidance_snapshot")
            payload = {
                "identity": vars(identity), "quest_ref": work.quest_ref,
                "provenance_ref": work.provenance_ref,
                "operation": ({"job_ref": operation.job_ref,
                    "operation_name": operation.operation_name, "unit_ref": operation.unit_ref}
                    if isinstance(operation, StageGuidanceOperation)
                    else {"generation": operation.generation, "resume": operation.resume}),
                "deliveries": [item.as_dict() for item in deliveries],
                "direction_cut": direction_cut,
            }
            snapshot_hash = canonical_hash(payload)
            now = time.time()
            connection.execute(text(
                "INSERT INTO hc_guidance_snapshots VALUES "
                "(:snapshot_ref,:root_kind,:run_ref,:operation_ref,:quest_ref,"
                ":snapshot_json,:snapshot_hash,:now)"
            ), {**vars(identity), "snapshot_ref": snapshot_ref, "quest_ref": work.quest_ref,
                "snapshot_json": canonical_json(payload), "snapshot_hash": snapshot_hash,
                "now": now,
            })
            for delivery in deliveries:
                guide = decoded_object(delivery.guide_json)
                connection.execute(text(
                    "INSERT INTO hc_guidance_deliveries (delivery_ref,snapshot_ref,"
                    "constraint_ref,revision,guidance_hash,needs_treatment,created_at) "
                    "VALUES (:delivery_ref,:snapshot_ref,:constraint_ref,:revision,"
                    ":guidance_hash,:needs_treatment,:now)"
                ), {"delivery_ref": delivery.delivery_ref, "snapshot_ref": snapshot_ref,
                    "constraint_ref": guide["constraint_ref"], "revision": guide["revision"],
                    "guidance_hash": guide["guidance_hash"],
                    "needs_treatment": int(delivery.needs_treatment), "now": now,
                })
            self._feed.record(connection, "human_collaboration.guidance_prepared", {
                "snapshot_ref": snapshot_ref, "quest_ref": work.quest_ref,
                "run_ref": work.run_ref, "operation_ref": identity.operation_ref,
            })
            return FrozenGuidanceCut(FrozenGuidanceBinding(
                identity, work.quest_ref, snapshot_ref, snapshot_hash,
            ), work.provenance_ref, tuple(deliveries), direction_cut)

    def authorize_quest_goal_operation(
        self, *, scope: GuidanceRuntimeScope, binding: FrozenGuidanceBinding,
        reconcile: bool = False,
    ) -> FrozenGuidanceCut:
        cut = self._authorize_guidance(scope, binding, reconcile=reconcile)
        if cut.direction_cut is None:
            raise OwnerConflict("goal_direction_cut_missing")
        return cut

    def recover_operation_guidance(
        self, identity: GuidanceOperationIdentity,
    ) -> FrozenGuidanceCut:
        with self._database.read() as connection:
            row = connection.execute(text(
                "SELECT * FROM hc_guidance_snapshots WHERE root_kind=:root_kind "
                "AND run_ref=:run_ref AND operation_ref=:operation_ref"
            ), vars(identity)).first()
            if row is None:
                raise OwnerConflict("guidance_snapshot_missing")
            return _cut(row)

    def verify_frozen_guidance_binding(self, binding: FrozenGuidanceBinding) -> None:
        if not isinstance(binding, FrozenGuidanceBinding):
            raise OwnerConflict("guidance_snapshot_binding_invalid")
        cut = self.recover_operation_guidance(binding.identity)
        if cut.binding != binding:
            raise OwnerConflict("guidance_snapshot_binding_invalid")

    def read_operation_guidance(
        self, *, scope: GuidanceRuntimeScope, binding: FrozenGuidanceBinding,
        delivery_ref: str | None = None, effect_id: str | None = None,
        offset: int = 0, limit: int = GUIDANCE_DOCUMENT_MAX_BYTES,
        reconcile: bool = False,
    ) -> dict[str, object]:
        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= GUIDANCE_DOCUMENT_MAX_BYTES):
            raise OwnerConflict("guidance_read_bounds_invalid")
        with self._database.fenced_write() as connection:
            cut = self._authorize_guidance(scope, binding, reconcile=reconcile)
            if delivery_ref is None:
                if reconcile or effect_id is not None or offset != 0:
                    raise OwnerConflict("guidance_delivery_required")
                return {"binding": binding.as_dict(), "summary_only": True,
                    "deliveries": [_summary(item) for item in cut.deliveries]}
            delivery = _delivery(cut, delivery_ref)
            guide = decoded_object(delivery.guide_json)
            document = canonical_json(guide["guidance"]).encode("utf-8")
            if offset >= len(document) or document[offset] & 0xC0 == 0x80:
                raise OwnerConflict("guidance_read_bounds_invalid")
            command = {"delivery_ref": delivery_ref, "offset": offset, "limit": limit}
            key = _effect_key(binding, "read", effect_id)
            replay = _effect(connection, key, "read", command)
            if replay is not None:
                return replay
            if reconcile:
                raise OwnerConflict("guidance_effect_not_found")
            end = min(len(document), offset + limit)
            while end < len(document) and document[end] & 0xC0 == 0x80:
                end -= 1
            if end == offset:
                raise OwnerConflict("guidance_read_bounds_invalid")
            pages = connection.execute(text(
                "SELECT start_offset,end_offset FROM hc_guidance_read_pages "
                "WHERE delivery_ref=:delivery_ref ORDER BY start_offset"
            ), {"delivery_ref": delivery_ref}).all()
            covered = 0
            for start, stop in sorted([(r.start_offset, r.end_offset) for r in pages]
                    + [(offset, end)]):
                if start > covered:
                    break
                covered = max(covered, stop)
            full_read = covered == len(document)
            payload = {
                "binding": binding.as_dict(), "delivery_ref": delivery_ref,
                "guide_ref": {key: guide[key] for key in (
                    "constraint_ref", "revision", "guidance_hash", "receipt_ref", "receipt_hash")},
                "original_text": guide["guidance"].get("text") if offset == 0
                    and end == len(document) else None,
                "strength": guide["guidance"].get("strength", 3),
                "assistant_understanding": guide["guidance"].get("assistant_understanding"),
                "semantic_scope": guide["guidance"].get("semantic_scope"),
                "preserve_conditions": guide["guidance"].get("preserve_conditions", []),
                "scope_confirmation": delivery.scope_confirmation,
                "applies_to_work": delivery.applies_to_work,
                "background_only": not delivery.applies_to_work,
                "text": document[offset:end].decode("utf-8"), "offset": offset,
                "next_offset": None if end == len(document) else end,
                "complete": offset == 0 and end == len(document),
                "full_read": full_read, "document_length": len(document),
                "needs_treatment": delivery.needs_treatment,
                "prior_treatment": None if delivery.prior_treatment_json is None
                    else decoded_object(delivery.prior_treatment_json),
                "received": True,
            }
            receipt = _record_effect(connection, key, "read", command, payload)
            connection.execute(text(
                "INSERT INTO hc_guidance_read_pages VALUES (:key,:delivery_ref,:start,:end)"
            ), {"key": key, "delivery_ref": delivery_ref, "start": offset, "end": end})
            connection.execute(text(
                "UPDATE hc_guidance_deliveries SET received_at=COALESCE(received_at,:now),"
                "read_at=CASE WHEN :full_read THEN COALESCE(read_at,:now) ELSE read_at END "
                "WHERE delivery_ref=:delivery_ref"
            ), {"delivery_ref": delivery_ref, "now": time.time(), "full_read": full_read})
            self._feed.record(connection, "human_collaboration.guidance_read", {
                "delivery_ref": delivery_ref, "full_read": full_read,
                "quest_ref": binding.quest_ref,
            })
            return receipt

    def feedback_operation_guidance(
        self, *, scope: GuidanceRuntimeScope, binding: FrozenGuidanceBinding,
        delivery_ref: str, effect_id: str, understanding: str, changes: str,
        continuing_work: str, reasons: str, disposition: str,
        goal_impact: str | None = None,
        reconcile: bool = False,
    ) -> dict[str, object]:
        command = {"delivery_ref": delivery_ref, "understanding": understanding,
            "changes": changes, "continuing_work": continuing_work, "reasons": reasons,
            "disposition": disposition,
            **({} if goal_impact is None else {"goal_impact": goal_impact})}
        if (any(not isinstance(command[key], str) or not command[key].strip()
                    or len(command[key]) > 8192
                for key in ("understanding", "changes", "continuing_work", "reasons"))
                or disposition not in {"applied", "considered", "deferred", "goal_alignment_pending"}
                or goal_impact not in {None, "none", "requires_evolution", "undetermined"}
                or disposition == "goal_alignment_pending" and goal_impact == "none"):
            raise OwnerConflict("guidance_feedback_invalid")
        key = _effect_key(binding, "feedback", effect_id)
        with self._database.fenced_write() as connection:
            cut = self._authorize_guidance(scope, binding, reconcile=reconcile)
            delivery = _delivery(cut, delivery_ref)
            replay = _effect(connection, key, "feedback", command)
            if replay is not None:
                return replay
            if reconcile:
                raise OwnerConflict("guidance_effect_not_found")
            guide = decoded_object(delivery.guide_json)
            if not delivery.applies_to_work:
                raise OwnerConflict("guidance_outside_confirmed_scope")
            impact = goal_impact or ("requires_evolution" if disposition == "goal_alignment_pending" else "none")
            confirmed_scope = guide["guidance"].get("semantic_scope")
            if (impact == "requires_evolution" and confirmed_scope is not None
                    and confirmed_scope["kind"] != "quest"):
                raise OwnerConflict("guidance_goal_scope_confirmation_required")
            row = connection.execute(text(
                "SELECT read_at FROM hc_guidance_deliveries WHERE delivery_ref=:delivery_ref"
            ), {"delivery_ref": delivery_ref}).one()
            if row.read_at is None:
                raise OwnerConflict("guidance_exact_read_required")
            params = {"root_kind": binding.identity.root_kind,
                "run_ref": binding.identity.run_ref, "constraint_ref": guide["constraint_ref"],
                "revision": guide["revision"], "guidance_hash": guide["guidance_hash"]}
            prior = connection.execute(text(
                "SELECT feedback_json FROM hc_guidance_treatments WHERE root_kind=:root_kind "
                "AND run_ref=:run_ref AND constraint_ref=:constraint_ref AND revision=:revision "
                "AND guidance_hash=:guidance_hash"
            ), params).first()
            if prior is not None:
                raise OwnerConflict("guidance_already_treated")
            payload = {**command, "binding": binding.as_dict(), "guide_ref": {
                key: guide[key] for key in ("constraint_ref", "revision", "guidance_hash",
                    "receipt_ref", "receipt_hash")}, "declared_by_root": True,
                "goal_impact": impact, "goal_update_pending": impact != "none",
                "semantic_scope": confirmed_scope,
            }
            receipt = _record_effect(connection, key, "feedback", command, payload)
            connection.execute(text(
                "INSERT INTO hc_guidance_treatments VALUES (:root_kind,:run_ref,:constraint_ref,"
                ":revision,:guidance_hash,:delivery_ref,:effect_key,:feedback_json,:now)"
            ), {**params, "delivery_ref": delivery_ref, "effect_key": key,
                "feedback_json": canonical_json(receipt), "now": time.time()})
            self._feed.record(connection, "human_collaboration.guidance_treated", {
                "delivery_ref": delivery_ref, "quest_ref": binding.quest_ref,
                "disposition": disposition,
            })
            return receipt

    def _authorize_guidance(self, scope, binding, *, reconcile):
        if binding is None:
            raise OwnerConflict("guidance_snapshot_missing")
        self.verify_frozen_guidance_binding(binding)
        self._agent_runtime.verify_guidance_read_scope(scope, binding, reconcile=reconcile)
        return self.recover_operation_guidance(binding.identity)

    def query_guidance_deliveries(self, constraint_ref: str) -> list[dict[str, object]]:
        with self._database.read() as connection:
            rows = connection.execute(text(
                "SELECT d.*,s.root_kind,s.run_ref,s.operation_ref,s.quest_ref,"
                "s.snapshot_json,s.snapshot_hash,t.feedback_json FROM "
                "hc_guidance_deliveries d JOIN hc_guidance_snapshots s USING(snapshot_ref) "
                "LEFT JOIN hc_guidance_treatments t ON t.root_kind=s.root_kind "
                "AND t.run_ref=s.run_ref AND t.constraint_ref=d.constraint_ref "
                "AND t.revision=d.revision AND t.guidance_hash=d.guidance_hash "
                "WHERE d.constraint_ref=:constraint_ref ORDER BY d.created_at,d.delivery_ref"
            ), {"constraint_ref": constraint_ref}).all()
        result = []
        for row in rows:
            delivery = _delivery(_cut(row), row.delivery_ref)
            result.append({"delivery_ref": row.delivery_ref, "root_kind": row.root_kind,
                "run_ref": row.run_ref, "operation_ref": row.operation_ref,
                "prepared_at": row.created_at, "received_at": row.received_at,
                "read_at": row.read_at, "needs_treatment": bool(row.needs_treatment),
                "applies_to_work": delivery.applies_to_work,
                "background_only": not delivery.applies_to_work,
                "scope_confirmation": delivery.scope_confirmation,
                "treatment": None if row.feedback_json is None else decoded_object(row.feedback_json),
            })
        return result


def validate_guidance_scope(connection, quest_ref: str, scope: object) -> dict[str, object]:
    value = normalize_guidance_scope(scope, quest_ref=quest_ref)
    if connection.execute(text("SELECT quest_ref FROM rg_quests WHERE quest_ref=:quest_ref"),
            value).first() is None:
        raise OwnerConflict("guidance_scope_unbound")
    if value["kind"] != "quest":
        question = connection.execute(text(
            "SELECT question_ref FROM rg_questions WHERE quest_ref=:quest_ref AND question_ref=:question_ref "
            "UNION ALL SELECT question_ref FROM rg_manual_questions WHERE quest_ref=:quest_ref AND question_ref=:question_ref "
            "UNION ALL SELECT question_ref FROM rg_autonomous_questions WHERE quest_ref=:quest_ref AND question_ref=:question_ref"
        ), value).first()
        if question is None:
            raise OwnerConflict("guidance_scope_unbound")
    if value["kind"] in {"cycle", "target"}:
        cycle = connection.execute(text(
            "SELECT cycle_ref FROM ae_cycles WHERE quest_ref=:quest_ref "
            "AND question_ref=:question_ref AND cycle_ref=:cycle_ref"
        ), value).first()
        if cycle is None:
            raise OwnerConflict("guidance_scope_unbound")
    if value["kind"] == "target":
        target = connection.execute(text(
            "SELECT t.target_ref FROM rg_targets t JOIN rg_target_graphs g USING(graph_ref) "
            "WHERE t.target_ref=:target_ref AND g.quest_ref=:quest_ref AND g.cycle_ref=:cycle_ref"
        ), value).first()
        if target is None:
            raise OwnerConflict("guidance_scope_unbound")
    return value


def guidance_applies_to_work(connection, scope, work) -> bool:
    value = normalize_guidance_scope(scope, quest_ref=work.quest_ref)
    if value["kind"] == "quest":
        return True
    if work.root_kind == "target":
        row = connection.execute(text(
            "SELECT g.cycle_ref,q.question_ref,t.target_ref FROM rg_targets t "
            "JOIN rg_target_graphs g USING(graph_ref) JOIN ae_stage_run_requests q "
            "ON q.request_ref=g.request_ref WHERE t.target_ref=:ref AND g.quest_ref=:quest_ref"
        ), {"ref": work.provenance_ref, "quest_ref": work.quest_ref}).first()
    else:
        row = connection.execute(text(
            "SELECT cycle_ref,question_ref,NULL AS target_ref FROM ae_stage_run_requests "
            "WHERE request_ref=:ref AND quest_ref=:quest_ref"
        ), {"ref": work.provenance_ref, "quest_ref": work.quest_ref}).first()
    if row is None:
        raise OwnerConflict("guidance_scope_work_unbound")
    return (value["question_ref"] == row.question_ref
        and (value["kind"] == "question" or value["cycle_ref"] == row.cycle_ref)
        and (value["kind"] != "target" or value["target_ref"] == row.target_ref))


def _cut(row) -> FrozenGuidanceCut:
    payload = decoded_object(row.snapshot_json)
    identity = GuidanceOperationIdentity(row.root_kind, row.run_ref, row.operation_ref)
    if (canonical_hash(payload) != row.snapshot_hash
            or payload["identity"] != vars(identity) or payload["quest_ref"] != row.quest_ref):
        raise OwnerConflict("guidance_snapshot_invalid")
    direction_cut = payload.get("direction_cut")
    if direction_cut is not None and not isinstance(direction_cut, dict):
        raise OwnerConflict("guidance_snapshot_invalid")
    return FrozenGuidanceCut(FrozenGuidanceBinding(
        identity, row.quest_ref, row.snapshot_ref, row.snapshot_hash,
    ), payload["provenance_ref"], tuple(FrozenGuidanceDelivery(
        item["delivery_ref"], canonical_json(item["guide"]), item["needs_treatment"],
        None if item["prior_treatment"] is None else canonical_json(item["prior_treatment"]),
        item.get("applies_to_work", True), item.get("scope_confirmation", "legacy_unconfirmed"),
    ) for item in payload["deliveries"]), direction_cut)


def _delivery(cut, delivery_ref):
    for delivery in cut.deliveries:
        if delivery.delivery_ref == delivery_ref:
            return delivery
    raise OwnerConflict("guidance_delivery_unbound")


def _summary(delivery):
    guide = decoded_object(delivery.guide_json)
    return {"delivery_ref": delivery.delivery_ref, "constraint_ref": guide["constraint_ref"],
        "revision": guide["revision"], "guidance_hash": guide["guidance_hash"],
        "strength": guide["guidance"].get("strength", 3),
        "semantic_scope": guide["guidance"].get("semantic_scope"),
        "scope_confirmation": delivery.scope_confirmation,
        "applies_to_work": delivery.applies_to_work,
        "background_only": not delivery.applies_to_work,
        "needs_treatment": delivery.needs_treatment,
        "reader": {"operation": "human_guidance.read", "delivery_ref": delivery.delivery_ref},
        "prior_treatment": None if delivery.prior_treatment_json is None
            else decoded_object(delivery.prior_treatment_json)}


def _effect_key(binding, kind, effect_id):
    if not isinstance(effect_id, str) or not effect_id or len(effect_id) > 128:
        raise OwnerConflict("guidance_effect_id_invalid")
    return "guidance_effect_" + canonical_hash({"identity": vars(binding.identity),
        "snapshot_ref": binding.snapshot_ref, "kind": kind, "effect_id": effect_id})


def _effect(connection, key, kind, command):
    row = connection.execute(text(
        "SELECT * FROM hc_guidance_effects WHERE effect_key=:key"
    ), {"key": key}).first()
    if row is None:
        return None
    receipt = decoded_object(row.receipt_json)
    if row.kind != kind or row.command_hash != canonical_hash(command):
        raise OwnerConflict("guidance_effect_conflict")
    if canonical_hash(receipt) != row.receipt_hash:
        raise OwnerConflict("guidance_receipt_invalid")
    return receipt


def _record_effect(connection, key, kind, command, payload):
    receipt = {**payload, "receipt": {"issuer": "human_collaboration",
        "kind": "guidance_" + kind, "receipt_ref": new_ref("hc_receipt"),
        "payload_hash": canonical_hash(payload)}}
    connection.execute(text(
        "INSERT INTO hc_guidance_effects VALUES (:key,:kind,:delivery_ref,:command_hash,"
        ":receipt_json,:receipt_hash,:now)"
    ), {"key": key, "kind": kind, "delivery_ref": command["delivery_ref"],
        "command_hash": canonical_hash(command), "receipt_json": canonical_json(receipt),
        "receipt_hash": canonical_hash(receipt), "now": time.time()})
    return receipt
