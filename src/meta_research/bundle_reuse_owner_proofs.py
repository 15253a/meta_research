"""Production Owner-backed verifier for fixed Bundle reuse proofs.

The Bundle proof dataclasses intentionally contain issuer-neutral projections.
The boolean fields are only transport shape; this adapter establishes truth by
querying the durable RM/RG issuer records and their current anchor facts.
"""

from __future__ import annotations

from typing import Protocol

from meta_research.bundle_protocol import ContentBindingProof, ReceiptProof
from meta_research.owners.common import OwnerConflict


class ReuseContentOwner(Protocol):
    def verify_reuse_source_version(self, **values) -> None: ...

    def verify_implementation_content(self, **values) -> None: ...


class ReuseEligibilityOwner(Protocol):
    def verify_reuse_eligibility(self, **values) -> None: ...


class BundleTargetCandidateOwnerProofVerifier:
    """Verify every Target candidate proof against its actual issuing Owner."""

    def __init__(
        self,
        research_memory: ReuseContentOwner,
        research_graph: ReuseEligibilityOwner,
    ) -> None:
        self._research_memory = research_memory
        self._research_graph = research_graph

    def verify_reuse_source_receipt(
        self,
        *,
        tier: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        license_ref: str | None,
        source_content_hash_ref: str | None,
        patch_ref: str | None,
        receipt: ReceiptProof,
    ) -> None:
        _require_current_receipt(
            receipt,
            expected_subject_ref=exact_version_ref,
            code="reuse_source_version_receipt_invalid",
        )
        self._research_memory.verify_reuse_source_version(
            tier=tier,
            source_ref=source_ref,
            exact_version_ref=exact_version_ref,
            implementation_revision_ref=implementation_revision_ref,
            license_ref=license_ref,
            source_content_hash_ref=source_content_hash_ref,
            patch_ref=patch_ref,
            receipt_ref=receipt.receipt_ref,
            receipt_subject_ref=receipt.subject_ref,
        )

    def verify_reuse_content_receipt(
        self,
        *,
        tier: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        license_ref: str | None,
        source_content_hash_ref: str | None,
        patch_ref: str | None,
        binding: ContentBindingProof,
        receipt: ReceiptProof,
    ) -> None:
        if binding.subject_ref != implementation_revision_ref:
            raise OwnerConflict("implementation_content_binding_invalid")
        _require_current_receipt(
            receipt,
            expected_subject_ref=binding.content_hash_ref,
            code="implementation_content_receipt_invalid",
        )
        self._research_memory.verify_implementation_content(
            source_ref=source_ref,
            exact_version_ref=exact_version_ref,
            implementation_revision_ref=implementation_revision_ref,
            license_ref=license_ref,
            source_content_hash_ref=source_content_hash_ref,
            patch_ref=patch_ref,
            content_hash_ref=binding.content_hash_ref,
            receipt_ref=receipt.receipt_ref,
            receipt_subject_ref=receipt.subject_ref,
        )

    def verify_reuse_eligibility_receipt(
        self,
        *,
        tier: str,
        source_ref: str,
        exact_version_ref: str,
        implementation_revision_ref: str,
        implementation_content_hash_ref: str,
        eligibility_anchor_ref: str,
        binding: ContentBindingProof,
        receipt: ReceiptProof,
    ) -> None:
        if tier not in {
            "accepted-local",
            "related-history",
            "global-baseline-pool",
        }:
            raise OwnerConflict("reuse_eligibility_tier_invalid")
        _require_current_receipt(
            receipt,
            expected_subject_ref=binding.content_hash_ref,
            code="reuse_eligibility_receipt_invalid",
        )
        self._research_graph.verify_reuse_eligibility(
            tier=tier,
            source_ref=source_ref,
            exact_version_ref=exact_version_ref,
            implementation_revision_ref=implementation_revision_ref,
            implementation_content_hash_ref=implementation_content_hash_ref,
            eligibility_anchor_ref=eligibility_anchor_ref,
            eligibility_ref=binding.subject_ref,
            eligibility_content_hash_ref=binding.content_hash_ref,
            receipt_ref=receipt.receipt_ref,
            receipt_subject_ref=receipt.subject_ref,
        )


def _require_current_receipt(
    receipt: ReceiptProof,
    *,
    expected_subject_ref: str,
    code: str,
) -> None:
    if (
        not isinstance(receipt, ReceiptProof)
        or not receipt.verified
        or not receipt.currentness_known
        or not receipt.current
        or receipt.subject_ref != expected_subject_ref
    ):
        raise OwnerConflict(code)
