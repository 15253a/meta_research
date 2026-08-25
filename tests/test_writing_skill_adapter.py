from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from dataclasses import replace

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.writing_contract import (
    WRITING_REPORT_INTENT_SCHEMA,
    WRITING_RESEARCH_SNAPSHOT_SCHEMA,
)
from meta_research.writing_skill import (
    CodexWritingSkillAdapter,
    WritingSkillDraft,
    WritingSkillRequest,
    WritingSkillUnavailable,
    WritingSourceMaterial,
    writing_review_task_hash,
)


_SOURCE = b"rare morphology remains visible\n"


class _SequenceRunner:
    def __init__(
        self,
        outputs: list[dict[str, object]],
        *,
        context_canary_seen: bool = False,
    ) -> None:
        self._outputs = iter(outputs)
        self._context_canary_seen = context_canary_seen
        self.calls: list[tuple[list[str], str, dict[str, object]]] = []

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output = next(self._outputs)
        output_path.write_text(
            json.dumps(output, ensure_ascii=False), encoding="utf-8"
        )
        self.calls.append((argv, prompt, schema))
        thread_ref = "codex-writing-primary:1"
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": thread_ref}
        ]
        reviewer_ref = output.get("reviewer_agent_ref")
        if isinstance(reviewer_ref, str):
            child_prompt = prompt.split(
                "<<<WRITING_CHILD_REVIEW_TASK_BEGIN>>>\n", 1
            )[1].split("\n<<<WRITING_CHILD_REVIEW_TASK_END>>>", 1)[0]
            task = json.loads(
                child_prompt.split("\nreview_task=", 1)[1].split(
                    "\nresponse_schema_ref=", 1
                )[0]
            )
            child_message = json.dumps(
                {
                    "schema_ref": "meta-research/writing-child-review-result/v1",
                    "review_task_hash": task["review_task_hash"],
                    "context_canary_seen": self._context_canary_seen,
                    "findings": output["findings"],
                },
                ensure_ascii=False,
            )
            for tool, status in (
                ("spawn_agent", "pending_init"),
                ("wait", "completed"),
            ):
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": f"collab-{tool}:1",
                            "type": "collab_tool_call",
                            "tool": tool,
                            "sender_thread_id": thread_ref,
                            "receiver_thread_ids": [reviewer_ref],
                            "prompt": child_prompt if tool == "spawn_agent" else None,
                            "agents_states": {
                                reviewer_ref: {
                                    "status": status,
                                    **(
                                        {"message": child_message}
                                        if tool == "wait"
                                        else {}
                                    ),
                                }
                            },
                            "status": "completed",
                        },
                    }
                )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="\n".join(
                json.dumps(event, ensure_ascii=False) for event in events
            ),
            stderr="",
        )

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, prompt, timeout)


def _fake_codex(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf 'codex-writing-test 1\\n'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _request(adapter: CodexWritingSkillAdapter) -> WritingSkillRequest:
    source_hash = hashlib.sha256(_SOURCE).hexdigest()
    snapshot = {
        "schema_ref": WRITING_RESEARCH_SNAPSHOT_SCHEMA,
        "quest_ref": "quest:writing-adapter",
        "quest": {"quest_ref": "quest:writing-adapter"},
        "questions": [],
        "accepted_sources": [
            {
                "version_ref": "asset_version:source-1",
                "content_hash": source_hash,
            }
        ],
        "advancement": {"cycle": None, "idea": None, "plan": None},
        "snapshot_ref": "writing_snapshot:adapter",
    }
    snapshot["snapshot_hash"] = canonical_hash(snapshot)
    return WritingSkillRequest(
        run_ref="writing_run:adapter",
        attempt_ref="writing_attempt:adapter",
        fence_ref="writing_fence:adapter",
        intent={
            "schema_ref": WRITING_REPORT_INTENT_SCHEMA,
            "title": "形态保真报告",
            "audience": "研究负责人",
            "purpose": "验证证据输入",
            "instructions": "只引用冻结来源。",
        },
        snapshot=snapshot,
        root_session_ref="writing_root:adapter",
        revision=1,
        runtime_binding=adapter.runtime_binding(),
        source_materials=(
            WritingSourceMaterial(
                version_ref="asset_version:source-1",
                content_hash=source_hash,
                file_name="observation.txt",
                media_type="text/plain; charset=utf-8",
                content=_SOURCE,
                materialized_sha256=source_hash,
            ),
        ),
    )


def _draft_output() -> dict[str, object]:
    return {
        "markdown": (
            "# 形态保真报告\n\n"
            "<!-- meta-research-claim:supported refs=citation:source-1 -->\n"
            "rare morphology remains visible [[citation:citation:source-1]]\n"
        ),
        "citations": [
            {
                "citation_ref": "citation:source-1",
                "source_version_ref": "asset_version:source-1",
                "locator": "line:1",
                "claim": "rare morphology remains visible",
                "source_quote": "rare morphology remains visible",
            }
        ],
    }


def test_production_adapter_stages_exact_rm_sources_and_reuses_one_root_session(
    tmp_path: Path,
) -> None:
    draft = _draft_output()
    runner = _SequenceRunner(
        [
            draft,
            {
                "reviewer_agent_ref": "codex-writing-reviewer:1",
                "findings": [],
                "dispositions": [],
                "final_markdown": draft["markdown"],
                "citations": draft["citations"],
            },
        ]
    )
    adapter = CodexWritingSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        model_ref="test-model",
        process_runner=runner,
    )
    request = _request(adapter)

    result = adapter.execute(request)

    assert result.primary_session_ref == "codex-writing-primary:1"
    assert result.reviewer_agent_ref == "codex-writing-reviewer:1"
    assert len(runner.calls) == 2
    primary_argv, primary_prompt, _primary_schema = runner.calls[0]
    review_argv, review_prompt, _review_schema = runner.calls[1]
    assert primary_argv[:2] == [str(tmp_path / "codex"), "exec"]
    assert review_argv[-3:] == ["resume", "codex-writing-primary:1", "-"]
    assert 'web_search="disabled"' in primary_argv
    assert 'web_search="disabled"' in review_argv
    assert 'web_search="live"' not in primary_argv
    assert 'web_search="live"' not in review_argv
    assert "mcp_servers={}" in primary_argv
    assert "mcp_servers={}" in review_argv
    assert 'shell_environment_policy.inherit="none"' in primary_argv
    assert "danger-full-access" not in primary_argv
    assert "external-research-disabled" in request.runtime_binding.capability_bindings
    assert "web-search-live" not in request.runtime_binding.capability_bindings
    assert "accepted_source_manifest=" in primary_prompt
    assert '"accepted_source_manifest":' in review_prompt
    assert 'fresh_context_mode":"fork_turns:none"' in review_prompt
    assert "root_context_canary=" in review_prompt
    assert "attempt_ref=" not in primary_prompt
    assert "fence_ref=" not in primary_prompt

    source_root = (
        tmp_path
        / "provider"
        / "writing-inputs"
        / request.snapshot["snapshot_hash"]
    )
    permission_profile = next(
        value
        for value in primary_argv
        if value.startswith("permissions.writing_snapshot=")
    )
    review_permission_profile = next(
        value
        for value in review_argv
        if value.startswith("permissions.writing_snapshot=")
    )
    assert "--sandbox" not in primary_argv
    assert "--sandbox" not in review_argv
    assert 'default_permissions="writing_snapshot"' in primary_argv
    assert 'default_permissions="writing_snapshot"' in review_argv
    assert f'{json.dumps(str(source_root))}="read"' in permission_profile
    assert f'{json.dumps(str(source_root))}="read"' in review_permission_profile
    assert (
        f'{json.dumps(str(tmp_path / "provider" / "research-workspace"))}="write"'
        in permission_profile
    )
    assert (
        f'{json.dumps(str(tmp_path / "provider" / "writing-inputs"))}="read"'
        not in permission_profile
    )
    assert 'network={enabled=false}' in permission_profile
    manifest = json.loads((source_root / "manifest.json").read_text("utf-8"))
    assert manifest["snapshot_hash"] == request.snapshot["snapshot_hash"]
    assert manifest["sources"][0]["version_ref"] == "asset_version:source-1"
    staged = Path(manifest["sources"][0]["path"])
    assert staged.read_bytes() == _SOURCE
    assert not staged.is_relative_to(
        tmp_path / "provider" / "research-workspace"
    )


def test_production_adapter_rejects_a_symlinked_daemon_source_root(
    tmp_path: Path,
) -> None:
    adapter = CodexWritingSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=_SequenceRunner([_draft_output()]),
    )
    request = _request(adapter)
    staging_parent = tmp_path / "provider" / "writing-inputs"
    staging_parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-provider-sandbox"
    outside.mkdir()
    (staging_parent / request.snapshot["snapshot_hash"]).symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(
        WritingSkillUnavailable, match="writing_source_staging_unsafe"
    ):
        adapter.generate_draft(request)
    assert list(outside.iterdir()) == []


def test_review_spool_replays_after_crash_rotates_only_attempt_and_fence(
    tmp_path: Path,
) -> None:
    draft_output = _draft_output()
    review_output = {
        "reviewer_agent_ref": "codex-writing-reviewer:stable",
        "findings": [],
        "dispositions": [],
        "final_markdown": draft_output["markdown"],
        "citations": draft_output["citations"],
    }
    runner = _SequenceRunner([review_output])
    adapter = CodexWritingSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    request = replace(
        _request(adapter),
        native_session_ref="codex-writing-primary:1",
        job_ref="writing_job:stable-review",
    )
    draft = WritingSkillDraft(
        markdown=str(draft_output["markdown"]),
        citations=tuple(draft_output["citations"]),
        primary_session_ref="codex-writing-primary:1",
        adapter_kind="persisted_checkpoint",
    )

    first = adapter.review_draft(request, draft)
    recovered_request = replace(
        request,
        attempt_ref="writing_attempt:after-crash",
        fence_ref="writing_fence:after-crash",
    )
    replay = adapter.review_draft(recovered_request, draft)

    assert len(runner.calls) == 1
    assert replay == first
    assert writing_review_task_hash(recovered_request, draft) == (
        writing_review_task_hash(request, draft)
    )


def test_production_adapter_fails_closed_if_staged_source_is_replaced(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner([_draft_output(), _draft_output()])
    adapter = CodexWritingSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    request = _request(adapter)
    adapter.generate_draft(request)
    source_root = (
        tmp_path
        / "provider"
        / "writing-inputs"
        / request.snapshot["snapshot_hash"]
    )
    manifest = json.loads((source_root / "manifest.json").read_text("utf-8"))
    Path(manifest["sources"][0]["path"]).write_bytes(b"tampered\n")

    with pytest.raises(
        WritingSkillUnavailable, match="writing_source_staging_conflict"
    ):
        adapter.generate_draft(request)
    assert len(runner.calls) == 1


def test_production_adapter_rejects_a_child_that_inherited_root_context(
    tmp_path: Path,
) -> None:
    draft = _draft_output()
    runner = _SequenceRunner(
        [
            draft,
            {
                "reviewer_agent_ref": "codex-writing-reviewer:contaminated",
                "findings": [],
                "dispositions": [],
                "final_markdown": draft["markdown"],
                "citations": draft["citations"],
            },
        ],
        context_canary_seen=True,
    )
    adapter = CodexWritingSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )

    with pytest.raises(
        WritingSkillUnavailable, match="writing_child_review_result_invalid"
    ):
        adapter.execute(_request(adapter))


def test_writing_runtime_binding_seals_the_complete_provider_contract(
    tmp_path: Path,
) -> None:
    adapter = CodexWritingSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=_SequenceRunner([]),
    )

    binding = adapter.runtime_binding()

    binding.validate()
    assert "filesystem-read-root-confined" in binding.capability_bindings
    assert "environment-inheritance-none" in binding.capability_bindings
    assert "filesystem-danger-full-access" not in binding.capability_bindings
    assert "filesystem-workspace-write" not in binding.capability_bindings
    assert any(
        item.startswith("adapter-source:meta_research.provider_supervisor@sha256:")
        for item in binding.resource_bindings
    )
    assert any(
        item.startswith("transport-seal-key:sha256:")
        for item in binding.resource_bindings
    )
    assert any(
        item.startswith("provider-timeout-seconds:")
        for item in binding.resource_bindings
    )
    assert sum(
        item.startswith("output-schema:") for item in binding.resource_bindings
    ) == 2

    invalid_bindings = (
        replace(binding, packaged_skill_bundle_hash="g" * 64),
        replace(binding, mcp_bindings=("unsealed-mcp",)),
        replace(binding, capability_bindings=("external-publish",)),
        replace(
            binding,
            resource_bindings=binding.resource_bindings
            + (binding.resource_bindings[0],),
        ),
        replace(binding, resource_bindings=("package:../escape",)),
    )
    for invalid in invalid_bindings:
        with pytest.raises(OwnerConflict, match="writing_runtime_binding"):
            invalid.validate()


def test_cancel_reconciliation_inspects_writing_provider_phases(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    adapter = CodexWritingSkillAdapter(
        workspace,
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=_SequenceRunner([]),
    )
    job_ref = "writing-job:cancel-reconciliation"
    operation = (
        workspace
        / "provider-operations"
        / canonical_hash({"job_ref": job_ref})
        / "writing-primary"
    )
    operation.mkdir(parents=True)
    (operation / "partial-spool").write_text("unknown", encoding="utf-8")

    assert adapter.reconcile_cancelled_job(job_ref) is False
