from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.runtime_conditions import render_runtime_conditions
from test_corrected_quest_initialization import (
    DeterministicDraftingAdapter, DeterministicProbe, _authenticated_client, _ready_v2,
)


def test_edit_conditions_through_authenticated_api_keeps_research_facts(tmp_path):
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "conditions-web"), proposal_drafter=adapter,
        intent_drafting_provider=adapter, host_compute_probe=DeterministicProbe(),
    )
    try:
        hc = runtime.owners.human_collaboration
        ready = _ready_v2(runtime, "runtime-conditions")
        hc.confirm_quest(
            ready["initialization_id"],
            quest_draft_revision=ready["quest_draft"]["revision"],
            quest_draft_hash=ready["quest_draft"]["hash"],
            proposal_ref=ready["proposal"]["ref"], proposal_hash=ready["proposal"]["hash"],
            preview_ref=ready["confirmation_preview"]["ref"],
            preview_hash=ready["confirmation_preview"]["hash"],
            idempotency_key="conditions-confirm",
        )
        for _ in range(8):
            hc.reconcile_once()
        with runtime._database.read() as connection:
            quest = dict(connection.execute(text("SELECT * FROM rg_quests")).mappings().one())
        client, headers = _authenticated_client(runtime)
        url = f"/api/v1/quests/{quest['quest_ref']}/runtime-conditions"
        initial = client.get(url)
        assert initial.status_code == 200
        value = initial.json()
        assert "GPU-test-1" in value["text"] and "81920" in value["text"]
        assert "30d" in value["text"]
        payload = {"text": "仅使用 GPU-test-1；优先完成 CPU 核验，再按需训练。", "expected_revision": value["revision"]}
        assert client.put(url, json=payload).status_code == 403
        saved = client.put(url, json=payload, headers=headers)
        assert saved.status_code == 200
        assert saved.json()["revision"] != value["revision"]
        assert client.get(url).json() == saved.json()
        assert payload["text"] in render_runtime_conditions(runtime.data_root.root, quest_ref=quest["quest_ref"])
        assert client.put(url, json=payload, headers=headers).status_code == 409
        assert client.get(url).json() == saved.json()
        assert client.put(url, json={**payload, "text": " "}, headers=headers).status_code == 409
        with runtime._database.read() as connection:
            assert dict(connection.execute(text("SELECT * FROM rg_quests")).mappings().one()) == quest
    finally:
        runtime.close()
