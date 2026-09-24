from __future__ import annotations

import json
import tomllib

import pytest

from meta_research.root_capabilities import ROOT_AGENT_KINDS, ROOT_CAPABILITY_ENTRY_PATHS, root_capability_profile
from meta_research.system_prompt import RESEARCH_SYSTEM_PROMPT
from test_root_capability_floor import _invoke_root_without_tool_activity


@pytest.mark.parametrize("root_kind", ROOT_AGENT_KINDS)
@pytest.mark.parametrize("entry_path", ("initial", "resume", "recovery"))
def test_real_root_adapter_passes_human_request_instructions(tmp_path, root_kind, entry_path):
    _, argv = _invoke_root_without_tool_activity(tmp_path, root_kind=root_kind, entry_path=entry_path)
    settings = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--config"]
    instructions = [setting for setting in settings if setting.startswith("developer_instructions=")]
    assert len(instructions) == 1
    actual = tomllib.loads(instructions[0])["developer_instructions"]
    assert actual.startswith(RESEARCH_SYSTEM_PROMPT)
    language_instructions = actual[len(RESEARCH_SYSTEM_PROMPT):]
    assert "本回合输出语言：zh。" in language_instructions
    assert "直接使用中文" in language_instructions
    assert "委派时传递同一语言要求" in language_instructions
    assert "human_request.open.reconcile" in RESEARCH_SYSTEM_PROMPT


def test_all_lifecycle_paths_bind_the_same_instructions():
    for kind in ROOT_AGENT_KINDS:
        profile = root_capability_profile(kind)
        assert "research_system_prompt_hash" in profile.as_dict()
        assert len({profile.codex_arguments(entry_path=entry) for entry in ROOT_CAPABILITY_ENTRY_PATHS}) == 1
        assert "human_request" in json.loads(next(arg.split("=", 1)[1] for arg in profile.codex_arguments() if arg.startswith("developer_instructions=")))
