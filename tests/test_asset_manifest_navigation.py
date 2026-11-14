"""Accepted Target content stays discoverable through the shared asset directory."""
from types import SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.research_content import read_content
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_root_formal_entities import _accept
from test_stage_asset_intake import channel
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def test_asset_directory_finds_exact_accepted_target_implementation(tmp_path):
    runtime, lifecycle, memory, authority, handle, _, evidence = _root_finalizer_fixture(tmp_path)
    try:
        graph = runtime.owners.research_graph
        quest = graph.query_target_graph(authority.stage_request_ref).quest_ref
        _, call = channel(runtime, SimpleNamespace(quest_ref=quest, question_ref="asset-directory-root"))
        TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=graph).finalize(
                handle=handle, evidence=evidence)
        pending = call("research_memory.assets.page", query="target-root-artifact-0000")
        assert not pending.get("isError"), pending
        assert pending["structuredContent"]["items"] == []
        _, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        implementation = next(entry for entry in manifest.entries if entry.role == "implementation")
        binding = implementation.binding
        content = read_content(graph, runtime.owners.research_memory, quest_ref=quest,
                               source_ref=binding.version_ref, version_ref=binding.version_ref)
        assert content["content_hash"] == binding.content_hash
        response = call("research_memory.assets.page", query="target-root-artifact-0000", limit=50)
        assert not response.get("isError"), response
        page = response["structuredContent"]
        assert page["next_offset"] is None
        assert binding.version_ref in [item["asset"]["version_ref"] for item in page["items"]], (
            "The exact accepted Target code is readable in this Quest but absent from its asset directory."
        )
        selected = page["items"][0]
        reader = selected["reader"]
        exact = call(reader["operation"], **reader["args"])
        assert not exact.get("isError"), exact
        assert exact["structuredContent"]["content_hash"] == binding.content_hash

        # Only the original version belongs to this accepted Target. A newer
        # version in the same RM family must not replace it in discovery.
        asset_memory = runtime.owners.research_memory
        status = asset_memory.query_asset_status(binding.asset_ref)
        newer = asset_memory.submit_asset_intake(AssetIntakeRequest(
            source_kind="text", custody_mode="managed", display_name="unshared-updated-code.txt",
            content=b"A later version outside the Target's accepted snapshot.", asset_ref=binding.asset_ref,
            change={"kind": "supplement", "predecessor_version_ref": binding.version_ref,
                    "expected_revision": status["revision"], "explanation": "A separate later revision."}),
            idempotency_key="unshared-code-version")
        assert newer.asset is not None, newer
        with pytest.raises(OwnerConflict, match="asset_quest_scope_invalid"):
            graph.verify_asset_quest_scope(newer.asset.version_ref, quest_ref=quest)
        response = call("research_memory.assets.page", query="target-root-artifact-0000", limit=1)
        assert not response.get("isError"), response
        selected = response["structuredContent"]["items"][0]
        assert selected["asset"]["version_ref"] == binding.version_ref
        assert selected["lifecycle"]["version_ref"] == binding.version_ref
        assert selected["lifecycle"]["state"] == "superseded"
        hidden = call("research_memory.assets.page", query="unshared-updated-code")
        assert not hidden.get("isError"), hidden
        assert hidden["structuredContent"]["items"] == []

        refs, offset = [], 0
        while True:
            response = call("research_memory.assets.page", offset=offset, limit=1)
            assert not response.get("isError"), response
            page = response["structuredContent"]
            refs.extend(item["asset"]["asset_ref"] for item in page["items"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        assert binding.asset_ref in refs and len(refs) == len(set(refs))
        _, foreign = channel(runtime, SimpleNamespace(quest_ref="foreign-asset-quest", question_ref="foreign-root"))
        assert foreign("research_memory.assets.page")["structuredContent"]["items"] == []

        # A separately accepted family role deliberately grants the existing
        # broader family scope. Its directory entry remains unique and current.
        graph.accept_asset_role(binding=binding, role="evidence", quest_ref=quest,
                                idempotency_key="explicit-code-family")
        response = call("research_memory.assets.page", query="unshared-updated-code")
        assert not response.get("isError"), response
        entries = response["structuredContent"]["items"]
        assert len(entries) == 1
        assert entries[0]["asset"]["version_ref"] == newer.asset.version_ref
    finally:
        runtime.close()
