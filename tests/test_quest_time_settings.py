from meta_research.paths import prepare_data_root
from test_idea_owner_integrity import _confirm_question_with_prefix
from test_idea_stage_recovery import _IdeaProvider, _runtime
from test_public_human_collaboration_web import _authenticated_client


def test_human_configuration_updates_the_same_budget_once_and_keeps_used_time(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(prepare_data_root(tmp_path / "time-settings"), _IdeaProvider())
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_question_with_prefix(runtime, "time-settings")["quest_ref"]
        client, headers = _authenticated_client(runtime)
        path = f"/api/v1/quests/{quest}/runtime-conditions"
        clock_path = f"/api/v1/quests/{quest}/research-time"
        now[0] = 1060.0
        observed = client.get(clock_path)
        assert observed.status_code == 200, observed.text
        assert observed.json() == ae.query_research_time(quest)
        current = client.get(path).json()
        body = {"text": "仅使用已批准的设备。", "expected_revision": current["revision"],
                "time_budget_change": {"expected_revision": observed.json()["budget_revision"], "total_seconds": 7200}}
        assert client.put(path, json=body).status_code == 403
        saved = client.put(path, json=body, headers=headers)
        assert saved.status_code == 200, saved.text
        after = client.get(clock_path).json()
        assert after["approved_seconds"] == 7200
        assert after["used_seconds"] == 60
        assert after["remaining_seconds"] == 7140
        now[0] = 1120.0
        replay = client.put(path, json=body, headers=headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["revision"] == saved.json()["revision"]
        assert client.get(clock_path).json()["remaining_seconds"] == 7080
        stale = {**body, "text": "新文字不应在预算冲突时被保存。", "expected_revision": saved.json()["revision"]}
        rejected = client.put(path, json=stale, headers=headers)
        assert rejected.status_code == 409, rejected.text
        assert client.get(path).json()["text"] == saved.json()["text"]
    finally:
        runtime.close()
