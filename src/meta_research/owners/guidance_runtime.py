from __future__ import annotations

import re

from sqlalchemy import text

from meta_research.human_guidance import (
    FrozenGuidanceBinding, GuidanceRuntimeScope, StageGuidanceOperation,
    TargetGuidanceOperation, VerifiedGuidanceWork,
)
from meta_research.owners.common import OwnerConflict, decoded_object
from meta_research.provider_supervisor import provider_operation_ref


class GuidanceRuntimeMixin:
    def verify_stage_guidance_operation(self, operation: StageGuidanceOperation) -> VerifiedGuidanceWork:
        scope = operation.scope
        name = operation.operation_name
        if (scope.root_kind not in {"idea", "plan", "bundle", "reasoning"}
                or not isinstance(operation.job_ref, str) or not operation.job_ref
                or not isinstance(name, str)
                or not (name in {"primary", "review"}
                    or scope.root_kind == "bundle" and re.fullmatch(r"(?:dispatch|target-batch)-[1-9][0-9]*", name)
                    or scope.root_kind == "reasoning" and name == "autonomous-resume")):
            raise OwnerConflict("guidance_operation_invalid")
        verified = self.verify_root_agent_runtime_scope(**scope.as_dict())
        with self._database.read() as connection:
            units = connection.execute(text(
                "SELECT * FROM ar_provider_units WHERE run_ref=:run_ref "
                "AND attempt_ref=:attempt_ref AND fence_ref=:fence_ref "
                "AND operation_ref=:job_ref AND status='active' "
                "AND (:unit_ref IS NULL OR unit_ref=:unit_ref)"
            ), {**scope.as_dict(), "job_ref": operation.job_ref, "unit_ref": operation.unit_ref}).all()
            run = connection.execute(text(
                "SELECT request_ref FROM ar_stage_runs WHERE run_ref=:run_ref AND stage=:root_kind"
            ), scope.as_dict()).first()
        unit_kind = scope.root_kind + ("_primary" if name == "primary" else "_review")
        if len(units) != 1 or units[0].unit_kind != unit_kind or run is None:
            raise OwnerConflict("guidance_provider_unit_unbound")
        if scope.root_kind == "bundle" and name not in {"primary", "review"}:
            from meta_research.owners.common import canonical_hash
            expected = "provider_unit_" + canonical_hash({
                "operation_ref": operation.job_ref, "operation_name": name,
                "attempt_ref": scope.attempt_ref,
            })[:64]
            if units[0].unit_ref != expected:
                raise OwnerConflict("guidance_provider_unit_unbound")
        if name == "autonomous-resume":
            with self._database.read() as connection:
                checkpoint = connection.execute(text(
                    "SELECT reasoning_checkpoint_ref FROM ar_stage_attempts WHERE attempt_ref=:attempt_ref"
                ), {"attempt_ref": scope.attempt_ref}).scalar_one()
            if checkpoint is None:
                raise OwnerConflict("guidance_provider_unit_unbound")
        return VerifiedGuidanceWork(scope.root_kind, scope.run_ref,
            verified["quest_ref"], run.request_ref)

    def verify_target_guidance_preparation(self, operation: TargetGuidanceOperation) -> VerifiedGuidanceWork:
        scope = operation.scope
        if scope.root_kind != "target" or type(operation.generation) is not int or operation.generation < 1:
            raise OwnerConflict("guidance_operation_invalid")
        verified = self._verify_root_agent_runtime_scope(
            **scope.as_dict(), allowed_statuses=frozenset({"admitted", "executed", "running"}),
        )
        with self._database.read() as connection:
            run = connection.execute(text(
                "SELECT status,native_session_ref FROM ar_harness_runs WHERE run_ref=:run_ref"
            ), {"run_ref": scope.run_ref}).one()
            generation = int(connection.execute(text(
                "SELECT COALESCE(MAX(generation),0)+1 FROM ar_harness_provider_operations "
                "WHERE run_ref=:run_ref"
            ), {"run_ref": scope.run_ref}).scalar_one())
            continuation = None
            if run.status == "running" and operation.resume:
                from meta_research.owners.agent_runtime_harness import _target_root_human_request_continuation_generation
                continuation = _target_root_human_request_continuation_generation(connection, scope.run_ref)
        if (operation.generation != generation
                or operation.operation_ref != provider_operation_ref(scope.run_ref, "harness_turn", generation)
                or (not operation.resume and (run.status != "admitted" or run.native_session_ref is not None))
                or (operation.resume and (run.native_session_ref is None
                    or run.status not in {"executed", "running"}
                    or run.status == "running" and continuation != generation))):
            raise OwnerConflict("guidance_target_preparation_stale")
        return VerifiedGuidanceWork("target", scope.run_ref, verified["quest_ref"], verified["target_ref"])

    def verify_guidance_read_scope(
        self, scope: GuidanceRuntimeScope, binding: FrozenGuidanceBinding, *, reconcile: bool = False,
    ) -> VerifiedGuidanceWork:
        if (scope.root_kind != binding.identity.root_kind or scope.run_ref != binding.identity.run_ref):
            raise OwnerConflict("guidance_snapshot_unbound")
        verified = (self.verify_root_agent_human_request_reconcile_scope
            if reconcile else self.verify_root_agent_runtime_scope)(**scope.as_dict())
        if verified["quest_ref"] != binding.quest_ref:
            raise OwnerConflict("guidance_snapshot_unbound")
        with self._database.read() as connection:
            row = connection.execute(text(
                "SELECT snapshot_json,snapshot_hash FROM hc_guidance_snapshots WHERE snapshot_ref=:ref"
            ), {"ref": binding.snapshot_ref}).first()
            if row is None or row.snapshot_hash != binding.snapshot_hash:
                raise OwnerConflict("guidance_snapshot_unbound")
            payload = decoded_object(row.snapshot_json)
            if scope.root_kind == "target":
                operation = connection.execute(text(
                    "SELECT status FROM ar_harness_provider_operations WHERE operation_ref=:operation_ref "
                    "AND run_ref=:run_ref"
                ), vars(binding.identity)).first()
                if operation is None or (not reconcile and operation.status != "running"):
                    raise OwnerConflict("guidance_operation_stale")
            elif not reconcile:
                spec = payload["operation"]
                self.verify_stage_guidance_operation(StageGuidanceOperation(
                    scope, spec["job_ref"], spec["operation_name"], spec["unit_ref"],
                ))
        return VerifiedGuidanceWork(scope.root_kind, scope.run_ref,
            binding.quest_ref, payload["provenance_ref"])
