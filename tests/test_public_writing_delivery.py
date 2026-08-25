from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

import meta_research.writing as writing_module
from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.writing_delivery import (
    LocalFilesystemWritingDeliveryProvider,
    WritingDeliveryOutcomeUnknown,
    WritingDeliveryProviderRegistry,
)
from meta_research.writing_renderer import WritingRendererRegistry
from test_public_writing_report import (
    _DeterministicDraftingAdapter,
    _DeterministicProbe,
    _DeterministicWritingSkill,
    _confirm_direct_quest,
    _runtime,
)


def _completed_report(runtime) -> dict[str, object]:
    quest = _confirm_direct_quest(runtime)
    drafted = runtime.writing.create_report_intent(
        quest_ref=quest["quest_ref"],
        title="Exact externally delivered report",
        audience="Research lead",
        purpose="Exercise the explicit external-delivery boundary",
        instructions="Keep the accepted evidence boundary visible.",
        idempotency_key="delivery-writing-create",
    )
    previewed = runtime.writing.preview_report_intent(
        drafted["intent_id"], idempotency_key="delivery-writing-preview"
    )
    preview = previewed["impact_preview"]
    admitted = runtime.writing.confirm_report_intent(
        drafted["intent_id"],
        draft_revision=previewed["draft_revision"],
        draft_hash=previewed["draft_hash"],
        preview_ref=preview["preview_ref"],
        preview_hash=preview["preview_hash"],
        idempotency_key="delivery-writing-confirm",
    )
    run_ref = admitted["run"]["run_ref"]
    for _step in range(8):
        projected = runtime.writing.query_writing_report(run_ref)
        if projected["citation"]["status"] == "accepted":
            return projected
        assert runtime.writing.process_once()
    raise AssertionError("Writing report did not reach accepted citation")


class _LocalAckLostOnceProvider(LocalFilesystemWritingDeliveryProvider):
    def __init__(self) -> None:
        self.execute_calls = 0
        self.reconcile_calls = 0

    def execute(self, request):
        self.execute_calls += 1
        super().execute(request)
        raise WritingDeliveryOutcomeUnknown("provider_ack_lost")

    def reconcile(self, request):
        self.reconcile_calls += 1
        return super().reconcile(request)


class _CountingLocalProvider(LocalFilesystemWritingDeliveryProvider):
    def __init__(self) -> None:
        self.execute_calls = 0
        self.reconcile_calls = 0

    def execute(self, request):
        self.execute_calls += 1
        return super().execute(request)

    def reconcile(self, request):
        self.reconcile_calls += 1
        return super().reconcile(request)


class _PersistentUnknownLocalProvider(LocalFilesystemWritingDeliveryProvider):
    def __init__(self) -> None:
        self.execute_calls: list[str] = []
        self.reconcile_calls: list[str] = []

    def execute(self, request):
        target = str(request.target["path"])
        self.execute_calls.append(target)
        if target.endswith("unknown.md"):
            raise WritingDeliveryOutcomeUnknown("provider_ack_lost")
        return super().execute(request)

    def reconcile(self, request):
        target = str(request.target["path"])
        self.reconcile_calls.append(target)
        if target.endswith("unknown.md"):
            raise WritingDeliveryOutcomeUnknown("provider_still_unknown")
        return super().reconcile(request)


class _SwapParentInExecutionWindowProvider(
    LocalFilesystemWritingDeliveryProvider
):
    def __init__(self, parent: Path, moved_parent: Path) -> None:
        self._parent = parent
        self._moved_parent = moved_parent
        self.swapped = False

    def execute(self, request):
        self._parent.rename(self._moved_parent)
        self._parent.mkdir(mode=0o700)
        self.swapped = True
        return super().execute(request)


def _runtime_with_delivery_provider(
    path: Path,
    provider,
    *,
    renderer_registry: WritingRendererRegistry | None = None,
):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        writing_skill_provider=_DeterministicWritingSkill(),
        writing_delivery_provider_registry=WritingDeliveryProviderRegistry(
            (provider,)
        ),
        writing_renderer_registry=renderer_registry,
    )


class _UpgradedMarkdownRenderer:
    document_type = "report"
    renderer_ref = "meta-research/renderer/markdown/v2-test"
    output_format = "markdown"
    media_type = "text/markdown; charset=utf-8"
    file_extension = ".md"

    def render(self, markdown, citations):
        del citations
        return ("<!-- renderer-v2 -->\n" + markdown).encode("utf-8")


def test_external_publish_requires_exact_preview_and_ar_execution(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "writing-delivery")
    try:
        report = _completed_report(runtime)
        run_ref = report["run"]["run_ref"]
        target = tmp_path / "external" / "accepted-report.md"
        target.parent.mkdir(mode=0o700)

        drafted = runtime.writing.create_delivery_intent(
            run_ref,
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-publish-draft",
        )

        assert drafted["status"] == "not_attempted"
        assert drafted["confirmation_status"] == "draft"
        assert drafted["operation"] is None
        target_binding = drafted["payload"]["target_binding"]
        assert target_binding["provider_ref"] == "local-filesystem"
        assert target_binding["action"] == "publish"
        assert target_binding["provider_binding"]["schema_ref"] == (
            "meta-research/local-directory-chain-binding/v1"
        )
        assert set(target_binding["provider_binding"]) == {
            "schema_ref",
            "parent_path_hash",
            "parent_chain_digest",
            "component_count",
        }
        assert len(target_binding["provider_binding"]["parent_path_hash"]) == 64
        assert len(target_binding["provider_binding"]["parent_chain_digest"]) == 64
        assert target_binding["provider_binding"]["component_count"] >= 1
        assert target.exists() is False
        assert runtime.writing.process_delivery_once() is False

        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"], idempotency_key="delivery-publish-preview"
        )
        preview = previewed["impact_preview"]
        assertion = preview["target_assertion"]
        assert assertion["operation_ref"] == drafted["payload"]["operation_ref"]
        assert assertion["version_ref"] == report["deliverable"]["version_ref"]
        assert assertion["target"]["path"] == str(target)
        assert assertion["target_binding"] == target_binding
        assert previewed["status"] == "not_attempted"
        assert target.exists() is False

        confirmed = runtime.writing.confirm_delivery_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=preview["preview_ref"],
            preview_hash=preview["preview_hash"],
            idempotency_key="delivery-publish-confirm",
        )
        assert confirmed["status"] == "not_attempted"
        assert confirmed["confirmation_status"] == "confirmed"
        assert confirmed["operation"]["authority_status"] == "admitted"
        assert confirmed["operation"]["operation_receipt"]["issuer"] == (
            "agent_runtime"
        )
        assert target.exists() is False

        assert runtime.writing.process_delivery_once() is True
        delivered = runtime.writing.query_delivery_operation(
            drafted["payload"]["operation_ref"]
        )
        rendered = runtime.writing.render_report(run_ref, format="markdown")

        assert delivered["status"] == "completed"
        assert delivered["authority_status"] == "completed"
        assert delivered["execution_receipt"]["issuer"] == "agent_runtime"
        assert delivered["provider_observations"][0]["outcome"] == "completed"
        assert target.read_bytes() == rendered["content"]
        assert hashlib.sha256(target.read_bytes()).hexdigest() == drafted["payload"][
            "renderer_artifact_sha256"
        ]
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        facts = runtime.owners.agent_runtime.query_snapshot().facts
        assert facts["writing_delivery_operation_count"] == 1
        assert facts["writing_delivery_completed_count"] == 1
        assert facts["writing_delivery_reconciliation_count"] == 0
        assert runtime.writing.process_delivery_once() is False
    finally:
        runtime.close()


def test_delivery_scheduler_selects_without_hydrating_completed_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path / "writing-delivery-lightweight-selector")
    try:
        report = _completed_report(runtime)
        run_ref = report["run"]["run_ref"]
        target_root = tmp_path / "lightweight-selector-external"
        target_root.mkdir(mode=0o700)

        def confirm_delivery(name: str) -> dict[str, object]:
            drafted = runtime.writing.create_delivery_intent(
                run_ref,
                action="publish",
                provider_ref="local-filesystem",
                target={
                    "path": str(target_root / f"{name}.md"),
                    "permissions": 0o600,
                    "expected_existing_hash": None,
                },
                output_format="markdown",
                idempotency_key=f"delivery-selector-{name}-draft",
            )
            previewed = runtime.writing.preview_delivery_intent(
                drafted["intent_id"],
                idempotency_key=f"delivery-selector-{name}-preview",
            )
            runtime.writing.confirm_delivery_intent(
                drafted["intent_id"],
                draft_revision=previewed["draft_revision"],
                draft_hash=previewed["draft_hash"],
                preview_ref=previewed["impact_preview"]["preview_ref"],
                preview_hash=previewed["impact_preview"]["preview_hash"],
                idempotency_key=f"delivery-selector-{name}-confirm",
            )
            return drafted

        completed = confirm_delivery("completed")
        completed_ref = completed["payload"]["operation_ref"]
        assert runtime.writing.process_delivery_once(
            expected_operation_ref=completed_ref
        )
        pending = confirm_delivery("pending")
        pending_ref = pending["payload"]["operation_ref"]

        authority = runtime.owners.agent_runtime.writing_delivery

        def fail_if_history_is_hydrated(*_args, **_kwargs):
            raise AssertionError("delivery scheduler hydrated operation history")

        monkeypatch.setattr(authority, "query_operations", fail_if_history_is_hydrated)
        monkeypatch.setattr(authority, "query_operation", fail_if_history_is_hydrated)

        assert runtime.writing.next_runnable_delivery_operation_ref() == pending_ref
    finally:
        runtime.close()


def test_delivery_confirmation_fails_closed_when_target_changes_after_preview(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-delivery-stale")
    try:
        report = _completed_report(runtime)
        target = tmp_path / "stale-external" / "report.md"
        target.parent.mkdir(mode=0o700)
        drafted = runtime.writing.create_delivery_intent(
            report["run"]["run_ref"],
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-stale-draft",
        )
        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"], idempotency_key="delivery-stale-preview"
        )
        target.write_bytes(b"somebody else created this target\n")
        target.chmod(0o600)

        with pytest.raises(
            OwnerConflict, match="writing_delivery_target_already_exists"
        ):
            runtime.writing.confirm_delivery_intent(
                drafted["intent_id"],
                draft_revision=previewed["draft_revision"],
                draft_hash=previewed["draft_hash"],
                preview_ref=previewed["impact_preview"]["preview_ref"],
                preview_hash=previewed["impact_preview"]["preview_hash"],
                idempotency_key="delivery-stale-confirm",
            )

        stale = runtime.writing.query_delivery_intent(drafted["intent_id"])
        assert stale["impact_preview"]["status"] == "stale"
        assert target.read_bytes() == b"somebody else created this target\n"
        assert runtime.owners.agent_runtime.writing_delivery.query_operations(
            report["run"]["run_ref"]
        ) == ()
    finally:
        runtime.close()


def test_delivery_confirmation_stales_when_the_exact_parent_directory_is_replaced(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-delivery-parent-identity-stale")
    try:
        report = _completed_report(runtime)
        target_parent = tmp_path / "parent-identity-external"
        target_parent.mkdir(mode=0o700)
        target = target_parent / "report.md"
        drafted = runtime.writing.create_delivery_intent(
            report["run"]["run_ref"],
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-parent-identity-draft",
        )
        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"],
            idempotency_key="delivery-parent-identity-preview",
        )
        moved_parent = tmp_path / "parent-identity-original"
        target_parent.rename(moved_parent)
        target_parent.mkdir(mode=0o700)

        with pytest.raises(OwnerConflict, match="writing_delivery_target_stale"):
            runtime.writing.confirm_delivery_intent(
                drafted["intent_id"],
                draft_revision=previewed["draft_revision"],
                draft_hash=previewed["draft_hash"],
                preview_ref=previewed["impact_preview"]["preview_ref"],
                preview_hash=previewed["impact_preview"]["preview_hash"],
                idempotency_key="delivery-parent-identity-confirm",
            )

        stale = runtime.writing.query_delivery_intent(drafted["intent_id"])
        assert stale["impact_preview"]["status"] == "stale"
        assert stale["operation"] is None
        assert not target.exists()
        assert not (moved_parent / target.name).exists()
    finally:
        runtime.close()


def test_delivery_confirmation_stales_when_an_exact_ancestor_directory_is_replaced(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-delivery-ancestor-identity-stale")
    try:
        report = _completed_report(runtime)
        trusted_root = tmp_path / "ancestor-identity-external"
        target_parent = trusted_root / "accepted"
        target_parent.mkdir(parents=True, mode=0o700)
        target = target_parent / "report.md"
        drafted = runtime.writing.create_delivery_intent(
            report["run"]["run_ref"],
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-ancestor-identity-draft",
        )
        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"],
            idempotency_key="delivery-ancestor-identity-preview",
        )
        moved_root = tmp_path / "ancestor-identity-original"
        trusted_root.rename(moved_root)
        target_parent.mkdir(parents=True, mode=0o700)

        with pytest.raises(OwnerConflict, match="writing_delivery_target_stale"):
            runtime.writing.confirm_delivery_intent(
                drafted["intent_id"],
                draft_revision=previewed["draft_revision"],
                draft_hash=previewed["draft_hash"],
                preview_ref=previewed["impact_preview"]["preview_ref"],
                preview_hash=previewed["impact_preview"]["preview_hash"],
                idempotency_key="delivery-ancestor-identity-confirm",
            )

        stale = runtime.writing.query_delivery_intent(drafted["intent_id"])
        assert stale["impact_preview"]["status"] == "stale"
        assert stale["operation"] is None
        assert not target.exists()
        assert not (moved_root / "accepted" / target.name).exists()
    finally:
        runtime.close()


def test_confirmed_delivery_rejects_a_real_parent_swap_in_the_execution_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_parent = tmp_path / "execution-window-external"
    target_parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "execution-window-original"
    provider = _SwapParentInExecutionWindowProvider(target_parent, moved_parent)
    runtime = _runtime_with_delivery_provider(
        tmp_path / "writing-delivery-execution-window",
        provider,
    )
    try:
        report = _completed_report(runtime)
        target = target_parent / "report.md"
        drafted = runtime.writing.create_delivery_intent(
            report["run"]["run_ref"],
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-execution-window-draft",
        )
        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"],
            idempotency_key="delivery-execution-window-preview",
        )
        runtime.writing.confirm_delivery_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=previewed["impact_preview"]["preview_ref"],
            preview_hash=previewed["impact_preview"]["preview_hash"],
            idempotency_key="delivery-execution-window-confirm",
        )

        assert runtime.writing.process_delivery_once() is True

        operation = runtime.writing.query_delivery_operation(
            drafted["payload"]["operation_ref"]
        )
        assert provider.swapped is True
        assert operation["status"] == "partial"
        assert operation["failure"] == {"code": "writing_delivery_target_stale"}
        assert not target.exists()
        assert not (moved_parent / target.name).exists()

        monkeypatch.setattr(
            writing_module.time,
            "time",
            lambda: operation["updated_at"] + 2.0,
        )
        assert runtime.writing.process_delivery_once() is False
        assert not target.exists()
        assert not (moved_parent / target.name).exists()
    finally:
        runtime.close()


def test_delivery_confirmation_caller_conflict_keeps_a_current_preview(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-delivery-current-preview")
    try:
        report = _completed_report(runtime)
        target = tmp_path / "current-preview-external" / "report.md"
        target.parent.mkdir(mode=0o700)
        drafted = runtime.writing.create_delivery_intent(
            report["run"]["run_ref"],
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-current-draft",
        )
        preview_key = "delivery-current-preview"
        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"], idempotency_key=preview_key
        )

        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            runtime.writing.confirm_delivery_intent(
                drafted["intent_id"],
                draft_revision=previewed["draft_revision"],
                draft_hash=previewed["draft_hash"],
                preview_ref=previewed["impact_preview"]["preview_ref"],
                preview_hash=previewed["impact_preview"]["preview_hash"],
                idempotency_key=preview_key,
            )

        current = runtime.writing.query_delivery_intent(drafted["intent_id"])
        assert current["impact_preview"]["status"] == "current"
        assert target.exists() is False
    finally:
        runtime.close()


def test_partial_delivery_backoff_allows_a_later_delivery_to_run(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-delivery-partial-fairness")
    try:
        report = _completed_report(runtime)
        run_ref = report["run"]["run_ref"]
        target_root = tmp_path / "partial-fairness-external"
        target_root.mkdir(mode=0o700)

        first_target = target_root / "stale.md"
        first = runtime.writing.create_delivery_intent(
            run_ref,
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(first_target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-partial-first-draft",
        )
        first_preview = runtime.writing.preview_delivery_intent(
            first["intent_id"], idempotency_key="delivery-partial-first-preview"
        )
        runtime.writing.confirm_delivery_intent(
            first["intent_id"],
            draft_revision=first_preview["draft_revision"],
            draft_hash=first_preview["draft_hash"],
            preview_ref=first_preview["impact_preview"]["preview_ref"],
            preview_hash=first_preview["impact_preview"]["preview_hash"],
            idempotency_key="delivery-partial-first-confirm",
        )
        first_target.write_bytes(b"unrelated user content\n")
        first_target.chmod(0o600)
        assert runtime.writing.process_delivery_once() is True
        assert runtime.writing.query_delivery_operation(
            first["payload"]["operation_ref"]
        )["status"] == "partial"

        second_target = target_root / "later.md"
        second = runtime.writing.create_delivery_intent(
            run_ref,
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(second_target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-partial-second-draft",
        )
        second_preview = runtime.writing.preview_delivery_intent(
            second["intent_id"], idempotency_key="delivery-partial-second-preview"
        )
        runtime.writing.confirm_delivery_intent(
            second["intent_id"],
            draft_revision=second_preview["draft_revision"],
            draft_hash=second_preview["draft_hash"],
            preview_ref=second_preview["impact_preview"]["preview_ref"],
            preview_hash=second_preview["impact_preview"]["preview_hash"],
            idempotency_key="delivery-partial-second-confirm",
        )

        assert runtime.writing.process_delivery_once() is True
        completed = runtime.writing.query_delivery_operation(
            second["payload"]["operation_ref"]
        )
        assert completed["status"] == "completed"
        assert second_target.exists()
        assert first_target.read_bytes() == b"unrelated user content\n"
    finally:
        runtime.close()


def test_outcome_unknown_backoff_survives_restart_and_allows_later_delivery(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "writing-delivery-unknown-fairness"
    target_root = tmp_path / "unknown-fairness-external"
    target_root.mkdir(mode=0o700)
    first_provider = _PersistentUnknownLocalProvider()
    runtime = _runtime_with_delivery_provider(data_path, first_provider)
    try:
        report = _completed_report(runtime)
        run_ref = report["run"]["run_ref"]
        unknown_target = target_root / "unknown.md"
        first = runtime.writing.create_delivery_intent(
            run_ref,
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(unknown_target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-unknown-first-draft",
        )
        first_preview = runtime.writing.preview_delivery_intent(
            first["intent_id"], idempotency_key="delivery-unknown-first-preview"
        )
        runtime.writing.confirm_delivery_intent(
            first["intent_id"],
            draft_revision=first_preview["draft_revision"],
            draft_hash=first_preview["draft_hash"],
            preview_ref=first_preview["impact_preview"]["preview_ref"],
            preview_hash=first_preview["impact_preview"]["preview_hash"],
            idempotency_key="delivery-unknown-first-confirm",
        )
        assert runtime.writing.process_delivery_once() is True
        assert runtime.writing.query_delivery_operation(
            first["payload"]["operation_ref"]
        )["status"] == "outcome_unknown"
    finally:
        runtime.close()

    restarted_provider = _PersistentUnknownLocalProvider()
    restarted = _runtime_with_delivery_provider(data_path, restarted_provider)
    try:
        later_target = target_root / "later.md"
        second = restarted.writing.create_delivery_intent(
            run_ref,
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(later_target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-unknown-second-draft",
        )
        second_preview = restarted.writing.preview_delivery_intent(
            second["intent_id"], idempotency_key="delivery-unknown-second-preview"
        )
        restarted.writing.confirm_delivery_intent(
            second["intent_id"],
            draft_revision=second_preview["draft_revision"],
            draft_hash=second_preview["draft_hash"],
            preview_ref=second_preview["impact_preview"]["preview_ref"],
            preview_hash=second_preview["impact_preview"]["preview_hash"],
            idempotency_key="delivery-unknown-second-confirm",
        )

        assert restarted.writing.process_delivery_once() is True
        assert restarted.writing.query_delivery_operation(
            second["payload"]["operation_ref"]
        )["status"] == "completed"
        assert later_target.exists()
        assert restarted_provider.reconcile_calls == []
    finally:
        restarted.close()


def test_unavailable_provider_is_rejected_before_renderer_asset_creation(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-delivery-provider-unavailable")
    try:
        report = _completed_report(runtime)
        inventory_before = tuple(
            item.version_ref
            for item in runtime.owners.research_memory.query_asset_inventory()
        )

        with pytest.raises(
            OwnerConflict, match="writing_delivery_provider_unavailable"
        ):
            runtime.writing.create_delivery_intent(
                report["run"]["run_ref"],
                action="send",
                provider_ref="mail-provider-not-installed",
                target={
                    "target_ref": "recipient:research-lead",
                    "permissions": ["send"],
                    "expected_existing_hash": None,
                },
                output_format="markdown",
                idempotency_key="delivery-provider-unavailable",
            )

        assert tuple(
            item.version_ref
            for item in runtime.owners.research_memory.query_asset_inventory()
        ) == inventory_before
    finally:
        runtime.close()


def test_delivery_draft_and_preview_recover_through_public_run_after_restart(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "writing-delivery-intent-restart"
    target = tmp_path / "intent-restart-external" / "report.md"
    target.parent.mkdir(mode=0o700)
    runtime = _runtime(data_path)
    try:
        report = _completed_report(runtime)
        run_ref = report["run"]["run_ref"]
        drafted = runtime.writing.create_delivery_intent(
            run_ref,
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-intent-restart-draft",
        )
        intent_id = drafted["intent_id"]
    finally:
        runtime.close()

    runtime = _runtime(data_path)
    try:
        recovered_draft = runtime.writing.query_writing_report(run_ref)[
            "deliveries"
        ][0]
        assert recovered_draft["intent_id"] == intent_id
        assert recovered_draft["confirmation_status"] == "draft"
        assert recovered_draft["status"] == "not_attempted"
        previewed = runtime.writing.preview_delivery_intent(
            intent_id,
            idempotency_key="delivery-intent-restart-preview",
        )
        preview_ref = previewed["impact_preview"]["preview_ref"]
    finally:
        runtime.close()

    runtime = _runtime(data_path)
    try:
        recovered_preview = runtime.writing.query_writing_report(run_ref)[
            "deliveries"
        ][0]
        assert recovered_preview["confirmation_status"] == "previewed"
        assert recovered_preview["impact_preview"]["preview_ref"] == preview_ref
        confirmed = runtime.writing.confirm_delivery_intent(
            intent_id,
            draft_revision=recovered_preview["draft_revision"],
            draft_hash=recovered_preview["draft_hash"],
            preview_ref=preview_ref,
            preview_hash=recovered_preview["impact_preview"]["preview_hash"],
            idempotency_key="delivery-intent-restart-confirm",
        )
        assert confirmed["confirmation_status"] == "confirmed"
        assert confirmed["status"] == "not_attempted"
        assert target.exists() is False
    finally:
        runtime.close()


def test_delivery_preview_fails_closed_on_renderer_custody_loss_and_recovers_exactly(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-delivery-custody")
    try:
        report = _completed_report(runtime)
        run_ref = report["run"]["run_ref"]
        target = tmp_path / "custody-external" / "report.md"
        target.parent.mkdir(mode=0o700)
        drafted = runtime.writing.create_delivery_intent(
            run_ref,
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-custody-draft",
        )
        inventory_before = tuple(
            (item.version_ref, item.receipt)
            for item in runtime.owners.research_memory.query_asset_inventory()
        )
        citations_before = runtime.owners.research_graph.query_writing_citation_history(
            run_ref
        )
        content_hash = drafted["payload"]["renderer_content_hash"]
        managed_object = (
            runtime.data_root.objects / "assets" / content_hash[:2] / content_hash
        )
        exact_bytes = managed_object.read_bytes()
        managed_object.unlink()

        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            runtime.writing.preview_delivery_intent(
                drafted["intent_id"], idempotency_key="delivery-custody-preview"
            )
        assert target.exists() is False
        assert runtime.owners.research_graph.query_writing_citation_history(
            run_ref
        ) == citations_before

        managed_object.write_bytes(exact_bytes)
        recovered = runtime.writing.preview_delivery_intent(
            drafted["intent_id"], idempotency_key="delivery-custody-preview-recovered"
        )

        assert recovered["impact_preview"]["status"] == "current"
        assert tuple(
            (item.version_ref, item.receipt)
            for item in runtime.owners.research_memory.query_asset_inventory()
        ) == inventory_before
        assert runtime.owners.research_graph.query_writing_citation_history(
            run_ref
        ) == citations_before
    finally:
        runtime.close()


def test_confirmed_delivery_keeps_its_exact_version_when_a_successor_starts(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-delivery-successor")
    try:
        report = _completed_report(runtime)
        run_ref = report["run"]["run_ref"]
        version_ref = report["deliverable"]["version_ref"]
        accepted_bytes = runtime.writing.render_report(
            run_ref,
            version_ref=version_ref,
            format="markdown",
        )["content"]
        target = tmp_path / "successor-external" / "accepted-v1.md"
        target.parent.mkdir(mode=0o700)
        drafted = runtime.writing.create_delivery_intent(
            run_ref,
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-successor-draft",
        )
        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"], idempotency_key="delivery-successor-preview"
        )
        runtime.writing.confirm_delivery_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=previewed["impact_preview"]["preview_ref"],
            preview_hash=previewed["impact_preview"]["preview_hash"],
            idempotency_key="delivery-successor-confirm",
        )

        runtime.writing.request_revision(
            run_ref,
            feedback=("Prepare a future revision without changing the confirmed v1 effect.",),
            idempotency_key="delivery-successor-revision",
        )
        assert runtime.writing.query_writing_report(run_ref)["run"][
            "attempt_generation"
        ] == 2

        assert runtime.writing.process_delivery_once() is True
        completed = runtime.writing.query_delivery_operation(
            drafted["payload"]["operation_ref"]
        )
        assert completed["status"] == "completed"
        assert completed["payload"]["version_ref"] == version_ref
        assert target.read_bytes() == accepted_bytes
    finally:
        runtime.close()


def test_confirmed_delivery_materializes_frozen_renderer_after_renderer_upgrade(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "writing-delivery-renderer-upgrade"
    target = tmp_path / "renderer-upgrade-external" / "accepted-v1.md"
    target.parent.mkdir(mode=0o700)
    runtime = _runtime_with_delivery_provider(data_path, _CountingLocalProvider())
    try:
        report = _completed_report(runtime)
        drafted = runtime.writing.create_delivery_intent(
            report["run"]["run_ref"],
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-renderer-upgrade-draft",
        )
        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"],
            idempotency_key="delivery-renderer-upgrade-preview",
        )
        runtime.writing.confirm_delivery_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=previewed["impact_preview"]["preview_ref"],
            preview_hash=previewed["impact_preview"]["preview_hash"],
            idempotency_key="delivery-renderer-upgrade-confirm",
        )
        frozen_bytes = runtime.owners.research_memory.materialize_asset(
            drafted["payload"]["renderer_version_ref"]
        ).content
    finally:
        runtime.close()

    provider = _CountingLocalProvider()
    restarted = _runtime_with_delivery_provider(
        data_path,
        provider,
        renderer_registry=WritingRendererRegistry((_UpgradedMarkdownRenderer(),)),
    )
    try:
        assert restarted.writing.process_delivery_once() is True
        completed = restarted.writing.query_delivery_operation(
            drafted["payload"]["operation_ref"]
        )
        assert completed["status"] == "completed"
        assert provider.execute_calls == 1
        assert target.read_bytes() == frozen_bytes
        assert not target.read_bytes().startswith(b"<!-- renderer-v2 -->")
    finally:
        restarted.close()


def test_ack_loss_restarts_into_reconciliation_without_a_second_external_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_path = tmp_path / "writing-delivery-restart"
    first_provider = _LocalAckLostOnceProvider()
    runtime = _runtime_with_delivery_provider(data_path, first_provider)
    target = tmp_path / "restart-external" / "report.md"
    target.parent.mkdir(mode=0o700)
    try:
        report = _completed_report(runtime)
        drafted = runtime.writing.create_delivery_intent(
            report["run"]["run_ref"],
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-restart-draft",
        )
        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"], idempotency_key="delivery-restart-preview"
        )
        runtime.writing.confirm_delivery_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=previewed["impact_preview"]["preview_ref"],
            preview_hash=previewed["impact_preview"]["preview_hash"],
            idempotency_key="delivery-restart-confirm",
        )

        assert runtime.writing.process_delivery_once() is True
        unknown = runtime.writing.query_delivery_operation(
            drafted["payload"]["operation_ref"]
        )
        delivered_bytes = target.read_bytes()
        delivered_mtime = target.stat().st_mtime_ns
        assert unknown["status"] == "outcome_unknown"
        assert first_provider.execute_calls == 1
        renderer_hash = drafted["payload"]["renderer_content_hash"]
        renderer_object = (
            runtime.data_root.objects
            / "assets"
            / renderer_hash[:2]
            / renderer_hash
        )
        renderer_object.unlink()
    finally:
        runtime.close()

    restarted_provider = _CountingLocalProvider()
    restarted = _runtime_with_delivery_provider(data_path, restarted_provider)
    try:
        recovered = restarted.writing.query_delivery_operation(
            drafted["payload"]["operation_ref"]
        )
        assert recovered["status"] == "outcome_unknown"

        monkeypatch.setattr(
            writing_module, "_WRITING_DELIVERY_RETRY_BACKOFF_SECONDS", 0.0
        )
        assert restarted.writing.process_delivery_once() is True
        completed = restarted.writing.query_delivery_operation(
            drafted["payload"]["operation_ref"]
        )

        assert completed["status"] == "completed"
        assert completed["reconciliation_receipt"]["issuer"] == "agent_runtime"
        assert restarted_provider.reconcile_calls == 1
        assert restarted_provider.execute_calls == 0
        assert target.read_bytes() == delivered_bytes
        assert target.stat().st_mtime_ns == delivered_mtime
        renderer_inventory = (
            restarted.owners.research_memory.query_asset_inventory_item(
                drafted["payload"]["renderer_version_ref"]
            )
        )
        assert renderer_inventory is not None
        assert renderer_inventory.availability == "unavailable"
    finally:
        restarted.close()


def test_ack_loss_reconcile_rejects_same_bytes_under_a_replaced_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_path = tmp_path / "writing-delivery-reconcile-parent-stale"
    target_parent = tmp_path / "reconcile-parent"
    target_parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "reconcile-parent-moved"
    target = target_parent / "report.md"
    first_provider = _LocalAckLostOnceProvider()
    runtime = _runtime_with_delivery_provider(data_path, first_provider)
    try:
        report = _completed_report(runtime)
        drafted = runtime.writing.create_delivery_intent(
            report["run"]["run_ref"],
            action="publish",
            provider_ref="local-filesystem",
            target={
                "path": str(target),
                "permissions": 0o600,
                "expected_existing_hash": None,
            },
            output_format="markdown",
            idempotency_key="delivery-reconcile-parent-stale-draft",
        )
        previewed = runtime.writing.preview_delivery_intent(
            drafted["intent_id"],
            idempotency_key="delivery-reconcile-parent-stale-preview",
        )
        runtime.writing.confirm_delivery_intent(
            drafted["intent_id"],
            draft_revision=previewed["draft_revision"],
            draft_hash=previewed["draft_hash"],
            preview_ref=previewed["impact_preview"]["preview_ref"],
            preview_hash=previewed["impact_preview"]["preview_hash"],
            idempotency_key="delivery-reconcile-parent-stale-confirm",
        )
        assert runtime.writing.process_delivery_once() is True
        unknown = runtime.writing.query_delivery_operation(
            drafted["payload"]["operation_ref"]
        )
        exact_bytes = target.read_bytes()
        assert unknown["status"] == "outcome_unknown"
        assert first_provider.execute_calls == 1
    finally:
        runtime.close()

    target_parent.rename(moved_parent)
    target_parent.mkdir(mode=0o700)
    target.write_bytes(exact_bytes)
    target.chmod(0o600)
    replacement_mtime = target.stat().st_mtime_ns
    original_target = moved_parent / target.name
    original_mtime = original_target.stat().st_mtime_ns

    restarted_provider = _CountingLocalProvider()
    restarted = _runtime_with_delivery_provider(data_path, restarted_provider)
    try:
        monkeypatch.setattr(
            writing_module,
            "_WRITING_DELIVERY_RETRY_BACKOFF_SECONDS",
            0.0,
        )
        assert restarted.writing.process_delivery_once() is True
        reconciled = restarted.writing.query_delivery_operation(
            drafted["payload"]["operation_ref"]
        )

        assert reconciled["status"] == "partial"
        assert reconciled["failure"] == {
            "code": "writing_delivery_target_stale"
        }
        assert restarted_provider.reconcile_calls == 1
        assert restarted_provider.execute_calls == 0
        assert target.read_bytes() == exact_bytes
        assert target.stat().st_mtime_ns == replacement_mtime
        assert original_target.read_bytes() == exact_bytes
        assert original_target.stat().st_mtime_ns == original_mtime
    finally:
        restarted.close()
