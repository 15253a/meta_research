"""The installed shared method is reachable at the real native provider boundary."""
from pathlib import Path
import re

import pytest

from meta_research.creation_basis import first_creation_instructions
from meta_research.research_guidance import shared_writing_resources


def _read_linked_files(source, seen=None):
    seen = set() if seen is None else seen
    if source in seen:
        return seen
    seen.add(source)
    body = source.read_text(encoding="utf-8")
    for link in re.findall(r"\]\(([^)]+)\)", body):
        relative = link.split("#", 1)[0]
        if relative and "://" not in relative:
            path = (source.parent / relative).resolve()
            assert path.is_file(), path
            _read_linked_files(path, seen)
    return seen


@pytest.mark.parametrize("root_kind", ("companion", "idea", "plan", "bundle", "target", "reasoning"))
@pytest.mark.parametrize("entry_path", ("initial", "resume"))
def test_provider_has_same_readable_writing_method_without_a_writing_session(tmp_path, root_kind, entry_path):
    from test_public_guidance_delivery import _developer
    from test_root_capability_floor import _invoke_root_without_tool_activity

    _, argv = _invoke_root_without_tool_activity(tmp_path, root_kind=root_kind, entry_path=entry_path)
    developer = _developer(argv)
    source = Path(re.search(r"^Shared writing skill source: (.+)$", developer, re.MULTILINE).group(1))
    resources = shared_writing_resources()
    assert source.is_absolute()
    assert source.read_text(encoding="utf-8") == resources["SKILL.md"]
    assert resources["SKILL.md"] not in developer
    loaded = _read_linked_files(source)
    for name, content in resources.items():
        assert (source.parent / name).read_text(encoding="utf-8") == content
        if name.startswith("references/"):
            assert source.parent / name in loaded
            assert content not in developer


def test_creation_main_method_and_branch_references_are_installed_and_not_flattened():
    instructions = first_creation_instructions()
    source = Path(instructions["paths"]["SKILL.md"])
    assert source.read_text(encoding="utf-8") in instructions["instructions"]
    loaded = _read_linked_files(source)
    for name, location in instructions["paths"].items():
        assert Path(location) in loaded
        if name.startswith("references/"):
            assert Path(location).read_text(encoding="utf-8") not in instructions["instructions"]
    contract = Path(instructions["paths"]["references/context-contract.md"]).read_text(encoding="utf-8")
    assert "manual_question_creation" in contract
    assert "confirmed Seed" in contract
    assert "not yet connected" not in contract


def test_missing_writing_reference_fails_instead_of_silently_selecting_old_copy(tmp_path, monkeypatch):
    import meta_research.research_guidance as guidance
    root = tmp_path / "skills"
    (root / "writing-report").mkdir(parents=True)
    (root / "writing-report" / "SKILL.md").write_text("# partial package", encoding="utf-8")
    monkeypatch.setattr(guidance, "shared_reference_root", lambda: root)
    with pytest.raises(FileNotFoundError):
        guidance.shared_writing_instructions("companion")
