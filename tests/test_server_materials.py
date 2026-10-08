import os

import pytest
from fastapi.testclient import TestClient

from meta_research.owners.common import OwnerConflict
from meta_research.server_materials import ServerFiles
from meta_research.web import create_app
from test_root_workspace import _make
from test_public_human_reply_delivery import _login


def test_authenticated_host_browse_paging_cancel_and_metadata_selection(tmp_path):
    runtime = _make(tmp_path / "runtime")
    original = tmp_path / "original"
    original.mkdir()
    for name in ("one.txt", "two.txt", "three.txt"):
        (original / name).write_text(name)
    (original / "nested").mkdir()
    (original / "nested" / "hidden.txt").write_text("Not expanded.")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            assert client.get("/api/v1/server-materials/browse", params={"path": str(original)}).status_code == 401
            headers = _login(client, runtime)
            first = client.get("/api/v1/server-materials/browse", params={"path": str(original), "limit": 2}).json()
            assert first["server"]["hostname"] == __import__("socket").gethostname()
            assert first["server"]["platform"] == "Linux"
            assert len(first["entries"]) == 2
            second = client.get("/api/v1/server-materials/browse", params={"path": str(original), "limit": 2, "cursor": first["next_cursor"]}).json()
            assert {entry["name"] for entry in first["entries"] + second["entries"]} == {"one.txt", "two.txt", "three.txt", "nested"}
            assert second["next_cursor"] is None
            selected = client.get("/api/v1/server-materials/inspect", params={"path": str(original), "description": "Exact original explanation."}).json()
            assert (selected["kind"], selected["description"]) == ("directory", "Exact original explanation.")
            assert "sha256" not in selected["observation"]
            page = client.get("/api/v1/server-materials/browse", params={"path": str(original), "limit": 1}).json()
            assert client.request("DELETE", f"/api/v1/server-materials/cursors/{page['next_cursor']}", headers=headers, json={}).json() == {"cancelled": True}
            assert client.get("/api/v1/server-materials/browse", params={"path": str(original), "cursor": page["next_cursor"]}).status_code == 409
            assert client.get("/api/v1/server-materials/inspect", params={"path": str(original / "missing")}).status_code == 409
    finally:
        runtime.root_workspaces.server_files.close()
        runtime.close()


def test_reference_range_reads_large_original_and_rejects_drift_escape_and_symlinks(tmp_path):
    files = ServerFiles()
    original = tmp_path / "original"
    original.mkdir()
    huge = original / "huge.bin"
    with huge.open("wb") as stream:
        stream.seek(2 * 1024**3)
        stream.write(b"End.")
    (original / "small.txt").write_text("Literal small range.")
    (original / "escape").symlink_to(tmp_path)
    selection = files.inspect(str(original), description="Large project.")
    page = files.discover(selection, actor="test")
    huge_entry = next(entry for entry in page["entries"] if entry["name"] == "huge.bin")
    read = files.read(selection, path="huge.bin", observation_ref=huge_entry["observation"]["observation_ref"], offset=2 * 1024**3, max_bytes=4)
    assert (read["text"], read["bytes"], read["eof"]) == ("End.", 4, True)
    small = next(entry for entry in page["entries"] if entry["name"] == "small.txt")
    read = files.read(selection, path="small.txt", observation_ref=small["observation"]["observation_ref"], offset=8, max_bytes=5)
    assert (read["text"], read["bytes"], read["eof"]) == ("small", 5, False)
    (original / "small.txt").write_text("Changed.")
    with pytest.raises(OwnerConflict, match="material_source_changed"):
        files.read(selection, path="small.txt", observation_ref=small["observation"]["observation_ref"])
    for path in ("../outside", "/outside", "escape/anything"):
        with pytest.raises(OwnerConflict):
            files.read(selection, path=path, observation_ref="not-used")
    with pytest.raises(OwnerConflict, match="material_unsafe"):
        files.inspect(str(original / "escape"))
    files.close()
