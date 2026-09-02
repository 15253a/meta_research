from __future__ import annotations

import hashlib
from types import SimpleNamespace

from meta_research.experiment_contract import (
    AcceptedExperimentAssetRole,
    FormalMetricResult,
)
from meta_research.owners.common import (
    AcceptedAssetBinding,
    AcceptanceReceipt,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.research_graph import AcceptedAssetRole, TargetCommit
from meta_research.owners.research_memory import AcceptedAssetVersion, MaterializedAsset
from meta_research.target_commit_evidence import (
    TARGET_COMMIT_EVIDENCE_MEDIA_TYPE,
    TargetCommitEvidenceCatalog,
    target_commit_evidence_document,
    target_commit_evidence_provenance,
)


def _receipt(issuer: str, kind: str, subject_ref: str) -> AcceptanceReceipt:
    return AcceptanceReceipt(
        issuer=issuer,
        kind=kind,
        receipt_ref=f"{issuer}:{kind}:{subject_ref}",
        subject_ref=subject_ref,
        payload_hash=canonical_hash(
            {"issuer": issuer, "kind": kind, "subject_ref": subject_ref}
        ),
    )


def _measurement_role(
    role: str,
    *,
    subject_kind: str,
    subject_ref: str,
    ordinal: int = 0,
) -> AcceptedExperimentAssetRole:
    role_ref = f"role:{role}:{ordinal}"
    version_ref = f"version:{role}:{ordinal}"
    return AcceptedExperimentAssetRole(
        role_ref=role_ref,
        subject_kind=subject_kind,
        subject_ref=subject_ref,
        role=role,
        ordinal=ordinal,
        binding=AcceptedAssetBinding(
            asset_ref=f"asset:{role}:{ordinal}",
            version_ref=version_ref,
            content_hash=canonical_hash({"content": role, "ordinal": ordinal}),
            manifest_hash=canonical_hash({"manifest": role, "ordinal": ordinal}),
            receipt=_receipt("research_memory", "asset_acceptance", version_ref),
        ),
        receipt=_receipt(
            "research_graph", "experiment_asset_role_acceptance", role_ref
        ),
    )


def _manifest_entry(role: AcceptedExperimentAssetRole) -> dict[str, object]:
    receipt = role.binding.receipt.as_public_dict()
    return {
        "role": role.role,
        "binding": {
            "asset_ref": role.binding.asset_ref,
            "version_ref": role.binding.version_ref,
            "content_hash": role.binding.content_hash,
            "manifest_hash": role.binding.manifest_hash,
            "receipt": {key: value for key, value in receipt.items() if key != "status"},
        },
    }


def test_v3_target_commit_evidence_uses_formal_measurement_roles() -> None:
    quest_ref = "quest:formal-evidence"
    target_ref = "target:formal-evidence"
    target_run_ref = "target-run:formal-evidence"
    variant_run_ref = "variant-run:formal-evidence"
    attempt_ref = "evaluation-attempt:formal-evidence"
    result = _measurement_role(
        "result_content",
        subject_kind="evaluation_attempt",
        subject_ref=attempt_ref,
    )
    checkpoint = _measurement_role(
        "checkpoint_artifact",
        subject_kind="variant_run",
        subject_ref=variant_run_ref,
    )
    log = _measurement_role(
        "log_asset",
        subject_kind="evaluation_attempt",
        subject_ref=attempt_ref,
    )
    analysis = _measurement_role(
        "analysis_asset",
        subject_kind="evaluation_attempt",
        subject_ref=attempt_ref,
    )
    roles = (result, checkpoint, log, analysis)
    metrics = {"metric:effect": 0.25}
    metric = FormalMetricResult(
        metric_result_ref="metric-result:formal-evidence",
        evaluation_attempt_ref=attempt_ref,
        result_role_ref=result.role_ref,
        metrics=metrics,
        metrics_hash=canonical_hash(metrics),
        receipt=_receipt(
            "research_graph", "formal_measurement_acceptance", attempt_ref
        ),
    )
    closure = {
        "schema_ref": "meta-research/target-commit-closure/v3",
        "accepted_measurement": {"variant_run_ref": variant_run_ref},
        "formal_metric": metric.as_public_dict(),
        "result_manifest": {"entries": [_manifest_entry(role) for role in roles]},
        "measurement_attempt": {
            "checkpoint_role_refs": [checkpoint.role_ref],
            "result_role_ref": result.role_ref,
        },
        "result_content": {"content": {"metrics": metrics}},
    }
    commit = TargetCommit(
        commit_ref="target-commit:formal-evidence",
        target_ref=target_ref,
        target_run_ref=target_run_ref,
        evaluation_attempt_ref=attempt_ref,
        target_spec_hash=canonical_hash({"target_ref": target_ref}),
        closure=closure,
        closure_hash=canonical_hash(closure),
        result_disposition="positive",
        receipt=_receipt(
            "research_graph", "target_commit", "target-commit:formal-evidence"
        ),
    )

    evidence_content = canonical_json(target_commit_evidence_document(commit)).encode()
    evidence_version_ref = "asset-version:target-commit-evidence"
    evidence_receipt = _receipt(
        "research_memory", "asset_acceptance", evidence_version_ref
    )
    evidence_asset = AcceptedAssetVersion(
        asset_ref="asset:target-commit-evidence",
        version_ref=evidence_version_ref,
        memory_ref=evidence_version_ref,
        version_number=1,
        source_kind="generated",
        display_name="target-commit-evidence.json",
        media_type=TARGET_COMMIT_EVIDENCE_MEDIA_TYPE,
        content_hash=hashlib.sha256(evidence_content).hexdigest(),
        manifest_hash=canonical_hash({"version_ref": evidence_version_ref}),
        byte_count=len(evidence_content),
        provenance=target_commit_evidence_provenance(commit),
        custody_modes=("managed_copy",),
        accepted_at=1.0,
        receipt=evidence_receipt,
    )
    evidence_role = AcceptedAssetRole(
        role_ref="role:target-commit-evidence",
        version_ref=evidence_asset.version_ref,
        asset_ref=evidence_asset.asset_ref,
        asset_hash=evidence_asset.content_hash,
        manifest_hash=evidence_asset.manifest_hash,
        role="evidence",
        quest_ref=quest_ref,
        accepted_at=1.0,
        asset_receipt=evidence_receipt,
        receipt=_receipt(
            "research_graph", "evidence_role_acceptance", "role:target-commit-evidence"
        ),
    )

    graph = SimpleNamespace(
        query_target_commits_for_quest=lambda value: (
            (commit,) if value == quest_ref else ()
        ),
        query_asset_roles=lambda **values: (
            (evidence_role,)
            if values == {"quest_ref": quest_ref, "role": "evidence"}
            else ()
        ),
        query_target_formal_metric_result=lambda value: (
            metric if value == attempt_ref else None
        ),
        query_target_measurement_asset_roles=lambda value: (
            roles if value == attempt_ref else ()
        ),
    )
    memory = SimpleNamespace(
        query_asset_version=lambda value: (
            evidence_asset if value == evidence_version_ref else None
        ),
        materialize_asset=lambda value: MaterializedAsset(
            memory_ref=value,
            file_name="target-commit-evidence.json",
            media_type=TARGET_COMMIT_EVIDENCE_MEDIA_TYPE,
            content=evidence_content,
        ),
    )

    catalog = TargetCommitEvidenceCatalog(graph, memory)  # type: ignore[arg-type]
    leaves = catalog.resolve_reasoning_target_evidence_leaves(
        quest_ref=quest_ref,
        target_commit_refs=(commit.commit_ref,),
    )

    assert [leaf.role for leaf in leaves] == [
        "MetricResult",
        "CheckpointArtifact",
        "LogAsset",
        "AnalysisAsset",
    ]
    assert [leaf.source_role_ref for leaf in leaves] == [
        role.role_ref for role in roles
    ]
