"""Role guidance remains readable at the adapter's real provider boundary."""
from dataclasses import replace
from pathlib import Path
import re

from test_public_guidance_delivery import _developer
from test_plan_skill_adapter import _fake_codex, _request, _plan, _SequenceRunner
from meta_research.plan_skill import CodexPlanSkillAdapter


class RoleReadingRunner:
    def __init__(self, delegate, role, references):
        self.delegate = delegate
        self.role = role
        self.references = references
        self.observations = []

    def __call__(self, argv, prompt, timeout, environment=None):
        developer = _developer(argv)
        match = re.search(r"^Role skill source: (.+)$", developer + "\n" + prompt, re.MULTILINE)
        assert match
        source = Path(match.group(1))
        assert source.parent.name == self.role and source.is_absolute()
        cwd = Path(argv[argv.index("--cd") + 1])
        assert cwd.is_dir()
        documents = {name: (source.parent / name).read_text(encoding="utf-8") for name in self.references}
        for name, body in documents.items():
            assert body and body not in prompt and body not in developer, name
            for target in re.findall(r"\]\(([^)]+)\)", body):
                relative = target.split("#", 1)[0]
                if relative and "://" not in relative:
                    assert (source.parent / name).parent.joinpath(relative).is_file(), target
        self.observations.append((source, documents, developer, prompt))
        return self.delegate(argv, prompt, timeout, environment)


def test_plan_reads_role_contracts_on_demand_and_preserves_revision_input(tmp_path):
    runner = RoleReadingRunner(_SequenceRunner([{"plan": _plan()}, {"plan": _plan("Revised gap rationale")}]),
        "plan_stage", ("references/contract.md", "references/owner-operations.md", "references/tool-contracts.md"))
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    request = _request(runtime_binding=adapter.runtime_binding())
    first = adapter.generate_draft(request)
    revised = replace(request, native_session_ref=first.primary_session_ref,
        submission_revision=2, predecessor_submission_ref="plan-submission:1",
        owner_rejection_receipt_ref="receipt:rejection:1", owner_rejection_kind="domain",
        owner_feedback=("Explain why the remaining gap is not arranged this Cycle.",))
    result = adapter.generate_draft(revised)
    assert result.primary_session_ref == first.primary_session_ref
    assert len(runner.observations) == 2
    assert runner.observations[0][:2] == runner.observations[1][:2]
    assert "Explain why the remaining gap is not arranged this Cycle." in runner.observations[1][3]
    assert "哪种约束保留稀有形态？" in runner.observations[0][3]
    assert "比较拓扑一致性与像素重建。" in runner.observations[0][3]


def test_idea_delivers_method_without_forcing_optional_checklist(tmp_path):
    from meta_research.idea_skill import CodexIdeaSkillAdapter
    from test_idea_skill_contract import _request as idea_request, _idea_set, _SequenceRunner as IdeaRunner
    outcome = _idea_set()
    outcome["notes"] = "保留当前证据边界，交 Plan 判断具体投入。"
    for candidate in outcome["candidates"]:
        for key in ("assumptions", "risks", "falsification_hint", "material_difference"):
            candidate.pop(key, None)
        candidate["notes"] = "现有观察尚不能区分两种解释；新比较将改变本轮选择。"
    delegate = IdeaRunner([{"outcome": outcome}])
    runner = RoleReadingRunner(delegate, "idea_stage", ("references/io-contract.md", "references/contract.md"))
    adapter = CodexIdeaSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    result = adapter.generate_draft(idea_request(runtime_binding=adapter.runtime_binding()))
    assert result.draft == outcome
    schema = delegate.calls[0][2]
    # Provider output transport must allow the same focused candidate as the domain contract.
    from jsonschema import Draft202012Validator
    Draft202012Validator(schema).validate({"outcome": outcome})
