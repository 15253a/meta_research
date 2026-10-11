import json

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.proposal_output import read_proposal_output
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    ensure_transport_key,
    write_transport_envelope,
)


GENERATION = {"ref": "generation:test", "status": "running", "failure": None}


def operation(tmp_path, name, text=None, *, job_ref=None, invocation_overrides=None):
    job_ref = job_ref or GENERATION["ref"] + ":proposal"
    workspace = tmp_path / "companion-provider"
    _, key = ensure_transport_key(workspace)
    directory = workspace / "provider-operations" / canonical_hash({"job_ref": job_ref}) / name
    directory.mkdir(parents=True)
    invocation = {"schema_ref": "meta-research/codex-provider-operation/v3",
                  "job_ref": job_ref, "operation_name": name, **(invocation_overrides or {})}
    write_transport_envelope(directory / "invocation.json", invocation, key)
    if text is not None:
        (directory / "stdout.jsonl").write_bytes(text.encode("utf-8"))
    return directory


def test_current_first_question_synthesis_is_visible(tmp_path):
    expected = '{"type":"turn.started"}\n'
    operation(tmp_path, "first-question-synthesis", expected)
    page = read_proposal_output(tmp_path, GENERATION, 0, 65536)
    assert page["text"] == expected
    assert page["phase"] == "drafting"
    assert page["availability"] == "ready"


def test_understanding_stream_continues_into_synthesis_without_resetting_cursor(tmp_path):
    understanding = '{"type":"item.completed","item":{"type":"agent_message","text":"读取材料"}}'
    directory = operation(tmp_path, "initialization-understanding", understanding,
                          job_ref=GENERATION["ref"] + ":proposal:understanding")
    first = read_proposal_output(tmp_path, GENERATION, 0, 65536)
    assert first["text"] == understanding
    assert first["phase"] == "understanding"
    synthesis = '{"type":"turn.started"}\n'
    operation(tmp_path, "first-question-synthesis", synthesis)
    second = read_proposal_output(tmp_path, GENERATION, first["next_offset"], 65536)
    assert second["text"] == "\n" + synthesis
    assert second["phase"] == "drafting"
    assert first["text"] + second["text"] == understanding + "\n" + synthesis
    assert second["next_offset"] == second["source_bytes"]
    with (directory / "stdout.jsonl").open("rb") as stream:
        assert stream.read() == understanding.encode("utf-8")


def test_bounded_pages_preserve_utf8_and_phase_boundary(tmp_path):
    understanding = '{"text":"资料"}\n'
    synthesis = '{"text":"问题"}\n'
    operation(tmp_path, "initialization-understanding", understanding,
              job_ref=GENERATION["ref"] + ":proposal:understanding")
    operation(tmp_path, "first-question-synthesis", synthesis)
    offset, pieces = 0, []
    for _ in range(40):
        page = read_proposal_output(tmp_path, GENERATION, offset, 4)
        assert page["next_offset"] > offset
        assert page["next_offset"] - offset <= 4
        pieces.append(page["text"])
        offset = page["next_offset"]
        if not page["has_more"]:
            break
    assert "".join(pieces) == understanding + synthesis
    assert offset == len((understanding + synthesis).encode("utf-8"))


def test_observed_understanding_phase_does_not_require_stdout_yet(tmp_path):
    operation(tmp_path, "initialization-understanding",
              job_ref=GENERATION["ref"] + ":proposal:understanding")
    page = read_proposal_output(tmp_path, GENERATION, 0, 65536)
    assert page["phase"] == "understanding"
    assert page["availability"] == "waiting"
    assert page["text"] == ""


def test_legacy_proposal_output_and_cursor_validation_remain_supported(tmp_path):
    expected = '{"type":"turn.started"}\n'
    operation(tmp_path, "proposal-fork", expected)
    page = read_proposal_output(tmp_path, GENERATION, 0, 65536)
    assert page["text"] == expected
    with pytest.raises(ValueError, match="proposal_output_cursor_stale"):
        read_proposal_output(tmp_path, GENERATION, page["next_offset"] + 1, 65536)


@pytest.mark.parametrize("name,overrides", [
    ("first-question-synthesis", {"job_ref": "other-generation:proposal"}),
    ("initialization-understanding", {"operation_name": "companion-turn"}),
])
def test_each_operation_requires_its_exact_signed_identity(tmp_path, name, overrides):
    suffix = ":proposal:understanding" if name == "initialization-understanding" else ":proposal"
    operation(tmp_path, name, '{"type":"turn.started"}\n',
              job_ref=GENERATION["ref"] + suffix, invocation_overrides=overrides)
    with pytest.raises(ValueError, match="proposal_output_unavailable"):
        read_proposal_output(tmp_path, GENERATION, 0, 65536)


def test_unsigned_invocation_is_rejected(tmp_path):
    directory = operation(tmp_path, "first-question-synthesis", "output")
    (directory / "invocation.json").write_text(json.dumps({"schema_ref": "meta-research/codex-provider-operation/v3"}))
    with pytest.raises(ProviderSupervisorError):
        read_proposal_output(tmp_path, GENERATION, 0, 65536)


def test_synthesis_and_legacy_cannot_ambiguously_share_a_cursor(tmp_path):
    operation(tmp_path, "proposal-fork", "legacy")
    operation(tmp_path, "first-question-synthesis", "current")
    with pytest.raises(ValueError, match="proposal_output_unavailable"):
        read_proposal_output(tmp_path, GENERATION, 0, 65536)


@pytest.mark.parametrize("filename", ["stdout.jsonl", "invocation.json"])
def test_redirected_spool_files_are_not_followed(tmp_path, filename):
    directory = operation(tmp_path, "first-question-synthesis", "private output")
    original = directory / filename
    other = tmp_path / ("other-" + filename)
    original.rename(other)
    try:
        original.symlink_to(other)
    except OSError:
        pytest.skip("This platform does not permit file symlinks")
    with pytest.raises(ValueError, match="proposal_output_unavailable"):
        read_proposal_output(tmp_path, GENERATION, 0, 65536)


def test_waiting_empty_and_ended_generations_remain_distinct(tmp_path):
    waiting = read_proposal_output(tmp_path, GENERATION, 0, 65536)
    assert waiting["availability"] == "waiting"
    assert waiting["phase"] is None
    ended = read_proposal_output(tmp_path, {**GENERATION, "status": "failed"}, 0, 65536)
    assert ended["availability"] == "unavailable"


@pytest.fixture
def preparation_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from meta_research.composition import build_production_runtime
    from meta_research.paths import prepare_data_root
    from meta_research.web import create_app

    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"),
                                       startup_power_probe=False, startup_harness_diagnostics=False)
    creation = {"route": "deepfetch", "quest_draft": {"revision": 2, "hash": "current-draft"},
                "deepfetch": {"request_ref": "deepfetch:current", "basis_revision": 2,
                              "basis_hash": "current-draft", "freshness": "current", "status": "queued"}}
    monkeypatch.setattr(runtime.owners.human_collaboration, "query_quest_creation", lambda _: creation)
    client = TestClient(create_app(runtime, base_url="http://testserver", control_key="test-control"))
    response = client.post("/auth/bootstrap", headers={"Origin": "http://testserver"},
                           json={"token": runtime.authentication.issue_bootstrap_token()})
    assert response.status_code == 200
    try:
        yield client, runtime.data_root.root, creation
    finally:
        client.close()
        runtime.close()


def test_deepfetch_preparation_endpoint_streams_understanding_before_a_proposal_exists(preparation_client):
    client, data_root, creation = preparation_client
    text = '{"type":"turn.started"}\n'
    directory = operation(data_root, "initialization-understanding", text,
                          job_ref=creation["deepfetch"]["request_ref"] + ":understanding")
    url = "/api/v1/quest-initializations/init/preparation-output"
    response = client.get(url, params={"request_ref": "deepfetch:current", "after": 0})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    first = response.json()
    assert first["generation_ref"] == "deepfetch:current"
    assert first["text"] == text
    assert first["phase"] == "understanding"
    extra = '{"type":"turn.completed"}\n'
    with (directory / "stdout.jsonl").open("ab") as stream:
        stream.write(extra.encode())
    response = client.get(url, params={"request_ref": "deepfetch:current", "after": first["next_offset"]})
    assert response.status_code == 200
    assert response.json()["text"] == extra


@pytest.mark.parametrize("changed", ["request", "route", "revision", "hash", "freshness"])
def test_deepfetch_preparation_endpoint_rejects_other_requests_or_draft_bases(preparation_client, changed):
    client, data_root, creation = preparation_client
    operation(data_root, "initialization-understanding", "private output",
              job_ref=creation["deepfetch"]["request_ref"] + ":understanding")
    requested = "deepfetch:current"
    if changed == "request":
        requested = "deepfetch:other"
    elif changed == "route":
        creation["route"] = "direct"
    elif changed == "revision":
        creation["quest_draft"]["revision"] += 1
    elif changed == "hash":
        creation["quest_draft"]["hash"] = "new-draft"
    else:
        creation["deepfetch"]["freshness"] = "stale"
    response = client.get("/api/v1/quest-initializations/init/preparation-output",
                          params={"request_ref": requested})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "preparation_output_request_changed"
    assert "private output" not in response.text
