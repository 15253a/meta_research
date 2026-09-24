"""Long-lived Quests retain precise assets beyond a context discovery page.

Use real RM intake and RG receipt validation in fresh temporary data roots. The
shared fixture substitutes only Quest drafting and the host compute observation;
it does not substitute asset custody, role acceptance, or evidence authority.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from test_public_research_asset_roles import _accepted_quest, _runtime


@pytest.mark.parametrize("role", ["evidence", "quest_source_material"])
def test_quest_accepts_101_precise_assets_without_weakening_role_proof(
    tmp_path: Path, role: str,
) -> None:
    runtime = _runtime(tmp_path / role)
    try:
        quest = _accepted_quest(runtime)
        memory = runtime.owners.research_memory
        graph = runtime.owners.research_graph
        accepted = []
        versions = set()
        for index in range(101):
            intake = memory.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="text",
                    custody_mode="managed",
                    display_name=f"observation-{index}.txt",
                    content=f"{role}: distinct observation {index}\n".encode(),
                ),
                idempotency_key=f"growth-intake-{index}",
            )
            assert intake.asset is not None
            binding = intake.asset.as_binding()
            idempotency_key = f"growth-role-{index}"
            result = graph.accept_asset_role(
                binding=binding,
                role=role,
                quest_ref=quest.quest_ref,
                idempotency_key=idempotency_key,
            )
            assert result.role == role
            assert result.version_ref == binding.version_ref
            assert result.receipt.issuer == "research_graph"
            versions.add(binding.version_ref)
            accepted.append(result)

        assert len(versions) == len(accepted) == 101
        assert graph.accept_asset_role(
            binding=binding,
            role=role,
            quest_ref=quest.quest_ref,
            idempotency_key=idempotency_key,
        ) == accepted[-1]
        assert graph.accept_asset_role(
            binding=binding,
            role=role,
            quest_ref=quest.quest_ref,
            idempotency_key="growth-role-semantic-replay",
        ) == accepted[-1]

        with pytest.raises(OwnerConflict, match="asset_receipt_invalid"):
            graph.accept_asset_role(
                binding=replace(binding, content_hash="f" * 64),
                role=role,
                quest_ref=quest.quest_ref,
                idempotency_key="growth-role-forged-receipt",
            )

        if role == "evidence":
            seen = set()
            offset = 0
            page_sizes = []
            while True:
                page, refs = graph.query_evidence_reference_page(
                    quest.quest_ref, offset=offset, limit=32,
                )
                assert page["total_count"] == 101
                assert page["shown_count"] == len(refs)
                assert page["offset"] == offset
                assert page["references_hash"] == canonical_hash(list(refs))
                assert not seen.intersection(refs)
                seen.update(refs)
                page_sizes.append(len(refs))
                if page["next_offset"] is None:
                    break
                assert page["next_offset"] == offset + len(refs)
                offset = page["next_offset"]
            assert page_sizes == [32, 32, 32, 5]
            assert seen == versions
    finally:
        runtime.close()
