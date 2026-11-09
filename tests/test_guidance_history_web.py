import json

from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_public_research_asset_roles import _runtime, _accepted_quest


def _client(runtime, base_url="http://testserver"):
    client = TestClient(create_app(runtime, base_url=base_url, control_key="isolated-test"), base_url=base_url)
    auth = client.post("/auth/bootstrap", headers={"Origin": base_url},
                       json={"token": runtime.authentication.issue_bootstrap_token()})
    assert auth.status_code == 200
    return client


def test_human_library_discovers_exact_guidance_without_consuming_it(tmp_path):
    runtime = _runtime(tmp_path / "runtime")
    try:
        quest = _accepted_quest(runtime).quest_ref
        foreign = "quest_foreign"
        hc = runtime.owners.human_collaboration
        guide = hc.submit_human_guidance(quest_ref=quest, original_text="Retain the failed controls.",
                                         strength=4, idempotency_key="formal")
        hc.submit_research_input(quest_ref=quest, text_content="A new observation.", idempotency_key="input")
        client = _client(runtime)
        anonymous = TestClient(client.app)
        assert anonymous.get("/api/v1/research-library/human", params={"quest_ref": quest}).status_code == 401
        revision = hc.query_snapshot().revision
        page = client.get("/api/v1/research-library/human", params={"quest_ref": quest})
        assert page.status_code == 200, page.text
        assert len(page.json()["items"]) == 2
        item = next(item for item in page.json()["items"] if item.get("constraint_ref") == guide["constraint_ref"])
        reader = item["reader"]
        exact = client.get("/api/v1/research-content", params={"quest_ref": quest, **reader})
        assert exact.status_code == 200, exact.text
        original = json.loads(exact.json()["text"])
        assert original["guidance"]["text"] == "Retain the failed controls."
        assert original["treatment_count"] == 0
        assert original["treatment_state"] == "not_processed"
        treatments = client.get("/api/v1/research-library/human", params={
            "quest_ref": quest, "constraint_ref": guide["constraint_ref"], "version_ref": reader["version_ref"]})
        assert treatments.status_code == 200, treatments.text
        assert treatments.json()["result_state"] == "no_records"
        assert client.get("/api/v1/research-content", params={"quest_ref": foreign, **reader}).status_code == 409
        assert client.get("/api/v1/research-content", params={"quest_ref": quest, **reader, "version_ref": "wrong"}).status_code == 409
        assert anonymous.get("/api/v1/research-content", params={"quest_ref": quest, **reader}).status_code == 401
        assert hc.query_guidance_deliveries(guide["constraint_ref"]) == []
        assert hc.query_snapshot().revision == revision
    finally:
        runtime.close()


def test_guidance_pages_bind_query_and_keep_empty_states_distinct(tmp_path):
    runtime = _runtime(tmp_path / "runtime")
    try:
        quest = _accepted_quest(runtime).quest_ref
        hc = runtime.owners.human_collaboration
        client = _client(runtime)
        empty = client.get("/api/v1/research-library/human", params={"quest_ref": quest}).json()
        assert empty["result_state"] == "no_records"
        for index in range(3):
            hc.submit_human_guidance(quest_ref=quest, original_text=f"Preserve uncertainty {index}.",
                                     strength=3, idempotency_key=f"formal-{index}")
        args = {"quest_ref": quest, "limit": 1, "query": "uncertainty"}
        first = client.get("/api/v1/research-library/human", params=args).json()
        continuation = {**args, "guidance_offset": first["guidance_next_offset"],
                        "guidance_query_revision": first["guidance_query_revision"]}
        second = client.get("/api/v1/research-library/human", params=continuation)
        assert second.status_code == 200, second.text
        assert first["items"][0]["ref"] != second.json()["items"][0]["ref"]
        changed = client.get("/api/v1/research-library/human", params={**continuation, "query": "other"})
        assert changed.status_code == 409
        assert changed.json()["detail"]["code"] == "research_query_changed"
        hc.submit_human_guidance(quest_ref=quest, original_text="Additional uncertainty.",
                                 strength=3, idempotency_key="later")
        assert client.get("/api/v1/research-library/human", params=continuation).status_code == 409
        no_match = client.get("/api/v1/research-library/human", params={**args, "query": "absent term"}).json()
        assert no_match["result_state"] == "no_match"
    finally:
        runtime.close()


def test_http_treatment_pages_read_all_actual_root_receipts(tmp_path):
    from test_guidance_treatment_history import _multi_root_history

    runtime, quest, guide, feedback = _multi_root_history(tmp_path)
    try:
        client = _client(runtime, "http://127.0.0.1:8999")
        hc = runtime.owners.human_collaboration
        before = hc.query_guidance_deliveries(guide["constraint_ref"])
        args = {"quest_ref": quest, "constraint_ref": guide["constraint_ref"],
                "version_ref": guide["guidance_hash"], "limit": 1}
        receipts = []
        while True:
            response = client.get("/api/v1/research-library/human", params=args)
            assert response.status_code == 200, response.text
            page = response.json()
            for item in page["items"]:
                exact = client.get("/api/v1/research-content", params={"quest_ref": quest, **item["reader"]})
                assert exact.status_code == 200, exact.text
                receipts.append(json.loads(exact.json()["text"]))
                denied = client.get("/api/v1/research-content", params={"quest_ref": "quest_foreign", **item["reader"]})
                assert denied.status_code == 409
            if page["next_offset"] is None:
                break
            args.update(offset=page["next_offset"], query_revision=page["query_revision"])
        assert len(receipts) == 3
        assert len({value["binding"]["identity"]["run_ref"] for value in receipts}) == 2
        assert {value["receipt"]["receipt_ref"] for value in receipts} == {value["receipt"]["receipt_ref"] for value in feedback}
        assert hc.query_guidance_deliveries(guide["constraint_ref"]) == before
    finally:
        runtime.close()
