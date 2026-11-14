"""Exercise RG Literature and Human Interact links through exact content reads."""
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from meta_research.owners.research_memory import AssetIntakeRequest
from test_dataset_effect_scope_recovery import _scope
from test_public_plan_stage import _confirm_direct_quest
from test_research_datasets import _asset
from test_root_workspace import _make, _channel, _external_context, _call, _accepted, _directories, _native_file


def _json_call(runtime, channel, operation, **arguments):
    chunks = []
    while True:
        page = _accepted(_call(runtime, channel, operation, **arguments))
        chunks.append(page["text"])
        if page.get("next_offset") is None:
            return json.loads("".join(chunks))
        arguments = {**arguments, "offset": page["next_offset"]}


def _uploaded_bytes(runtime, channel, descriptor):
    result = bytearray()
    args = {**descriptor["args"], "limit": 5}
    while True:
        page = _accepted(_call(runtime, channel, descriptor["operation"], **args))
        result.extend(base64.b64decode(page["text"]) if page["encoding"] == "base64" else page["text"].encode("utf-8"))
        if page["complete"]:
            return bytes(result)
        args["offset"] = page["next_offset"]


def _directory_asset(runtime, tmp_path, quest, key):
    source = tmp_path / key
    source.mkdir()
    (source / "article.txt").write_text("An original with a negative observation.")
    (source / "appendix.bin").write_bytes(b"\xff\x00appendix\x80")
    result = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
        source_kind="local_path", custody_mode="managed", display_name=key,
        source_locator=str(source)), idempotency_key=key)
    assert result.asset is not None
    binding = result.asset.as_binding()
    runtime.owners.research_graph.accept_asset_role(binding=binding, role="evidence",
        quest_ref=quest, idempotency_key="role-" + key)
    return binding


def _assert_directory_content(runtime, channel, reader):
    directory = _accepted(_call(runtime, channel, "research_memory.content.read", **reader))
    assert {entry["path"] for entry in directory["entries"]} == {"article.txt", "appendix.bin"}, directory
    original = _accepted(_call(runtime, channel, "research_memory.content.read",
        **reader, entry_path="article.txt"))
    assert original["text"] == "An original with a negative observation."
    binary = _accepted(_call(runtime, channel, "research_memory.content.read",
        **reader, entry_path="appendix.bin"))
    assert binary["encoding"] == "base64"
    assert base64.b64decode(binary["text"]) == b"\xff\x00appendix\x80"


@pytest.mark.parametrize("selected", ["original", "reading"])
def test_literature_directory_original_and_reading_bindings_are_navigable(tmp_path, selected):
    runtime = _make(tmp_path / "runtime")
    try:
        q = SimpleNamespace(**_confirm_direct_quest(runtime))
        graph = runtime.owners.research_graph
        directory = _directory_asset(runtime, tmp_path, q.quest_ref, selected)
        text = _asset(runtime, content=b"Other selected research material.", key="other")
        graph.accept_asset_role(binding=text, role="evidence", quest_ref=q.quest_ref, idempotency_key="role-other")
        paper, reading = (directory, text) if selected == "original" else (text, directory)
        registered = graph.register_literature(quest_ref=q.quest_ref, record_ref="doi:10.1000/multiple-files",
            title="Original and supplement", source="External authors; publisher", fulltext_status="obtained",
            asset_bindings=[paper.as_dict()], idempotency_key="register")
        usage = graph.reference_literature(quest_ref=q.quest_ref, literature_ref=registered["ref"],
            question_ref=q.question_ref, purpose="Inspect original and supplement", reading_summary="Limits retained",
            reading_bindings=[reading.as_dict()], idempotency_key="reading")
        channel = _channel(runtime, _external_context(_scope(runtime, quest_ref=q.quest_ref)))
        page = _accepted(_call(runtime, channel, "research_memory.literature.page", query="multiple-files"))
        item, = page["items"]
        for descriptor, expected in ((item["reader"], registered), (item["judgments"][0]["reader"], usage)):
            op, args = descriptor["operation"], {key: value for key, value in descriptor.items() if key != "operation"}
            assert _accepted(_call(runtime, channel, op, **args))["result"] == expected
        selected_item = item if selected == "original" else item["judgments"][0]
        _assert_directory_content(runtime, channel, selected_item["readers"][0])
    finally:
        runtime.close()


def test_human_input_directory_asset_is_reachable_from_history(tmp_path):
    runtime = _make(tmp_path / "runtime")
    try:
        q = SimpleNamespace(**_confirm_direct_quest(runtime))
        binding = _directory_asset(runtime, tmp_path, q.quest_ref, "human-observations")
        accepted = runtime.owners.human_collaboration.submit_research_input(quest_ref=q.quest_ref,
            text_content="Keep the original and appendix.", asset_bindings=[binding.as_dict()], idempotency_key="input")
        channel = _channel(runtime, _external_context(_scope(runtime, quest_ref=q.quest_ref)))
        listing = json.loads(_accepted(_call(runtime, channel, "human_request.read"))["text"])
        reader = listing["research_inputs"]["items"][0]["reader"]
        exact = json.loads(_accepted(_call(runtime, channel, "research_memory.content.read", **reader))["text"])
        assert exact["input_ref"] == accepted["input_ref"]
        returned = exact["asset_bindings"][0]
        assert returned["version_ref"] == binding.version_ref
        _assert_directory_content(runtime, channel, {"source_ref": returned["version_ref"], "version_ref": returned["version_ref"]})
    finally:
        runtime.close()


def test_human_request_reply_directory_is_reachable_from_history(tmp_path):
    from meta_research.human_reply import ProvidedReply, ServerReference
    from test_public_human_reply_delivery import _open
    runtime = _make(tmp_path / "runtime")
    try:
        scope, context, opened = _open(runtime)
        original = tmp_path / "observations"
        original.mkdir()
        (original / "raw.txt").write_text("Original human observations.")
        (original / "notes.txt").write_text("Retain uncertainty.")
        selection = runtime.root_workspaces.server_files.inspect(str(original))
        runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"],
            reply=ProvidedReply("Read both original files.", {}, (ServerReference(selection),)), idempotency_key="reply")
        channel = _channel(runtime, context)
        listing = json.loads(_accepted(_call(runtime, channel, "human_request.read"))["text"])
        reader = next(item for item in listing["items"] if item["request_ref"] == opened["request_ref"])["latest_response"]["reader"]
        exact = json.loads(_accepted(_call(runtime, channel, reader["operation"],
            **{key: value for key, value in reader.items() if key != "operation"}))["text"])
        reference = exact["response"]["delivery"]["work_materials"][0]["reference_ref"]
        directory = _accepted(_call(runtime, channel, "research_workspace.materials.discover", reference_ref=reference))
        assert {entry["path"] for entry in directory["entries"]} == {"raw.txt", "notes.txt"}
        for entry in directory["entries"]:
            read = _accepted(_call(runtime, channel, "research_workspace.materials.read", reference_ref=reference,
                path=entry["path"], observation_ref=entry["observation"]["observation_ref"], max_bytes=1000))
            assert read["text"] == (original / entry["path"]).read_text()
    finally:
        runtime.close()


def test_human_request_uploaded_reply_is_readable_by_a_later_history_reader(tmp_path):
    from meta_research.human_reply import ProvidedReply, Upload
    from test_public_human_reply_delivery import _open
    runtime = _make(tmp_path / "runtime")
    try:
        _, receiving_context, opened = _open(runtime)
        literal = b"\xfforiginal human measurement\x80"
        accepted = runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"],
            reply=ProvidedReply("Keep the original measurement.", {},
                (Upload("measurement.bin", "application/octet-stream", literal),)), idempotency_key="upload-reply")
        original_file = _native_file(runtime, receiving_context, accepted["delivery"]["uploaded_files"][0])
        assert original_file.read_bytes() == literal
        original_file.unlink()
        context = _external_context(_scope(runtime, quest_ref=accepted["delivery"]["quest_ref"],
            run_ref="later-history-reader", root_ref="later-history-root"))
        channel = _channel(runtime, context)
        listing = _json_call(runtime, channel, "human_request.read")
        reader = next(item for item in listing["items"] if item["request_ref"] == opened["request_ref"])["latest_response"]["reader"]
        exact = _json_call(runtime, channel, reader["operation"],
            **{key: value for key, value in reader.items() if key != "operation"})
        upload = exact["response"]["delivery"]["uploaded_files"][0]
        visible = {item["workspace_ref"] for item in _directories(runtime, context)}
        assert upload["workspace_ref"] not in visible
        descriptor, = exact["upload_readers"]
        assert descriptor["path"] == "measurement.bin"
        assert _uploaded_bytes(runtime, channel, descriptor["reader"]) == literal
        from meta_research.owners.common import canonical_hash
        assert canonical_hash(exact["response"]) == reader["version_ref"]
    finally:
        runtime.close()


@pytest.mark.parametrize("kind", ["guidance", "input"])
def test_human_guidance_and_input_original_directories_are_reachable_from_history(tmp_path, kind):
    runtime = _make(tmp_path / "runtime")
    try:
        q = SimpleNamespace(**_confirm_direct_quest(runtime))
        runtime.idea_stage.start("material-receiver")
        human = runtime.owners.human_collaboration
        source = tmp_path / "human-originals"
        source.mkdir()
        (source / "raw.txt").write_text("An original human observation.")
        (source / "notes.txt").write_text("A limitation supplied by the human.")
        command = {"receiver": human.current_material_receiver(q.quest_ref),
            "selections": [runtime.root_workspaces.server_files.inspect(str(source))],
            "description": "Keep both files accessible for this Quest.",
            "uploads": [{"relative_path": "measurement.bin", "media_type": "application/octet-stream",
                "content_base64": base64.b64encode(b"\xffhuman measurement\x80").decode()}]}
        if kind == "guidance":
            human.submit_human_guidance(quest_ref=q.quest_ref, original_text="Use these observations.",
                work_materials=command, idempotency_key="guidance")
        else:
            human.submit_research_input(quest_ref=q.quest_ref, text_content="Use these observations.",
                work_materials=command, idempotency_key="input")
        context = _external_context(_scope(runtime, quest_ref=q.quest_ref))
        channel = _channel(runtime, context)
        if kind == "guidance":
            listing = _json_call(runtime, channel, "human_request.read", entry_kind="guidance")
            reader = listing["items"][0]["reader"]
            exact = _json_call(runtime, channel, reader["operation"],
                **{key: value for key, value in reader.items() if key != "operation"})
        else:
            listing = _json_call(runtime, channel, "human_request.read")
            reader = listing["research_inputs"]["items"][0]["reader"]
            exact = _json_call(runtime, channel, "research_memory.content.read", **reader)
        upload = exact["work_materials"][0]["uploads"][0]["file"]
        if kind == "guidance":
            visible = {item["workspace_ref"] for item in _directories(runtime, context)}
            assert upload["workspace_ref"] not in visible
            descriptor, = exact["upload_readers"]
            literal = _uploaded_bytes(runtime, channel, descriptor["reader"])
        else:
            # Unlike formal guidance, a proactive input retains its exact
            # original material command, including the uploaded bytes.
            literal = base64.b64decode(exact["material_command"]["uploads"][0]["content_base64"], validate=True)
        assert literal == b"\xffhuman measurement\x80"
        assert hashlib.sha256(literal).hexdigest() == upload["sha256"]
        reference = exact["work_materials"][0]["references"][0]["reference_ref"]
        directory = _accepted(_call(runtime, channel, "research_workspace.materials.discover", reference_ref=reference))
        assert {entry["path"] for entry in directory["entries"]} == {"raw.txt", "notes.txt"}
        for entry in directory["entries"]:
            read = _accepted(_call(runtime, channel, "research_workspace.materials.read", reference_ref=reference,
                path=entry["path"], observation_ref=entry["observation"]["observation_ref"], max_bytes=1000))
            assert read["text"] == (source / entry["path"]).read_text()
    finally:
        runtime.close()


@pytest.mark.parametrize("kind", ["guidance", "reply"])
def test_human_upload_reader_rejects_wrong_identity_path_and_corrupt_command(tmp_path, kind):
    from sqlalchemy import text
    from meta_research.human_upload_content import human_upload_readers, read_human_upload
    from meta_research.human_reply import ProvidedReply, Upload
    from meta_research.owners.common import OwnerConflict, canonical_hash
    from test_public_human_reply_delivery import _open
    runtime = _make(tmp_path / "runtime")
    try:
        human = runtime.owners.human_collaboration
        literal = b"\xfforiginal measurement\x80"
        if kind == "guidance":
            quest = _confirm_direct_quest(runtime)["quest_ref"]
            runtime.idea_stage.start("upload-receiver")
            command = {"receiver": human.current_material_receiver(quest), "selections": [],
                "description": "Exact upload.", "uploads": [{"relative_path": "measurement.bin",
                    "media_type": "application/octet-stream", "content_base64": base64.b64encode(literal).decode()}]}
            accepted = human.submit_human_guidance(quest_ref=quest, original_text="Use this exact measurement.",
                work_materials=command, idempotency_key="guidance")
            source, version = accepted["constraint_ref"], canonical_hash(accepted["guidance"])
            corrupt_sql = "UPDATE hc_work_material_submissions SET command_json='{}' WHERE anchor_kind='guidance' AND anchor_ref=:ref"
            corrupt_code = "material_receipt_invalid"
        else:
            _, _, opened = _open(runtime)
            accepted = human.respond_to_human_request(opened["request_ref"], reply=ProvidedReply("Exact upload.", {},
                (Upload("measurement.bin", "application/octet-stream", literal),)), idempotency_key="reply")
            quest = accepted["delivery"]["quest_ref"]
            source, version = accepted["response_ref"], canonical_hash(accepted)
            corrupt_sql = "UPDATE hc_reply_deliveries SET command_json='{}' WHERE response_ref=:ref"
            corrupt_code = "human_response_spool_invalid"
        arguments = {"quest_ref": quest, "source_ref": source, "version_ref": version}
        descriptor, = human_upload_readers(human, **arguments, source_quest_ref=quest)
        assert descriptor["reader"]["args"]["source_quest_ref"] == quest
        content = read_human_upload(human, **arguments, entry_path=descriptor["path"])
        assert base64.b64decode(content["text"]) == literal
        with pytest.raises(OwnerConflict, match="content_version_unbound"):
            read_human_upload(human, **{**arguments, "version_ref": "wrong"}, entry_path=descriptor["path"])
        with pytest.raises(OwnerConflict, match="content_source_unbound|research_help_request_unbound"):
            read_human_upload(human, **{**arguments, "quest_ref": "foreign"}, entry_path=descriptor["path"])
        with pytest.raises(OwnerConflict, match="content_entry_invalid"):
            read_human_upload(human, **arguments, entry_path="../unselected.txt")
        with pytest.raises(OwnerConflict, match="content_page_invalid"):
            read_human_upload(human, **arguments, entry_path=descriptor["path"], offset=len(literal) + 1)
        with runtime._database.write() as connection:
            connection.execute(text(corrupt_sql), {"ref": source})
        with pytest.raises(OwnerConflict, match=corrupt_code):
            read_human_upload(human, **arguments, entry_path=descriptor["path"])
    finally:
        runtime.close()


@pytest.mark.parametrize("confirmed", [False, True])
def test_guidance_uploads_keep_their_accepted_source_even_if_a_material_anchor_is_changed(tmp_path, confirmed):
    from sqlalchemy import text
    from guidance_confirmation_helpers import prepare_guidance_submission
    from meta_research.human_upload_content import human_upload_readers, read_human_upload
    from meta_research.owners.common import OwnerConflict, canonical_hash
    runtime = _make(tmp_path / "runtime")
    try:
        human = runtime.owners.human_collaboration
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        runtime.idea_stage.start("guidance-receiver")
        command = {"receiver": human.current_material_receiver(quest), "selections": [],
            "description": "Exact upload.", "uploads": [{"relative_path": "measurement.bin",
                "media_type": "application/octet-stream", "content_base64": base64.b64encode(b"\xffmeasurement\x80").decode()}]}
        if confirmed:
            proposal = prepare_guidance_submission(human, quest, "Use this exact measurement.",
                key="prepare", work_materials=command)
            original = human.submit_human_guidance(quest_ref=quest, original_text=proposal["text"],
                strength=proposal["strength"], proposal_ref=proposal["proposal_ref"],
                expected_proposal_hash=proposal["expected_proposal_hash"], work_materials=command,
                idempotency_key="guidance")
        else:
            original = human.submit_human_guidance(quest_ref=quest, original_text="Use this exact measurement.",
                work_materials=command, idempotency_key="guidance")
        arguments = {"quest_ref": quest, "source_ref": original["constraint_ref"],
            "version_ref": canonical_hash(original["guidance"])}
        descriptor, = human_upload_readers(human, **arguments)
        upload = original["work_materials"][0]["uploads"][0]
        location = runtime.root_workspaces._runtime_location(upload["work_ref"])
        (location.directory / upload["file"]["path"]).unlink()
        assert base64.b64decode(read_human_upload(human, **arguments, entry_path=descriptor["path"])["text"]) == b"\xffmeasurement\x80"
        other = human.submit_human_guidance(quest_ref=quest, original_text="A separate instruction.", idempotency_key="other")
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE hc_work_material_submissions SET anchor_ref=:other WHERE anchor_ref=:original"),
                {"other": other["constraint_ref"], "original": original["constraint_ref"]})
        with pytest.raises(OwnerConflict, match="material_receipt_invalid"):
            human_upload_readers(human, quest_ref=quest, source_ref=other["constraint_ref"],
                version_ref=canonical_hash(other["guidance"]))
    finally:
        runtime.close()
