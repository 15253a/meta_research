import json
from types import SimpleNamespace

import pytest

from meta_research.human_research_context import read_human_research_context
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpError
from test_public_research_asset_roles import _runtime, _accepted_quest


def _read(runtime, quest, arguments):
    # Scope verification is the external runtime boundary in this reader test.
    class Scope:
        def verify_root_agent_runtime_scope(self, **kwargs):
            return {"quest_ref": quest}
    context = SimpleNamespace(root_kind="companion", run_ref="run", attempt_ref="attempt",
        root_session_ref="root", fence_ref="fence", capability_binding_hash="binding")
    return read_human_research_context(Scope(), runtime.owners.human_collaboration, context, arguments)


def test_history_discovers_only_submitted_guidance_and_reads_exact_semantics(tmp_path):
    runtime = _runtime(tmp_path / "runtime")
    try:
        quest = _accepted_quest(runtime).quest_ref
        hc = runtime.owners.human_collaboration
        hc.record_agent_proposal("quest:" + quest, {
            "proposal_kind": "soft_constraint", "text": "Unsubmitted idea",
            "assistant_understanding": "Draft only", "applies_to": ["Quest"],
            "semantic_scope": {"kind": "quest", "quest_ref": quest},
            "strength": 5, "preserve_conditions": [], "work_materials": None,
        }, "unsubmitted")
        accepted = hc.submit_human_guidance(quest_ref=quest,
            original_text="Preserve the negative result.", strength=4, idempotency_key="formal")
        listed = json.loads(_read(runtime, quest, {"entry_kind": "guidance"})["text"])
        assert len(listed["items"]) == 1
        item = listed["items"][0]
        assert item["constraint_ref"] == accepted["constraint_ref"]
        assert "guidance" not in item
        args = {k: v for k, v in item["reader"].items() if k != "operation"}
        exact = json.loads(_read(runtime, quest, args)["text"])
        assert exact["guidance"]["text"] == "Preserve the negative result."
        assert exact["guidance"]["strength"] == 4
        assert exact["meaning"] == "formally_submitted_guidance"
        assert exact["status"] == "active"
        assert hc.query_guidance_deliveries(item["constraint_ref"]) == []
        with pytest.raises(SemanticMcpError, match="content_version_unbound"):
            _read(runtime, quest, {**args, "version_ref": "wrong"})
        foreign = "quest_foreign"
        with pytest.raises(SemanticMcpError, match="content_source_unbound"):
            _read(runtime, foreign, args)
    finally:
        runtime.close()


def test_input_pages_bind_query_scope_and_detect_changed_records(tmp_path):
    runtime = _runtime(tmp_path / "runtime")
    try:
        quest = _accepted_quest(runtime).quest_ref
        hc = runtime.owners.human_collaboration
        for i in range(3):
            hc.submit_research_input(quest_ref=quest, text_content=f"negative finding {i}",
                                    idempotency_key=f"input-{i}")
        first = hc.query_research_inputs(quest_ref=quest, query="negative", limit=1)
        assert first["result_state"] == "matches"
        with pytest.raises(OwnerConflict, match="research_query_revision_required"):
            hc.query_research_inputs(quest_ref=quest, query="negative", limit=1, offset=first["next_offset"])
        second = hc.query_research_inputs(quest_ref=quest, query="negative", limit=1,
            offset=first["next_offset"], query_revision=first["query_revision"])
        assert first["items"][0]["input_ref"] != second["items"][0]["input_ref"]
        with pytest.raises(OwnerConflict, match="research_query_changed"):
            hc.query_research_inputs(quest_ref=quest, query="other", limit=1,
                offset=first["next_offset"], query_revision=first["query_revision"])
        hc.submit_research_input(quest_ref=quest, text_content="another finding", idempotency_key="late")
        with pytest.raises(OwnerConflict, match="research_query_changed"):
            hc.query_research_inputs(quest_ref=quest, query="negative", limit=1,
                offset=first["next_offset"], query_revision=first["query_revision"])
    finally:
        runtime.close()


def _request(runtime, quest, key):
    return runtime.owners.agent_runtime.open_human_request(
        request_kind="offline_action", obligation=f"Review negative observation {key}",
        business_purpose="Retain the failed work and choose further investigation.",
        target_assertion={"quest_ref": quest}, acceptance_conditions=("Give an observation",),
        direct_waiter={"waiter_ref": key, "generation": 1, "target_assertion": {"quest_ref": quest},
                       "wait_scope": "local", "other_blockers": []},
        quest_ref=quest, idempotency_key=key)


def test_request_history_versions_replies_and_rejects_stale_continuation(tmp_path):
    runtime = _runtime(tmp_path / "runtime")
    try:
        quest = _accepted_quest(runtime).quest_ref
        hc = runtime.owners.human_collaboration
        _request(runtime, quest, "first")
        request = _request(runtime, quest, "second")
        first = hc.query_research_help_page(quest_ref=quest, limit=1)
        second = hc.query_research_help_page(quest_ref=quest, limit=1, cursor=first["next_cursor"])
        assert first["items"][0]["request_ref"] != second["items"][0]["request_ref"]
        response = hc.respond_to_human_request(request["request_ref"], decision="provided",
            facts={"observation": "A negative control did not reproduce the effect."},
            note="Keep uncertainty explicit.", idempotency_key="reply")
        with pytest.raises(OwnerConflict, match="research_query_changed"):
            hc.query_research_help_page(quest_ref=quest, limit=1, cursor=first["next_cursor"])
        listed = json.loads(_read(runtime, quest, {"query": "negative observation"})["text"])
        item = next(item for item in listed["items"] if item["request_ref"] == request["request_ref"])
        args = {k: v for k, v in item["latest_response"]["reader"].items() if k != "operation"}
        exact = json.loads(_read(runtime, quest, args)["text"])
        assert exact["response"]["response_ref"] == response["response_ref"]
        assert exact["response"]["facts"]["observation"] == "A negative control did not reproduce the effect."
        with pytest.raises(SemanticMcpError, match="content_version_unbound"):
            _read(runtime, quest, {**args, "version_ref": "wrong"})
    finally:
        runtime.close()


def test_guidance_pages_keep_withdrawn_meaning_without_delivery_side_effects(tmp_path):
    runtime = _runtime(tmp_path / "runtime")
    try:
        quest = _accepted_quest(runtime).quest_ref
        hc = runtime.owners.human_collaboration
        accepted = hc.submit_human_guidance(quest_ref=quest, original_text="Keep the control.",
            idempotency_key="one", strength=2)
        hc.submit_human_guidance(quest_ref=quest, original_text="Allow exploratory work.",
            idempotency_key="two", strength=1)
        first = hc.query_research_guidance(quest_ref=quest, limit=1)
        hc.withdraw_soft_constraint(accepted["constraint_ref"], accepted["revision"], "withdraw-one")
        with pytest.raises(OwnerConflict, match="research_query_changed"):
            hc.query_research_guidance(quest_ref=quest, limit=1, offset=first["next_offset"],
                query_revision=first["query_revision"])
        withdrawn = hc.query_research_guidance(quest_ref=quest, query="control")["items"][0]
        exact = hc.read_research_guidance(withdrawn["constraint_ref"], quest_ref=quest,
            version_ref=withdrawn["reader"]["version_ref"])
        assert exact["status"] == "withdrawn"
        assert exact["guidance"]["text"] == "Keep the control."
        assert hc.query_guidance_deliveries(accepted["constraint_ref"]) == []
        assert hc.query_research_guidance(quest_ref=quest, query="absent")["result_state"] == "no_match"
        assert hc.query_research_guidance(quest_ref="quest_other")["result_state"] == "no_records"
    finally:
        runtime.close()


def test_real_channel_reads_input_material_and_rechecks_retired_root(tmp_path):
    from meta_research.human_research_context import human_research_context_operation
    from meta_research.research_content import research_content_operations
    from meta_research.semantic_mcp import SemanticMcpGateway
    from test_dataset_effect_scope_recovery import _scope
    from test_research_datasets import _asset

    runtime = _runtime(tmp_path / "runtime")
    try:
        quest = _accepted_quest(runtime).quest_ref
        graph, memory, agent = (runtime.owners.research_graph, runtime.owners.research_memory,
                                runtime.owners.agent_runtime)
        hc = runtime.owners.human_collaboration
        asset = _asset(runtime, content=b"Original negative observation.\n")
        graph.accept_asset_role(binding=asset, role="evidence", quest_ref=quest, idempotency_key="origin")
        accepted = hc.submit_research_input(quest_ref=quest, text_content="Consider this negative observation.",
            asset_bindings=[asset.as_dict()], idempotency_key="input")
        hc.submit_human_guidance(quest_ref=quest, original_text="Retain uncertainty.", idempotency_key="guide")
        scope = _scope(runtime, quest_ref=quest)
        human_op = human_research_context_operation(agent_runtime=agent, human_collaboration=hc)
        content_op = next(op for op in research_content_operations(research_graph=graph,
            research_memory=memory, agent_runtime=agent, human_collaboration=hc)
            if op.semantic_operation_id == "research_memory.content.read")
        gateway = SemanticMcpGateway((human_op, content_op))
        channel, _ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
            root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
            capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
            operation_ids=(human_op.semantic_operation_id, content_op.semantic_operation_id))

        def call(operation, arguments):
            _, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": operation, "arguments": arguments}})
            return response["result"]

        directory = call("human_request.read", {})
        assert not directory.get("isError"), directory
        summary = json.loads(directory["structuredContent"]["text"])
        reader = summary["research_inputs"]["items"][0]["reader"]
        body = call("research_memory.content.read", reader)
        assert not body.get("isError"), body
        original = json.loads(body["structuredContent"]["text"])
        assert original["input_ref"] == accepted["input_ref"]
        assert original["text"] == "Consider this negative observation."
        binding = original["asset_bindings"][0]
        material_args = {"source_ref": binding["version_ref"], "version_ref": binding["version_ref"]}
        material = call("research_memory.content.read", material_args)
        assert not material.get("isError"), material
        assert material["structuredContent"]["text"] == "Original negative observation.\n"
        guidance_page = call("human_request.read", {"entry_kind": "guidance"})
        guide_reader = json.loads(guidance_page["structuredContent"]["text"])["items"][0]["reader"]
        guide_args = {k: v for k, v in guide_reader.items() if k != "operation"}
        assert not call("human_request.read", guide_args).get("isError")
        agent.complete_external_root_task_scope(root_kind="companion", root_runtime_scope=scope)
        for operation, args in (("human_request.read", {}), ("human_request.read", guide_args),
                                ("research_memory.content.read", reader),
                                ("research_memory.content.read", material_args)):
            rejected = call(operation, args)
            assert rejected.get("isError") and "scope_stale" in str(rejected), rejected
    finally:
        runtime.close()


def test_request_directory_does_not_prepare_lifecycle_or_all_replies(tmp_path):
    from sqlalchemy import event
    runtime = _runtime(tmp_path / "runtime")
    try:
        quest = _accepted_quest(runtime).quest_ref
        hc = runtime.owners.human_collaboration
        request = _request(runtime, quest, "many-replies")
        submitted = []
        for index in range(5):
            submitted.append(hc.respond_to_human_request(request["request_ref"], decision="deferred",
                facts={"investigation": index}, note=f"Still investigating step {index}",
                idempotency_key=f"reply-{index}")["response_ref"])
        statements = []
        def trace(connection, cursor, sql, parameters, context, many):
            statements.append(sql)
        event.listen(runtime._database._engine, "before_cursor_execute", trace)
        try:
            page = hc.query_research_help_page(quest_ref=quest)
        finally:
            event.remove(runtime._database._engine, "before_cursor_execute", trace)
        lifecycle = [sql for sql in statements if any(table in sql for table in (
            "owner_human_request_waiters", "owner_human_request_resume_validations",
            "owner_human_request_resume_consumptions", "hc_human_request_response_rejections"))]
        observations = {"selected_requests": len(page["items"]), "stored_responses": 5,
            "returned_responses": sum(len(item.get("responses", [])) for item in page["items"]),
            "lifecycle_queries": len(lifecycle), "sql_queries": len(statements)}
        (tmp_path / "human-history-observation.json").write_text(json.dumps(observations))
        assert not lifecycle, observations
        item = page["items"][0]
        assert item["response_count"] == 5
        assert len(item["responses"]) == 1
        assert item["responses"][0]["response_ref"] == submitted[-1]
        seen = []
        args = {k: v for k, v in item["responses_reader"].items() if k != "operation"}
        while True:
            result = json.loads(_read(runtime, quest, args)["text"])
            seen.extend(response["response_ref"] for response in result["items"])
            if result["next_offset"] is None:
                break
            args.update(response_offset=result["next_offset"], query_revision=result["query_revision"])
        assert seen == list(reversed(submitted))
        first_replies = hc.query_research_response_page(quest_ref=quest,
            request_ref=request["request_ref"], limit=2)
        next_replies = hc.query_research_response_page(quest_ref=quest,
            request_ref=request["request_ref"], limit=2, offset=first_replies["next_offset"],
            query_revision=first_replies["query_revision"])
        assert [reply["response_ref"] for reply in first_replies["items"] + next_replies["items"]] == list(reversed(submitted))[0:4]
        from fastapi.testclient import TestClient
        from meta_research.web import create_app
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="isolated-test")) as client:
            auth = client.post("/auth/bootstrap", headers={"Origin": "http://testserver"},
                json={"token": runtime.authentication.issue_bootstrap_token()})
            assert auth.status_code == 200
            params = {"quest_ref": quest, "request_ref": request["request_ref"], "limit": 2}
            first_http = client.get("/api/v1/research-library/human", params=params)
            assert first_http.status_code == 200, first_http.text
            first_http = first_http.json()
            second_http = client.get("/api/v1/research-library/human", params={**params,
                "offset": first_http["next_offset"], "query_revision": first_http["query_revision"]})
            assert second_http.status_code == 200, second_http.text
            assert [item["ref"] for item in first_http["items"] + second_http.json()["items"]] == list(reversed(submitted))[0:4]
            exact_http = client.get("/api/v1/research-content", params={"quest_ref": quest,
                **first_http["items"][0]["reader"]})
            assert exact_http.status_code == 200, exact_http.text
            assert json.loads(exact_http.json()["text"])["response_ref"] == submitted[-1]
        hc.respond_to_human_request(request["request_ref"], decision="deferred",
            facts={"investigation": 5}, note="Another step", idempotency_key="reply-late")
        with pytest.raises(OwnerConflict, match="research_query_changed"):
            hc.query_research_response_page(quest_ref=quest, request_ref=request["request_ref"],
                offset=first_replies["next_offset"], query_revision=first_replies["query_revision"])
    finally:
        runtime.close()
