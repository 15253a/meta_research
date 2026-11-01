import json
from io import BytesIO

import pytest

from meta_research.companion_writing import WritingRequest, writing_message
from meta_research.writing_content import generate
from meta_research.owners.common import OwnerConflict
from meta_research.root_capabilities import root_operation_catalog
from meta_research.semantic_mcp import ROOT_AGENT_COMMON_OPERATION_IDS


def test_writing_preserves_free_requirements_and_uses_ordinary_assistant():
    task = WritingRequest(document_type="paper", include_presentation=True,
        task_scope="revision", requirements="保留负结果，只改讨论；PPT 也更新。",
        model={"kind": "configured_api", "config_ref": "my-model"}, output_directory="drafts/revision")
    message = writing_message(task, "只修改选定版本", None)
    assert "保留负结果，只改讨论；PPT 也更新。" in message
    assert '"include_presentation": true' in message
    assert '"task_scope": "revision"' in message
    assert '"config_ref": "my-model"' in message
    assert "writing-report/SKILL.md" in message
    assert "不向 Cycle 派工" in message


@pytest.mark.parametrize("directory", ["../escape", "/tmp/escape", "C:/outside", ".creation-context", "dir/../outside"])
def test_output_directory_stays_in_assistant_workspace(directory):
    with pytest.raises(ValueError):
        WritingRequest(requirements="draft", output_directory=directory)


def test_missing_selected_template_fails_before_queuing():
    class Memory:
        def query_asset_version(self, ref):
            return None
    task = WritingRequest(requirements="Use selected template", materials=[{
        "version_ref": "missing-version", "content_hash": "a" * 64, "role": "document_template"}])
    with pytest.raises(OwnerConflict, match="writing_material_version_unavailable"):
        writing_message(task, task.requirements, Memory())


def test_selected_model_receives_only_explicit_material_and_never_falls_back(tmp_path):
    config = tmp_path / "private.json"
    config.write_text(json.dumps({"selected": {"endpoint": "https://model.example/v1/chat/completions",
        "model": "chosen-model", "api_key_env": "PRIVATE_MODEL_KEY"}}))
    env = {"META_RESEARCH_CONTENT_MODELS": str(config), "PRIVATE_MODEL_KEY": "private-test-key"}
    calls = []
    def provider(request, timeout):
        calls.append(request)
        return BytesIO(json.dumps({"choices": [{"message": {"content": "Revised selected paragraph."}}]}).encode())
    request = {"messages": [{"role": "user", "content": "Only selected paragraph and evidence."}]}
    assert generate("selected", request, environment=env, opener=provider) == "Revised selected paragraph."
    payload = json.loads(calls[0].data)
    assert payload == {"model": "chosen-model", **request}
    assert "private-test-key" not in calls[0].data.decode()
    assert calls[0].get_header("Authorization") == "Bearer private-test-key"
    def failed(request, timeout):
        calls.append(request)
        raise OSError("provider unavailable")
    with pytest.raises(ValueError, match="content_model_call_failed"):
        generate("selected", request, environment=env, opener=failed)
    assert len(calls) == 2


def test_assistant_catalog_does_not_grant_research_writes():
    catalog = root_operation_catalog("companion", common_operation_ids=ROOT_AGENT_COMMON_OPERATION_IDS)
    assert "research_memory.content.read" in catalog
    assert "research_workspace.read" in catalog
    assert "research_graph.datasets.register" not in catalog
    assert "research_memory.assets.intake" not in catalog
    assert "research_memory.assets.retire" not in catalog
    assert "research_graph.artifact_roles.adjust" not in catalog
    assert "acquisition.request" not in catalog
