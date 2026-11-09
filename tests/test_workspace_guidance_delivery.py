"""Installed role guidance agrees with paths delivered to a real Stage adapter.

The provider is a controlled reader, not a scientific model. Owner admission,
resource loading, workspace binding and prompt construction remain production.
"""
import json
from pathlib import Path
import re

from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.runtime_conditions import split_runtime_prompt
from test_cycle_role_delivery import _RoleReadingRunner
from test_plan_skill_adapter import _fake_codex
from test_public_guidance_delivery import _developer
from test_root_workspace import _make, _Writer, _confirm_direct_quest, _finish_idea_stage


class _DeliveryObserved(BaseException):
    """Stop at the provider boundary without claiming a scientific result."""


def test_plan_provider_reads_predecessor_using_paths_and_installed_custody_contract(tmp_path):
    observed = []

    def read_work(argv, prompt, timeout, environment=None):
        del timeout, environment
        conditions, science = split_runtime_prompt(prompt)
        entries, _ = json.JSONDecoder().raw_decode(conditions.split("Work directories: ", 1)[1])
        own, = [entry for entry in entries if entry["access"] == "read_write"]
        prior, = [entry for entry in entries if entry["root_kind"] == "idea"]
        assert own["root_kind"] == "plan" and prior["access"] == "read"
        assert own["cycle_ref"] == prior["cycle_ref"]
        assert own["quest_ref"] == prior["quest_ref"]
        cwd = Path(argv[argv.index("--cd") + 1])
        assert str(cwd) == own["directory"]
        source = Path(prior["directory"]) / "settling.txt"
        original = source.read_bytes()
        assert original == b"Allow 15 minutes before measuring.\n"
        (cwd / "read-predecessor.txt").write_bytes(original)
        assert source.read_bytes() == original
        developer = _developer(argv)
        root = Path(re.search(r"^Shared reference root: (.+)$", developer, re.M).group(1))
        custody = (root / "references" / "materials-and-custody.md").read_text(encoding="utf-8")
        observed.append((conditions, science, custody, entries))
        raise _DeliveryObserved()

    reading = _RoleReadingRunner(read_work, "plan_stage")
    adapter = CodexPlanSkillAdapter(tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")), process_runner=reading)
    runtime = _make(tmp_path / "runtime", idea=_Writer(), plan=adapter)
    try:
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(8):
            try:
                runtime.plan_stage.process_once()
            except _DeliveryObserved:
                break
        assert len(observed) == 1, runtime.plan_stage.query_current()
        conditions, science, custody, entries = observed[0]
        assert "current and previous two Cycles" in conditions
        assert "Unlisted paths are not granted" in conditions
        assert "do not imply absent research history" in conditions
        assert "Work directories" in custody and "read_write" in custody
        assert "accepted_question=" in science and "完整 IdeaSet=" in science
        assert "Work directories: " not in science
        assert all(entry["directory"] not in science for entry in entries)
        assert len(reading.role_observations) == 1
    finally:
        runtime.close()
