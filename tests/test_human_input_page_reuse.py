"""Exercise exact human input reads with real Owners and isolated stored facts."""
import json

import pytest
from sqlalchemy import text

from meta_research import context_presentation as presentation
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.research_content import read_content
from test_research_content_reader import channel
from test_root_workspace import _make
from test_public_plan_stage import _confirm_direct_quest
from test_dataset_effect_scope_recovery import _scope


@pytest.fixture
def source(tmp_path):
    runtime = _make(tmp_path / "runtime")
    try:
        created = _confirm_direct_quest(runtime)
        quest = runtime.owners.research_graph.query_question(created["initialization_id"])
        record = runtime.owners.human_collaboration.submit_research_input(
            quest_ref=quest.quest_ref, text_content="研究材料。" * 12000,
            idempotency_key="input")
        yield runtime, quest, record
    finally:
        runtime.close()


def read(source, cache, **kwargs):
    runtime, quest, record = source
    return read_content(runtime.owners.research_graph, runtime.owners.research_memory,
        quest_ref=quest.quest_ref, source_ref=record["input_ref"],
        version_ref=record["content_hash"], page_cache=cache, **kwargs)


def test_human_input_pages_reuse_encoding_and_one_verified_owner_read(source, monkeypatch):
    from meta_research import human_research_input
    encodings, verifications = [], []
    encode, verify = presentation._json, human_research_input._verified_input_value
    def counted_encode(value):
        raw = encode(value)
        encodings.append(len(raw.encode("utf-8")))
        return raw
    def counted_verify(row):
        verifications.append(row.input_ref)
        return verify(row)
    monkeypatch.setattr(presentation, "_json", counted_encode)
    monkeypatch.setattr(human_research_input, "_verified_input_value", counted_verify)
    cache = presentation.ExactContextPages()
    pages, offset, chunks = [], 0, []
    while True:
        page = read(source, cache, offset=offset, limit=8192)
        pages.append(page)
        chunks.append(page["text"])
        assert page["selected_evidence_binding"]["source_binding"]["content_hash"] == source[2]["content_hash"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(chunks)) == source[2]
    assert len({page["content_hash"] for page in pages}) == 1
    assert len(encodings) == 1
    assert len(verifications) == len(pages)


def test_warm_human_pages_reject_corrupt_original_and_recover(source):
    runtime, _, record = source
    cache = presentation.ExactContextPages()
    original = read(source, cache)
    # Inject storage corruption at the database boundary after warming the page.
    with runtime._database.fenced_write() as c:
        payload = c.execute(text("SELECT payload_json FROM hc_research_inputs WHERE input_ref=:ref"),
                            {"ref": record["input_ref"]}).scalar_one()
        corrupt = json.loads(payload)
        corrupt["text"] = "Changed original"
        c.execute(text("UPDATE hc_research_inputs SET payload_json=:payload WHERE input_ref=:ref"),
                  {"ref": record["input_ref"], "payload": canonical_json(corrupt)})
    with pytest.raises(OwnerConflict, match="human_input_receipt_invalid"):
        read(source, cache)
    with runtime._database.fenced_write() as c:
        c.execute(text("UPDATE hc_research_inputs SET payload_json=:payload WHERE input_ref=:ref"),
                  {"ref": record["input_ref"], "payload": payload})
    assert read(source, cache) == original
    assert read(source, presentation.ExactContextPages()) == original


def test_material_arrival_changes_full_representation_and_scope_stays_live(source):
    runtime, quest, record = source
    cache = presentation.ExactContextPages()
    original = read(source, cache)
    # Model asynchronous material completion at the persistence boundary. The
    # body identity stays the same; this mutable envelope has a separate life.
    receiver = {"kind": "current", "quest_ref": quest.quest_ref, "roots": []}
    command = {"receiver": receiver, "description": "Newly available material", "selections": []}
    with runtime._database.fenced_write() as c:
        c.execute(text("INSERT INTO hc_work_material_submissions "
            "(submission_ref,idempotency_key,command_hash,command_json,receiver_json,anchor_kind,anchor_ref,state,created_at) "
            "VALUES ('material-page','material-page',:hash,:command,:receiver,'input',:ref,'ready',1)"),
            {"hash": canonical_hash(command), "command": canonical_json(command),
             "receiver": canonical_json(receiver), "ref": record["input_ref"]})
    changed = read(source, cache)
    assert changed["content_hash"] != original["content_hash"]
    assert changed == read(source, None)
    assert changed["version_ref"] == record["content_hash"]
    call = channel(runtime, quest)
    args = {"source_ref": record["input_ref"], "version_ref": record["content_hash"]}
    assert not call(**args).get("isError")
    assert call(**{**args, "version_ref": "wrong"}).get("isError")
    _scope(runtime, quest_ref=quest.quest_ref, run_ref=quest.quest_ref,
           root_ref=quest.question_ref, generation=2)
    assert call(**args).get("isError")
