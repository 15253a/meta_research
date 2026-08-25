from __future__ import annotations

from typing import cast

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_graph import (
    ResearchGraphInterface,
    TargetCommit,
    TargetCommitEvidenceAuthority,
)
from meta_research.owners.research_memory import ResearchMemoryInterface
from meta_research.plan_contract import EVIDENCE_REF_SCHEMA_REF


TARGET_COMMIT_EVIDENCE_SCHEMA_REF = "meta-research/target-commit-evidence/v1"
TARGET_COMMIT_EVIDENCE_MEDIA_TYPE = (
    "application/vnd.meta-research.target-commit-evidence+json"
)
TARGET_COMMIT_EVIDENCE_CAPABILITIES = (
    "experiment_result",
    "metric_result",
    "query_support",
)


def target_commit_evidence_closure_refs(
    commit: TargetCommit,
) -> tuple[str, ...]:
    return (
        commit.target_ref,
        commit.target_run_ref,
        commit.evaluation_attempt_ref,
    )


def target_commit_evidence_provenance(
    commit: TargetCommit,
) -> dict[str, object]:
    return {
        "target_commit_root_ref": commit.commit_ref,
        "provenance_closure_refs": list(target_commit_evidence_closure_refs(commit)),
        "capabilities": list(TARGET_COMMIT_EVIDENCE_CAPABILITIES),
        "target_commit_closure_hash": commit.closure_hash,
        "result_disposition": commit.result_disposition,
    }


def target_commit_metric_result(commit: TargetCommit) -> dict[str, object]:
    """Project the metric leaf from either accepted TargetCommit closure."""

    legacy = commit.closure.get("metric_result")
    if type(legacy) is dict:
        return cast(dict[str, object], legacy)
    root = commit.closure.get("root_measurement")
    if type(root) is not dict:
        raise OwnerConflict("target_commit_metric_result_invalid")
    root_value = cast(dict[str, object], root)
    metric_result_ref = root_value.get("metric_result_ref")
    metrics = root_value.get("metrics")
    receipt = root_value.get("receipt")
    if (
        type(metric_result_ref) is not str
        or not metric_result_ref
        or type(metrics) is not dict
        or not metrics
        or any(
            type(key) is not str
            or not key
            or type(value) not in {int, float}
            for key, value in cast(dict[object, object], metrics).items()
        )
        or type(receipt) is not dict
    ):
        raise OwnerConflict("target_commit_metric_result_invalid")
    return {
        "metric_result_ref": metric_result_ref,
        "metrics": metrics,
        "receipt": receipt,
    }


def target_commit_evidence_document(
    commit: TargetCommit,
) -> dict[str, object]:
    return {
        "schema_ref": TARGET_COMMIT_EVIDENCE_SCHEMA_REF,
        "target_commit_ref": commit.commit_ref,
        "target_ref": commit.target_ref,
        "target_run_ref": commit.target_run_ref,
        "evaluation_attempt_ref": commit.evaluation_attempt_ref,
        "target_spec_hash": commit.target_spec_hash,
        "target_commit_closure_hash": commit.closure_hash,
        "result_disposition": commit.result_disposition,
        "metric_result": target_commit_metric_result(commit),
        "result_content": commit.closure["result_content"],
        "target_commit_receipt": commit.receipt.as_public_dict(),
    }


class TargetCommitEvidenceCatalog(TargetCommitEvidenceAuthority):
    """Expose only RM/RG evidence leaves rooted in real TargetCommits."""

    def __init__(
        self,
        research_graph: ResearchGraphInterface,
        research_memory: ResearchMemoryInterface,
    ) -> None:
        self._research_graph = research_graph
        self._research_memory = research_memory

    def query_plan_evidence_catalog(
        self, *, quest_ref: str
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        commits = {
            commit.commit_ref: commit
            for commit in self._research_graph.query_target_commits_for_quest(quest_ref)
        }
        accepted: dict[str, dict[str, object]] = {}
        roles = self._research_graph.query_asset_roles(
            quest_ref=quest_ref,
            role="evidence",
        )
        for role in roles:
            asset = self._research_memory.query_asset_version(role.version_ref)
            if asset is None:
                raise OwnerConflict("target_commit_evidence_asset_missing")
            provenance = asset.provenance
            root_ref = provenance.get("target_commit_root_ref")
            commit = commits.get(root_ref) if isinstance(root_ref, str) else None
            if commit is None:
                # A generic evidence role or a claimed root is not a Baseline
                # Pool member.  It remains visible through the generic Asset
                # projection, but cannot masquerade as TargetCommit evidence.
                continue
            expected_provenance = target_commit_evidence_provenance(commit)
            if any(
                provenance.get(key) != value
                for key, value in expected_provenance.items()
            ):
                continue
            if asset.media_type != TARGET_COMMIT_EVIDENCE_MEDIA_TYPE:
                continue
            materialized = self._research_memory.materialize_asset(role.version_ref)
            expected_content = canonical_json(
                target_commit_evidence_document(commit)
            ).encode("utf-8")
            if materialized.content != expected_content:
                continue
            if (
                role.asset_ref != asset.asset_ref
                or role.asset_hash != asset.content_hash
                or role.manifest_hash != asset.manifest_hash
                or role.asset_receipt != asset.receipt
            ):
                raise OwnerConflict("target_commit_evidence_binding_invalid")
            evidence_ref = (
                "evidence_"
                + canonical_hash({"target_commit_ref": commit.commit_ref})[:32]
            )
            candidate = {
                "schema_ref": EVIDENCE_REF_SCHEMA_REF,
                "evidence_ref": evidence_ref,
                "asset_version_ref": asset.version_ref,
                "asset_ref": asset.asset_ref,
                "content_hash": asset.content_hash,
                "manifest_hash": asset.manifest_hash,
                "target_commit_root_ref": commit.commit_ref,
                "provenance_closure_refs": list(
                    target_commit_evidence_closure_refs(commit)
                ),
                "capabilities": list(TARGET_COMMIT_EVIDENCE_CAPABILITIES),
                "eligibility_token_ref": role.receipt.receipt_ref,
                "integrity_receipt_ref": asset.receipt.receipt_ref,
                "availability_receipt_ref": asset.receipt.receipt_ref,
                "currentness_receipt_ref": role.receipt.receipt_ref,
                "asset_receipt": asset.receipt.as_public_dict(),
                "role_ref": role.role_ref,
                "role_receipt": role.receipt.as_public_dict(),
            }
            previous = accepted.get(commit.commit_ref)
            if previous is None or cast(str, candidate["role_ref"]) < cast(
                str, previous["role_ref"]
            ):
                accepted[commit.commit_ref] = candidate
        catalog = tuple(
            sorted(
                accepted.values(),
                key=lambda item: cast(str, item["evidence_ref"]),
            )
        )
        return len(catalog), catalog

    def verify_plan_evidence_catalog(
        self,
        *,
        quest_ref: str,
        evidence_catalog: list[dict[str, object]],
        expected_reference_revision: int,
        require_current: bool = True,
        require_complete: bool = True,
        selected_evidence_refs: frozenset[str] | None = None,
    ) -> None:
        if (
            not isinstance(expected_reference_revision, int)
            or isinstance(expected_reference_revision, bool)
            or expected_reference_revision < 0
            or not isinstance(evidence_catalog, list)
            or not all(isinstance(item, dict) for item in evidence_catalog)
            or (
                selected_evidence_refs is not None
                and not isinstance(selected_evidence_refs, frozenset)
            )
        ):
            raise OwnerConflict("plan_evidence_catalog_invalid")
        current_revision, current_catalog = self.query_plan_evidence_catalog(
            quest_ref=quest_ref
        )
        if expected_reference_revision > current_revision:
            raise OwnerConflict("plan_evidence_catalog_invalid")
        if len(evidence_catalog) != expected_reference_revision:
            raise OwnerConflict("plan_evidence_catalog_invalid")
        current_by_ref = {
            cast(str, item["evidence_ref"]): item for item in current_catalog
        }
        supplied_refs: list[str] = []
        for item in evidence_catalog:
            evidence_ref = item.get("evidence_ref")
            if (
                not isinstance(evidence_ref, str)
                or not evidence_ref
                or evidence_ref in supplied_refs
                or current_by_ref.get(evidence_ref) != item
            ):
                raise OwnerConflict("plan_evidence_catalog_invalid")
            supplied_refs.append(evidence_ref)
        if require_current and (
            expected_reference_revision != current_revision
            or tuple(evidence_catalog) != current_catalog
        ):
            raise OwnerConflict("plan_evidence_catalog_stale")
        if (
            require_complete
            and require_current
            and tuple(evidence_catalog) != current_catalog
        ):
            raise OwnerConflict("plan_evidence_catalog_invalid")
        if selected_evidence_refs is not None and not selected_evidence_refs.issubset(
            supplied_refs
        ):
            raise OwnerConflict("plan_evidence_catalog_invalid")
