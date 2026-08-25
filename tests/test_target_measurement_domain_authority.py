from __future__ import annotations

import sqlite3
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import text

import meta_research.owners.research_graph as research_graph_module
from meta_research.bundle_skill import BundleSkillRequest
from meta_research.bundle_target_contract import FORMAL_STRATEGY_UPDATE_SCHEMA_REF
from meta_research.migration import upgrade_database
from meta_research.owners.common import OwnerConflict
from test_plan_stage_migration import _upgrade_to_revision
from test_public_bundle_stage import (
    _DeterministicBundleSkill,
    _TwoGapPlanSkill,
    _bundle_runtime,
    _confirm_direct_quest,
    _finish_idea_stage,
    _finish_plan_stage,
    _formal_candidate,
    _formal_target_plan,
)


def _accept_initial_graph(runtime):
    _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    _finish_plan_stage(runtime)
    for _step in range(10):
        assert runtime.bundle_stage.process_once()
        current = runtime.bundle_stage.query_current()
        if current["target_graph"]["status"] == "accepted":
            request_ref = current["stage_run_request"]["request_ref"]
            graph = runtime.owners.research_graph.query_target_graph(request_ref)
            assert graph is not None
            return current, graph
    raise AssertionError("Bundle did not accept its initial TargetGraph")


def _domain_counts(runtime) -> dict[str, int]:
    with runtime._database.read() as connection:
        row = connection.execute(
            text(
                "SELECT experiment_baseline_count, experiment_variant_count, "
                "evaluation_protocol_count, protocol_version_count, "
                "evaluation_count, variant_run_count, "
                "evaluation_attempt_count, "
                "target_measurement_domain_authority_count FROM "
                "research_graph_state WHERE singleton = 'owner'"
            )
        ).one()
        provider_requests = connection.execute(
            text("SELECT COUNT(*) FROM rg_experiment_requests")
        ).scalar_one()
    return {
        "baseline": int(row.experiment_baseline_count),
        "variant": int(row.experiment_variant_count),
        "protocol": int(row.evaluation_protocol_count),
        "version": int(row.protocol_version_count),
        "evaluation": int(row.evaluation_count),
        "variant_run": int(row.variant_run_count),
        "evaluation_attempt": int(row.evaluation_attempt_count),
        "authority": int(row.target_measurement_domain_authority_count),
        "provider_request": int(provider_requests),
    }


def test_initial_authority_is_restart_safe_idempotent_and_pre_execution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "initial-authority"
    runtime = _bundle_runtime(root)
    try:
        _current, graph = _accept_initial_graph(runtime)
        target = graph.targets[0]
        accepted = runtime.owners.research_graph.query_target_measurement_domain_authority(
            target.target_ref
        )
        assert accepted is not None
        assert accepted.target_spec_hash == target.spec_hash
        assert accepted.receipt.subject_ref == accepted.authority_hash
        assert accepted.graph_generation == 0
        assert accepted.protocol_aggregation_proof is not None
        assert tuple(part.part_key for part in accepted.protocol_parts) == (
            f"part:{target.target_key}:first",
            f"part:{target.target_key}:second",
        )
        proof = accepted.protocol_aggregation_proof
        assert (
            proof.aggregation_evidence_receipt.subject_ref
            == proof.aggregation_evidence_binding.content_hash_ref
        )
        runtime.owners.research_graph.verify_target_measurement_domain_authority(
            target_ref=target.target_ref,
            measurement_contract_hash=accepted.measurement_contract_hash,
            identities=accepted.identities,
            receipt=accepted.receipt,
        )
        runtime.owners.research_graph.verify_target_measurement_protocol_aggregation(
            target_ref=target.target_ref,
            parts=accepted.protocol_parts,
            proof=proof,
        )
        with pytest.raises(
            OwnerConflict,
            match="target_measurement_protocol_aggregation_invalid",
        ):
            runtime.owners.research_graph.verify_target_measurement_protocol_aggregation(
                target_ref=target.target_ref,
                parts=tuple(reversed(accepted.protocol_parts)),
                proof=proof,
            )
        with pytest.raises(
            OwnerConflict,
            match="target_measurement_protocol_aggregation_invalid",
        ):
            runtime.owners.research_graph.verify_target_measurement_protocol_aggregation(
                target_ref=target.target_ref,
                parts=accepted.protocol_parts,
                proof=replace(
                    proof,
                    aggregation_rule_ref="aggregation:wrong",
                ),
            )
        with pytest.raises(
            OwnerConflict,
            match="target_measurement_protocol_aggregation_invalid",
        ):
            runtime.owners.research_graph.verify_target_measurement_protocol_aggregation(
                target_ref=target.target_ref,
                parts=accepted.protocol_parts,
                proof=replace(
                    proof,
                    aggregation_evidence_receipt=replace(
                        proof.aggregation_evidence_receipt,
                        subject_ref="0" * 64,
                    ),
                ),
            )
        assert _domain_counts(runtime) == {
            "baseline": 1,
            "variant": 1,
            "protocol": 1,
            "version": 1,
            "evaluation": 1,
            "variant_run": 0,
            "evaluation_attempt": 0,
            "authority": 1,
            "provider_request": 0,
        }

        run = runtime.owners.agent_runtime.query_bundle_stage_run(graph.request_ref)
        assert run is not None and run.execution is not None
        replay = runtime.owners.research_graph.accept_target_graph(
            request_ref=graph.request_ref,
            run_ref=graph.run_ref,
            attempt_ref=graph.attempt_ref,
            fence_ref=graph.fence_ref,
            submission_ref=graph.submission_ref,
            context_pack_ref=graph.context_pack_ref,
            target_plan=graph.target_plan,
            target_plan_hash=graph.target_plan_hash,
            execution_payload_hash=run.execution.payload_hash,
            execution_receipt=graph.execution_receipt,
        )
        assert replay == graph
        assert _domain_counts(runtime)["authority"] == 1
    finally:
        runtime.close()

    restarted = _bundle_runtime(root)
    try:
        reread = restarted.owners.research_graph.query_target_measurement_domain_authority(
            target.target_ref
        )
        assert reread == accepted
        with restarted._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_stage_attempts SET execution_receipt_hash = "
                    ":tampered WHERE attempt_ref = :attempt_ref"
                ),
                {"tampered": "0" * 64, "attempt_ref": graph.attempt_ref},
            )
        with pytest.raises(OwnerConflict, match="attempt_execution"):
            restarted.owners.research_graph.query_target_measurement_domain_authority(
                target.target_ref
            )
    finally:
        restarted.close()


def test_rolling_append_binds_exact_cas_source_and_has_no_stale_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "rolling-authority",
        plan_skill_provider=_TwoGapPlanSkill(),
    )
    try:
        current, graph = _accept_initial_graph(runtime)
        initial = graph.targets[0]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(graph.request_ref)
        assert run is not None and run.execution is not None
        assert run.native_session_ref is not None
        checkpoint = runtime.owners.agent_runtime.query_bundle_inbox_checkpoint(
            run.run_ref
        )
        assert checkpoint is not None
        plan_document = current["stage_run_request"]["accepted_formal_plan_binding"][
            "plan_document"
        ]
        second_brief = cast(list[dict[str, object]], plan_document["experiment_briefs"])[
            1
        ]
        second_key = cast(str, second_brief["experiment_key"])
        second = _formal_candidate(
            completion_document=cast(
                dict[str, object], graph.target_plan["completion_contract"]
            ),
            label="rolling-independent-target",
            experiment_key=second_key,
            cell=f"measurement:{second_key}",
        )

        def proposal(key: str, candidate: dict[str, object]):
            return runtime.owners.agent_runtime.record_bundle_target_proposal(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref=run.native_session_ref,
                graph_ref=graph.graph_ref,
                base_generation=graph.head_generation,
                base_head_receipt=graph.head_receipt,
                strategy_update={
                    "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
                    "revision": 2,
                    "candidates": [candidate],
                    "requires_accepted_labels": [],
                    "strategy_complete": True,
                },
                inbox_checkpoint=checkpoint,
                idempotency_key=key,
            )

        accepted_proposal = proposal("rolling-domain-accepted", second)
        stale_candidate = deepcopy(second)
        cast(dict[str, object], stale_candidate["candidate"])["local_label"] = (
            "rolling-stale-target"
        )
        stale_proposal = proposal("rolling-domain-stale", stale_candidate)
        original_verify = research_graph_module._verify_target_candidate_owner_proofs
        proof_checks = 0

        def reject_locked_recheck(*args, **kwargs):
            nonlocal proof_checks
            proof_checks += 1
            if proof_checks == 2:
                raise OwnerConflict("target_candidate_owner_proof_unverified")
            return original_verify(*args, **kwargs)

        monkeypatch.setattr(
            research_graph_module,
            "_verify_target_candidate_owner_proofs",
            reject_locked_recheck,
        )
        before_drift = _domain_counts(runtime)
        with pytest.raises(
            OwnerConflict,
            match="target_candidate_owner_proof_unverified",
        ):
            runtime.owners.research_graph.append_target_batch(
                graph_ref=graph.graph_ref,
                proposal_ref=accepted_proposal.proposal_ref,
                proposal=accepted_proposal.proposal,
                proposal_hash=accepted_proposal.proposal_hash,
                proposal_receipt=accepted_proposal.receipt,
            )
        assert proof_checks == 2
        assert _domain_counts(runtime) == before_drift
        unchanged_head = runtime.owners.research_graph.query_target_graph_head(
            graph.graph_ref
        )
        assert unchanged_head.generation == graph.head_generation
        assert unchanged_head.receipt == graph.head_receipt
        monkeypatch.setattr(
            research_graph_module,
            "_verify_target_candidate_owner_proofs",
            original_verify,
        )
        head = runtime.owners.research_graph.append_target_batch(
            graph_ref=graph.graph_ref,
            proposal_ref=accepted_proposal.proposal_ref,
            proposal=accepted_proposal.proposal,
            proposal_hash=accepted_proposal.proposal_hash,
            proposal_receipt=accepted_proposal.receipt,
        )
        assert head.generation == 1
        graph_after = runtime.owners.research_graph.query_target_graph(
            graph.request_ref
        )
        assert graph_after is not None and len(graph_after.targets) == 2
        appended = next(
            target for target in graph_after.targets if target.target_ref != initial.target_ref
        )
        authority = runtime.owners.research_graph.query_target_measurement_domain_authority(
            appended.target_ref
        )
        assert authority is not None and authority.graph_generation == 1
        with runtime._database.read() as connection:
            row = connection.execute(
                text(
                    "SELECT * FROM rg_target_measurement_domain_authorities "
                    "WHERE target_ref = :target_ref"
                ),
                {"target_ref": appended.target_ref},
            ).one()
        assert row.append_ref is not None
        assert row.predecessor_head_receipt_ref == graph.head_receipt.receipt_ref
        assert row.predecessor_head_receipt_hash == graph.head_receipt.payload_hash
        assert row.proposal_ref == accepted_proposal.proposal_ref
        assert row.proposal_hash == accepted_proposal.proposal_hash
        assert row.proposal_receipt_ref == accepted_proposal.receipt.receipt_ref
        assert row.proposal_receipt_hash == accepted_proposal.receipt.payload_hash
        before_stale = _domain_counts(runtime)
        with pytest.raises(OwnerConflict, match="target_graph_append_base_stale"):
            runtime.owners.research_graph.append_target_batch(
                graph_ref=graph.graph_ref,
                proposal_ref=stale_proposal.proposal_ref,
                proposal=stale_proposal.proposal,
                proposal_hash=stale_proposal.proposal_hash,
                proposal_receipt=stale_proposal.receipt,
            )
        assert _domain_counts(runtime) == before_stale
        replay = runtime.owners.research_graph.append_target_batch(
            graph_ref=graph.graph_ref,
            proposal_ref=accepted_proposal.proposal_ref,
            proposal=accepted_proposal.proposal,
            proposal_hash=accepted_proposal.proposal_hash,
            proposal_receipt=accepted_proposal.receipt,
        )
        assert replay == head
        assert _domain_counts(runtime) == before_stale
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_bundle_target_proposals SET receipt_hash = "
                    ":tampered WHERE proposal_ref = :proposal_ref"
                ),
                {
                    "tampered": "0" * 64,
                    "proposal_ref": accepted_proposal.proposal_ref,
                },
            )
        with pytest.raises(OwnerConflict, match="bundle_target_proposal"):
            runtime.owners.research_graph.query_target_measurement_domain_authority(
                appended.target_ref
            )
    finally:
        runtime.close()


class _MultiExperimentTargetSkill(_DeterministicBundleSkill):
    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        briefs = cast(list[dict[str, object]], request.plan_document["experiment_briefs"])
        keys = tuple(cast(str, brief["experiment_key"]) for brief in briefs)
        cell = "measurement:shared-atomic-contract"
        document = _formal_target_plan(
            request,
            cells_by_experiment={key: (cell,) for key in keys},
            candidates=(("multi-experiment-target", keys[0], cell, (), "normal"),),
            strategy_complete=True,
        )
        update = cast(dict[str, object], document["initial_strategy_update"])
        candidate = cast(list[dict[str, object]], update["candidates"])[0]
        completion = cast(dict[str, object], document["completion_contract"])
        experiments = cast(list[dict[str, object]], completion["experiments"])
        cast(dict[str, object], candidate["candidate"])["experiment_keys"] = list(keys)
        candidate["semantic_inputs"] = [
            deepcopy(cast(dict[str, object], item["semantic_inputs"]))
            for item in experiments
        ]
        cast(dict[str, object], candidate["measurement_contract"])[
            "experiment_keys"
        ] = list(keys)
        return document


def test_multi_experiment_keys_are_never_collapsed_to_the_first_key(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "multi-key-authority",
        plan_skill_provider=_TwoGapPlanSkill(),
        bundle_skill_provider=_MultiExperimentTargetSkill(),
    )
    try:
        _current, graph = _accept_initial_graph(runtime)
        target = graph.targets[0]
        accepted = runtime.owners.research_graph.query_target_measurement_domain_authority(
            target.target_ref
        )
        assert accepted is not None
        assert len(accepted.experiment_keys) == 2
        assert accepted.experiment_keys == (
            accepted.measurement_contract.experiment_keys
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rg_target_measurement_domain_authorities SET "
                    "experiment_keys_json = :keys, experiment_keys_hash = :hash "
                    "WHERE target_ref = :target_ref"
                ),
                {
                    "keys": f'["{accepted.experiment_keys[0]}"]',
                    "hash": "0" * 64,
                    "target_ref": target.target_ref,
                },
            )
        with pytest.raises(
            OwnerConflict,
            match="target_measurement_domain_authority_integrity_invalid",
        ):
            runtime.owners.research_graph.query_target_measurement_domain_authority(
                target.target_ref
            )
    finally:
        runtime.close()


class _NoPartsTargetSkill(_DeterministicBundleSkill):
    def _target_plan(self, request: BundleSkillRequest) -> dict[str, object]:
        document = super()._target_plan(request)
        update = cast(dict[str, object], document["initial_strategy_update"])
        candidate = cast(list[dict[str, object]], update["candidates"])[0]
        measurement = cast(dict[str, object], candidate["measurement_contract"])
        protocol = cast(dict[str, object], measurement["protocol_version"])
        protocol["internal_part_keys"] = []
        protocol["aggregation"] = None
        return document


def test_protocol_without_internal_parts_has_no_aggregation_proof(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "no-parts-authority",
        bundle_skill_provider=_NoPartsTargetSkill(),
    )
    try:
        _current, graph = _accept_initial_graph(runtime)
        target_ref = graph.targets[0].target_ref
        accepted = runtime.owners.research_graph.query_target_measurement_domain_authority(
            target_ref
        )
        assert accepted is not None
        assert accepted.protocol_parts == ()
        assert accepted.protocol_aggregation_proof is None
        runtime.owners.research_graph.verify_target_measurement_protocol_aggregation(
            target_ref=target_ref,
            parts=(),
            proof=None,
        )
    finally:
        runtime.close()


def test_target_measurement_schema_and_pre_0027_target_fail_closed_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "target-domain-migration.sqlite3"
    _upgrade_to_revision(database, "0026_bundle_inbox_runtime")
    upgrade_database(database)
    upgrade_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0030_writing_delivery",)
        foreign_keys = {
            (row[2], row[3], row[4])
            for row in connection.execute(
                "PRAGMA foreign_key_list('rg_target_measurement_domain_authorities')"
            )
        }
        assert {
            ("rm_plan_documents", "plan_content_ref", "content_ref"),
            ("ae_stage_run_requests", "stage_request_ref", "request_ref"),
            ("ae_stage_commits", "stage_commit_ref", "commit_ref"),
            ("rg_targets", "target_ref", "target_ref"),
        } <= foreign_keys
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

    runtime = _bundle_runtime(tmp_path / "legacy-missing-authority")
    try:
        _current, graph = _accept_initial_graph(runtime)
        target_ref = graph.targets[0].target_ref
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "DELETE FROM rg_target_measurement_domain_authorities WHERE "
                    "target_ref = :target_ref"
                ),
                {"target_ref": target_ref},
            )
        with pytest.raises(
            OwnerConflict,
            match="target_measurement_domain_authority_required",
        ):
            runtime.owners.research_graph.query_target_launch_request(
                target_ref
            )
    finally:
        runtime.close()
