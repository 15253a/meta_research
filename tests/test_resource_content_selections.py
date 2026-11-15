"""Resource indexes preserve logical scope while retaining one verified RM parent."""
import shutil

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.research_content import read_content
from meta_research.resource_selections import content_selection_ref
from test_research_environments import _environment
from test_research_datasets import _dataset


@pytest.fixture
def runtime(tmp_path):
    from test_target_root_finalizer import _current_bundle_runtime
    runtime = _current_bundle_runtime(tmp_path / "resources")
    yield runtime
    runtime.close()


def _quest(runtime, _prefix):
    from test_public_plan_stage import _confirm_direct_quest
    from types import SimpleNamespace
    return SimpleNamespace(**_confirm_direct_quest(runtime))


def _parent(runtime, tmp_path):
    directory = tmp_path / "original"
    (directory / "one").mkdir(parents=True)
    (directory / "two").mkdir()
    (directory / "empty").mkdir()
    (directory / "one" / "source.py").write_text("first selected content", encoding="utf-8")
    (directory / "two" / "source.py").write_text("second selected content", encoding="utf-8")
    result = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
        source_kind="local_path", custody_mode="managed", display_name="implementation",
        source_locator=str(directory)), idempotency_key="parent")
    assert result.asset is not None
    return directory, result.asset.as_binding()


def _selection(binding, path):
    return {"asset_binding": binding.as_dict(), "path": path}


@pytest.mark.parametrize("path,kind", [("one", "directory"), ("one/source.py", "file"), ("empty", "directory")])
def test_environment_registers_reads_and_retains_one_selected_parent_after_workspace_deleted(runtime, tmp_path, path, kind):
    graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
    quest = _quest(runtime, "selection")
    directory, binding = _parent(runtime, tmp_path)
    graph.accept_asset_role(binding=binding, role="evidence", quest_ref=quest.quest_ref, idempotency_key="origin")
    selection = _selection(binding, path)
    environment = _environment(graph, quest_ref=quest.quest_ref, selections=[selection, selection])
    assert environment["selections"] == [selection]
    assert environment["asset_bindings"] == [binding.as_dict()]
    assert graph.query_environment(environment["environment_ref"]) == environment
    assert graph.resolve_resource_selection(content_selection_ref(selection), quest_ref=quest.quest_ref).as_dict() == selection
    shutil.rmtree(directory)
    page = read_content(graph, memory, quest_ref=quest.quest_ref,
        source_ref=environment["environment_ref"], version_ref=binding.version_ref)
    assert page["kind"] == kind
    assert page["selection"] == selection
    if path == "one":
        assert [entry["path"] for entry in page["entries"]] == ["source.py"]
        content = read_content(graph, memory, quest_ref=quest.quest_ref,
            source_ref=environment["environment_ref"], version_ref=binding.version_ref, entry_path="source.py")
        assert content["text"] == "first selected content"
    elif kind == "file":
        assert page["text"] == "first selected content"
    else:
        assert page["entries"] == []
    with runtime._database.read() as connection:
        assert connection.execute(text("SELECT count(*) FROM rm_asset_versions WHERE version_ref=:ref"),
                                  {"ref": binding.version_ref}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM rg_environment_assets WHERE environment_ref=:ref"),
                                  {"ref": environment["environment_ref"]}).scalar_one() == 1
    release = memory.assess_release_eligibility(binding.version_ref,
        expected_reference_revision=graph.query_asset_reference_revision(), idempotency_key="release")
    assert not release.eligible


def test_two_child_selections_have_distinct_exact_readers_and_no_implicit_parent(runtime, tmp_path):
    graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
    quest = _quest(runtime, "two-selections")
    _, binding = _parent(runtime, tmp_path)
    graph.accept_asset_role(binding=binding, role="evidence", quest_ref=quest.quest_ref, idempotency_key="origin")
    environment = _environment(graph, quest_ref=quest.quest_ref,
        selections=[_selection(binding, "one"), _selection(binding, "two")])
    page = read_content(graph, memory, quest_ref=quest.quest_ref,
        source_ref=environment["environment_ref"], version_ref=binding.version_ref)
    assert page["kind"] == "selections"
    assert len({item["selection_ref"] for item in page["items"]}) == 2
    for item, expected in zip(page["items"], ("first selected content", "second selected content")):
        content = read_content(graph, memory, quest_ref=quest.quest_ref, **item["reader"], entry_path="source.py")
        assert content["text"] == expected
    for kwargs, code in (({"entry_path": "source.py"}, "content_selection_required"),
                         ({"selection_path": "empty"}, "content_selection_unbound"),
                         ({"selection_path": None}, "content_selection_unbound")):
        with pytest.raises(OwnerConflict, match=code):
            read_content(graph, memory, quest_ref=quest.quest_ref,
                source_ref=environment["environment_ref"], version_ref=binding.version_ref, **kwargs)


def test_invalid_selection_creates_no_resource_or_dependency(runtime, tmp_path):
    graph = runtime.owners.research_graph
    _, binding = _parent(runtime, tmp_path)
    for path in ("missing", "one/../two", "/one"):
        with pytest.raises(OwnerConflict):
            _environment(graph, key=path, selections=[_selection(binding, path)])
    with runtime._database.read() as connection:
        assert connection.execute(text("SELECT count(*) FROM rg_environments")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM rg_environment_assets")).scalar_one() == 0


def test_dataset_and_environment_use_the_same_selected_content(runtime, tmp_path):
    graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
    quest = _quest(runtime, "shared-resource")
    _, binding = _parent(runtime, tmp_path)
    graph.accept_asset_role(binding=binding, role="evidence", quest_ref=quest.quest_ref, idempotency_key="origin")
    selections = [_selection(binding, "one")]
    version = graph.register_dataset_version(dataset_ref=_dataset(graph)["dataset_ref"],
        version_label="selected", meaning="The selected observations", selections=selections,
        idempotency_key="dataset-selection")
    graph.reference_dataset(dataset_version_ref=version["dataset_version_ref"],
        question_ref=quest.question_ref, idempotency_key="adopt")
    environment = _environment(graph, quest_ref=quest.quest_ref, selections=selections)
    assert version["asset_bindings"] == environment["asset_bindings"] == [binding.as_dict()]
    assert version["selection_readers"][0]["selection_ref"] == environment["selection_readers"][0]["selection_ref"]
    for source in (version["dataset_version_ref"], environment["environment_ref"]):
        result = read_content(graph, memory, quest_ref=quest.quest_ref, source_ref=source,
            version_ref=binding.version_ref, entry_path="source.py")
        assert result["text"] == "first selected content"


def test_whole_asset_registration_uses_the_same_selection_contract(runtime):
    from test_research_datasets import _asset
    graph = runtime.owners.research_graph
    binding = _asset(runtime)
    environment = _environment(graph, asset_bindings=[binding])
    assert environment["selections"] == [_selection(binding, None)]
    assert environment["selection_readers"][0]["reader"]["selection_path"] is None
    assert graph.query_environment(environment["environment_ref"]) == environment
