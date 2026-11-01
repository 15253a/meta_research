import json
import pytest

from meta_research.root_capabilities import root_capability_profile


@pytest.mark.parametrize("language", ["zh", "en"])
def test_acquisition_harness_guidance_keeps_download_responsibility_narrow(language):
    argv = root_capability_profile("acquisition").codex_arguments(output_language=language)
    instructions = json.loads(next(arg.split("=", 1)[1] for arg in argv
                                   if arg.startswith("developer_instructions=")))
    assert "逐篇" in instructions
    assert "HumanRequest" in instructions
    assert "凭据" in instructions
    assert f"本回合输出语言：{language}" in instructions
    assert "references/human-guidance.md" in instructions
    assert "references/materials-and-custody.md" in instructions
    assert "Question" not in instructions
    assert "Cycle" not in instructions
    assert "research-navigation.md" not in instructions
