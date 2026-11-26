"""Targeted verification for host-direct creation, lightweight understanding, full path passthrough, and live activity backend."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from meta_research.companion import CodexCompanionAdapter
from meta_research.companion_native_handoff import ensure_companion_native_handoff
from meta_research.creation_basis import (
    InitializationUnderstandingRequest,
    InitializationUnderstandingResult,
    empty_manifest,
    empty_understanding,
    validate_reference_understanding,
)
from meta_research.creation_inputs import CreationAnchor, CreationInputIdentity, MaterialSet
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.native_codex_bridge import _exec_item
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.proposal_output import read_preparation_output, read_proposal_output
from meta_research.provider_supervisor import ensure_transport_key, write_transport_envelope
from meta_research.root_capabilities import root_capability_profile
from meta_research.runtime_conditions import resume_runtime_conditions
from meta_research.web import create_app
from meta_research.work_material_contract import CREATION_MATERIAL_OPERATION_IDS
from test_creation_material_consumption import _call, _open, _value
from test_public_plan_stage import _confirm_direct_quest
from test_root_workspace import _accepted, _channel, _context, _external_context, _make, _scope


def test_pillar1_host_direct_creation_without_chroot_or_runtime_config(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "note.txt").write_text("calibration 16\n", encoding="utf-8")
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, reference = _open(client, runtime, source, "host-direct")
            human, roots = runtime.owners.human_collaboration, runtime.root_workspaces
            # Must NOT require configure_creation_runtime or geteuid()==0 or ProtectedCreationRuntime.
            roots._creation_runtime_configuration = None
            inputs = human.creation_material_snapshot(anchor)
            work = roots.protected_creation(binding, inputs, operation_ref="host-direct-op")
            assert work.runtime is None
            assert work.execution_binding["runtime_contract"] == "host-direct-v1"
            assert work.work_directory.is_dir()
            assert work.execution_binding["work_directory"] == str(work.work_directory)
            assert not work.execution_binding["work_directory"].startswith("/workspace/")
            # Copy returns real host working_path, never /workspace/...
            access = work.channel()

            class Connection:
                token = access.token

            gateway = runtime.harnesses._gateway
            listing = _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[0], reference_ref=reference["reference_ref"]))
            entry = next(item for item in listing["entries"] if item["name"] == "note.txt")
            receipt = _value(_call(
                gateway,
                Connection(),
                CREATION_MATERIAL_OPERATION_IDS[2],
                effect_id="copy-host",
                reference_ref=reference["reference_ref"],
                path="note.txt",
                observation_ref=entry["observation"]["observation_ref"],
                max_bytes=1024,
            ))
            assert receipt["working_path"] == str(work.work_directory / receipt["work_file"]["path"])
            assert not receipt["working_path"].startswith("/workspace/")
            assert Path(receipt["working_path"]).read_text(encoding="utf-8") == "calibration 16\n"
            work.completed_handoff()
            identity = work.seal()
            assert len(identity.consumed) == 1
    finally:
        runtime.close()


def test_pillar1_companion_uses_unified_host_invoke_and_open_prompt(tmp_path):
    source = tmp_path / "dataset"
    source.mkdir()
    (source / "readme.md").write_text("Experiment notes.", encoding="utf-8")
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, reference = _open(client, runtime, source, "host-invoke")
            human, roots = runtime.owners.human_collaboration, runtime.root_workspaces
            adapter = CodexCompanionAdapter(tmp_path / "adapter")
            adapter.bind_workspaces(roots)
            captured = {}

            def fake_invoke(**kwargs):
                captured.update(kwargs)
                return {
                    "material_composition": [],
                    "work_already_done": [],
                    "claims_and_conditions": [{
                        "ref": "c1",
                        "text": "Experiment notes present.",
                        "kind": "reported_work",
                        "conditions": ["Host direct check"],
                        "sources": [{"witness_ref": reference["reference_ref"], "location": "readme.md:1"}],
                    }],
                    "conflicts": [],
                    "gaps": [],
                    "unfinished_questions": [],
                    "coverage": [{
                        "material_key": reference["reference_ref"],
                        "kind": "partial",
                        "read_ranges": [{"witness_ref": reference["reference_ref"], "location": "readme.md:1"}],
                        "unread_description": "Other files unread.",
                    }],
                    "selection": [],
                }, "native-host-1", ""

            adapter._invoke = fake_invoke
            inputs = human.creation_material_snapshot(anchor)
            request = InitializationUnderstandingRequest(
                anchor.ref, anchor.draft_revision, anchor.draft_hash,
                {"goal": "Test host direct understanding"}, empty_manifest(),
                "host-invoke:understanding", binding.location.root_session_ref, None, inputs=inputs,
            )
            result = adapter.understand_initialization(request)
            # Pillar 1: unified host durable path (invocation_runner is None, no ProtectedCreationRunner)
            assert captured["invocation_runner"] is None
            assert captured["provider_execution_binding"]["runtime_contract"] == "host-direct-v1"
            prompt = captured["prompt"]
            assert "unreadable external originals" not in prompt.lower()
            # Pillar 2: lightweight framing, selection=[], direct MCP operation IDs, open host tools
            assert "selection" in prompt and "[]" in prompt
            assert "research_workspace.materials.discover" in prompt
            assert "research_workspace.materials.read" in prompt
            assert str(source) in prompt
            # Pillar 2: accept host-direct citations with selection == []
            memory = runtime.owners.research_memory.creation_bases
            basis = memory.accept_reference_prepared(request, result)
            assert basis["understanding"]["selection"] == []
            view = memory.understanding_view(basis)
            assert view["claims_and_conditions"][0]["sources"][0]["provenance"]["kind"] == "host_direct"
    finally:
        runtime.close()


def test_pillar2_dual_citations_accept_host_direct_and_reject_forged(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "note.txt").write_text("hello world\n", encoding="utf-8")
    single = tmp_path / "single.txt"
    single.write_text("single file content\n", encoding="utf-8")

    runtime = _make(tmp_path / "runtime")
    try:
        server_files = runtime.root_workspaces.server_files
        anchor = CreationAnchor("quest_initialization", "init_test", None, 1, "a" * 64)
        dir_ref = {
            "reference_ref": "material:dir1",
            "submission_ref": "sub:1",
            "source": server_files.inspect(str(directory), description="dir"),
            "description": "dir",
        }
        file_ref = {
            "reference_ref": "material:file1",
            "submission_ref": "sub:1",
            "source": server_files.inspect(str(single), description="file"),
            "description": "file",
        }
        inputs = MaterialSet(anchor, (dir_ref, file_ref))
        identity = CreationInputIdentity(anchor, inputs.set_hash, ())

        # Accept exact reference_ref, host:<reference_ref>, and nested absolute path.
        valid = empty_understanding()
        valid["coverage"] = [
            {
                "material_key": "material:dir1",
                "kind": "partial",
                "read_ranges": [{"witness_ref": str(directory / "note.txt"), "location": "note.txt:1"}],
                "unread_description": "Only inspected note.txt.",
            },
            {
                "material_key": "material:file1",
                "kind": "read",
                "read_ranges": [{"witness_ref": "host:material:file1", "location": "single.txt:1"}],
                "unread_description": "",
            },
        ]
        valid["claims_and_conditions"] = [
            {
                "ref": "claim-dir",
                "text": "Directory note says hello.",
                "kind": "reported_work",
                "conditions": ["Host direct"],
                "sources": [{"witness_ref": "material:dir1", "location": "note.txt:1"}],
            },
            {
                "ref": "claim-file",
                "text": "Single file says content.",
                "kind": "reported_work",
                "conditions": ["Host direct"],
                "sources": [{"witness_ref": "host:material:file1", "location": "single.txt:1"}],
            },
        ]
        assert validate_reference_understanding(valid, inputs, identity, server_files) == valid

        # Reject forged / outside witness_ref values.
        for forged in ("fake-witness", "host:material:unknown", "/etc/passwd", str(tmp_path / "outside.txt")):
            bad = empty_understanding()
            bad["coverage"] = [
                {
                    "material_key": "material:dir1",
                    "kind": "partial",
                    "read_ranges": [{"witness_ref": forged, "location": "x:1"}],
                    "unread_description": "partial",
                },
                {
                    "material_key": "material:file1",
                    "kind": "unread",
                    "read_ranges": [],
                    "unread_description": "unread",
                },
            ]
            with pytest.raises(OwnerConflict, match="creation_understanding_invalid"):
                validate_reference_understanding(bad, inputs, identity, server_files)
    finally:
        runtime.close()


def test_pillar2_multi_agent_remains_enabled():
    for kind in ("companion", "idea", "plan", "bundle", "target", "reasoning", "deepfetch"):
        profile = root_capability_profile(kind)
        assert profile.multi_agent_enabled is True
        assert ("--enable", "multi_agent") in tuple(zip(profile.codex_arguments(), profile.codex_arguments()[1:]))


def test_pillar2_mounted_materials_passthrough_and_resume_updates(tmp_path):
    source = tmp_path / "mounted-dir"
    source.mkdir()
    (source / "a.txt").write_text("data", encoding="utf-8")
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, reference = _open(client, runtime, source, "passthrough")
            prompt = runtime.root_workspaces.native_directory_prompt(binding.location)
            assert "Mounted server materials" in prompt
            assert str(source) in prompt
            assert reference["reference_ref"] in prompt

            # Resume runtime conditions surfaces newly mounted material references.
            before = prompt
            second = tmp_path / "added-file.txt"
            second.write_text("extra", encoding="utf-8")
            from test_public_human_reply_delivery import _login
            from test_public_work_materials import _source
            headers = {**_login(client, runtime), "Idempotency-Key": "add-second"}
            resp = client.post(
                f"/api/v1/quest-initializations/{anchor.ref}/material-references",
                headers=headers,
                json={
                    "receiver": runtime.owners.human_collaboration.material_receiver("creation", anchor.ref),
                    "selections": [_source(client, second)],
                    "description": "Added later.",
                },
            )
            assert resp.status_code == 201
            after = runtime.root_workspaces.native_directory_prompt(binding.location)
            delta = resume_runtime_conditions(after, (before,), ordered=True)
            assert str(second) in delta
    finally:
        runtime.close()


def test_pillar2_quest_directory_references_includes_acquired_and_deepfetch_scope_has_material_refs(tmp_path):
    from test_public_first_question_deepfetch import (
        DeterministicDeepFetchProvider,
        DeterministicProbe,
        RecordingAcquisitionProvider,
        _authenticate,
        _open_and_queue_deepfetch,
        _write_headers,
    )
    from test_public_work_materials import _source
    from meta_research.composition import build_production_runtime
    from meta_research.paths import prepare_data_root

    source = tmp_path / "existing-repo"
    source.mkdir()
    (source / "notes.md").write_text("baseline=0.72\n", encoding="utf-8")

    class SelectionEmptyDrafter(CodexCompanionAdapter):
        def understand_initialization(self, request):
            work = self._workspaces.protected_creation(self._creation_workspace(request), request.inputs, operation_ref=request.job_ref)
            work.completed_handoff()
            identity = work.seal()
            ref = request.inputs.references[0]["reference_ref"]
            understanding = empty_understanding()
            understanding["coverage"] = [{
                "material_key": ref,
                "kind": "partial",
                "read_ranges": [{"witness_ref": ref, "location": "notes.md:1"}],
                "unread_description": "Rest unread.",
            }]
            understanding["claims_and_conditions"] = [{
                "ref": "b1",
                "text": "Baseline is 0.72.",
                "kind": "reported_work",
                "conditions": ["Provided repo"],
                "sources": [{"witness_ref": ref, "location": "notes.md:1"}],
            }]
            return InitializationUnderstandingResult(understanding, "native-df-1", identity, work)

    data_root = prepare_data_root(tmp_path / "data")
    drafter = SelectionEmptyDrafter(data_root.root / "companion-provider")
    provider = DeterministicDeepFetchProvider()
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=drafter,
        deepfetch_provider=provider,
        host_compute_probe=DeterministicProbe(),
        acquisition_provider=RecordingAcquisitionProvider(),
    )
    client, headers = _authenticate(runtime)
    try:
        init_id, queued = _open_and_queue_deepfetch(client, headers, key_prefix="df-refs")
        human = runtime.owners.human_collaboration
        resp = client.post(
            f"/api/v1/quest-initializations/{init_id}/material-references",
            headers=_write_headers(headers, "df-mat"),
            json={
                "receiver": human.material_receiver("creation", init_id),
                "selections": [_source(client, source)],
                "description": "Repo reference.",
            },
        )
        assert resp.status_code == 201
        req = human.query_deepfetch_request(queued["deepfetch"]["request_ref"])
        prepared = human.prepare_deepfetch_creation_basis(req)
        # Even though selection == [] (so accepted_material_bindings is empty),
        # material_references must be passed into DeepFetch scope!
        assert prepared.accepted_material_bindings == ()
        refs = prepared.scope.get("material_references")
        assert isinstance(refs, list) and len(refs) == 1
        assert refs[0]["absolute_path"] == str(source)
    finally:
        client.close()
        runtime.close()


def test_pillar3_reasoning_summary_flag_bridge_and_proposal_output_timestamps(tmp_path):
    # 1. codex exec argv includes model_reasoning_summary="concise"
    captured = []

    def fake_runner(argv, prompt, timeout):
        captured.append(list(argv))
        out = Path(argv[argv.index("--output-last-message") + 1])
        out.write_text('{"reply":"ok"}', encoding="utf-8")
        import subprocess
        return subprocess.CompletedProcess(argv, 0, '{"type":"thread.started","thread_id":"t-1"}\n{"type":"turn.completed"}\n', "")

    adapter = CodexIdeaSkillAdapter(tmp_path / "idea-transport", process_runner=fake_runner)
    adapter._invoke(
        operation_name="primary",
        prompt="hello",
        schema={"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"], "additionalProperties": False},
        native_session_ref=None,
        job_ref=None,
    )
    assert captured
    argv = captured[0]
    configs = [argv[i + 1] for i, v in enumerate(argv[:-1]) if v == "--config"]
    assert 'model_reasoning_summary="concise"' in configs

    # 2. native_codex_bridge translates reasoning items
    translated = _exec_item({"type": "reasoning", "id": "rs-1", "summary": ["Inspecting repo layout"]})
    assert translated is not None
    assert translated["type"] == "reasoning"
    assert translated["summary"] == ["Inspecting repo layout"]

    # 3. proposal_output includes read-only started_at and last_event_at without mutating stdout.jsonl
    data_root = tmp_path / "data"
    workspace = data_root / "companion-provider"
    _, key = ensure_transport_key(workspace)
    job_ref = "gen-ts:proposal:understanding"
    directory = workspace / "provider-operations" / canonical_hash({"job_ref": job_ref}) / "initialization-understanding"
    directory.mkdir(parents=True)
    write_transport_envelope(
        directory / "invocation.json",
        {"schema_ref": "meta-research/codex-provider-operation/v3", "job_ref": job_ref, "operation_name": "initialization-understanding"},
        key,
    )
    raw = b'{"type":"turn.started"}\n'
    (directory / "stdout.jsonl").write_bytes(raw)
    page = read_proposal_output(data_root, {"ref": "gen-ts", "status": "running", "failure": None, "started_at": 1700000000.0}, 0, 65536)
    assert page["started_at"] == 1700000000.0
    assert isinstance(page["last_event_at"], float)
    assert (directory / "stdout.jsonl").read_bytes() == raw

    prep = read_preparation_output(
        data_root,
        {"request_ref": "gen-ts:proposal", "status": "running", "failure": None, "preparation": {"ref": "gen-ts:proposal", "started_at": 1700000005.0}},
        0,
        65536,
    )
    assert prep["started_at"] == 1700000005.0
    assert isinstance(prep["last_event_at"], float)


def test_pillar2_creation_basis_propagates_across_plan_bundle_reasoning_and_successor(tmp_path):
    from meta_research.bundle_contract import BUNDLE_CONTEXT_PACK_SCHEMA_REF, validate_bundle_context_pack
    from meta_research.plan_contract import PLAN_CONTEXT_PACK_SCHEMA_REF, validate_plan_context_pack
    from test_public_plan_stage import _DeterministicPlanSkill, _DeterministicIdeaSkill
    from test_root_workspace import _finish_idea_stage

    # Part A: Plan -> Bundle propagation
    runtime = _make(tmp_path / "runtime-pb", idea=_DeterministicIdeaSkill(), plan=_DeterministicPlanSkill(no_gap=False))
    try:
        created = _confirm_direct_quest(runtime)
        quest_ref = created["quest_ref"]
        question_ref = created["question_ref"]
        question = runtime.owners.research_graph.query_question_by_ref(question_ref)
        memory = runtime.owners.research_memory.creation_bases
        req = InitializationUnderstandingRequest(
            created["initialization_id"],
            created["quest_draft"]["revision"],
            created["quest_draft"]["hash"],
            created["quest_draft"]["value"],
            empty_manifest(),
            "basis-empty-sel-pb",
            created["intent_session"]["ref"],
            None,
        )
        basis = memory.accept_prepared(req, empty_understanding())
        expected_ref = memory.reference(basis)
        memory.associate_question(question.as_binding(), basis)

        _finish_idea_stage(runtime)
        assert runtime.plan_stage.process_once()
        fg = runtime.owners.advancement_engine.query_foreground(quest_ref)
        plan_req = runtime.owners.advancement_engine.query_plan_stage_request(fg["cycle_ref"])
        assert plan_req is not None
        assert plan_req.context_pack.get("creation_basis") == expected_ref
        validate_plan_context_pack(
            plan_req.context_pack,
            cycle_ref=fg["cycle_ref"],
            accepted_question_binding=question.as_binding().as_dict(),
        )
        assert PLAN_CONTEXT_PACK_SCHEMA_REF == plan_req.context_pack["schema_ref"]

        # Advance Plan -> Bundle request creation
        for _ in range(12):
            if runtime.plan_stage.process_once():
                continue
            break
        assert runtime.bundle_stage.process_once()
        bundle_req = runtime.owners.advancement_engine.query_bundle_stage_request(fg["cycle_ref"])
        assert bundle_req is not None
        assert bundle_req.context_pack.get("creation_basis") == expected_ref
        validate_bundle_context_pack(
            bundle_req.context_pack,
            cycle_ref=fg["cycle_ref"],
            accepted_question_binding=question.as_binding().as_dict(),
            accepted_formal_plan_binding=bundle_req.accepted_formal_plan.as_dict(),
        )
        assert BUNDLE_CONTEXT_PACK_SCHEMA_REF == bundle_req.context_pack["schema_ref"]
    finally:
        runtime.close()

    # Part B: Reasoning + successor Cycle Idea context propagation
    runtime2 = _make(tmp_path / "runtime-rs", idea=_DeterministicIdeaSkill(no_viable=True), plan=_DeterministicPlanSkill(no_gap=False))
    try:
        created2 = _confirm_direct_quest(runtime2)
        quest_ref2 = created2["quest_ref"]
        question_ref2 = created2["question_ref"]
        question2 = runtime2.owners.research_graph.query_question_by_ref(question_ref2)
        memory2 = runtime2.owners.research_memory.creation_bases
        req2 = InitializationUnderstandingRequest(
            created2["initialization_id"],
            created2["quest_draft"]["revision"],
            created2["quest_draft"]["hash"],
            created2["quest_draft"]["value"],
            empty_manifest(),
            "basis-empty-sel-rs",
            created2["intent_session"]["ref"],
            None,
        )
        basis2 = memory2.accept_prepared(req2, empty_understanding())
        expected_ref2 = memory2.reference(basis2)
        memory2.associate_question(question2.as_binding(), basis2)

        _finish_idea_stage(runtime2)
        fg2 = runtime2.owners.advancement_engine.query_foreground(quest_ref2)
        assert fg2["stage"] == "reasoning"
        reasoning_req = runtime2.owners.advancement_engine.ensure_reasoning_stage_request(
            cycle_ref=fg2["cycle_ref"],
            accepted_question=question2.as_binding(),
            idempotency_key="reasoning-req-basis-check",
        )
        assert reasoning_req.context_pack.get("creation_basis") == expected_ref2
        # Verify successor Idea context freeze also preserves creation_basis
        idea_commit = runtime2.owners.advancement_engine.query_idea_stage_commit(
            runtime2.owners.advancement_engine.query_idea_stage_request(fg2["cycle_ref"]).request_ref
        )
        pos = runtime2.owners.advancement_engine.query_cycle_position(fg2["cycle_ref"])
        frozen = runtime2.owners.advancement_engine._freeze_reasoning_successor_idea_context(
            cycle_ref=fg2["cycle_ref"],
            accepted_question=question2.as_binding(),
            source_commit=idea_commit,
            cycle_position=pos,
        )
        assert frozen.get("creation_basis") == expected_ref2
    finally:
        runtime2.close()


def test_pillar2_quest_directory_references_includes_acquired_and_init_request(tmp_path):
    from sqlalchemy import text
    from meta_research.owners.common import canonical_json
    from test_public_work_materials import _source

    runtime = _make(tmp_path / "runtime")
    try:
        created = _confirm_direct_quest(runtime)
        quest_ref = created["quest_ref"]
        init_id = created["initialization_id"]
        src_acquired = tmp_path / "acq.txt"
        src_acquired.write_text("acquired data\n", encoding="utf-8")
        src_init_req = tmp_path / "init-req.txt"
        src_init_req.write_text("init human request material\n", encoding="utf-8")

        human = runtime.owners.human_collaboration
        sel_acq = runtime.root_workspaces.server_files.inspect(str(src_acquired), description="acq")
        sel_req = runtime.root_workspaces.server_files.inspect(str(src_init_req), description="init-req")

        # Insert an 'acquired' submission bound to the quest and an init-phase 'request' submission
        # whose root carries initialization_id (with quest_ref=None, as before confirmation).
        with human._database.fenced_write() as connection:
            acq_cmd = {
                "receiver": {"kind": "acquired", "quest_ref": quest_ref, "roots": [{"workspace_ref": "ws:old", "quest_ref": quest_ref}]},
                "selections": [sel_acq],
                "description": "Acquired in cycle 1",
            }
            sub_acq = human._insert_material_submission(
                connection, command=acq_cmd, key="test-acq-sub", anchor_kind="acquired", anchor_ref="run:old", validate_receiver=False
            )
            req_cmd = {
                "receiver": {
                    "kind": "request",
                    "request_ref": "hr:init-1",
                    "roots": [{"workspace_ref": "ws:init-acq", "quest_ref": None, "initialization_id": init_id}],
                },
                "selections": [sel_req],
                "description": "Provided during initialization HumanRequest",
            }
            sub_req = human._insert_material_submission(
                connection, command=req_cmd, key="test-init-req-sub", anchor_kind="request", anchor_ref="hr:init-1", validate_receiver=False
            )
        ref_acq = human.query_material_submission(sub_acq)["references"][0]["reference_ref"]
        ref_req = human.query_material_submission(sub_req)["references"][0]["reference_ref"]

        later_ctx = _external_context(_scope(runtime, quest_ref=quest_ref, run_ref="later-root", root_ref="later-session"))
        visible_refs = human._quest_directory_references(later_ctx)
        assert ref_acq in visible_refs
        assert ref_req in visible_refs

        # Also check native_directory_prompt includes both mounted materials!
        later_loc = runtime.root_workspaces.bind_runtime(later_ctx).location
        prompt = runtime.root_workspaces.native_directory_prompt(later_loc)
        assert str(src_acquired) in prompt
        assert str(src_init_req) in prompt
    finally:
        runtime.close()


def test_p1_corrupt_spool_during_protected_creation_revokes_token_and_fails_operation(tmp_path):
    from sqlalchemy import text
    from meta_research.companion import DraftingUnavailable
    from meta_research.idea_skill import IdeaSkillUnavailable

    source = tmp_path / "original"
    source.mkdir()
    (source / "note.txt").write_text("alpha\n", encoding="utf-8")
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, _reference = _open(client, runtime, source, "corrupt-spool")
            human, roots = runtime.owners.human_collaboration, runtime.root_workspaces

            class DurableLikeRunner:
                def __init__(self):
                    self.cancelled: list[str] = []

                def run_durable_job(self, *args, **kwargs):
                    raise AssertionError("not invoked directly in unit test")

                def cancel_job(self, job_ref: str):
                    self.cancelled.append(job_ref)

            runner = DurableLikeRunner()
            adapter = CodexCompanionAdapter(tmp_path / "adapter", process_runner=runner)
            adapter.bind_workspaces(roots)
            job_ref = "corrupt-op:understanding"
            spool_dir = (
                adapter._workspace
                / "provider-operations"
                / canonical_hash({"job_ref": job_ref})
                / "initialization-understanding"
            )
            spool_dir.mkdir(parents=True)
            (spool_dir / "invocation.json").write_text("{corrupt-json", encoding="utf-8")
            captured_token: list[str] = []

            def failing_invoke(**kwargs):
                captured_token.append(kwargs["mcp_token"])
                raise IdeaSkillUnavailable("codex_cli_failed")

            adapter._invoke = failing_invoke
            inputs = human.creation_material_snapshot(anchor)
            request = InitializationUnderstandingRequest(
                anchor.ref, anchor.draft_revision, anchor.draft_hash,
                {"goal": "Test corrupt spool safety"}, empty_manifest(),
                job_ref, binding.location.root_session_ref, None, inputs=inputs,
            )
            with pytest.raises(DraftingUnavailable) as failure:
                adapter.understand_initialization(request)
            assert failure.value.code == "protected_creation_unknown_outcome"
            # Live MCP token must be revoked and DB row must NOT remain active.
            assert captured_token
            import hashlib
            token_hash = hashlib.sha256(captured_token[0].encode()).hexdigest()
            assert token_hash not in roots._creation_channels
            with human._database.read() as connection:
                state = connection.execute(
                    text("SELECT state FROM hc_creation_material_operations WHERE operation_ref=:ref"),
                    {"ref": job_ref},
                ).scalar_one()
            assert state == "unknown_outcome"

            # Part B: cancel_job continues stopping remaining targets even when one target has a corrupt spool.
            parent_ref = "batch-cancel"
            bad_ref = f"{parent_ref}:a"
            good_ref = f"{parent_ref}:b"
            bad_dir = (
                adapter._workspace
                / "provider-operations"
                / canonical_hash({"job_ref": bad_ref})
                / "initialization-understanding"
            )
            bad_dir.mkdir(parents=True)
            (bad_dir / "invocation.json").write_text("{not-valid", encoding="utf-8")

            class FakeWork:
                def __init__(self):
                    self.stopped = False

                def request_stop(self):
                    self.stopped = True
                    return {"status": "stopped", "descendants_ended": True}

            bad_work = FakeWork()
            good_work = FakeWork()
            adapter._creation_calls[bad_ref] = bad_work
            adapter._creation_calls[good_ref] = good_work
            runner.cancelled.clear()
            assert adapter.cancel_job(parent_ref) is False
            assert bad_ref in runner.cancelled
            assert good_ref in runner.cancelled
            assert bad_work.stopped is True
            assert good_work.stopped is True
    finally:
        runtime.close()


def test_p1_host_direct_predecessor_reassessment_support_and_rejection(tmp_path):
    from test_public_human_reply_delivery import _login
    from test_public_work_materials import _source

    dir1 = tmp_path / "repo-a"
    dir1.mkdir()
    (dir1 / "notes.md").write_text("baseline=0.72\n", encoding="utf-8")
    dir2 = tmp_path / "repo-b"
    dir2.mkdir()
    (dir2 / "other.md").write_text("unused\n", encoding="utf-8")

    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, ref1 = _open(client, runtime, dir1, "host-reassess")
            human, roots = runtime.owners.human_collaboration, runtime.root_workspaces
            headers = {**_login(client, runtime), "Idempotency-Key": "add-dir2"}
            resp = client.post(
                f"/api/v1/quest-initializations/{anchor.ref}/material-references",
                headers=headers,
                json={
                    "receiver": human.material_receiver("creation", anchor.ref),
                    "selections": [_source(client, dir2)],
                    "description": "Second unread repo",
                },
            )
            assert resp.status_code == 201
            ref2 = resp.json()["references"][0]
            inputs = human.creation_material_snapshot(anchor)

            # Round 1: host-direct understanding citing dir1 only (dir2 unread, selection == []).
            work1 = roots.protected_creation(binding, inputs, operation_ref="host-r1")
            work1.completed_handoff()
            identity1 = work1.seal()
            u1 = empty_understanding()
            u1["coverage"] = [
                {
                    "material_key": ref1["reference_ref"],
                    "kind": "partial",
                    "read_ranges": [{"witness_ref": ref1["reference_ref"], "location": "notes.md:1"}],
                    "unread_description": "Other files unread.",
                },
                {
                    "material_key": ref2["reference_ref"],
                    "kind": "unread",
                    "read_ranges": [],
                    "unread_description": "Not inspected yet.",
                },
            ]
            u1["claims_and_conditions"] = [{
                "ref": "c1",
                "text": "Baseline is 0.72.",
                "kind": "reported_work",
                "conditions": ["Host direct check"],
                "sources": [{"witness_ref": ref1["reference_ref"], "location": "notes.md:1"}],
            }]
            req1 = InitializationUnderstandingRequest(
                anchor.ref, anchor.draft_revision, anchor.draft_hash,
                {"goal": "Initial goal"}, empty_manifest(),
                "host-r1", binding.location.root_session_ref, None, inputs=inputs,
            )
            memory = runtime.owners.research_memory.creation_bases
            predecessor = memory.accept_reference_prepared(
                req1, InitializationUnderstandingResult(u1, "native-r1", identity1, work1)
            )
            assert predecessor["input_identity"]["consumed"] == []

            def make_delta(pred, inherited_evidence):
                additions = empty_understanding()
                additions["coverage"] = [
                    {
                        "material_key": ref1["reference_ref"],
                        "kind": "partial",
                        "read_ranges": [{"witness_ref": ref1["reference_ref"], "location": "notes.md:1"}],
                        "unread_description": "Other files unread.",
                    },
                    {
                        "material_key": ref2["reference_ref"],
                        "kind": "unread",
                        "read_ranges": [],
                        "unread_description": "Still unread.",
                    },
                ]
                return {
                    "predecessor": memory.reference(pred),
                    "change_assessment": "Draft refined; mounted repo unchanged.",
                    "decisions": [{
                        "prior_statement_ref": "c1",
                        "disposition": "retain",
                        "explanation": "Unchanged host-direct finding.",
                        "applicable_conditions": ["Host direct check"],
                        "affected_scope": "",
                        "creation_limit": "none",
                        "replacement_refs": [],
                    }],
                    "additions": additions,
                    "inherited_evidence": inherited_evidence,
                    "inherited_selection_keys": [],
                    "literature_decisions": [],
                }

            # Round 2a: user refines draft -> reassessment with inherited_evidence == [] succeeds cleanly.
            view1 = human.query_quest_creation(anchor.ref)
            saved2a = human.revise_quest_draft(
                anchor.ref,
                {**view1["quest_draft"]["value"], "goal": "Updated goal"},
                view1["quest_draft"]["hash"],
                "rev-host-2a",
                view1["quest_draft"]["revision"],
            )
            anchor2a = CreationAnchor(
                "quest_initialization", anchor.ref, None,
                saved2a["quest_draft"]["revision"], saved2a["quest_draft"]["hash"],
            )
            inputs2a = human.creation_material_snapshot(anchor2a)
            context2a = memory.reassessment_context(inputs2a)
            assert context2a["evidence_facts"] == []
            assert context2a["host_cited_references"] == [{
                "reference_ref": ref1["reference_ref"],
                "present_in_current_materials": True,
                "absolute_path": str(dir1),
            }]
            work2a = roots.protected_creation(binding, inputs2a, operation_ref="host-r2a")
            work2a.completed_handoff()
            identity2a = work2a.seal()
            req2a = InitializationUnderstandingRequest(
                anchor2a.ref, anchor2a.draft_revision, anchor2a.draft_hash,
                {"goal": "Updated goal"}, empty_manifest(),
                "host-r2a", binding.location.root_session_ref, "native-r1",
                inputs=inputs2a, reassessment=context2a,
            )
            accepted_a = memory.accept_reference_prepared(
                req2a, InitializationUnderstandingResult({}, "native-r2a", identity2a, work2a, reassessment=make_delta(predecessor, []))
            )
            assert accepted_a["inherited_evidence"] == []
            assert memory.understanding_view(accepted_a)["claims_and_conditions"][0]["sources"][0]["provenance"]["kind"] == "host_direct"

            # Round 2b: chained reassessment with live_source host-ref entry succeeds without bogus ReadWitness.
            saved2b = human.revise_quest_draft(
                anchor.ref,
                {**saved2a["quest_draft"]["value"], "goal": "Updated goal 2"},
                saved2a["quest_draft"]["hash"],
                "rev-host-2b",
                saved2a["quest_draft"]["revision"],
            )
            anchor2b = CreationAnchor(
                "quest_initialization", anchor.ref, None,
                saved2b["quest_draft"]["revision"], saved2b["quest_draft"]["hash"],
            )
            inputs2b = human.creation_material_snapshot(anchor2b)
            context2b = memory.reassessment_context(inputs2b)
            assert context2b["predecessor"] == memory.reference(accepted_a)
            assert context2b["host_cited_references"] == [{
                "reference_ref": ref1["reference_ref"],
                "present_in_current_materials": True,
                "absolute_path": str(dir1),
            }]
            work2b = roots.protected_creation(binding, inputs2b, operation_ref="host-r2b")
            work2b.completed_handoff()
            identity2b = work2b.seal()
            req2b = InitializationUnderstandingRequest(
                anchor2b.ref, anchor2b.draft_revision, anchor2b.draft_hash,
                {"goal": "Updated goal 2"}, empty_manifest(),
                "host-r2b", binding.location.root_session_ref, "native-r2a",
                inputs=inputs2b, reassessment=context2b,
            )
            accepted_b = memory.accept_reference_prepared(
                req2b,
                InitializationUnderstandingResult(
                    {}, "native-r2b", identity2b, work2b,
                    reassessment=make_delta(
                        accepted_a,
                        [{"kind": "live_source", "witness_ref": ref1["reference_ref"], "selected_material_key": None}],
                    ),
                ),
            )
            assert accepted_b["inherited_evidence"] == []

            # Round 2c: Reject forged, uncited, managed_history, non-null selection key, and removed references.
            invalid_cases = [
                [{"kind": "live_source", "witness_ref": "fake-witness", "selected_material_key": None}],
                [{"kind": "live_source", "witness_ref": ref2["reference_ref"], "selected_material_key": None}],
                [{"kind": "managed_history", "witness_ref": ref1["reference_ref"], "selected_material_key": None}],
                [{"kind": "live_source", "witness_ref": ref1["reference_ref"], "selected_material_key": "sel-1"}],
            ]
            for bad_inherited in invalid_cases:
                with pytest.raises(OwnerConflict, match="creation_reassessment_invalid"):
                    memory._compose_reassessment(inputs2b, make_delta(accepted_a, bad_inherited))

            # Removed reference: inputs without ref1 must reject inheriting ref1.
            reduced_inputs = MaterialSet(anchor2b, tuple(item for item in inputs2b.references if item["reference_ref"] != ref1["reference_ref"]))
            with pytest.raises(OwnerConflict, match="creation_reassessment_invalid"):
                memory._compose_reassessment(
                    reduced_inputs,
                    make_delta(accepted_a, [{"kind": "live_source", "witness_ref": ref1["reference_ref"], "selected_material_key": None}]),
                )
    finally:
        runtime.close()


def test_p2_hardening_guards(tmp_path):
    from meta_research.creation_basis import _resolve_host_reference_ref
    from meta_research.creation_failure_cleanup import close_failed_initialization_work
    from meta_research.idea_skill import IdeaSkillUnavailable
    from meta_research.runtime_conditions import _MOUNTED_MATERIALS_MARKER

    # P2-1: Mounted material description containing "Work directories: " does not trigger JSONDecodeError.
    before = (
        'Use native tools. Work directories: [{"workspace_ref":"ws:1","directory":"/tmp/w1","access":"read_write"}]\n'
        + _MOUNTED_MATERIALS_MARKER
        + json.dumps([{
            "reference_ref": "material:1",
            "absolute_path": "/data/a",
            "kind": "directory",
            "access": "read_only",
            "description": "Contains Work directories: [not valid json",
        }])
    )
    after = (
        'Use native tools. Work directories: [{"workspace_ref":"ws:1","directory":"/tmp/w1","access":"read_write"}]\n'
        + _MOUNTED_MATERIALS_MARKER
        + json.dumps([
            {
                "reference_ref": "material:1",
                "absolute_path": "/data/a",
                "kind": "directory",
                "access": "read_only",
                "description": "Contains Work directories: [not valid json",
            },
            {
                "reference_ref": "material:2",
                "absolute_path": "/data/b",
                "kind": "file",
                "access": "read_only",
                "description": "Another Work directories: broken",
            },
        ])
    )
    delta = resume_runtime_conditions(after, (before,), ordered=True)
    assert "挂载材料授权更新=" in delta
    assert "工作目录授权更新=" not in delta
    # Re-parsing the delta line itself must also not crash.
    assert resume_runtime_conditions(after, (delta, before), ordered=True) == ""

    # P2-4: _resolve_host_reference_ref handles ValueError (e.g. embedded NUL) gracefully.
    assert _resolve_host_reference_ref("/tmp/bad\x00path", {"material:1": {"source": {"absolute_path": str(tmp_path), "kind": "directory"}}}) is None

    # P2-3: Symlink pointing into work_root is rejected with creation_original_in_work.
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            real_src = tmp_path / "real-src"
            real_src.mkdir()
            (real_src / "x.txt").write_text("x", encoding="utf-8")
            anchor, binding, _ = _open(client, runtime, real_src, "symlink-check")
            roots = runtime.root_workspaces
            identity = canonical_hash({"workspace_ref": binding.location.workspace_ref, "root_session_ref": binding.location.root_session_ref})
            work_root = (roots._bases["companion"] / "creation-work" / identity).absolute()
            work_root.mkdir(parents=True, exist_ok=True)
            inside = work_root / "secret.txt"
            inside.write_text("inside", encoding="utf-8")
            link = tmp_path / "escape-link.txt"
            link.symlink_to(inside)
            bad_inputs = MaterialSet(anchor, ({
                "reference_ref": "material:sym",
                "submission_ref": "sub:sym",
                "source": {"absolute_path": str(link), "kind": "file"},
                "description": "symlink into work",
            },))
            with pytest.raises(OwnerConflict) as err:
                roots.protected_creation(binding, bad_inputs, operation_ref="sym-op")
            assert err.value.code == "creation_original_in_work"
    finally:
        runtime.close()

    # P2-2: close_failed_initialization_work maps IdeaSkillUnavailable from _reconcile_cancelled_job_spools to OwnerConflict.
    runtime2 = _make(tmp_path / "runtime2")
    try:
        with TestClient(create_app(runtime2, base_url="http://testserver", control_key="control")) as client:
            src = tmp_path / "cleanup-src"
            src.mkdir()
            (src / "a.txt").write_text("a", encoding="utf-8")
            anchor, binding, _ = _open(client, runtime2, src, "cleanup-exc")
            human, roots = runtime2.owners.human_collaboration, runtime2.root_workspaces
            gen_ref = "gen-cleanup-exc"
            op_ref = f"{gen_ref}:proposal:understanding"
            inputs = human.creation_material_snapshot(anchor)
            work = roots.protected_creation(binding, inputs, operation_ref=op_ref)
            from sqlalchemy import text
            with human._database.fenced_write() as connection:
                connection.execute(
                    text(
                        "INSERT INTO hc_proposal_generation_attempts "
                        "(generation_ref, initialization_id, idempotency_key, request_hash, route, "
                        "basis_revision, basis_hash, starting_proposal_revision, status, adapter_kind, "
                        "attempt_count, created_at, started_at, completed_at, failure_code) "
                        "VALUES (:gen, :init, 'cleanup-key', :req_hash, 'direct', :rev, :hash, 0, "
                        "'failed', 'production', 1, 1.0, 1.0, 2.0, 'proposal_output_invalid')"
                    ),
                    {
                        "gen": gen_ref,
                        "init": anchor.ref,
                        "req_hash": canonical_hash({"gen": gen_ref}),
                        "rev": anchor.draft_revision,
                        "hash": anchor.draft_hash,
                    },
                )
            adapter = CodexCompanionAdapter(tmp_path / "cleanup-adapter")
            adapter.bind_workspaces(roots)
            op_dir = (
                adapter._workspace
                / "provider-operations"
                / canonical_hash({"job_ref": op_ref})
                / "initialization-understanding"
            )
            op_dir.mkdir(parents=True)
            prompt_text = "test prompt"
            schema_obj = {"type": "object"}
            (op_dir / "prompt.txt").write_text(prompt_text, encoding="utf-8")
            (op_dir / "output-schema.json").write_text(json.dumps(schema_obj), encoding="utf-8")
            _, key = ensure_transport_key(adapter._workspace)
            write_transport_envelope(
                op_dir / "invocation.json",
                {
                    "schema_ref": "meta-research/codex-provider-operation/v3",
                    "job_ref": op_ref,
                    "operation_name": "initialization-understanding",
                    "provider_execution_binding": work.execution_binding,
                    "workspace_binding": work.binding.seal(),
                    "working_directory": str(work.binding.directory),
                    "mcp_scope_binding_hash": work.operation.binding_hash,
                    "prompt_hash": canonical_hash(prompt_text),
                    "output_schema_hash": canonical_hash(schema_obj),
                },
                key,
            )

            def boom(_job_ref):
                raise IdeaSkillUnavailable("codex_operation_spool_invalid")

            adapter._reconcile_cancelled_job_spools = boom
            with pytest.raises(OwnerConflict) as cleanup_err:
                close_failed_initialization_work(adapter, anchor.ref, anchor.draft_revision, anchor.draft_hash)
            assert cleanup_err.value.code == "protected_creation_cleanup_unverified"
    finally:
        runtime2.close()

    # P2-6: ensure_companion_native_handoff allows non-UUID native_session_ref in host-direct mode
    # (when no legacy .protected/<identity> jail exists), while still checking sealed/failed operations.
    runtime3 = _make(tmp_path / "runtime3")
    try:
        created = _confirm_direct_quest(runtime3)
        human, roots = runtime3.owners.human_collaboration, runtime3.root_workspaces
        quest_ref = created["quest_ref"]
        init_id = created["initialization_id"]
        prep_session = created["intent_session"]["ref"]
        non_uuid_native = "native-host-session-1"
        from sqlalchemy import text
        with human._database.fenced_write() as connection:
            connection.execute(
                text("UPDATE hc_intent_drafting_sessions SET native_session_ref=:native WHERE initialization_id=:init"),
                {"native": non_uuid_native, "init": init_id},
            )
        init_binding = roots.bind_initialization(init_id, prep_session)
        anchor3 = CreationAnchor("quest_initialization", init_id, None, created["quest_draft"]["revision"], created["quest_draft"]["hash"])
        inputs3 = MaterialSet(anchor3, ())
        from meta_research.owners.common import canonical_json
        with human._database.fenced_write() as connection:
            connection.execute(
                text(
                    "INSERT INTO hc_creation_material_operations "
                    "(operation_ref,fence_ref,binding_hash,workspace_ref,root_session_ref,anchor_json,"
                    "material_set_json,material_set_hash,state,created_at) "
                    "VALUES ('handoff-op-1','fence-1',:binding,:workspace,:session,:anchor,:inputs,:hash,'active',1.0)"
                ),
                {
                    "binding": canonical_hash({"op": "handoff-op-1"}),
                    "workspace": init_binding.location.workspace_ref,
                    "session": prep_session,
                    "anchor": canonical_json(anchor3.as_dict()),
                    "inputs": canonical_json(inputs3.as_dict()),
                    "hash": inputs3.set_hash,
                },
            )
        companion_session = human.query_companion("quest:" + quest_ref)["session_ref"]
        companion_binding = roots.bind_companion_session("quest:" + quest_ref, companion_session)
        adapter3 = CodexCompanionAdapter(tmp_path / "handoff-adapter")
        adapter3.bind_workspaces(roots)
        # While still active, handoff MUST still raise companion_native_handoff_unsealed.
        with pytest.raises(OwnerConflict) as unsealed:
            ensure_companion_native_handoff(adapter3, companion_binding, non_uuid_native)
        assert unsealed.value.code == "companion_native_handoff_unsealed"
        # Once sealed, host-direct mode returns cleanly even with non-UUID native_session_ref.
        with human._database.fenced_write() as connection:
            connection.execute(
                text("UPDATE hc_creation_material_operations SET state='sealed' WHERE operation_ref='handoff-op-1'")
            )
        ensure_companion_native_handoff(adapter3, companion_binding, non_uuid_native)
    finally:
        runtime3.close()


