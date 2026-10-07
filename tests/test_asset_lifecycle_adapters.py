from meta_research.research_content import research_content_operations
from meta_research.semantic_mcp import SemanticMcpGateway
from test_dataset_effect_scope_recovery import _scope
from test_public_research_asset_web import _authenticated_client
from test_research_datasets import _runtime, _quest


def channel(runtime, question, generation=1):
    scope = _scope(
        runtime,
        quest_ref=question.quest_ref,
        run_ref=question.quest_ref,
        root_ref=question.question_ref,
        generation=generation,
    )
    operations = research_content_operations(
        research_graph=runtime.owners.research_graph,
        research_memory=runtime.owners.research_memory,
        agent_runtime=runtime.owners.agent_runtime,
    )
    gateway = SemanticMcpGateway(operations)
    token, _ = gateway.issue_channel(
        run_ref=scope["run_ref"],
        attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"],
        fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"],
        root_kind="companion",
        phase="primary",
        operation_ids=tuple(op.semantic_operation_id for op in operations),
    )

    def call(name, **arguments):
        _, response = gateway.dispatch(
            token.token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        return response["result"]

    return call


def test_scoped_intake_retry_waits_for_a_current_caller_after_storage_failure(
    tmp_path, monkeypatch
):
    from sqlalchemy import text
    from meta_research.owners.common import OwnerConflict
    import meta_research.owners.research_memory as memory_module

    runtime = _runtime(tmp_path / "scoped-retry")
    try:
        question = _quest(runtime, "retry")
        call = channel(runtime, question)
        memory = runtime.owners.research_memory
        original = memory._accept_prepared_asset

        def fail_once(*args, **values):
            monkeypatch.setattr(memory, "_accept_prepared_asset", original)
            raise OwnerConflict("asset_source_unavailable")

        monkeypatch.setattr(memory_module, "ASSET_INTAKE_RETRY_BASE_SECONDS", 0)
        monkeypatch.setattr(memory, "_accept_prepared_asset", fail_once)
        arguments = dict(
            effect_id="recoverable",
            intake={
                "source_kind": "text",
                "custody_mode": "managed",
                "display_name": "recoverable.txt",
                "text": "Exact retry content.\n",
            },
        )
        before = [
            memory.query_asset_version(item.memory_ref)
            for item in memory.query_asset_inventory()
        ]
        result = call("research_memory.assets.intake", **arguments)
        assert not result["isError"], result
        assert result["structuredContent"]["status"] == "queued"
        job_ref = result["structuredContent"]["job_ref"]
        next_call = channel(runtime, question, generation=2)
        assert call("research_memory.assets.intake", **arguments)["isError"]
        assert memory.process_asset_intake_once() is False
        assert memory.query_asset_intake(job_ref).status == "queued"
        assert memory.query_asset_intake(job_ref).asset is None
        after = [
            memory.query_asset_version(item.memory_ref)
            for item in memory.query_asset_inventory()
        ]
        assert after == before
        accepted = next_call("research_memory.assets.intake", **arguments)
        assert not accepted["isError"], accepted
        assert accepted["structuredContent"]["status"] == "accepted"
        version_ref = accepted["structuredContent"]["asset"]["version_ref"]
        assert (
            memory.materialize_asset(version_ref).content == b"Exact retry content.\n"
        )
    finally:
        runtime.close()


def test_http_and_semantic_share_exact_correction_and_retirement_contract(tmp_path):
    runtime = _runtime(tmp_path / "adapters")
    client, headers = _authenticated_client(runtime)
    try:
        one, two = _quest(runtime, "one"), _quest(runtime, "two")
        call = channel(runtime, one)

        def semantic_intake(key, body, **values):
            result = call(
                "research_memory.assets.intake",
                effect_id=key,
                intake={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": key + ".txt",
                    "media_type": "text/plain",
                    "text": body,
                    **values,
                },
            )
            assert result["isError"] is False, result
            assert result["structuredContent"]["status"] == "accepted"
            return result["structuredContent"]["asset"]

        with client:
            first = semantic_intake("original", "wrong unit\n")
            basis = semantic_intake("basis", "calibrated unit\n")
            state = call(
                "research_memory.assets.lifecycle", version_ref=first["version_ref"]
            )["structuredContent"]
            assert state["revision"] == 1
            correction = {
                "kind": "correction",
                "predecessor_version_ref": first["version_ref"],
                "expected_revision": 1,
                "explanation": "Corrects only the transcription; comparison needs recheck.",
                "error": "Wrong unit",
                "scope": "Instrument transcription",
                "evidence_bindings": [
                    {
                        k: basis[k]
                        for k in (
                            "asset_ref",
                            "version_ref",
                            "content_hash",
                            "manifest_hash",
                            "receipt",
                        )
                    }
                ],
                "impact": [
                    {
                        "work_ref": "comparison",
                        "judgment": "recheck",
                        "explanation": "Recompute using calibrated units.",
                    }
                ],
            }
            response = client.post(
                "/api/v1/research-assets/intakes",
                headers={**headers, "Idempotency-Key": "http-correction"},
                json={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": "corrected.txt",
                    "media_type": "text/plain",
                    "text": "right unit\n",
                    "asset_ref": first["asset_ref"],
                    "change": correction,
                },
            )
            assert response.status_code == 201, response.text
            second = response.json()["asset"]
            current = call(
                "research_memory.assets.current", version_ref=first["version_ref"]
            )["structuredContent"]
            assert current["asset"]["version_ref"] == second["version_ref"]
            detail = client.get(
                "/api/v1/research-assets/" + first["version_ref"]
            ).json()
            assert detail["lifecycle"]["changes"] == current["lifecycle"]["changes"]
            for asset, literal in ((first, "wrong unit\n"), (second, "right unit\n")):
                page = call(
                    "research_memory.content.read",
                    source_ref=asset["version_ref"],
                    version_ref=asset["version_ref"],
                )
                assert page["isError"] is False, page
                assert page["structuredContent"]["text"] == literal
                assert (
                    client.get(
                        "/api/v1/research-assets/" + asset["version_ref"] + "/content"
                    ).content
                    == literal.encode()
                )
            assert (
                channel(runtime, two)(
                    "research_memory.assets.lifecycle", version_ref=first["version_ref"]
                )["structuredContent"]["code"]
                == "asset_quest_scope_invalid"
            )
            disposable = semantic_intake("disposable", "obsolete incorrect draft\n")
            state = call(
                "research_memory.assets.lifecycle",
                version_ref=disposable["version_ref"],
            )["structuredContent"]
            request = {
                "version_ref": disposable["version_ref"],
                "expected_revision": 1,
                "expected_reference_revision": state["reference_revision"],
                "explanation": "Verified obsolete draft with no dependent research or explanation value.",
                "low_value": True,
                "obsolete": True,
                "incorrect": True,
                "impact_understood": True,
                "has_explanation_value": False,
                "effect_id": "retire-draft",
            }
            result = call("research_memory.assets.retire", **request)
            assert result["isError"] is False, result
            fact = result["structuredContent"]
            assert fact["kind"] == "retirement"
            assert (
                call("research_memory.assets.retire", **request)["structuredContent"]
                == fact
            )
            assert call("research_memory.assets.retire.reconcile", **request)[
                "structuredContent"
            ] == {"status": "accepted", "result": fact}
            changed = call(
                "research_memory.assets.retire.reconcile",
                **{**request, "explanation": "Changed interpretation"},
            )
            assert (
                changed["structuredContent"]["code"]
                == "asset_retirement_idempotency_conflict"
            )
            missing = call(
                "research_memory.assets.retire.reconcile",
                **{**request, "effect_id": "never-submitted"},
            )
            assert missing["structuredContent"] == {
                "status": "not_found",
                "result": None,
            }
            assert (
                client.get(
                    "/api/v1/research-assets/" + disposable["version_ref"]
                ).json()["lifecycle"]["changes"][-1]
                == fact
            )
            assert (
                call(
                    "research_memory.assets.current",
                    version_ref=disposable["version_ref"],
                )["structuredContent"]["asset"]
                is None
            )
            assert (
                call(
                    "research_memory.content.read",
                    source_ref=disposable["version_ref"],
                    version_ref=disposable["version_ref"],
                )["structuredContent"]["text"]
                == "obsolete incorrect draft\n"
            )
            protected = call(
                "research_memory.assets.retire",
                **{
                    **request,
                    "version_ref": basis["version_ref"],
                    "effect_id": "retire-basis",
                },
            )
            assert protected["structuredContent"]["code"] == "asset_retirement_blocked"
            assert protected["structuredContent"]["details"]["reasons"] == [
                "active_references"
            ]
            channel(runtime, one, generation=2)
            assert (
                call(
                    "research_memory.assets.current", version_ref=first["version_ref"]
                )["isError"]
                is True
            )
    finally:
        runtime.close()


def test_semantic_asset_discovery_uses_one_filtered_page(tmp_path):
    runtime = _runtime(tmp_path / "pages")
    try:
        question = _quest(runtime, "page")
        call = channel(runtime, question)
        assets = []
        for index in range(3):
            result = call(
                "research_memory.assets.intake",
                effect_id=f"page-{index}",
                intake={
                    "source_kind": "text",
                    "custody_mode": "managed",
                    "display_name": f"match-{index}.txt",
                    "text": f"literal {index}",
                },
            )
            assert result["isError"] is False, result
            assets.append(result["structuredContent"]["asset"])
        runtime.owners.research_graph.accept_asset_role(
            binding=runtime.owners.research_memory.query_asset_version(
                assets[0]["version_ref"]
            ).as_binding(),
            role="evidence",
            quest_ref=question.quest_ref,
            idempotency_key="also-a-role",
        )
        refs, offset = [], 0
        while True:
            result = call(
                "research_memory.assets.page", query="match", offset=offset, limit=1
            )["structuredContent"]
            assert len(result["items"]) == 1
            refs.append(result["items"][0]["asset"]["asset_ref"])
            if result["next_offset"] is None:
                break
            offset = result["next_offset"]
        assert refs == [asset["asset_ref"] for asset in reversed(assets)]
    finally:
        runtime.close()


def test_current_full_conformance_accepts_the_expanded_asset_operation_catalog(
    tmp_path,
):
    from test_harness_full_conformance import _runtime, _full_request
    from meta_research.harness import FULL_CONFORMANCE_V2

    runtime, _, _ = _runtime(tmp_path / "asset-catalog-conformance")
    try:
        admitted = runtime.harnesses.start_full_conformance(_full_request())
        assert admitted.contract_ref == FULL_CONFORMANCE_V2
        required = {
            binding["semantic_operation_id"]
            for binding in admitted.runs[0].mcp_binding.operation_bindings
        }
        assert {
            "research_memory.assets.page",
            "research_memory.assets.lifecycle",
            "research_memory.assets.current",
            "research_memory.assets.intake",
            "research_memory.assets.intake.reconcile",
            "research_memory.assets.retire",
            "research_memory.assets.retire.reconcile",
        } <= required
        for _ in range(4):
            if runtime.harnesses.query_status()["status"] == "ready":
                break
            assert runtime.harnesses.advance_full_conformance(
                mcp_base_url="http://127.0.0.1:8765"
            )
        assert runtime.harnesses.query_status()["status"] == "ready"
    finally:
        runtime.close()
