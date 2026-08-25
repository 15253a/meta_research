from __future__ import annotations

import sqlite3
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import meta_research.composition as composition
import meta_research.provider_supervisor as live_provider_supervisor
import pytest

from meta_research.database import Database
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.writing_contract import WritingRuntimeBinding
from meta_research.writing_skill import (
    CodexWritingSkillAdapter,
    WritingSkillDraft,
    WritingSkillRequest,
    WritingSkillResult,
    WritingSkillUnavailable,
)
from test_migration_recovery import _downgrade_to_revision, _upgrade_to_revision
from test_public_writing_report import _confirm_direct_quest, _runtime
from test_writing_skill_adapter import _SequenceRunner, _fake_codex, _request


_PRE_0030_WRITING_ADAPTER_SHA256 = (
    "a6ef050f866709ac6620d982caaf8f5e0d7bf21e548490968c9fa550b21bd453"
)
_PRE_0030_PROVIDER_SUPERVISOR_SHA256 = (
    "623fa83da2893790aaee02351865db1d2a1c9cc90987e11795ef34c3a24b7ab1"
)
_LEGACY_RUNTIME_TABLE = "ar_runtime_instances"


class _LegacyRuntimeProtectionShim:
    """Satisfy only the current composition root's daemon identity lookup.

    Every executable Owner/Harness seam receives ``None`` below, matching the
    pre-protection runtime.  This object therefore cannot grant a fake permit;
    its one temporary table is removed before the fixture is downgraded to
    migration 0029.
    """

    def __init__(self, *, database: Database, **_kwargs: object) -> None:
        self._database = database
        with self._database.write() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE ar_runtime_instances ("
                "incarnation_ref TEXT PRIMARY KEY, "
                "status TEXT NOT NULL, "
                "started_at REAL NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO ar_runtime_instances "
                "(incarnation_ref, status, started_at) VALUES "
                "('legacy_fixture_runtime', 'active', 0)"
            )

    def interrupt_active(self, **_kwargs: object) -> int:
        return 0

    def close(self) -> None:
        with self._database.write() as connection:
            connection.exec_driver_sql(f"DROP TABLE {_LEGACY_RUNTIME_TABLE}")


def _database_revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    assert revision is not None
    return str(revision[0])


def _install_pre_protection_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route 0033 composition through its historical unprotected seams."""

    agent_runtime_factory = composition.create_agent_runtime_interface
    human_collaboration_factory = composition.create_human_collaboration_interface
    harness_factory = composition.HarnessRuntime

    def create_agent_runtime(**kwargs: object):
        kwargs["runtime_protection"] = None
        return agent_runtime_factory(**kwargs)

    def create_human_collaboration(**kwargs: object):
        kwargs["runtime_protection"] = None
        return human_collaboration_factory(**kwargs)

    def create_harness(*args: object, **kwargs: object):
        kwargs["runtime_protection"] = None
        kwargs["runtime_boundary_recorder"] = None
        return harness_factory(*args, **kwargs)

    monkeypatch.setattr(
        composition, "create_agent_runtime_interface", create_agent_runtime
    )
    monkeypatch.setattr(
        composition,
        "create_human_collaboration_interface",
        create_human_collaboration,
    )
    monkeypatch.setattr(composition, "HarnessRuntime", create_harness)


class _Pre0030BindingProvider:
    """The already-installed 0029 provider boundary used to admit an old Run."""

    def __init__(
        self,
        binding: WritingRuntimeBinding,
        *,
        draft: WritingSkillDraft | None = None,
    ) -> None:
        self._binding = binding
        self._draft = draft

    def runtime_binding(self) -> WritingRuntimeBinding:
        return self._binding

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        if self._draft is None:
            raise AssertionError(
                f"0029 provider unexpectedly executed {request.run_ref}"
            )
        assert request.runtime_binding == self._binding
        return self._draft

    def review_draft(
        self, request: WritingSkillRequest, draft: WritingSkillDraft
    ) -> WritingSkillResult:
        raise AssertionError(
            f"0029 provider unexpectedly reviewed {request.run_ref}: {draft.markdown}"
        )


def _pre_0030_report_binding(
    adapter: CodexWritingSkillAdapter,
) -> WritingRuntimeBinding:
    current = adapter.runtime_binding("report")
    skill = (
        files("meta_research") / "skills" / "writing-report" / "SKILL.md"
    ).read_text(encoding="utf-8")
    resource_bindings = tuple(
        (
            "adapter-source:meta_research.writing_skill@sha256:"
            + _PRE_0030_WRITING_ADAPTER_SHA256
        )
        if item.startswith("adapter-source:meta_research.writing_skill@sha256:")
        else (
            "adapter-source:meta_research.provider_supervisor@sha256:"
            + _PRE_0030_PROVIDER_SUPERVISOR_SHA256
        )
        if item.startswith(
            "adapter-source:meta_research.provider_supervisor@sha256:"
        )
        else item
        for item in current.resource_bindings
    )
    return replace(
        current,
        instruction_set_hash=canonical_hash(
            {
                "skill_instructions": skill,
                "adapter_source_hash": _PRE_0030_WRITING_ADAPTER_SHA256,
                "supervisor_source_hash": (
                    _PRE_0030_PROVIDER_SUPERVISOR_SHA256
                ),
            }
        ),
        resource_bindings=resource_bindings,
    )


def _admit_pre_0030_report(
    root: Path,
    provider: _Pre0030BindingProvider,
    *,
    cross_primary_boundary: bool = False,
) -> tuple[str, str]:
    runtime = _runtime(root, provider)
    try:
        quest = _confirm_direct_quest(runtime)
        drafted = runtime.writing.create_report_intent(
            quest_ref=quest["quest_ref"],
            title="升级前仍在运行的 report",
            audience="研究负责人",
            purpose="验证 0030 升级后恢复",
            instructions="保持已冻结 Snapshot 与旧 runtime binding。",
            idempotency_key="pre-0030-writing-create",
        )
        previewed = runtime.writing.preview_report_intent(
            drafted["intent_id"],
            idempotency_key="pre-0030-writing-preview",
        )
        admitted = runtime.writing.confirm_report_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=previewed["impact_preview"]["preview_ref"],
            preview_hash=previewed["impact_preview"]["preview_hash"],
            idempotency_key="pre-0030-writing-confirm",
        )
        if cross_primary_boundary:
            assert runtime.writing.process_once() is True
            checkpointed = runtime.writing.query_writing_report(
                admitted["run"]["run_ref"]
            )
            assert checkpointed["execution"]["checkpoint"] is not None
            assert checkpointed["execution"]["receipt"] is None
        return admitted["run"]["run_ref"], admitted["run"][
            "runtime_binding_hash"
        ]
    finally:
        runtime.close()


def _create_pre_0030_report_fixture(
    root: Path,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _Pre0030BindingProvider,
    *,
    cross_primary_boundary: bool = False,
) -> tuple[str, str]:
    """Create with the last pre-delivery runtime, then retain real 0029 state."""

    _upgrade_to_revision(database, "0033_reasoning_successor_context")
    with monkeypatch.context() as legacy_runtime:
        legacy_runtime.setattr(
            composition, "RuntimeProtection", _LegacyRuntimeProtectionShim
        )
        legacy_runtime.setattr(
            composition, "upgrade_database", lambda _path: None
        )
        _install_pre_protection_composition(legacy_runtime)
        result = _admit_pre_0030_report(
            root,
            provider,
            cross_primary_boundary=cross_primary_boundary,
        )
    assert _database_revision(database) == "0033_reasoning_successor_context"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_LEGACY_RUNTIME_TABLE,),
        ).fetchone() is None
    _downgrade_to_revision(database, "0029_target_root_lifecycle")
    assert _database_revision(database) == "0029_target_root_lifecycle"
    return result


def test_0030_upgrade_resumes_a_pre_checkpoint_report_with_its_immutable_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "pre-checkpoint-upgrade"
    data_root = prepare_data_root(root)
    executable = str(_fake_codex(root / "codex"))
    workspace = root / "writing-skill-provider"
    binding_adapter = CodexWritingSkillAdapter(
        workspace,
        executable=executable,
        model_ref="test-model",
        process_runner=_SequenceRunner([]),
    )
    legacy_binding = _pre_0030_report_binding(binding_adapter)
    assert legacy_binding != binding_adapter.runtime_binding("report")
    current_paper_binding = binding_adapter.runtime_binding("paper")

    # A legacy runtime must execute the supervisor source sealed into its
    # immutable bundle.  Make the currently installed module observably
    # incompatible before recovery; a loader that resolves the absolute import
    # against the live module will now drift from ``legacy_binding``.
    monkeypatch.setattr(
        live_provider_supervisor,
        "transport_key_hash",
        lambda _value: "f" * 64,
    )

    run_ref, binding_hash = _create_pre_0030_report_fixture(
        root,
        data_root.database,
        monkeypatch,
        _Pre0030BindingProvider(legacy_binding),
    )

    runner = _SequenceRunner(
        [
            {
                "markdown": "# 升级恢复初稿\n\n当前证据尚不足。\n",
                "citations": [],
            }
        ]
    )
    current_adapter = CodexWritingSkillAdapter(
        workspace,
        executable=executable,
        model_ref="test-model",
        process_runner=runner,
    )
    restarted = _runtime(root, current_adapter)
    try:
        assert _database_revision(data_root.database) == "0035_runtime_protection"
        before = restarted.writing.query_writing_report(run_ref)
        assert before["run"]["runtime_binding_hash"] == binding_hash
        assert before["execution"]["checkpoint"] is None

        assert restarted.writing.process_once() is True

        recovered = restarted.writing.query_writing_report(run_ref)
        assert recovered["run"]["status"] == "active"
        assert recovered["run"]["runtime_binding_hash"] == binding_hash
        assert recovered["execution"]["checkpoint"] is not None
        assert len(runner.calls) == 1
        assert binding_adapter.runtime_binding("paper") == current_paper_binding
        assert live_provider_supervisor.transport_key_hash(b"still-live") == (
            "f" * 64
        )
    finally:
        restarted.close()


def test_0030_upgrade_resumes_a_checkpointed_report_before_review_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "checkpoint-upgrade"
    data_root = prepare_data_root(root)
    executable = str(_fake_codex(root / "codex"))
    workspace = root / "writing-skill-provider"
    binding_adapter = CodexWritingSkillAdapter(
        workspace,
        executable=executable,
        model_ref="test-model",
        process_runner=_SequenceRunner([]),
    )
    legacy_binding = _pre_0030_report_binding(binding_adapter)
    checkpoint_draft = WritingSkillDraft(
        markdown="# 升级前初稿\n\n当前证据尚不足。\n",
        citations=(),
        primary_session_ref="codex-writing-primary:1",
        adapter_kind="codex_cli",
    )

    run_ref, binding_hash = _create_pre_0030_report_fixture(
        root,
        data_root.database,
        monkeypatch,
        _Pre0030BindingProvider(legacy_binding, draft=checkpoint_draft),
        cross_primary_boundary=True,
    )

    runner = _SequenceRunner(
        [
            {
                "reviewer_agent_ref": "codex-writing-reviewer:upgrade",
                "findings": [],
                "dispositions": [],
                "final_markdown": checkpoint_draft.markdown,
                "citations": [],
            }
        ]
    )
    current_adapter = CodexWritingSkillAdapter(
        workspace,
        executable=executable,
        model_ref="test-model",
        process_runner=runner,
    )
    restarted = _runtime(root, current_adapter)
    try:
        assert _database_revision(data_root.database) == "0035_runtime_protection"
        before = restarted.writing.query_writing_report(run_ref)
        checkpoint = before["execution"]["checkpoint"]
        assert checkpoint is not None
        assert checkpoint["native_session_ref"] == "codex-writing-primary:1"
        assert before["execution"]["receipt"] is None
        assert before["run"]["runtime_binding_hash"] == binding_hash

        assert restarted.writing.process_once() is True

        recovered = restarted.writing.query_writing_report(run_ref)
        assert recovered["run"]["status"] == "active"
        assert recovered["run"]["runtime_binding_hash"] == binding_hash
        assert recovered["execution"]["checkpoint"] == checkpoint
        assert recovered["execution"]["receipt"] is not None
        assert len(runner.calls) == 1
        assert runner.calls[0][0][-3:] == [
            "resume",
            "codex-writing-primary:1",
            "-",
        ]
    finally:
        restarted.close()


@pytest.mark.parametrize(
    ("document_type", "profile_ref"),
    (("paper", "paper-v1"), ("presentation", "presentation-v1")),
)
def test_pre_0030_report_binding_cannot_execute_a_new_document_type(
    tmp_path: Path,
    document_type: str,
    profile_ref: str,
) -> None:
    runner = _SequenceRunner([])
    adapter = CodexWritingSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        model_ref="test-model",
        process_runner=runner,
    )
    request = replace(
        _request(adapter),
        runtime_binding=_pre_0030_report_binding(adapter),
        document_type=document_type,
        profile_ref=profile_ref,
    )

    with pytest.raises(
        WritingSkillUnavailable, match="writing_runtime_binding_drift"
    ):
        adapter.generate_draft(request)

    assert runner.calls == []
