from dataclasses import replace

import pytest

import meta_research.writing_skill as writing
from meta_research.writing_contract import WritingRuntimeBinding


@pytest.mark.parametrize("document_type", ["report", "paper", "presentation"])
@pytest.mark.parametrize("phase", ["primary", "review"])
def test_drifted_writing_binding_is_rejected_before_loading_or_execution(
    monkeypatch: pytest.MonkeyPatch, document_type: str, phase: str
) -> None:
    current = WritingRuntimeBinding(
        packaged_skill_bundle_hash="a" * 64,
        instruction_set_hash="b" * 64,
        model_ref="gpt-6-sol",
        harness_adapter_ref="test-current-harness",
        mcp_bindings=(),
        capability_bindings=(),
        resource_bindings=(),
    )
    current.validate()
    adapter = object.__new__(writing.CodexWritingSkillAdapter)
    monkeypatch.setattr(adapter, "runtime_binding", lambda _document_type: current)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("A mismatched binding must fail before legacy loading, staging or execution")

    monkeypatch.setattr(writing, "_load_legacy_report_module", forbidden, raising=False)
    monkeypatch.setattr(adapter, "_stage_source_materials", forbidden)
    monkeypatch.setattr(adapter, "_invoke_root_operation", forbidden)
    request = writing.WritingSkillRequest(
        run_ref="writing_run:test", attempt_ref="writing_attempt:test",
        fence_ref="writing_fence:test", intent={}, snapshot={},
        root_session_ref="writing_root:test", revision=1,
        runtime_binding=replace(current, packaged_skill_bundle_hash="c" * 64),
        native_session_ref="native:test", document_type=document_type,
        profile_ref=f"{document_type}-v1",
    )
    draft = writing.WritingSkillDraft(
        markdown="待审稿件", citations=(), primary_session_ref="native:test",
        adapter_kind="codex_cli",
    )
    with pytest.raises(writing.WritingSkillUnavailable) as raised:
        if phase == "primary":
            adapter.generate_draft(request)
        else:
            adapter.review_draft(request, draft)
    assert raised.value.code == "writing_runtime_binding_drift"
