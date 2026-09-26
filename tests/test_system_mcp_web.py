from __future__ import annotations

from test_trusted_ssh_loopback import _open_test_app


def test_system_mcp_management_is_persistent_scoped_and_revision_checked(tmp_path):
    root = tmp_path / "system-mcp"
    with _open_test_app(root) as (_, _, client):
        initial = client.get("/api/v1/system/mcp")
        assert initial.status_code == 200
        assert initial.json()["revision"] == 0
        assert "acquisition" in initial.json()["root_kinds"]
        headers = {"Origin": str(client.base_url).rstrip("/"),
                   "X-CSRF-Token": client.cookies.get("meta_research_csrf")}
        config = {"server_id": "fixture", "display_name": "Fixture",
                  "transport": "streamable_http", "connection": {"url": "http://127.0.0.1:9998/mcp"}}
        saved = client.post("/api/v1/system/mcp", headers=headers,
                            json={"expected_revision": 0, "config": config})
        assert saved.status_code == 201, saved.text
        assert saved.json()["servers"][0]["scope"] == {"mode": "all"}
        assert saved.json()["servers"][0]["connection_status"]["status"] == "unknown"
        conflict = client.put("/api/v1/system/mcp/fixture", headers=headers,
                              json={"expected_revision": 0, "config": {**config, "enabled": False}})
        assert conflict.status_code == 409
        assert client.get("/api/v1/system/mcp").json()["servers"][0]["enabled"] is True
    with _open_test_app(root) as (_, _, client):
        state = client.get("/api/v1/system/mcp").json()
        assert state["revision"] == 1
        assert state["servers"][0]["server_id"] == "fixture"
        headers = {"Origin": str(client.base_url).rstrip("/"),
                   "X-CSRF-Token": client.cookies.get("meta_research_csrf")}
        removed = client.request("DELETE", "/api/v1/system/mcp/fixture", headers=headers,
                                 json={"expected_revision": 1})
        assert removed.status_code == 200
        assert removed.json()["servers"] == []


def test_system_mcp_management_uses_existing_csrf_and_rejects_invalid_config(tmp_path):
    with _open_test_app(tmp_path / "secured-mcp") as (_, _, client):
        assert client.get("/api/v1/system/mcp").status_code == 200
        body = {"expected_revision": 0, "config": {"server_id": "meta_research"}}
        assert client.post("/api/v1/system/mcp", json=body).status_code == 403
        response = client.post("/api/v1/system/mcp", json=body,
                               headers={"Origin": str(client.base_url).rstrip("/"),
                                        "X-CSRF-Token": client.cookies.get("meta_research_csrf")})
        assert response.status_code == 422
        assert client.get("/api/v1/system/mcp").json()["revision"] == 0
