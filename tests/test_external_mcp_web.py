from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from test_corrected_quest_initialization import DeterministicDraftingAdapter, DeterministicProbe, _authenticated_client
from test_external_mcp import service, connection


def test_authenticated_api_roundtrip_default_roots_and_empty_selection(tmp_path):
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "app"), proposal_drafter=adapter,
        intent_drafting_provider=adapter, host_compute_probe=DeterministicProbe())
    try:
        client, headers = _authenticated_client(runtime)
        current = client.get("/api/v1/external-mcp").json()
        assert current["root_kinds"] == ["deepfetch", "acquisition", "companion", "idea", "plan", "bundle", "target", "reasoning", "writing"]
        payload = {"services": [service(tmp_path)], "expected_revision": current["revision"]}
        assert client.put("/api/v1/external-mcp", json=payload).status_code == 403
        saved = client.put("/api/v1/external-mcp", json=payload, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()["services"][0]["research_instructions"] == ""
        assert saved.json()["services"][0]["allowed_root_kinds"] == current["root_kinds"]
        assert client.get("/api/v1/external-mcp").json() == saved.json()
        assert client.put("/api/v1/external-mcp", json=payload, headers=headers).status_code == 409
        none = client.put("/api/v1/external-mcp", json={"services": [service(tmp_path, allowed_root_kinds=[])],
            "expected_revision": saved.json()["revision"]}, headers=headers)
        assert none.json()["services"][0]["allowed_root_kinds"] == []
        ready = client.post("/api/v1/external-mcp/test-connection", json={"connection": connection(tmp_path)}, headers=headers)
        assert ready.json()["status"] == "ready"
        assert ready.json()["tool_count"] == 2
        assert not (tmp_path / "calls.jsonl").exists()
    finally:
        runtime.close()
