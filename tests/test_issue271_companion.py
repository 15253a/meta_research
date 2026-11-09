"""#271 at the public HC, provider adapter and workspace boundaries."""
import json
from dataclasses import replace

import pytest

from meta_research.companion import CodexCompanionAdapter
from meta_research.quest_drafting import IntentTurnRequest
from test_public_manual_question_lifecycle import (
    QUESTION, DeterministicDraftingAdapter, _accept_root_question, _build_runtime, _open_and_confirm_seed,
)
from test_quest_drafting_adapters import RecordingRunner


@pytest.mark.parametrize("branch", ("conversation", "quest_initialization", "manual_question_creation"))
def test_companion_explanation_contract_reaches_new_and_continued_turns(tmp_path, branch):
    """Observe actual adapter input; factual reply quality needs live acceptance."""
    conversation = branch == "conversation"
    output = {"reply": "Only the observed trial is available; the other device is untested."}
    if conversation:
        output["agent_proposal"] = None
    runner = RecordingRunner(output, thread_id="reader-root")
    adapter = CodexCompanionAdapter(tmp_path / "provider", process_runner=runner)
    request = IntentTurnRequest(initialization_id="quest:reader", draft_revision=4,
        draft_hash="a" * 64, draft={"interaction_kind": branch,
            "reviewed_text": "Keep the overall goal; change only the sampling interval."},
        message="What can we conclude?", native_session_ref=None,
        creation_context_kind="manual_question_creation" if branch == "manual_question_creation" else "quest_initialization")
    first = adapter.reply(request)
    second = adapter.reply(replace(request, native_session_ref=first.native_session_ref,
        message="Explain the technical uncertainty and prepare guidance for this trial only."))
    for _, prompt, _ in runner.calls:
        assert "对话说明默认面向不熟悉领域的用户" in prompt
        assert "技术追问再展开" in prompt
        assert "关键限制、准确来源和待确认全文" in prompt
        assert "Keep the overall goal; change only the sampling interval." in prompt
        assert "current_draft_revision=4" in prompt
        assert "current_draft_hash=" + "a" * 64 in prompt
    assert first.native_session_ref == second.native_session_ref == "reader-root"
    assert second.reply == output["reply"]
    assert second.agent_proposal is None
    if conversation:
        assert "Assume the human is seeking help" in runner.calls[0][1]
        assert "<!-- bundled resource: companion-work.md -->" not in runner.calls[1][1]
    else:
        assert "Explain findings and preparation choices in connected, everyday language" in runner.calls[0][1]


def test_quest_companion_continues_preparation_files_without_copy_or_cleanup(tmp_path):
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        quest_ref, _ = _accept_root_question(runtime, "continuity")
        quest = runtime.owners.research_graph.query_quest_by_ref(quest_ref)
        human = runtime.owners.human_collaboration
        creation = human.query_quest_creation(quest.initialization_id)
        prepared = runtime.root_workspaces.bind_initialization(quest.initialization_id, creation["intent_session"]["ref"])
        (prepared.directory / "prior-draft.txt").write_text("original preparation", encoding="utf-8")
        scope = "quest:" + quest_ref
        session = human.query_companion(scope)
        companion = runtime.root_workspaces.bind_companion_session(scope, session["session_ref"])
        assert companion.directory == prepared.directory
        assert companion.location.workspace_ref == prepared.location.workspace_ref
        assert companion.location.root_session_ref == session["session_ref"]
        human.start_new_companion_session(scope, "new-conversation")
        current = human.query_companion(scope)
        assert current["workspace_path"] == str(prepared.directory)
        assert (prepared.directory / "prior-draft.txt").read_text() == "original preparation"
        other_quest, _ = _accept_root_question(runtime, "other")
        other = human.query_companion("quest:" + other_quest)
        assert other["workspace_path"] != current["workspace_path"]
    finally:
        runtime.close()


def test_manual_drafting_supplies_exact_saved_proposal_identity_and_seed(tmp_path):
    provider = DeterministicDraftingAdapter()
    runtime = _build_runtime(tmp_path / "runtime", drafting=provider)
    try:
        quest_ref, parent_ref = _accept_root_question(runtime, "manual")
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(human, quest_ref=quest_ref, parent_question_ref=parent_ref, key_prefix="manual")
        waived = human.record_manual_deepfetch_waiver(seeded["context_ref"], expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"], idempotency_key="waive")
        saved = human.save_manual_question_proposal(seeded["context_ref"], content=QUESTION,
            expected_basis_hash=waived["research_path"]["basis_hash"], idempotency_key="proposal")
        human.send_manual_drafting_message(seeded["context_ref"], expected_basis_hash=waived["research_path"]["basis_hash"],
            message="Keep the Seed, refine the answer boundary.", idempotency_key="message")
        assert human.process_drafting_once()
        request = provider.intent_requests[-1]
        assert request.draft["confirmed_seed"] == seeded["seed"]["value"]
        assert request.draft["current_submitted_proposal"] == QUESTION
        assert request.draft["proposal_revision"] == saved["proposal"]["revision"]
        assert request.draft["proposal_ref"] == saved["proposal"]["ref"]
        assert request.draft["proposal_hash"] == saved["proposal"]["hash"]
        assert human.query_manual_question_creation(seeded["context_ref"])["confirmation"] is None
    finally:
        runtime.close()


def test_manual_adapter_loads_branch_method_and_supplied_drafting_schema(tmp_path):
    runner = RecordingRunner({"reply": "Edit the answer boundary only."}, thread_id="manual-root")
    adapter = CodexCompanionAdapter(tmp_path / "provider", process_runner=runner)
    request = IntentTurnRequest(initialization_id="init", draft_revision=2, draft_hash="a" * 64,
        draft={"confirmed_seed": {"intent": "original human intent"}, "current_submitted_proposal": QUESTION,
            "proposal_revision": 2, "proposal_hash": "b" * 64}, message="Limit to observed evidence.", native_session_ref=None,
        creation_context_kind="manual_question_creation", creation_context_ref="manual-context", context_generation=3)
    adapter.reply(request)
    prompt = runner.calls[0][1]
    assert "first-creation" in prompt
    assert "context-contract.md" in prompt
    assert "drafting_response_schema=" in prompt
    assert set(runner.schemas[0]["properties"]) == {"reply"}
    assert "original human intent" in prompt and "manual-context" in prompt
    assert "context_generation=3" in prompt
    adapter.reply(replace(request, native_session_ref="manual-root", message="Keep that evidence boundary."))
    continued = runner.calls[1][1]
    assert "first-creation" not in continued
    assert "context-contract.md" in continued
    assert "Keep that evidence boundary." in continued


def test_companion_can_select_an_authorized_other_quest_for_exact_read_only(tmp_path):
    from test_dataset_effect_scope_recovery import _scope
    from test_research_datasets import _asset
    from meta_research.research_content import research_content_operations
    from meta_research.semantic_mcp import SemanticMcpGateway
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        first, root = _accept_root_question(runtime, "selected")
        other, _ = _accept_root_question(runtime, "authorized")
        asset = _asset(runtime, content=b"Evidence from the other authorized Quest.")
        runtime.owners.research_graph.accept_asset_role(binding=asset, role="evidence", quest_ref=other, idempotency_key="source")
        scope = _scope(runtime, quest_ref=first, run_ref=first, root_ref=root)
        gateway = SemanticMcpGateway(research_content_operations(research_graph=runtime.owners.research_graph,
            research_memory=runtime.owners.research_memory, agent_runtime=runtime.owners.agent_runtime,
            human_collaboration=runtime.owners.human_collaboration))
        token, _ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"], root_session_ref=scope["root_session_ref"],
            fence_ref=scope["fence_ref"], capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
            operation_ids=("research_memory.content.read", "research_graph.questions.page", "research_memory.assets.page"))
        def call(name, **args):
            _, response = gateway.dispatch(token.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": args}})
            return response["result"]
        assert call("research_memory.content.read", source_ref=asset.version_ref, version_ref=asset.version_ref).get("isError")
        read = call("research_memory.content.read", source_ref=asset.version_ref, version_ref=asset.version_ref, source_quest_ref=other)
        assert not read.get("isError"), read
        assert read["structuredContent"]["text"] == "Evidence from the other authorized Quest."
        assert not call("research_graph.questions.page", source_quest_ref=other).get("isError")
        quests = call("research_graph.questions.page", entry_kind="quests")
        assert not quests.get("isError"), quests
        assert {item["quest_ref"] for item in quests["structuredContent"]["items"]} == {first, other}
        assets = call("research_memory.assets.page", source_quest_ref=other)
        assert not assets.get("isError"), assets
        assert assets["structuredContent"]["items"][0]["asset"]["version_ref"] == asset.version_ref
        denied = call("research_memory.content.read", source_ref=asset.version_ref, version_ref=asset.version_ref, source_quest_ref="unconfirmed-quest")
        assert denied.get("isError") and "content_quest_not_authorized" in str(denied)
        assert call("research_memory.assets.retire", effect_id="unauthorized-write", version_ref=asset.version_ref).get("isError")
    finally:
        runtime.close()


def test_companion_download_checks_exact_file_and_hidden_paths(tmp_path):
    import hashlib
    import pytest
    from meta_research.semantic_mcp import SemanticMcpError
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        quest, _ = _accept_root_question(runtime, "download")
        scope = "quest:" + quest
        session = runtime.owners.human_collaboration.query_companion(scope)
        binding = runtime.root_workspaces.bind_companion_session(scope, session["session_ref"])
        content = b"editable document bytes" * 4000
        (binding.directory / "report.docx").write_bytes(content)
        arguments = dict(workspace_ref=binding.location.workspace_ref, path="report.docx", expected_sha256=hashlib.sha256(content).hexdigest())
        downloaded = runtime.root_workspaces.download_companion_session(scope, session["session_ref"], **arguments)
        assert downloaded["content"] == content and downloaded["total_bytes"] > 65536
        (binding.directory / "report.docx").write_bytes(b"new revision")
        with pytest.raises(SemanticMcpError, match="workspace_content_changed"):
            runtime.root_workspaces.download_companion_session(scope, session["session_ref"], **arguments)
        for path in ("../secret", ".creation-context/basis.json", "folder/.delivery.json"):
            with pytest.raises(SemanticMcpError):
                runtime.root_workspaces.download_companion_session(scope, session["session_ref"], **{**arguments, "path": path})
    finally:
        runtime.close()


def test_all_asset_entry_readers_share_authorized_quest_selection(tmp_path):
    from test_dataset_effect_scope_recovery import _scope
    from test_research_datasets import _asset, _dataset, _version
    from test_root_workspace import _channel, _call, _accepted, _external_context
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        first, first_question = _accept_root_question(runtime, "reader-selected")
        other, other_question = _accept_root_question(runtime, "reader-other")
        graph = runtime.owners.research_graph
        asset = _asset(runtime, content=b"Other Quest dataset bytes")
        graph.accept_asset_role(binding=asset, role="evidence", quest_ref=other, idempotency_key="reader-source")
        dataset = _dataset(graph)
        version = _version(graph, dataset, asset)
        graph.reference_dataset(dataset_version_ref=version["dataset_version_ref"], question_ref=other_question,
            idempotency_key="reader-reference")
        environment = graph.register_environment(semantic_key="reader-environment", name="Microscope", meaning="Calibrated microscope",
            source="Room B", quest_ref=other, idempotency_key="reader-environment")
        scope = _scope(runtime, quest_ref=first, run_ref=first, root_ref=first_question)
        channel = _channel(runtime, _external_context(scope))
        cases = (
            ("research_graph.datasets.page", {}),
            ("research_graph.datasets.read", {"dataset_version_ref": version["dataset_version_ref"]}),
            ("research_graph.environments.page", {}),
            ("research_graph.environments.read", {"environment_ref": environment["environment_ref"]}),
            ("research_graph.baselines.page", {}),
            ("research_graph.baselines.read", {"baseline_ref": "baseline-not-created"}),
            ("research_graph.formal_results.read", {"ref": "variant_run_not_created"}),
            ("research_graph.question_history.read", {"question_ref": other_question}),
            ("research_graph.question_relations.read", {"question_ref": other_question}),
            ("human_request.read", {}),
        )
        for operation, arguments in cases:
            value = _accepted(_call(runtime, channel, operation, source_quest_ref=other, **arguments))
            assert value is not None, operation
            denied = _call(runtime, channel, operation, source_quest_ref="no-current-authority", **arguments)
            assert denied.get("isError") and "content_quest_not_authorized" in str(denied), (operation, denied)
        exact = _accepted(_call(runtime, channel, "research_graph.datasets.read",
            source_quest_ref=other, dataset_version_ref=version["dataset_version_ref"]))
        assert exact["result"]["dataset_version_ref"] == version["dataset_version_ref"]
    finally:
        runtime.close()


def test_companion_reads_another_authorized_quests_accepted_formal_lineage(tmp_path):
    from test_formal_run_snapshots import _scenario
    from test_stage_resource_handoff import _accept
    from test_dataset_effect_scope_recovery import _scope
    from test_root_workspace import _channel, _call, _accepted, _external_context
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path)
    try:
        _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        graph = runtime.owners.research_graph.query_target_graph(
            runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"])
        source_quest = graph.quest_ref
        scope = _scope(runtime, quest_ref="selected-workspace-quest", run_ref="comparison-work", root_ref="comparison-root")
        channel = _channel(runtime, _external_context(scope))
        arguments = dict(target_ref=handle.target_ref, source_quest_ref=source_quest)
        facts = _accepted(_call(runtime, channel, "research_graph.target_formal_results.read", **arguments))
        first = next(item for item in facts["items"] if item["run_key"] == "first")
        exact = _accepted(_call(runtime, channel, "research_graph.formal_results.read",
            ref=first["variant_run_ref"], source_quest_ref=source_quest))
        assert exact["result"]["target_ref"] == handle.target_ref
        assert exact["result"]["variant_run"] == first["variant_run"]
        assert _call(runtime, channel, "research_graph.target_formal_results.read",
            target_ref=handle.target_ref).get("isError")
    finally:
        runtime.close()
