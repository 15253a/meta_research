from sqlalchemy import text
import pytest

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.runtime_conditions import render_runtime_conditions
from test_corrected_quest_initialization import (
    DeterministicDraftingAdapter, DeterministicProbe, _authenticated_client, _ready_v2,
)


@pytest.mark.parametrize('style', ['focus', 'balanced', 'open'])
def test_creation_style_survives_acceptance_and_authenticated_configuration_change(tmp_path, style):
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(tmp_path/'style-web'),
        proposal_drafter=adapter, intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe())
    try:
        client, headers = _authenticated_client(runtime)
        opened_response = client.post('/api/v1/quest-initializations', json={},
            headers={**headers, 'Idempotency-Key': 'style-open'})
        assert opened_response.status_code == 201, opened_response.text
        opened = opened_response.json()
        assert opened['quest_draft']['value']['research_style'] == 'balanced'
        initialization = opened['initialization_id']
        hc = runtime.owners.human_collaboration
        probed = hc.observe_host_compute(initialization, ['GPU-test-1'], 'style-probe')
        draft = {**probed['quest_draft']['value'], 'goal': '保留既定研究条件。',
            'completion_criteria': '验证小型样例。', 'time_budget': '30d', 'research_style': style}
        saved_response = client.put(f'/api/v1/quest-initializations/{initialization}/draft',
            json={'draft': draft, 'expected_draft_revision': probed['quest_draft']['revision'],
                'expected_draft_hash': probed['quest_draft']['hash']},
            headers={**headers, 'Idempotency-Key': 'style-draft'})
        assert saved_response.status_code == 200, saved_response.text
        saved = saved_response.json()
        readback = client.get(f'/api/v1/quest-initializations/{initialization}').json()['quest_draft']['value']
        assert readback == saved['quest_draft']['value']
        assert readback['research_style'] == style and readback['goal'] == '保留既定研究条件。'
        hc.generate_question_proposal(initialization, saved['quest_draft']['hash'],
            'style-generate', saved['quest_draft']['revision'])
        assert hc.process_drafting_once()
        ready = hc.query_quest_creation(initialization)
        hc.confirm_quest(initialization, quest_draft_revision=ready['quest_draft']['revision'],
            quest_draft_hash=ready['quest_draft']['hash'], proposal_ref=ready['proposal']['ref'],
            proposal_hash=ready['proposal']['hash'], preview_ref=ready['confirmation_preview']['ref'],
            preview_hash=ready['confirmation_preview']['hash'], idempotency_key='style-confirm')
        for _ in range(8): hc.reconcile_once()
        quest = runtime.owners.research_graph.query_quest(initialization)
        assert quest is not None
        url = f'/api/v1/quests/{quest.quest_ref}/runtime-conditions'
        current = client.get(url).json()
        assert current['research_style'] == style and '30d' in current['text']
        changed = client.put(url, json={'text': current['text'], 'research_style': 'open',
            'expected_revision': current['revision']}, headers=headers)
        assert changed.status_code == 200, changed.text
        assert changed.json()['research_style'] == 'open'
        assert changed.json()['text'] == current['text']
        assert client.get(url).json() == changed.json()
    finally:
        runtime.close()


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
