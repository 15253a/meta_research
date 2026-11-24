from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.migration import upgrade_database
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from meta_research.research_content import discover_literature
from meta_research.semantic_mcp import (
    ROOT_AGENT_DATASET_OPERATION_IDS,
    ROOT_AGENT_LITERATURE_OPERATION_IDS,
    SemanticMcpGateway,
    _ROLE_ONLY_OPERATION_OWNERS,
)
from meta_research.semantic_owner_gateway import (
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
    _dataset_operations,
)
from meta_research.tool_navigation import purpose_paths
from meta_research.web import create_app


def _web_client(runtime, base_url: str = "http://testserver") -> TestClient:
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="isolated-test"),
        base_url=base_url,
    )
    auth = client.post(
        "/auth/bootstrap",
        headers={"Origin": base_url},
        json={"token": runtime.authentication.issue_bootstrap_token()},
    )
    assert auth.status_code == 200, auth.text
    return client


def _companion_channel(runtime, *, quest_ref: str, run_ref: str = "audit-companion"):
    agent = runtime.owners.agent_runtime
    scope = {
        "quest_ref": quest_ref,
        "run_ref": run_ref,
        "attempt_ref": "attempt-1",
        "root_session_ref": f"root-{run_ref}",
        "fence_ref": "fence-1",
        "runtime_binding_hash": "b" * 64,
        "generation": 1,
    }
    agent.register_external_root_task_scope(
        root_kind="companion", root_runtime_scope=scope
    )
    channel = runtime.harnesses.issue_resident_mcp_channel(
        run_ref=scope["run_ref"],
        attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"],
        fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"],
        root_kind="companion",
        phase="primary",
        subject_policy="operation_tree",
        operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS["companion"],
    )
    return channel, scope


def _mcp_call(runtime, channel, operation: str, **arguments):
    status, response, _ = runtime.harnesses.dispatch_mcp_http(
        channel.connection.token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": operation, "arguments": arguments},
        },
        mcp_session_id=None,
    )
    assert status == 200, response
    return response["result"]


def _gateway_call(
    gateway: SemanticMcpGateway, channel, operation: str, **arguments
):
    _, response = gateway.dispatch(
        channel.token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": operation, "arguments": arguments},
        },
    )
    return response["result"]


def _confirm_direct_quest_with_prefix(runtime, prefix: str) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, f"{prefix}-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-plan-test"],
        f"{prefix}-compute",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": f"{prefix}: 判断低照度显微图像去噪能否保留稀有形态。",
            "completion_criteria": "形成带反例和证据边界的比较结论。",
            "time_budget": "30d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "比较自监督和监督基线。",
        }
    )
    human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        f"{prefix}-draft",
        probed["quest_draft"]["revision"],
    )
    drafted = human.query_quest_creation(opened["initialization_id"])
    human.generate_question_proposal(
        opened["initialization_id"],
        drafted["quest_draft"]["hash"],
        f"{prefix}-proposal",
        drafted["quest_draft"]["revision"],
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(opened["initialization_id"])
    previewed = human.preview_confirmation(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key=f"{prefix}-preview",
    )
    human.confirm_quest(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key=f"{prefix}-confirm",
    )
    for _step in range(16):
        current = human.query_quest_creation(opened["initialization_id"])
        if current["status"] == "completed":
            return current
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return completed


def _confirm_deepfetch_quest_with_prefix(runtime, prefix: str) -> dict[str, object]:
    from test_public_first_question_deepfetch import _deepfetch_draft

    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, f"{prefix}-open")
    initialization_id = str(opened["initialization_id"])
    probed = human.observe_host_compute(
        initialization_id, ["GPU-deepfetch-1"], f"{prefix}-compute"
    )
    draft = _deepfetch_draft(probed)
    revised = human.revise_quest_draft(
        initialization_id,
        draft,
        str(probed["quest_draft"]["hash"]),
        f"{prefix}-draft",
        int(probed["quest_draft"]["revision"]),
    )
    human.prepare_acquisition_session(
        initialization_id,
        str(revised["quest_draft"]["hash"]),
        f"{prefix}-acq",
        int(revised["quest_draft"]["revision"]),
    )
    human.generate_question_proposal(
        initialization_id,
        str(revised["quest_draft"]["hash"]),
        f"{prefix}-proposal",
        int(revised["quest_draft"]["revision"]),
    )
    assert runtime.deepfetch.process_once()
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(initialization_id)
    assert proposed["status"] == "proposal_ready"
    previewed = human.preview_confirmation(
        initialization_id,
        quest_draft_revision=int(proposed["quest_draft"]["revision"]),
        quest_draft_hash=str(proposed["quest_draft"]["hash"]),
        proposal_ref=str(proposed["proposal"]["ref"]),
        proposal_hash=str(proposed["proposal"]["hash"]),
        idempotency_key=f"{prefix}-preview",
    )
    human.confirm_quest(
        initialization_id,
        quest_draft_revision=int(proposed["quest_draft"]["revision"]),
        quest_draft_hash=str(proposed["quest_draft"]["hash"]),
        proposal_ref=str(proposed["proposal"]["ref"]),
        proposal_hash=str(proposed["proposal"]["hash"]),
        preview_ref=str(previewed["confirmation_preview"]["ref"]),
        preview_hash=str(previewed["confirmation_preview"]["hash"]),
        idempotency_key=f"{prefix}-confirm",
    )
    for _step in range(16):
        completed = human.query_quest_creation(initialization_id)
        if completed["status"] == "completed":
            return completed
        assert human.reconcile_once()
    completed = human.query_quest_creation(initialization_id)
    assert completed["status"] == "completed"
    return completed


def _admit_idea_run(runtime, cycle_ref: str):
    ae = runtime.owners.advancement_engine
    ar = runtime.owners.agent_runtime
    for _ in range(6):
        req = ae.query_idea_stage_request(cycle_ref)
        if req is not None:
            run = ar.query_idea_stage_run(req.request_ref)
            if run is not None:
                return run
        runtime.idea_stage.process_once()
    raise AssertionError(f"Idea run was not admitted for cycle {cycle_ref}")


def test_dataset_origin_quest_scoping_immediate_visibility_and_migration_0092(
    tmp_path: Path,
) -> None:
    from test_migration_recovery import _upgrade_to_revision
    from test_public_plan_stage import (
        _DeterministicIdeaSkill,
        _DeterministicPlanSkill,
        _runtime as _plan_runtime,
    )
    from test_research_datasets import _asset

    runtime = _plan_runtime(
        tmp_path / "dataset-audit",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=_DeterministicPlanSkill(no_gap=False),
    )
    try:
        graph, agent = runtime.owners.research_graph, runtime.owners.agent_runtime
        qa = _confirm_direct_quest_with_prefix(runtime, "ds-qa")
        run_a = _admit_idea_run(runtime, str(qa["cycle_ref"]))
        qb = _confirm_direct_quest_with_prefix(runtime, "ds-qb")
        run_b = _admit_idea_run(runtime, str(qb["cycle_ref"]))

        quest_a_ref = str(qa["quest_ref"])
        quest_b_ref = str(qb["quest_ref"])

        asset_a = _asset(runtime, content=b"Quest A image split", key="ds-a")
        graph.accept_asset_role(
            binding=asset_a,
            role="evidence",
            quest_ref=quest_a_ref,
            idempotency_key="origin-a",
        )
        asset_b = _asset(runtime, content=b"Quest B different split", key="ds-b")
        graph.accept_asset_role(
            binding=asset_b,
            role="evidence",
            quest_ref=quest_b_ref,
            idempotency_key="origin-b",
        )

        gateway = SemanticMcpGateway(_dataset_operations(graph, agent))
        chan_a, _ = gateway.issue_channel(
            run_ref=run_a.run_ref,
            attempt_ref=run_a.attempt_ref,
            root_session_ref=run_a.root_session_ref,
            fence_ref=run_a.fence_ref,
            capability_binding_hash=run_a.runtime_binding_hash,
            root_kind="idea",
            phase="primary",
            operation_ids=ROOT_AGENT_DATASET_OPERATION_IDS,
        )
        chan_b, _ = gateway.issue_channel(
            run_ref=run_b.run_ref,
            attempt_ref=run_b.attempt_ref,
            root_session_ref=run_b.root_session_ref,
            fence_ref=run_b.fence_ref,
            capability_binding_hash=run_b.runtime_binding_hash,
            root_kind="idea",
            phase="primary",
            operation_ids=ROOT_AGENT_DATASET_OPERATION_IDS,
        )

        reg_a = _gateway_call(
            gateway,
            chan_a,
            "research_graph.datasets.register",
            effect_id="reg-a-1",
            semantic_key="cifar10",
            name="CIFAR-10 Quest A",
            meaning="Quest A train split",
            notes="notes-a",
        )
        assert not reg_a.get("isError"), reg_a
        ds_a = reg_a["structuredContent"]["result"]
        assert ds_a["origin_quest_ref"] == quest_a_ref

        # Cross-Quest registration with the same semantic_key and differing name/meaning/notes
        # must succeed without dataset_semantic_identity_conflict.
        reg_b = _gateway_call(
            gateway,
            chan_b,
            "research_graph.datasets.register",
            effect_id="reg-b-1",
            semantic_key="cifar10",
            name="CIFAR-10 Quest B Completely Different",
            meaning="Quest B custom benchmark",
            notes="notes-b",
        )
        assert not reg_b.get("isError"), reg_b
        ds_b = reg_b["structuredContent"]["result"]
        assert ds_b["origin_quest_ref"] == quest_b_ref
        assert ds_b["dataset_ref"] != ds_a["dataset_ref"]

        # Same-Quest identical payload deduplicates by payload_hash across effect_id.
        dup_a = _gateway_call(
            gateway,
            chan_a,
            "research_graph.datasets.register",
            effect_id="reg-a-dup",
            semantic_key="cifar10",
            name="CIFAR-10 Quest A",
            meaning="Quest A train split",
            notes="notes-a",
        )
        assert not dup_a.get("isError"), dup_a
        assert dup_a["structuredContent"]["result"]["dataset_ref"] == ds_a["dataset_ref"]

        # Same-Quest different notes creates a second snapshot under the same semantic_key.
        snap_a2 = _gateway_call(
            gateway,
            chan_a,
            "research_graph.datasets.register",
            effect_id="reg-a-snap2",
            semantic_key="cifar10",
            name="CIFAR-10 Quest A",
            meaning="Quest A train split",
            notes="notes-a-updated",
        )
        assert not snap_a2.get("isError"), snap_a2
        ds_a2 = snap_a2["structuredContent"]["result"]
        assert ds_a2["dataset_ref"] != ds_a["dataset_ref"]

        # Same-Quest register_version without calling datasets.reference is immediately visible
        # in the origin Quest's page/read and hidden from the foreign Quest.
        ver_a_res = _gateway_call(
            gateway,
            chan_a,
            "research_graph.datasets.register_version",
            effect_id="ver-a-1",
            dataset_ref=ds_a["dataset_ref"],
            version_label="v1",
            meaning="Unreferenced initial version",
            selections=[{"asset_binding": asset_a.as_dict(), "path": None}],
        )
        assert not ver_a_res.get("isError"), ver_a_res
        ver_a = ver_a_res["structuredContent"]["result"]
        assert ver_a["origin_quest_ref"] == quest_a_ref

        by_key = _gateway_call(
            gateway, chan_a, "research_graph.datasets.page", semantic_key="cifar10"
        )["structuredContent"]
        assert {item["dataset_ref"] for item in by_key["items"]} == {
            ds_a["dataset_ref"],
            ds_a2["dataset_ref"],
        }

        versions = _gateway_call(
            gateway,
            chan_a,
            "research_graph.datasets.page",
            dataset_ref=ds_a["dataset_ref"],
        )["structuredContent"]
        assert [item["dataset_version_ref"] for item in versions["items"]] == [
            ver_a["dataset_version_ref"]
        ]

        read_ver = _gateway_call(
            gateway,
            chan_a,
            "research_graph.datasets.read",
            dataset_version_ref=ver_a["dataset_version_ref"],
        )["structuredContent"]
        assert read_ver["status"] == "accepted"

        # Foreign Quest cannot see ds_a / ver_a and cannot register a version under ds_a.
        foreign_key = _gateway_call(
            gateway, chan_b, "research_graph.datasets.page", semantic_key="cifar10"
        )["structuredContent"]
        assert [item["dataset_ref"] for item in foreign_key["items"]] == [
            ds_b["dataset_ref"]
        ]

        foreign_read = _gateway_call(
            gateway,
            chan_b,
            "research_graph.datasets.read",
            dataset_version_ref=ver_a["dataset_version_ref"],
        )["structuredContent"]
        assert foreign_read["status"] == "not_found"

        bad_ver = _gateway_call(
            gateway,
            chan_b,
            "research_graph.datasets.register_version",
            effect_id="ver-b-on-a",
            dataset_ref=ds_a["dataset_ref"],
            version_label="hijack",
            meaning="Foreign quest version on Quest A dataset",
            selections=[{"asset_binding": asset_b.as_dict(), "path": None}],
        )
        assert bad_ver.get("isError") and "dataset_quest_scope_invalid" in str(bad_ver)
    finally:
        runtime.close()

    # Migration 0091 -> 0092 preserves pre-existing rows and enforces new schema/FK integrity.
    mig_root = prepare_data_root(tmp_path / "mig-0092")
    _upgrade_to_revision(mig_root.database, "0091_part4_goal_refs_and_hot_indexes")
    payload_pre = {
        "semantic_key": "legacy:cifar",
        "name": "Legacy",
        "meaning": "Pre-0092 dataset",
        "metadata": {},
        "notes": "",
    }
    dv_payload = {
        "dataset_ref": "dataset_legacy_1",
        "version_label": "v1",
        "meaning": "Pre-0092 version",
        "asset_bindings": [],
        "metadata": {},
        "notes": "",
    }
    with sqlite3.connect(mig_root.database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO rg_datasets (dataset_ref, semantic_key, payload_json, payload_hash, receipt_ref, receipt_hash, accepted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1.0)",
            (
                "dataset_legacy_1",
                "legacy:cifar",
                canonical_json(payload_pre),
                canonical_hash(payload_pre),
                "rg_ds_receipt_legacy_1",
                canonical_hash({"receipt": "ds"}),
            ),
        )
        connection.execute(
            "INSERT INTO rg_dataset_versions (dataset_version_ref, dataset_ref, version_label, payload_json, payload_hash, receipt_ref, receipt_hash, accepted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1.0)",
            (
                "dataset_version_legacy_1",
                "dataset_legacy_1",
                "v1",
                canonical_json(dv_payload),
                canonical_hash(dv_payload),
                "rg_dv_receipt_legacy_1",
                canonical_hash({"receipt": "dv"}),
            ),
        )
        connection.commit()

    upgrade_database(mig_root.database)
    with sqlite3.connect(mig_root.database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        ds_row = connection.execute(
            "SELECT dataset_ref, semantic_key, origin_quest_ref FROM rg_datasets WHERE dataset_ref='dataset_legacy_1'"
        ).fetchone()
        assert ds_row == ("dataset_legacy_1", "legacy:cifar", None)
        dv_row = connection.execute(
            "SELECT dataset_version_ref, origin_quest_ref FROM rg_dataset_versions WHERE dataset_version_ref='dataset_version_legacy_1'"
        ).fetchone()
        assert dv_row == ("dataset_version_legacy_1", None)
        qrel_cols = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(rg_question_relations)")
        }
        assert "relation_kind" in qrel_cols
        assert "related" in str(qrel_cols["relation_kind"][4])
        # Second row with same semantic_key and distinct payload_hash is now allowed.
        payload_second = {**payload_pre, "notes": "second snapshot"}
        connection.execute(
            "INSERT INTO rg_datasets (dataset_ref, semantic_key, origin_quest_ref, payload_json, payload_hash, receipt_ref, receipt_hash, accepted_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, 2.0)",
            (
                "dataset_legacy_2",
                "legacy:cifar",
                canonical_json(payload_second),
                canonical_hash(payload_second),
                "rg_ds_receipt_legacy_2",
                canonical_hash({"receipt": "ds2"}),
            ),
        )
        connection.commit()


def test_web_ui_ordinary_literature_and_multi_selection_reads(tmp_path: Path) -> None:
    from test_research_datasets import _quest
    from test_root_formal_entities import _accept
    from test_target_root_finalizer import _root_finalizer_fixture

    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path / "lit-sel-web")
    )
    try:
        (workspace / "outputs" / "analysis").mkdir(parents=True, exist_ok=True)
        (workspace / "outputs" / "analysis" / "research-note.md").write_text(
            "# Literature Note\nVerified ordinary reading note body.",
            encoding="utf-8",
        )
        result_path = workspace / "outputs" / "result.json"
        doc = json.loads(result_path.read_text(encoding="utf-8"))
        doc.setdefault("retained_artifacts", []).append(
            {"role": "analysis", "relative_path": "outputs/analysis"}
        )
        result_path.write_text(canonical_json(doc), encoding="utf-8")
        _commit, manifest = _accept(runtime, lifecycle, memory, handle, evidence)

        graph = runtime.owners.research_graph
        rm = runtime.owners.research_memory
        quest_ref = graph.query_target_graph(authority.stage_request_ref).quest_ref
        with runtime._database.read() as connection:
            question_ref = connection.execute(
                text(
                    "SELECT question_ref FROM rg_question_lifecycle WHERE quest_ref=:q LIMIT 1"
                ),
                {"q": quest_ref},
            ).scalar_one()
        foreign = _quest(runtime, "foreign-web-lit")

        pdf_dir = tmp_path / "pdf-single"
        pdf_dir.mkdir()
        (pdf_dir / "paper.pdf").write_bytes(b"%PDF-1.4\nExact ordinary paper body")
        pdf_intake = rm.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="managed",
                display_name="ordinary-paper.pdf",
                source_locator=str(pdf_dir / "paper.pdf"),
            ),
            idempotency_key="ord-pdf",
        )
        assert pdf_intake.asset is not None
        pdf_binding = pdf_intake.asset.as_binding()
        graph.accept_asset_role(
            binding=pdf_binding,
            role="evidence",
            quest_ref=quest_ref,
            idempotency_key="ord-pdf-role",
        )

        obtained_lit = graph.register_literature(
            record_ref="doi:10.5555/ord-obtained",
            title="Ordinary Obtained Paper",
            source="https://example.org/ordinary.pdf",
            fulltext_status="obtained",
            asset_bindings=[pdf_binding.as_dict()],
            quest_ref=quest_ref,
            idempotency_key="reg-ord-obtained",
        )
        missing_lit = graph.register_literature(
            record_ref="doi:10.5555/ord-missing",
            title="Ordinary Missing Paper",
            source="Paywalled Publisher",
            fulltext_status="missing",
            asset_bindings=[],
            quest_ref=quest_ref,
            idempotency_key="reg-ord-missing",
        )
        note_entry = next(e for e in manifest.entries if e.role == "analysis")
        lit_usage = graph.reference_literature(
            literature_ref=obtained_lit["ref"],
            question_ref=question_ref,
            purpose="Validate baseline assumptions",
            research_ref=handle.target_ref,
            reading_summary="Confirmed key bound with exact Target note.",
            reading_bindings=[note_entry.binding.as_dict()],
            quest_ref=quest_ref,
            idempotency_key="ref-ord-obtained",
        )

        # Multi-selection parent asset: sub/a, sub/b, and whole parent (path=None).
        parent_dir = tmp_path / "multi-sel-src"
        (parent_dir / "one").mkdir(parents=True)
        (parent_dir / "two").mkdir()
        (parent_dir / "one" / "part.txt").write_text(
            "selected scope one payload", encoding="utf-8"
        )
        (parent_dir / "two" / "part.txt").write_text(
            "selected scope two payload", encoding="utf-8"
        )
        parent_intake = rm.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="managed",
                display_name="multi-scope-parent",
                source_locator=str(parent_dir),
            ),
            idempotency_key="multi-parent",
        )
        assert parent_intake.asset is not None
        parent_binding = parent_intake.asset.as_binding()
        graph.accept_asset_role(
            binding=parent_binding,
            role="evidence",
            quest_ref=quest_ref,
            idempotency_key="multi-parent-role",
        )
        shutil.rmtree(parent_dir)

        ds = graph.register_dataset(
            semantic_key="multi-sel-ds",
            name="Multi Selection Dataset",
            meaning="Subpath selections",
            quest_ref=quest_ref,
            idempotency_key="multi-ds",
        )
        dv = graph.register_dataset_version(
            dataset_ref=ds["dataset_ref"],
            version_label="v-multi",
            meaning="Multiple subpaths + root",
            selections=[
                {"asset_binding": parent_binding.as_dict(), "path": "one"},
                {"asset_binding": parent_binding.as_dict(), "path": "two"},
                {"asset_binding": parent_binding.as_dict(), "path": None},
            ],
            quest_ref=quest_ref,
            idempotency_key="multi-dv",
        )
        env = graph.register_environment(
            semantic_key="multi-sel-env",
            name="Multi Selection Environment",
            meaning="Two config scopes",
            source="local package",
            selections=[
                {"asset_binding": parent_binding.as_dict(), "path": "one"},
                {"asset_binding": parent_binding.as_dict(), "path": "two"},
            ],
            quest_ref=quest_ref,
            idempotency_key="multi-env",
        )

        client = _web_client(runtime)

        # 1. Ordinary literature Web readers (cards + judgments + reading notes + missing).
        lit_page = client.get(
            "/api/v1/research-library/literature", params={"quest_ref": quest_ref}
        )
        assert lit_page.status_code == 200, lit_page.text
        by_record = {item["record_ref"]: item for item in lit_page.json()["items"]}

        obtained_card = by_record["doi:10.5555/ord-obtained"]
        assert obtained_card["reader"]["source_ref"] == pdf_binding.version_ref
        assert obtained_card["reader"]["version_ref"] == pdf_binding.version_ref
        pdf_resp = client.get(
            "/api/v1/research-content",
            params={"quest_ref": quest_ref, **obtained_card["reader"]},
        )
        assert pdf_resp.status_code == 200, pdf_resp.text
        assert "Exact ordinary paper body" in pdf_resp.json()["text"]

        assert len(obtained_card["judgments"]) == 1
        judgment = obtained_card["judgments"][0]
        assert judgment["reader"] == {
            "source_ref": lit_usage["ref"],
            "version_ref": lit_usage["payload_hash"],
        }
        j_resp = client.get(
            "/api/v1/research-content",
            params={"quest_ref": quest_ref, **judgment["reader"]},
        )
        assert j_resp.status_code == 200, j_resp.text
        assert "Confirmed key bound" in j_resp.json()["text"]
        assert len(judgment["readers"]) == 1
        note_resp = client.get(
            "/api/v1/research-content",
            params={"quest_ref": quest_ref, **judgment["readers"][0]},
        )
        assert note_resp.status_code == 200, note_resp.text
        assert "Verified ordinary reading note body" in note_resp.json()["text"]

        missing_card = by_record["doi:10.5555/ord-missing"]
        assert missing_card["reader"] == {
            "source_ref": missing_lit["ref"],
            "version_ref": missing_lit["payload_hash"],
        }
        miss_resp = client.get(
            "/api/v1/research-content",
            params={"quest_ref": quest_ref, **missing_card["reader"]},
        )
        assert miss_resp.status_code == 200, miss_resp.text
        assert "Ordinary Missing Paper" in miss_resp.json()["text"]

        # Foreign quest must receive 409 on ordinary literature and judgment reads.
        assert (
            client.get(
                "/api/v1/research-content",
                params={"quest_ref": foreign.quest_ref, **missing_card["reader"]},
            ).status_code
            == 409
        )
        assert (
            client.get(
                "/api/v1/research-content",
                params={"quest_ref": foreign.quest_ref, **judgment["reader"]},
            ).status_code
            == 409
        )

        # 2. Multi-selection DatasetVersion & Environment Web readers + selection_path handling.
        ds_vers = client.get(
            "/api/v1/research-library/datasets",
            params={"quest_ref": quest_ref, "dataset_ref": ds["dataset_ref"]},
        )
        assert ds_vers.status_code == 200, ds_vers.text
        dv_card = ds_vers.json()["items"][0]
        assert {r["selection_path"] for r in dv_card["readers"]} == {"one", "two", None}
        assert len(dv_card["readers"]) == 3
        assert dv_card["reader"] == dv_card["readers"][0]

        # Explicit selection_path="" must select the root (path=None) selection.
        root_dir = client.get(
            "/api/v1/research-content",
            params={
                "quest_ref": quest_ref,
                "source_ref": dv["dataset_version_ref"],
                "version_ref": parent_binding.version_ref,
                "selection_path": "",
            },
        )
        assert root_dir.status_code == 200, root_dir.text
        assert root_dir.json()["kind"] == "directory"
        assert {"one/part.txt", "two/part.txt"} <= {
            e["path"] for e in root_dir.json()["entries"]
        }

        # Omitted selection_path returns kind="selections" with callable per-selection readers.
        sel_index = client.get(
            "/api/v1/research-content",
            params={
                "quest_ref": quest_ref,
                "source_ref": dv["dataset_version_ref"],
                "version_ref": parent_binding.version_ref,
            },
        )
        assert sel_index.status_code == 200, sel_index.text
        assert sel_index.json()["kind"] == "selections"
        for item in sel_index.json()["items"]:
            r_params = {
                "quest_ref": quest_ref,
                "source_ref": item["reader"]["source_ref"],
                "version_ref": item["reader"]["version_ref"],
                "selection_path": (
                    ""
                    if item["reader"]["selection_path"] is None
                    else item["reader"]["selection_path"]
                ),
            }
            sub = client.get("/api/v1/research-content", params=r_params)
            assert sub.status_code == 200, sub.text
            assert sub.json()["kind"] == "directory"

        env_page = client.get(
            "/api/v1/research-library/environments", params={"quest_ref": quest_ref}
        )
        assert env_page.status_code == 200, env_page.text
        env_card = next(
            i
            for i in env_page.json()["items"]
            if i["environment_ref"] == env["environment_ref"]
        )
        assert [r["selection_path"] for r in env_card["readers"]] == ["one", "two"]
        env_sel = client.get(
            "/api/v1/research-content",
            params={
                "quest_ref": quest_ref,
                "source_ref": env["environment_ref"],
                "version_ref": parent_binding.version_ref,
            },
        )
        assert env_sel.status_code == 200 and env_sel.json()["kind"] == "selections"
        env_two = client.get(
            "/api/v1/research-content",
            params={
                "quest_ref": quest_ref,
                "source_ref": env["environment_ref"],
                "version_ref": parent_binding.version_ref,
                "selection_path": "two",
                "entry_path": "part.txt",
            },
        )
        assert env_two.status_code == 200
        assert env_two.json()["text"] == "selected scope two payload"
    finally:
        runtime.close()


def test_literature_pre_pagination_filtering_canonical_merge_and_rg_naming(
    tmp_path: Path,
) -> None:
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    from test_public_reasoning_stage import (
        _DeterministicReasoningSkill,
        _confirm_deepfetch_quest,
        _reasoning_runtime,
    )
    from test_research_datasets import _asset

    runtime = _reasoning_runtime(
        tmp_path / "lit-merge-filter",
        reasoning_skill=_DeterministicReasoningSkill(),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        quest_ref = str(quest["quest_ref"])
        q1_ref = str(quest["question_ref"])
        human = runtime.owners.human_collaboration
        graph = runtime.owners.research_graph
        rm = runtime.owners.research_memory

        # Pre-pagination query filtering: register 15 manual items with the target only in #0.
        for i in range(15):
            token = "needle-tail-token" if i == 0 else f"filler-paper-{i}"
            graph.register_literature(
                record_ref=f"arxiv:2601.{1000 + i}",
                title=f"Paper {i} ({token})",
                source="arXiv",
                fulltext_status="missing",
                asset_bindings=[],
                quest_ref=quest_ref,
                idempotency_key=f"bulk-lit-{i}",
            )
        hit = discover_literature(
            graph, rm, quest_ref=quest_ref, query="needle-tail-token", limit=2
        )
        assert hit["result_state"] == "matches"
        assert hit["scan_complete"] is True
        assert hit["next_offset"] is None
        assert [item["record_ref"] for item in hit["items"]] == ["arxiv:2601.1000"]

        miss = discover_literature(
            graph, rm, quest_ref=quest_ref, query="totally-nonexistent-clue", limit=2
        )
        assert miss["items"] == []
        assert miss["result_state"] == "no_match"
        assert miss["scan_complete"] is True
        assert miss["next_offset"] is None

        # Dual-track merge: DeepFetch has "doi:10.1000/example.one" on q1_ref.
        # Add manual registration with uppercase DOI + reading reference AND second Question
        # sharing DeepFetch record.
        seeded = _confirm_waived_manual_question(
            human,
            quest_ref=quest_ref,
            parent_question_ref=q1_ref,
            key_prefix="second-q-lit",
        )
        for _ in range(10):
            manual = human.query_manual_question_creation(seeded["context_ref"])
            if manual["status"] == "completed":
                break
            assert human.reconcile_once()
        assert manual["status"] == "completed"
        q2_obj = next(
            item
            for item in graph.query_question_tree(quest_ref=quest_ref)
            if item.question_ref != q1_ref
        )
        q2_ref = q2_obj.question_ref
        snap = rm.query_literature_snapshot(
            str(quest["proposal"]["literature_snapshot_ref"])
        )
        assert snap is not None
        rm.ensure_question_literature_revision(
            question_binding=q2_obj.as_binding(),
            source_snapshot_binding=snap.as_context_binding(),
            idempotency_key="q2-df-lit",
        )

        pdf = _asset(runtime, content=b"%PDF-1.4\nmerged doi", key="merged-pdf")
        graph.accept_asset_role(
            binding=pdf,
            role="evidence",
            quest_ref=quest_ref,
            idempotency_key="merged-pdf-role",
        )
        reg_upper = graph.register_literature(
            record_ref="doi:10.1000/EXAMPLE.ONE",
            title="Self-supervised denoising (manual track)",
            source="Manual DOI import",
            fulltext_status="obtained",
            asset_bindings=[pdf.as_dict()],
            quest_ref=quest_ref,
            idempotency_key="manual-doi-merge",
        )
        graph.reference_literature(
            literature_ref=reg_upper["ref"],
            question_ref=q2_ref,
            purpose="Cross-check DeepFetch finding manually",
            reading_summary="Manual judgment merged with DeepFetch card.",
            quest_ref=quest_ref,
            idempotency_key="manual-doi-ref",
        )

        merged = discover_literature(
            graph, rm, quest_ref=quest_ref, record_ref="doi:10.1000/Example.One"
        )
        assert len(merged["items"]) == 1
        card = merged["items"][0]
        assert set(card["question_refs"]) == {q1_ref, q2_ref}
        assert card["deepfetch_reader"]["source_ref"] == "doi:10.1000/example.one"
        assert len(card["readers"]) == 1
        assert any(
            j.get("reference_ref") and "Manual judgment" in j["summary"]
            for j in card["judgments"]
        )

        # Verify research_graph.literature.* naming, owning_module, and RG -> Literature routing.
        expected_ids = {
            "research_graph.literature.page",
            "research_graph.literature.register",
            "research_graph.literature.register.reconcile",
            "research_graph.literature.reference",
            "research_graph.literature.reference.reconcile",
            "research_graph.literature.read",
        }
        assert set(ROOT_AGENT_LITERATURE_OPERATION_IDS) <= expected_ids
        assert expected_ids <= set(ROOT_AGENT_SEMANTIC_OPERATION_IDS["companion"])
        for op_id in expected_ids:
            op = runtime.harnesses._gateway._operations[op_id]
            assert op.owning_module == "research_graph"
            paths = purpose_paths(op_id, op.owning_module, op.access_mode)
            assert ("RG", "Literature") in paths
    finally:
        runtime.close()


def test_question_history_stages_creation_basis_and_multistage_typed_relations(
    tmp_path: Path,
) -> None:
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    from test_public_plan_stage import _finish_idea_stage
    from test_public_reasoning_stage import (
        _DeterministicReasoningSkill,
        _confirm_deepfetch_quest,
        _reasoning_runtime,
        _tick_reasoning,
    )

    runtime = _reasoning_runtime(
        tmp_path / "q-history-rels",
        reasoning_skill=_DeterministicReasoningSkill(),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        quest_ref = str(quest["quest_ref"])
        q1_ref = str(quest["question_ref"])
        human = runtime.owners.human_collaboration
        graph = runtime.owners.research_graph

        # Spawn a second Question in the same Quest.
        seeded = _confirm_waived_manual_question(
            human,
            quest_ref=quest_ref,
            parent_question_ref=q1_ref,
            key_prefix="q2-relation",
        )
        for _ in range(10):
            manual = human.query_manual_question_creation(seeded["context_ref"])
            if manual["status"] == "completed":
                break
            assert human.reconcile_once()
        assert manual["status"] == "completed"
        q2_ref = next(
            item.question_ref
            for item in graph.query_question_tree(quest_ref=quest_ref)
            if item.question_ref != q1_ref
        )

        # Admit Idea run, record directed & symmetric relations from Idea, then finish Idea.
        idea_run = _admit_idea_run(runtime, str(quest["cycle_ref"]))
        idea_chan = runtime.harnesses.issue_resident_mcp_channel(
            run_ref=idea_run.run_ref,
            attempt_ref=idea_run.attempt_ref,
            root_session_ref=idea_run.root_session_ref,
            fence_ref=idea_run.fence_ref,
            capability_binding_hash=idea_run.runtime_binding_hash,
            root_kind="idea",
            phase="primary",
            subject_policy="operation_tree",
            operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS["idea"],
        )

        high_q, low_q = sorted((q1_ref, q2_ref), reverse=True)
        directed = _mcp_call(
            runtime,
            idea_chan,
            "research_graph.question_relations.record",
            effect_id="idea-directed-rel",
            left_question_ref=high_q,
            right_question_ref=low_q,
            kind="depends_on",
            notes="Directed prerequisite recorded during Idea.",
        )
        assert not directed.get("isError"), directed
        dir_rel = directed["structuredContent"]
        assert dir_rel["kind"] == "depends_on"
        assert (dir_rel["left_question_ref"], dir_rel["right_question_ref"]) == (
            high_q,
            low_q,
        )

        sym = _mcp_call(
            runtime,
            idea_chan,
            "research_graph.question_relations.record",
            effect_id="idea-sym-rel",
            left_question_ref=high_q,
            right_question_ref=low_q,
            kind="overlaps",
            notes="Symmetric overlap recorded during Idea.",
        )
        assert not sym.get("isError"), sym
        sym_rel = sym["structuredContent"]
        assert sym_rel["kind"] == "overlaps"
        assert (sym_rel["left_question_ref"], sym_rel["right_question_ref"]) == (
            low_q,
            high_q,
        )

        # Finish Idea stage (NoViableCandidate), BEFORE Reasoning runs.
        _finish_idea_stage(runtime)

        # Second DeepFetch Quest so Companion in other_quest can test cross-Quest reading.
        other_quest = _confirm_deepfetch_quest_with_prefix(runtime, "df-second")
        other_quest_ref = str(other_quest["quest_ref"])
        auth = human.query_broad_research_authorization(quest_ref)
        assert auth is not None and auth["effective_decision"] == "granted"
        comp_other, _ = _companion_channel(
            runtime, quest_ref=other_quest_ref, run_ref="comp-cross-cb"
        )
        denied_comp = _mcp_call(
            runtime,
            comp_other,
            "research_graph.question_relations.record",
            effect_id="comp-try",
            left_question_ref=q1_ref,
            right_question_ref=q2_ref,
            notes="Should fail",
        )
        assert denied_comp.get("isError")
        assert (
            denied_comp["structuredContent"]["details"]["reason"]
            == "root_kind_not_owner"
        )

        # Companion cross-Quest reads: questions.page, question_history.read, creation_basis.read.
        q_page = _mcp_call(
            runtime,
            comp_other,
            "research_graph.questions.page",
            source_quest_ref=quest_ref,
        )
        assert not q_page.get("isError"), q_page
        q1_card = next(
            i for i in q_page["structuredContent"]["items"] if i["question_ref"] == q1_ref
        )
        creation_bases = runtime.owners.research_memory.creation_bases
        expected_cb = creation_bases.reference(
            creation_bases.for_question(q1_ref, quest_ref)
        )
        assert q1_card["relation_count"] == 2
        assert q1_card["creation_basis"]["basis_ref"] == expected_cb["basis_ref"]

        hist = _mcp_call(
            runtime,
            comp_other,
            "research_graph.question_history.read",
            source_quest_ref=quest_ref,
            question_ref=q1_ref,
        )
        assert not hist.get("isError"), hist
        hist_data = hist["structuredContent"]
        assert hist_data["creation_basis"]["basis_ref"] == expected_cb["basis_ref"]
        cb_reader = hist_data["creation_basis"]["reader"]
        assert cb_reader["source_quest_ref"] == quest_ref
        cb_op_read = _mcp_call(
            runtime,
            comp_other,
            cb_reader["operation"],
            **{k: v for k, v in cb_reader.items() if k != "operation"},
        )
        assert not cb_op_read.get("isError"), cb_op_read

        # Web UI /api/v1/research-library/questions shows Idea stage BEFORE Reasoning + creation_basis + relations.
        client = _web_client(runtime)
        web_hist = client.get(
            "/api/v1/research-library/questions",
            params={"quest_ref": quest_ref, "question_ref": q1_ref},
        )
        assert web_hist.status_code == 200, web_hist.text
        wh = web_hist.json()
        assert any(
            item["stage"] == "idea" and item["disposition"] == "completed"
            for item in wh["items"]
        )
        assert wh["creation_basis_reader"]["source_ref"] == expected_cb["basis_ref"]
        idea_item = next(item for item in wh["items"] if item["stage"] == "idea")
        assert (
            client.get(
                "/api/v1/research-content",
                params={"quest_ref": quest_ref, **idea_item["reader"]},
            ).status_code
            == 200
        )
        cb_web = client.get(
            "/api/v1/research-content",
            params={"quest_ref": quest_ref, **wh["creation_basis_reader"]},
        )
        assert cb_web.status_code == 200, cb_web.text

        # Advance Reasoning into an admitted run and confirm Reasoning can also record relations.
        _tick_reasoning(runtime)
        cur_reason = _tick_reasoning(runtime)
        reason_run = runtime.owners.agent_runtime.query_reasoning_stage_run(
            cur_reason["stage_run_request"]["request_ref"]
        )
        assert reason_run is not None
        reason_chan = runtime.harnesses.issue_resident_mcp_channel(
            run_ref=reason_run.run_ref,
            attempt_ref=reason_run.attempt_ref,
            root_session_ref=reason_run.root_session_ref,
            fence_ref=reason_run.fence_ref,
            capability_binding_hash=reason_run.runtime_binding_hash,
            root_kind="reasoning",
            phase="primary",
            subject_policy="operation_tree",
            operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS["reasoning"],
        )
        reason_rel = _mcp_call(
            runtime,
            reason_chan,
            "research_graph.question_relations.record",
            effect_id="reasoning-rel",
            left_question_ref=q1_ref,
            right_question_ref=q2_ref,
            kind="supersedes",
            notes="Reasoning stage supersedes relation.",
        )
        assert not reason_rel.get("isError"), reason_rel

        web_rels = client.get(
            "/api/v1/research-library/questions",
            params={"quest_ref": quest_ref, "question_ref": q1_ref, "view": "relations"},
        )
        assert web_rels.status_code == 200, web_rels.text
        assert {item["relation_kind"] for item in web_rels.json()["items"]} == {
            "depends_on",
            "overlaps",
            "supersedes",
        }

        # Direct SQL tampering of relation_kind on non-related row triggers integrity error.
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rg_question_relations SET relation_kind='refines' WHERE relation_ref=:ref"
                ),
                {"ref": dir_rel["relation_ref"]},
            )
        with pytest.raises(OwnerConflict, match="question_relation_content_invalid"):
            graph.query_question_relations(quest_ref=quest_ref)
    finally:
        runtime.close()


def test_question_relation_writers_include_plan_bundle_and_reasoning_not_target(
    tmp_path: Path,
) -> None:
    from test_public_bundle_stage import _bundle_runtime, _finish_idea_stage
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    from test_public_plan_stage import _confirm_direct_quest
    from test_root_workspace import _bundle_context

    runtime = _bundle_runtime(tmp_path / "rel-stages")
    try:
        quest = _confirm_direct_quest(runtime)
        quest_ref = str(quest["quest_ref"])
        q1_ref = str(quest["question_ref"])
        human = runtime.owners.human_collaboration
        graph = runtime.owners.research_graph

        seeded = _confirm_waived_manual_question(
            human, quest_ref=quest_ref, parent_question_ref=q1_ref, key_prefix="q2-stages"
        )
        for _ in range(10):
            m = human.query_manual_question_creation(seeded["context_ref"])
            if m["status"] == "completed":
                break
            assert human.reconcile_once()
        q2_ref = next(
            item.question_ref
            for item in graph.query_question_tree(quest_ref=quest_ref)
            if item.question_ref != q1_ref
        )

        # Finish Idea -> enter Plan -> admit Plan run.
        _finish_idea_stage(runtime)
        assert runtime.plan_stage.process_once()
        assert runtime.plan_stage.process_once()
        plan_cur = runtime.plan_stage.query_current()
        plan_run = runtime.owners.agent_runtime.query_plan_stage_run(
            plan_cur["stage_run_request"]["request_ref"]
        )
        assert plan_run is not None
        plan_chan = runtime.harnesses.issue_resident_mcp_channel(
            run_ref=plan_run.run_ref,
            attempt_ref=plan_run.attempt_ref,
            root_session_ref=plan_run.root_session_ref,
            fence_ref=plan_run.fence_ref,
            capability_binding_hash=plan_run.runtime_binding_hash,
            root_kind="plan",
            phase="primary",
            subject_policy="operation_tree",
            operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS["plan"],
        )
        plan_rel = _mcp_call(
            runtime,
            plan_chan,
            "research_graph.question_relations.record",
            effect_id="plan-rel",
            left_question_ref=q2_ref,
            right_question_ref=q1_ref,
            kind="subquestion_of",
            notes="Plan subquestion relation.",
        )
        assert not plan_rel.get("isError"), plan_rel
        assert plan_rel["structuredContent"]["kind"] == "subquestion_of"

        # Complete Plan -> enter Bundle -> admit Bundle run.
        for _ in range(12):
            if runtime.plan_stage.query_current()["stage_commit"] is not None:
                break
            assert runtime.plan_stage.process_once()
        assert runtime.plan_stage.query_current()["stage_commit"] is not None
        assert runtime.bundle_stage.process_once()
        assert runtime.bundle_stage.process_once()
        bundle_ctx = _bundle_context(runtime)
        bundle_chan = runtime.harnesses.issue_resident_mcp_channel(
            run_ref=bundle_ctx.run_ref,
            attempt_ref=bundle_ctx.attempt_ref,
            root_session_ref=bundle_ctx.root_session_ref,
            fence_ref=bundle_ctx.fence_ref,
            capability_binding_hash=bundle_ctx.capability_binding_hash,
            root_kind="bundle",
            phase="dispatch",
            subject_policy="operation_tree",
            operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS["bundle"],
        )
        bundle_rel = _mcp_call(
            runtime,
            bundle_chan,
            "research_graph.question_relations.record",
            effect_id="bundle-rel",
            left_question_ref=q1_ref,
            right_question_ref=q2_ref,
            kind="refines",
            notes="Bundle relation.",
        )
        assert not bundle_rel.get("isError"), bundle_rel
        assert bundle_rel["structuredContent"]["kind"] == "refines"

        for op_id in (
            "research_graph.question_relations.record",
            "research_graph.question_relations.record.reconcile",
        ):
            assert _ROLE_ONLY_OPERATION_OWNERS[op_id] == frozenset(
                {"idea", "plan", "bundle", "reasoning"}
            )
        target_chan, _ = runtime.harnesses._gateway.issue_channel(
            run_ref="target-rel-denied",
            attempt_ref="a1",
            root_session_ref="s1",
            fence_ref="f1",
            capability_binding_hash="t" * 64,
            root_kind="target",
            phase="execution",
            operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS["target"],
        )
        denied_target = _gateway_call(
            runtime.harnesses._gateway,
            target_chan,
            "research_graph.question_relations.record",
            effect_id="target-rel",
            left_question_ref=q1_ref,
            right_question_ref=q2_ref,
            notes="Target must not write question relations",
        )
        assert denied_target.get("isError")
        assert (
            denied_target["structuredContent"]["details"]["reason"]
            == "root_kind_not_owner"
        )
        with pytest.raises(OwnerConflict, match="question_relation_source_invalid"):
            graph.record_question_relation(
                quest_ref=quest_ref,
                source_run_ref=bundle_ctx.run_ref,
                effect_id="wrong-stage",
                left_question_ref=q1_ref,
                right_question_ref=q2_ref,
                notes="Source stage mismatch",
                kind="related",
                source_stage="target",
            )
    finally:
        runtime.close()


def test_human_response_search_focused_paging_tools_and_baseline_web_fixes(
    tmp_path: Path,
) -> None:
    from test_baseline_discovery_search import _evaluation, _variant
    from test_public_human_collaboration_ladder import (
        _HumanRequestLifecycleDraftingProvider,
        _confirm_direct_quest,
        _runtime as _hc_runtime,
    )
    from test_root_formal_entities import _accept
    from test_target_root_finalizer import _completion_handoff, _root_finalizer_fixture

    # 1. Human response note/facts search + 4 focused single-view paging tools.
    provider = _HumanRequestLifecycleDraftingProvider()
    hc_runtime = _hc_runtime(tmp_path / "hc-audit", provider)
    try:
        human = hc_runtime.owners.human_collaboration
        provider.owner = hc_runtime.owners.agent_runtime
        confirmed = _confirm_direct_quest(hc_runtime)
        for _ in range(5):
            if not human.reconcile_once():
                break
        quest_ref = str(
            human.query_quest_creation(confirmed["initialization_id"])["quest_ref"]
        )
        human.send_companion_message(
            f"quest:{quest_ref}",
            "Trigger human request for response search test.",
            "hc-msg-1",
        )
        assert not human.process_drafting_once()
        assert provider.human_request is not None
        req_ref = str(provider.human_request["request_ref"])

        human.respond_to_human_request(
            req_ref,
            decision="deferred",
            facts={"approval_code": "FACT-NEEDLE-9981"},
            note="Human operator wrote NOTE-NEEDLE-7742 in reply.",
            idempotency_key="hc-resp-1",
        )

        # Searches response note and response facts_json.
        by_note = human.query_research_help_page(
            quest_ref=quest_ref, query="NOTE-NEEDLE-7742"
        )
        assert [item["request_ref"] for item in by_note["items"]] == [req_ref]
        by_facts = human.query_research_help_page(
            quest_ref=quest_ref, query="FACT-NEEDLE-9981"
        )
        assert [item["request_ref"] for item in by_facts["items"]] == [req_ref]

        # Seed inputs & guidance for the 4 focused paging tools.
        human.submit_research_input(
            quest_ref=quest_ref, text_content="Focused input alpha", idempotency_key="in-1"
        )
        human.submit_research_input(
            quest_ref=quest_ref, text_content="Focused input beta", idempotency_key="in-2"
        )
        guide = human.submit_human_guidance(
            quest_ref=quest_ref,
            original_text="Focused guidance constraint one",
            strength=3,
            idempotency_key="gd-1",
        )
        human.submit_human_guidance(
            quest_ref=quest_ref,
            original_text="Focused guidance constraint two",
            strength=4,
            idempotency_key="gd-2",
        )

        chan, _ = _companion_channel(hc_runtime, quest_ref=quest_ref, run_ref="hc-comp")
        resp_page = _mcp_call(
            hc_runtime,
            chan,
            "human_request.responses.page",
            request_ref=req_ref,
            offset=0,
            limit=1,
        )
        assert not resp_page.get("isError"), resp_page
        assert len(resp_page["structuredContent"]["items"]) == 1

        in_p1 = _mcp_call(
            hc_runtime,
            chan,
            "human_collaboration.inputs.page",
            query="Focused input",
            offset=0,
            limit=1,
        )["structuredContent"]
        in_p2 = _mcp_call(
            hc_runtime,
            chan,
            "human_collaboration.inputs.page",
            query="Focused input",
            offset=in_p1["next_offset"],
            limit=1,
            query_revision=in_p1["query_revision"],
        )["structuredContent"]
        assert len(in_p1["items"]) == 1 and len(in_p2["items"]) == 1

        gd_p1 = _mcp_call(
            hc_runtime,
            chan,
            "human_collaboration.guidance.page",
            query="Focused guidance",
            offset=0,
            limit=1,
        )["structuredContent"]
        gd_p2 = _mcp_call(
            hc_runtime,
            chan,
            "human_collaboration.guidance.page",
            query="Focused guidance",
            offset=gd_p1["next_offset"],
            limit=1,
            query_revision=gd_p1["query_revision"],
        )["structuredContent"]
        assert len(gd_p1["items"]) == 1 and len(gd_p2["items"]) == 1

        tr_p1 = _mcp_call(
            hc_runtime,
            chan,
            "human_collaboration.guidance.treatments.page",
            constraint_ref=guide["constraint_ref"],
            version_ref=canonical_hash(guide["guidance"]),
            offset=0,
            limit=1,
        )
        assert not tr_p1.get("isError"), tr_p1
        assert tr_p1["structuredContent"]["result_state"] == "no_records"

        for op_id in (
            "human_request.responses.page",
            "human_collaboration.inputs.page",
            "human_collaboration.guidance.page",
            "human_collaboration.guidance.treatments.page",
        ):
            op = hc_runtime.harnesses._gateway._operations[op_id]
            paths = purpose_paths(op_id, op.owning_module, op.access_mode)
            assert ("RG", "Human Interact") in paths
    finally:
        hc_runtime.close()

    # 2. Baseline Web fixes: implementation_artifacts in /api/v1/research-formal
    #    & independent runs / evaluations pagination in /api/v1/research-library/baselines.
    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path / "baseline-web")
    )
    try:
        doc_path = workspace / "outputs" / "result.json"
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        checkpoints = [
            item.relative_path
            for item in _completion_handoff(workspace, evidence).artifacts
            if item.role == "checkpoint"
        ]
        doc["formal_runs"] = [
            {
                "run_key": "web-run-1",
                "checkpoint_paths": checkpoints,
                "artifact_paths": ["logs/train.log"],
                "evaluations": [
                    {"attempt_key": "eval-a1", "metrics": doc["metrics"]}
                ],
            },
            {
                "run_key": "web-run-2",
                "checkpoint_paths": checkpoints,
                "artifact_paths": ["logs/train.log"],
                "evaluations": [],
            },
        ]
        doc_path.write_text(canonical_json(doc), encoding="utf-8")
        _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        quest_ref = graph.query_target_graph(authority.stage_request_ref).quest_ref
        baseline_ref = authority.identities.baseline_ref
        variant_ref = authority.identities.variant_ref

        _variant(runtime, baseline_ref, "extra")
        _evaluation(
            runtime,
            SimpleNamespace(quest_ref=quest_ref),
            variant_ref,
            "second-proto",
            description="Second eval",
        )
        client = _web_client(runtime)

        variant_page = client.get(
            "/api/v1/research-library/baselines",
            params={
                "quest_ref": quest_ref,
                "baseline_ref": baseline_ref,
                "variant_ref": variant_ref,
                "limit": 1,
            },
        )
        assert variant_page.status_code == 200, variant_page.text
        v_item = variant_page.json()["items"][0]
        assert len(v_item["runs"]) == 1
        assert v_item["runs_next_offset"] == 1
        assert len(v_item["evaluations"]) == 1
        assert v_item["evaluations_next_offset"] == 1

        run_p1 = client.get(
            "/api/v1/research-library/baselines",
            params={
                "quest_ref": quest_ref,
                "baseline_ref": baseline_ref,
                "variant_ref": variant_ref,
                "relation": "runs",
                "limit": 1,
                "offset": 0,
            },
        ).json()
        run_p2 = client.get(
            "/api/v1/research-library/baselines",
            params={
                "quest_ref": quest_ref,
                "baseline_ref": baseline_ref,
                "variant_ref": variant_ref,
                "relation": "runs",
                "limit": 1,
                "offset": run_p1["next_offset"],
                "query_revision": run_p1["query_revision"],
            },
        ).json()
        assert run_p1["items"][0]["variant_run_ref"] != run_p2["items"][0]["variant_run_ref"]
        assert run_p2["next_offset"] is None

        ev_p1 = client.get(
            "/api/v1/research-library/baselines",
            params={
                "quest_ref": quest_ref,
                "baseline_ref": baseline_ref,
                "variant_ref": variant_ref,
                "relation": "evaluations",
                "limit": 1,
                "offset": 0,
            },
        ).json()
        ev_p2 = client.get(
            "/api/v1/research-library/baselines",
            params={
                "quest_ref": quest_ref,
                "baseline_ref": baseline_ref,
                "variant_ref": variant_ref,
                "relation": "evaluations",
                "limit": 1,
                "offset": ev_p1["next_offset"],
                "query_revision": ev_p1["query_revision"],
            },
        ).json()
        assert ev_p1["items"][0]["evaluation_ref"] != ev_p2["items"][0]["evaluation_ref"]
        assert ev_p2["next_offset"] is None

        formal = client.get(
            "/api/v1/research-formal",
            params={"quest_ref": quest_ref, "ref": v_item["runs"][0]["variant_run_ref"]},
        )
        assert formal.status_code == 200, formal.text
        f_data = formal.json()
        impl_versions = {
            art["version_ref"] for art in f_data["result"]["implementation_artifacts"]
        }
        assert impl_versions
        item_versions = {item["reader"]["version_ref"] for item in f_data["items"]}
        assert impl_versions <= item_versions
        for item in f_data["items"]:
            content_resp = client.get(
                "/api/v1/research-content",
                params={"quest_ref": quest_ref, **item["reader"]},
            )
            assert content_resp.status_code == 200, content_resp.text
    finally:
        runtime.close()


def test_cycle2_idea_rebinds_changed_guidance_without_stale_deadlock(
    tmp_path: Path,
) -> None:
    from test_public_plan_stage import _finish_idea_stage
    from test_public_reasoning_stage import (
        _DeterministicReasoningSkill,
        _confirm_deepfetch_quest,
        _reasoning_runtime,
        _tick_reasoning,
    )

    runtime = _reasoning_runtime(
        tmp_path / "cycle2-guidance",
        reasoning_skill=_DeterministicReasoningSkill(),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        quest_ref = str(quest["quest_ref"])
        human = runtime.owners.human_collaboration

        g0 = human.submit_human_guidance(
            quest_ref=quest_ref,
            original_text="Cycle 1 initial constraint.",
            strength=3,
            idempotency_key="c1-guidance",
        )
        idea_commit = _finish_idea_stage(runtime)
        assert idea_commit["stage_commit"]["outcome_kind"] == "NoViableCandidate"

        # Complete Cycle 1 Reasoning -> AE creates Cycle 2 active foreground in Idea stage.
        fg = None
        for _ in range(12):
            _tick_reasoning(runtime)
            fg = runtime.owners.advancement_engine.query_foreground(quest_ref)
            if (
                fg is not None
                and fg["stage"] == "idea"
                and fg["cycle_ref"] != str(quest["cycle_ref"])
            ):
                break
        assert fg is not None
        assert fg["stage"] == "idea"
        cycle2_ref = str(fg["cycle_ref"])
        assert cycle2_ref != str(quest["cycle_ref"])

        successor = runtime.owners.advancement_engine.query_reasoning_successor_context(
            cycle2_ref
        )
        assert successor is not None
        assert [
            b["constraint_ref"]
            for b in successor["idea_context_pack"]["active_guidance_bindings"]
        ] == [g0["constraint_ref"]]

        # Mutate formal guidance AFTER Cycle 1 froze successor context and BEFORE Cycle 2
        # Idea Stage creates its StageRunRequest.
        human.withdraw_soft_constraint(
            str(g0["constraint_ref"]), int(g0["revision"]), "c2-withdraw-g0"
        )
        g1 = human.submit_human_guidance(
            quest_ref=quest_ref,
            original_text="Cycle 2 updated ablation constraint.",
            strength=5,
            idempotency_key="c2-guidance-g1",
        )

        # Must create Cycle 2 Idea StageRunRequest without guidance_bindings_stale.
        assert runtime.idea_stage.process_once() is True
        assert runtime.idea_stage.transient_error is None

        creation_bases = runtime.owners.research_memory.creation_bases
        expected_cb = creation_bases.reference(
            creation_bases.for_question(str(quest["question_ref"]), quest_ref)
        )
        req = runtime.owners.advancement_engine.query_idea_stage_request(cycle2_ref)
        assert req is not None
        assert [
            b["constraint_ref"] for b in req.context_pack["active_guidance_bindings"]
        ] == [g1["constraint_ref"]]
        assert req.context_pack.get("creation_basis") == expected_cb
    finally:
        runtime.close()
