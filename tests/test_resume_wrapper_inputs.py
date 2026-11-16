import json
from types import SimpleNamespace

import pytest

from meta_research.owners.common import canonical_json
from meta_research.root_workspace import WorkspaceBinding, WorkspaceLocation
from resume_stage_capture import make_adapter


def test_generic_root_replays_original_guidance_but_rejects_changed_caller_input(tmp_path, monkeypatch):
    from meta_research.idea_skill import IdeaSkillUnavailable

    adapter, _, runner = make_adapter("idea", tmp_path, [{"ok": True}])
    prepared = []

    def guidance(**values):
        prepared.append(values["prompt"])
        return values["prompt"] + ("\nCURRENT_HUMAN_INPUT" if len(prepared) == 1 else ""), None

    monkeypatch.setattr(adapter, "_prepare_stage_guidance", guidance)
    arguments = dict(operation_name="primary", job_ref="generic-root", native_session_ref=None,
        run_ref=None, attempt_ref=None, root_session_ref="root:1", fence_ref=None,
        schema={"type": "object", "additionalProperties": False,
                "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
    first = adapter._invoke_root_operation(prompt="ORIGINAL_REQUEST", **arguments)
    assert adapter._invoke_root_operation(prompt="ORIGINAL_REQUEST", **arguments) == first
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_spool_invalid"):
        adapter._invoke_root_operation(prompt="DIFFERENT_REQUEST", **arguments)
    assert len(runner.calls) == 1
    assert "CURRENT_HUMAN_INPUT" in runner.calls[0][1]


def test_shared_wrapper_sends_changes_once_and_keeps_sealed_input(tmp_path, monkeypatch):
    adapter, _, runner = make_adapter("idea", tmp_path, [{"ok": True}] * 7)
    work = tmp_path / "work"
    work.mkdir()
    binding = WorkspaceBinding(WorkspaceLocation("workspace:own", "idea", "root:1", "run:1",
        work, quest_ref="quest:1", cycle_ref="cycle:1"), "a" * 64)
    runtime = {"quest_ref": "quest:1", "revision": "revision:a", "text": "CONDITIONS_A " * 500,
               "research_time": {"remaining_seconds": 500, "observed_at": 1, "budget_policy": "advisory"}}
    directories = [{"workspace_ref": "workspace:own", "directory": str(work), "access": "read_write"},
                   {"workspace_ref": "workspace:prior", "directory": "/research/prior", "access": "read"}]
    monkeypatch.setattr("meta_research.idea_skill.render_runtime_conditions",
        lambda *args, **kwargs: "Fixed runtime instructions\n" + canonical_json(runtime))
    adapter._workspaces = SimpleNamespace(native_directory_prompt=lambda location:
        "\n\nFixed directory instructions. Work directories: " + canonical_json(directories))
    order = []
    adapter._human_guidance_authority = SimpleNamespace(root_input_order=lambda scope: tuple(reversed(order)))
    session = None

    def invoke(job, *, first=False):
        nonlocal session
        order.append(("unit:" + job, job, "attempt:1", "idea_primary"))
        result = adapter._invoke(operation_name="primary", prompt="CURRENT_EVENT " + job,
            schema={"type": "object", "additionalProperties": False, "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            native_session_ref=None if first else session, job_ref=job, run_ref="run:1",
            workspace_binding=binding, operation_input={"event": job})
        session = result[1]
        return runner.calls[-1][1]

    initial = invoke("initial", first=True)
    unchanged = invoke("unchanged")
    assert "CONDITIONS_A " * 500 in initial
    assert "Fixed directory instructions" in initial
    assert "CONDITIONS_A" not in unchanged and "/research/prior" not in unchanged
    assert "remaining_seconds" in unchanged
    runtime.update(revision="revision:b", text="CONDITIONS_B " * 500)
    directories.pop()
    changed = invoke("changed")
    assert "CONDITIONS_B " * 500 in changed
    assert '"removed_workspace_refs":["workspace:prior"]' in changed
    assert "CONDITIONS_B" not in invoke("after-change")
    runtime.update(revision="revision:a", text="CONDITIONS_A " * 500)
    assert "CONDITIONS_A " * 500 in invoke("reverted")
    assert "CONDITIONS_A" not in invoke("after-revert")
    assert "Fixed runtime instructions" in invoke("replacement", first=True)
    calls = len(runner.calls)
    runtime["text"] = "MUST_NOT_REWRITE_SEALED_INPUT"
    adapter._invoke(operation_name="primary", prompt="CURRENT_EVENT replacement",
        schema={"type": "object", "additionalProperties": False, "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
        native_session_ref=None, job_ref="replacement", run_ref="run:1", workspace_binding=binding,
        operation_input={"event": "replacement"})
    assert len(runner.calls) == calls
    assert all("MUST_NOT_REWRITE_SEALED_INPUT" not in call[1] for call in runner.calls)
    developer = lambda call: [value for value in call[0] if value.startswith("developer_instructions=")]
    assert developer(runner.calls[0]) == developer(runner.calls[1])
    (tmp_path / "wrapper-inputs.json").write_text(json.dumps([
        {"argv": call[0], "prompt": call[1], "schema": call[2]} for call in runner.calls
    ], ensure_ascii=False, indent=2), encoding="utf-8")


def test_external_tools_keep_capability_but_do_not_repeat_stable_service_context(tmp_path):
    from meta_research.external_mcp import parse_services
    from test_external_mcp import configured, service

    adapter, _, runner = make_adapter("idea", tmp_path / "provider", [{"ok": True}] * 4)
    external = configured(tmp_path / "external", research_instructions="NEW_SERVICE_INSTRUCTIONS")
    adapter.bind_external_mcp(external)
    session = None

    def invoke(job):
        nonlocal session
        result = adapter._invoke(operation_name="primary", prompt="EVENT " + job,
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            native_session_ref=session, job_ref=job, operation_input={"event": job})
        session = result[1]
        return runner.calls[-1]

    first = invoke("first")
    continued = invoke("continued")
    assert "NEW_SERVICE_INSTRUCTIONS" in first[1]
    assert "NEW_SERVICE_INSTRUCTIONS" not in continued[1]
    assert any("mcp_servers.meta_research_external.url" in arg for arg in continued[0])
    external.save_config(services=(), expected_revision=external.read_config().revision)
    removed = invoke("removed")
    assert '"removed_service_ids":["lab"]' in removed[1]
    external.save_config(services=parse_services([service(tmp_path / "external",
        research_instructions="RESTORED_SERVICE")]), expected_revision=external.read_config().revision)
    restored = invoke("restored")
    assert "RESTORED_SERVICE" in restored[1]
