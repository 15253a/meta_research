from __future__ import annotations

import json

from typing import cast

from meta_research.experiment_contract import AcceptedExperimentAssetRole
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.research_graph import (
    EvidenceReuseLeaf,
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
        "metric_result": _target_commit_metric_document(commit),
        "result_content": commit.closure["result_content"],
        "target_commit_receipt": commit.receipt.as_public_dict(),
    }


def _target_commit_metric_document(commit: TargetCommit) -> dict[str, object]:
    """Read the exact metric projection frozen by this TargetCommit version."""

    closure = commit.closure
    value = closure.get("metric_result")
    if isinstance(value, dict):
        return value
    value = closure.get("formal_metric")
    if isinstance(value, dict):
        return value
    value = closure.get("root_measurement")
    if isinstance(value, dict):
        metric_ref = value.get("metric_result_ref")
        attempt_ref = value.get("evaluation_attempt_ref")
        metrics = value.get("metrics")
        receipt = value.get("receipt")
        if (
            isinstance(metric_ref, str)
            and metric_ref
            and attempt_ref == commit.evaluation_attempt_ref
            and isinstance(metrics, dict)
            and isinstance(receipt, dict)
        ):
            return {
                "metric_result_ref": metric_ref,
                "evaluation_attempt_ref": attempt_ref,
                "result_role_ref": "target-root-result-document",
                "metrics": metrics,
                "metrics_hash": canonical_hash(metrics),
                "receipt": receipt,
            }
    raise OwnerConflict("target_commit_evidence_metric_invalid")


def evidence_catalog_page_metadata(
    *, total: int, catalog: tuple[dict[str, object], ...], scanned: int,
    offset: int, limit: int, question_ref: str | None,
    projections: list[dict[str, object]],
) -> dict[str, object]:
    next_offset = offset + scanned
    return {
        "total_candidates": total, "candidate_unit": "evidence_asset_role",
        "candidate_count": len(catalog),
        "scanned_count": scanned, "offset": offset, "limit": limit,
        "next_offset": next_offset if next_offset < total else None,
        "question_ref": question_ref,
        "selection": "current_question_then_recent_active_or_shared",
        "snapshot_hash": canonical_hash(list(catalog)),
        "projections": projections,
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
        self, *, quest_ref: str, target_commit_refs: tuple[str, ...] | None = None,
        current_only: bool = True, role_refs: tuple[str, ...] | None = None,
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        """Read exact roots, or a complete small catalog without truncation."""
        if target_commit_refs is not None:
            catalog = self._catalog_for_refs(
                quest_ref=quest_ref, target_commit_refs=target_commit_refs,
                current_only=current_only, role_refs=role_refs,
            )
        else:
            page, catalog = self.query_plan_evidence_page(quest_ref=quest_ref)
            if page["next_offset"] is not None:
                raise OwnerConflict("plan_evidence_catalog_pagination_required")
        return len(catalog), catalog

    def query_plan_evidence_page(
        self, *, quest_ref: str, question_ref: str | None = None,
        offset: int = 0, limit: int = 256,
    ) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
        total, candidates = self._research_graph.query_target_commit_evidence_candidates(
            quest_ref=quest_ref, question_ref=question_ref, offset=offset, limit=limit
        )
        projection_by_ref: dict[str, dict[str, object]] = {}
        catalog = self._catalog_for_candidates(
            quest_ref=quest_ref, candidates=candidates, projections=projection_by_ref
        )
        projections = [projection_by_ref[str(item["evidence_ref"])] for item in catalog]
        return evidence_catalog_page_metadata(
            total=total, catalog=catalog, scanned=len(candidates), offset=offset,
            limit=limit, question_ref=question_ref, projections=projections,
        ), catalog

    def _catalog_for_refs(
        self, *, quest_ref: str, target_commit_refs: tuple[str, ...],
        current_only: bool, role_refs: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, object], ...]:
        accepted: dict[str, dict[str, object]] = {}
        offset = 0
        while True:
            total, candidates = self._research_graph.query_target_commit_evidence_candidates(
                quest_ref=quest_ref, target_commit_refs=target_commit_refs,
                current_only=current_only, role_refs=role_refs, offset=offset,
            )
            for item in self._catalog_for_candidates(quest_ref=quest_ref, candidates=candidates):
                ref = str(item["evidence_ref"])
                previous = accepted.get(ref)
                if previous is None or str(item["role_ref"]) < str(previous["role_ref"]):
                    accepted[ref] = item
            offset += len(candidates)
            if not candidates or offset >= total:
                break
        return tuple(accepted[ref] for ref in sorted(accepted))

    def _catalog_for_candidates(
        self, *, quest_ref: str, candidates: tuple[dict[str, object], ...],
        projections: dict[str, dict[str, object]] | None = None,
    ) -> tuple[dict[str, object], ...]:
        commits = {
            commit.commit_ref: commit
            for commit in self._research_graph.query_target_commits_for_quest(
                quest_ref,
                target_commit_refs=tuple(str(row["target_commit_ref"]) for row in candidates),
            )
        }
        if not candidates:
            return ()
        accepted: dict[str, dict[str, object]] = {}
        roles = self._research_graph.query_asset_roles(
            quest_ref=quest_ref,
            role="evidence",
            version_refs=tuple(str(row["version_ref"]) for row in candidates),
        )
        selected_roles = {str(row["role_ref"]) for row in candidates}
        for role in roles:
            if role.role_ref not in selected_roles:
                continue
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
                if projections is not None:
                    source = next(row for row in candidates if row["role_ref"] == role.role_ref)
                    spec = json.loads(str(source["spec_json"]))
                    if canonical_hash(spec) != commit.target_spec_hash:
                        raise OwnerConflict("target_commit_evidence_spec_mismatch")
                    projections[evidence_ref] = {
                        "evidence_ref": evidence_ref,
                        "question_ref": source["question_ref"], "cycle_ref": source["cycle_ref"],
                        "target_commit_ref": commit.commit_ref,
                        "asset_version_ref": asset.version_ref, "content_hash": asset.content_hash,
                        "target_spec": spec, "target_spec_hash": commit.target_spec_hash,
                        "metric_result": _target_commit_metric_document(commit),
                        "result_disposition": commit.result_disposition,
                        "exact_content_reader": "research_memory.plan_evidence.read",
                    }
        catalog = tuple(
            sorted(
                accepted.values(),
                key=lambda item: cast(str, item["evidence_ref"]),
            )
        )
        return catalog

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
        if len(evidence_catalog) != expected_reference_revision or len(evidence_catalog) > 256:
            raise OwnerConflict("plan_evidence_catalog_invalid")
        root_refs = tuple(str(item.get("target_commit_root_ref", "")) for item in evidence_catalog)
        current_catalog = self._catalog_for_refs(
            quest_ref=quest_ref, target_commit_refs=root_refs, current_only=require_current,
            role_refs=tuple(str(item.get("role_ref", "")) for item in evidence_catalog),
        )
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
        # A Plan freezes its selected page, not the entire growing Quest.
        # New candidates cannot stale a valid cut; current membership still
        # fails closed when a selected source loses eligibility after pruning.
        if require_complete and len(current_by_ref) != len(evidence_catalog):
            raise OwnerConflict("plan_evidence_catalog_invalid")
        if selected_evidence_refs is not None and not selected_evidence_refs.issubset(
            supplied_refs
        ):
            raise OwnerConflict("plan_evidence_catalog_invalid")

    def resolve_plan_evidence_reuse_leaves(
        self,
        *,
        quest_ref: str,
        evidence_catalog: list[dict[str, object]],
        expected_reference_revision: int,
        evidence_reuse_set: list[dict[str, object]],
    ) -> tuple[EvidenceReuseLeaf, ...]:
        """Resolve only the exact evidence selected from a frozen Plan cut."""

        if not isinstance(evidence_reuse_set, list) or not all(
            isinstance(item, dict) for item in evidence_reuse_set
        ):
            raise OwnerConflict("plan_evidence_reuse_set_invalid")
        uses_by_ref: dict[str, list[dict[str, object]]] = {}
        for use in evidence_reuse_set:
            evidence_ref = use.get("evidence_ref")
            if not isinstance(evidence_ref, str) or not evidence_ref:
                raise OwnerConflict("plan_evidence_reuse_set_invalid")
            uses_by_ref.setdefault(evidence_ref, []).append(use)
        selected_refs = frozenset(uses_by_ref)
        self.verify_plan_evidence_catalog(
            quest_ref=quest_ref,
            evidence_catalog=evidence_catalog,
            expected_reference_revision=expected_reference_revision,
            # The Plan cut is immutable and may be historical by the time
            # Reasoning runs.  Verify its exact issuer-backed members without
            # silently switching to the latest catalog.
            require_current=False,
            require_complete=False,
            selected_evidence_refs=selected_refs,
        )
        if not selected_refs:
            return ()

        catalog_by_ref = {
            cast(str, item["evidence_ref"]): item for item in evidence_catalog
        }
        commits = {
            commit.commit_ref: commit
            for commit in self._research_graph.query_target_commits_for_quest(
                quest_ref,
                target_commit_refs=tuple(str(catalog_by_ref[ref]["target_commit_root_ref"]) for ref in selected_refs),
            )
        }
        leaves: list[EvidenceReuseLeaf] = []
        for evidence_ref in sorted(selected_refs):
            catalog_entry = catalog_by_ref.get(evidence_ref)
            if catalog_entry is None:
                raise OwnerConflict("plan_evidence_reuse_closure_invalid")
            target_commit_ref = catalog_entry.get("target_commit_root_ref")
            asset_version_ref = catalog_entry.get("asset_version_ref")
            if (
                not isinstance(target_commit_ref, str)
                or not target_commit_ref
                or not isinstance(asset_version_ref, str)
                or not asset_version_ref
            ):
                raise OwnerConflict("plan_evidence_reuse_closure_invalid")
            commit = commits.get(target_commit_ref)
            if commit is None:
                raise OwnerConflict("plan_evidence_reuse_closure_invalid")
            catalog_asset_receipt = _accepted_receipt(
                catalog_entry.get("asset_receipt"),
                error_code="plan_evidence_reuse_closure_invalid",
            )
            catalog_role_receipt = _accepted_receipt(
                catalog_entry.get("role_receipt"),
                error_code="plan_evidence_reuse_closure_invalid",
            )
            if (
                commit.receipt.issuer != "research_graph"
                or commit.receipt.kind != "target_commit"
                or commit.receipt.subject_ref != commit.commit_ref
                or catalog_asset_receipt.issuer != "research_memory"
                or catalog_asset_receipt.subject_ref != asset_version_ref
                or catalog_role_receipt.issuer != "research_graph"
                or catalog_role_receipt.subject_ref
                != catalog_entry.get("role_ref")
                or catalog_entry.get("integrity_receipt_ref")
                != catalog_asset_receipt.receipt_ref
                or catalog_entry.get("availability_receipt_ref")
                != catalog_asset_receipt.receipt_ref
                or catalog_entry.get("eligibility_token_ref")
                != catalog_role_receipt.receipt_ref
                or catalog_entry.get("currentness_receipt_ref")
                != catalog_role_receipt.receipt_ref
            ):
                raise OwnerConflict("plan_evidence_reuse_closure_invalid")
            leaves.extend(
                self._issuer_closed_role_leaves(
                    evidence_ref=evidence_ref,
                    catalog_entry=catalog_entry,
                    uses=uses_by_ref[evidence_ref],
                    commit=commit,
                    catalog_asset_receipt=catalog_asset_receipt,
                    catalog_role_receipt=catalog_role_receipt,
                )
            )
        return tuple(leaves)

    def resolve_reasoning_target_evidence_leaves(
        self,
        *,
        quest_ref: str,
        target_commit_refs: tuple[str, ...],
    ) -> tuple[EvidenceReuseLeaf, ...]:
        """Close the exact current-Cycle TargetCommit roles without a latest read."""

        if (
            not isinstance(target_commit_refs, tuple)
            or not all(
                isinstance(value, str) and value for value in target_commit_refs
            )
            or len(target_commit_refs) != len(set(target_commit_refs))
        ):
            raise OwnerConflict("reasoning_target_evidence_closure_invalid")
        if not target_commit_refs:
            return ()
        if len(target_commit_refs) > 256:
            return tuple(
                leaf
                for offset in range(0, len(target_commit_refs), 256)
                for leaf in self.resolve_reasoning_target_evidence_leaves(
                    quest_ref=quest_ref, target_commit_refs=target_commit_refs[offset:offset + 256]
                )
            )
        catalog = self._catalog_for_refs(
            quest_ref=quest_ref, target_commit_refs=target_commit_refs, current_only=False
        )
        catalog_by_commit = {
            cast(str, value["target_commit_root_ref"]): value
            for value in catalog
        }
        commits = {
            commit.commit_ref: commit
            for commit in self._research_graph.query_target_commits_for_quest(
                quest_ref, target_commit_refs=target_commit_refs
            )
        }
        leaves: list[EvidenceReuseLeaf] = []
        for target_commit_ref in target_commit_refs:
            commit = commits.get(target_commit_ref)
            catalog_entry = catalog_by_commit.get(target_commit_ref)
            if commit is None or catalog_entry is None:
                raise OwnerConflict("reasoning_target_evidence_closure_invalid")
            asset_receipt = _accepted_receipt(
                catalog_entry.get("asset_receipt"),
                error_code="reasoning_target_evidence_closure_invalid",
            )
            role_receipt = _accepted_receipt(
                catalog_entry.get("role_receipt"),
                error_code="reasoning_target_evidence_closure_invalid",
            )
            if (
                asset_receipt.issuer != "research_memory"
                or asset_receipt.subject_ref
                != catalog_entry.get("asset_version_ref")
                or role_receipt.issuer != "research_graph"
                or role_receipt.subject_ref != catalog_entry.get("role_ref")
                or catalog_entry.get("integrity_receipt_ref")
                != asset_receipt.receipt_ref
                or catalog_entry.get("availability_receipt_ref")
                != asset_receipt.receipt_ref
                or catalog_entry.get("eligibility_token_ref")
                != role_receipt.receipt_ref
                or catalog_entry.get("currentness_receipt_ref")
                != role_receipt.receipt_ref
            ):
                raise OwnerConflict("reasoning_target_evidence_closure_invalid")
            leaves.extend(
                self._issuer_closed_role_leaves(
                    evidence_ref=cast(str, catalog_entry["evidence_ref"]),
                    catalog_entry=catalog_entry,
                    uses=[],
                    commit=commit,
                    catalog_asset_receipt=asset_receipt,
                    catalog_role_receipt=role_receipt,
                )
            )
        return tuple(leaves)

    def _issuer_closed_role_leaves(
        self,
        *,
        evidence_ref: str,
        catalog_entry: dict[str, object],
        uses: list[dict[str, object]],
        commit: TargetCommit,
        catalog_asset_receipt: AcceptanceReceipt,
        catalog_role_receipt: AcceptanceReceipt,
    ) -> tuple[EvidenceReuseLeaf, ...]:
        """Re-read RM/RG roles from the exact committed Attempt.

        Native TargetCommit v3 stores the exact result manifest and selected
        Attempt.  RG's public role query independently revalidates every RM
        AssetVersion receipt; the formal metric query independently revalidates
        the measurement receipt.  Root commits without per-role RG acceptance
        remain metric-only instead of manufacturing diagnostic role receipts.
        """

        error_code = "plan_evidence_reuse_closure_invalid"
        metric_document = _target_commit_metric_document(commit)
        metric_result_ref = metric_document.get("metric_result_ref")
        if (
            not isinstance(metric_result_ref, str)
            or not metric_result_ref
            or metric_document.get("evaluation_attempt_ref")
            != commit.evaluation_attempt_ref
        ):
            raise OwnerConflict(error_code)
        formal_receipt = _accepted_receipt(
            metric_document.get("receipt"), error_code=error_code
        )
        if (
            formal_receipt.issuer != "research_graph"
            or formal_receipt.kind != "formal_measurement_acceptance"
            or formal_receipt.subject_ref != commit.evaluation_attempt_ref
        ):
            raise OwnerConflict(error_code)

        accepted_measurement = commit.closure.get("accepted_measurement")
        variant_run_ref = (
            accepted_measurement.get("variant_run_ref")
            if isinstance(accepted_measurement, dict)
            else commit.target_run_ref
        )
        if not isinstance(variant_run_ref, str) or not variant_run_ref:
            raise OwnerConflict(error_code)

        common = {
            "evidence_ref": evidence_ref,
            "source_variant_run_ref": variant_run_ref,
            "source_evaluation_attempt_ref": commit.evaluation_attempt_ref,
            "target_commit_ref": commit.commit_ref,
            "evidence_catalog_entry_hash": canonical_hash(catalog_entry),
            "evidence_use_hashes": tuple(canonical_hash(use) for use in uses),
            "formal_measurement_acceptance_receipt": formal_receipt,
            "target_commit_acceptance_receipt": commit.receipt,
        }

        if commit.closure.get("schema_ref") != (
            "meta-research/target-commit-closure/v3"
        ):
            return (
                EvidenceReuseLeaf(
                    **common,
                    role="MetricResult",
                    evidence_item_ref=metric_result_ref,
                    source_role_ref=cast(str, catalog_entry["role_ref"]),
                    source_subject_kind="EvaluationAttempt",
                    source_subject_ref=commit.evaluation_attempt_ref,
                    asset_version_ref=cast(
                        str, catalog_entry["asset_version_ref"]
                    ),
                    evidence_asset_receipt=catalog_asset_receipt,
                    evidence_role_receipt=catalog_role_receipt,
                ),
            )

        metric = self._research_graph.query_target_formal_metric_result(
            commit.evaluation_attempt_ref
        )
        if metric is None or metric.as_public_dict() != metric_document:
            raise OwnerConflict(error_code)
        roles = self._research_graph.query_target_measurement_asset_roles(
            commit.evaluation_attempt_ref
        )
        manifest = commit.closure.get("result_manifest")
        manifest_entries = (
            manifest.get("entries") if isinstance(manifest, dict) else None
        )
        measurement_attempt = commit.closure.get("measurement_attempt")
        selected_checkpoints = (
            measurement_attempt.get("checkpoint_role_refs")
            if isinstance(measurement_attempt, dict)
            else None
        )
        if (
            not isinstance(manifest_entries, list)
            or not isinstance(selected_checkpoints, list)
            or metric.result_role_ref
            != (
                measurement_attempt.get("result_role_ref")
                if isinstance(measurement_attempt, dict)
                else None
            )
        ):
            raise OwnerConflict(error_code)

        result_role = next(
            (role for role in roles if role.role_ref == metric.result_role_ref),
            None,
        )
        if result_role is None or result_role.role != "result_content":
            raise OwnerConflict(error_code)
        _require_manifest_role_binding(
            manifest_entries, result_role, error_code=error_code
        )

        result: list[EvidenceReuseLeaf] = [
            EvidenceReuseLeaf(
                **common,
                role="MetricResult",
                evidence_item_ref=metric_result_ref,
                source_role_ref=result_role.role_ref,
                source_subject_kind="EvaluationAttempt",
                source_subject_ref=commit.evaluation_attempt_ref,
                asset_version_ref=result_role.binding.version_ref,
                evidence_asset_receipt=result_role.binding.receipt,
                evidence_role_receipt=result_role.receipt,
            )
        ]
        role_names = {
            "checkpoint_artifact": "CheckpointArtifact",
            "log_asset": "LogAsset",
            "analysis_asset": "AnalysisAsset",
        }
        diagnostic_roles = sorted(
            (role for role in roles if role.role in role_names),
            key=lambda role: (
                ("checkpoint_artifact", "log_asset", "analysis_asset").index(
                    role.role
                ),
                role.ordinal,
                role.role_ref,
            ),
        )
        for role in diagnostic_roles:
            if role.role == "checkpoint_artifact":
                if role.role_ref not in selected_checkpoints:
                    continue
                subject_kind = "VariantRun"
                if role.subject_ref != variant_run_ref:
                    raise OwnerConflict(error_code)
            else:
                _require_manifest_role_binding(
                    manifest_entries, role, error_code=error_code
                )
                subject_kind = "EvaluationAttempt"
                if role.subject_ref != commit.evaluation_attempt_ref:
                    raise OwnerConflict(error_code)
            result.append(
                EvidenceReuseLeaf(
                    **common,
                    role=cast(
                        str, role_names[role.role]
                    ),  # runtime value is closed above
                    evidence_item_ref=role.role_ref,
                    source_role_ref=role.role_ref,
                    source_subject_kind=cast(str, subject_kind),
                    source_subject_ref=role.subject_ref,
                    asset_version_ref=role.binding.version_ref,
                    evidence_asset_receipt=role.binding.receipt,
                    evidence_role_receipt=role.receipt,
                )
            )
        return tuple(result)


def _require_manifest_role_binding(
    manifest_entries: list[object],
    role: AcceptedExperimentAssetRole,
    *,
    error_code: str,
) -> None:
    """Match an RG role to the exact RM binding frozen in the commit manifest."""

    expected_receipt = {
        key: value
        for key, value in role.binding.receipt.as_public_dict().items()
        if key != "status"
    }
    matches = [
        entry
        for entry in manifest_entries
        if isinstance(entry, dict)
        and entry.get("role") == role.role
        and isinstance(entry.get("binding"), dict)
        and cast(dict[str, object], entry["binding"]).get("asset_ref")
        == role.binding.asset_ref
        and cast(dict[str, object], entry["binding"]).get("version_ref")
        == role.binding.version_ref
        and cast(dict[str, object], entry["binding"]).get("content_hash")
        == role.binding.content_hash
        and cast(dict[str, object], entry["binding"]).get("manifest_hash")
        == role.binding.manifest_hash
        and cast(dict[str, object], entry["binding"]).get("receipt")
        == expected_receipt
    ]
    if len(matches) != 1:
        raise OwnerConflict(error_code)


def _accepted_receipt(value: object, *, error_code: str) -> AcceptanceReceipt:
    fields = {
        "status",
        "issuer",
        "kind",
        "receipt_ref",
        "subject_ref",
        "payload_hash",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("status") != "accepted"
        or any(
            not isinstance(value.get(field), str) or not value[field]
            for field in fields - {"status"}
        )
    ):
        raise OwnerConflict(error_code)
    return AcceptanceReceipt(
        issuer=cast(str, value["issuer"]),
        kind=cast(str, value["kind"]),
        receipt_ref=cast(str, value["receipt_ref"]),
        subject_ref=cast(str, value["subject_ref"]),
        payload_hash=cast(str, value["payload_hash"]),
    )
