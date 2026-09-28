import pytest

from test_research_datasets import _asset, _dataset, _quest
from test_public_research_asset_roles import _runtime


@pytest.fixture
def runtime(tmp_path):
    value = _runtime(tmp_path / "dataset-version-search")
    yield value
    value.close()


def referenced_version(runtime, dataset, question, key, **fields):
    graph = runtime.owners.research_graph
    binding = _asset(runtime, content=key.encode(), key=key + "-asset")
    graph.accept_asset_role(binding=binding, role="evidence", quest_ref=question.quest_ref,
                            idempotency_key=key + "-origin")
    values = {"version_label": key, "meaning": "Recorded source qualification.",
              "notes": "", "metadata": {}}
    values.update(fields)
    version = graph.register_dataset_version(dataset_ref=dataset["dataset_ref"],
        asset_bindings=[binding], idempotency_key=key + "-version", **values)
    graph.reference_dataset(dataset_version_ref=version["dataset_version_ref"],
        question_ref=question.question_ref, idempotency_key=key + "-reference")
    return version


@pytest.mark.parametrize("field,value", [
    ("version_label", "2026 NDA-4143 qualification"),
    ("meaning", "Public NDA collection 4143 evidence."),
    ("notes", "NDA access remains conditional."),
    ("metadata", {"source_collection": "NDA-4143"}),
])
def test_version_keyword_returns_parent_dataset_card(runtime, field, value):
    graph = runtime.owners.research_graph
    question = _quest(runtime, "version-search")
    dataset = _dataset(graph)
    version = referenced_version(runtime, dataset, question, "qualification", **{field: value})
    page = graph.query_datasets(query="nda", quest_ref=question.quest_ref)
    assert page["kind"] == "register"
    assert page["items"] == [dataset]
    assert page["total"] == 1
    assert graph.query_datasets(dataset_ref=dataset["dataset_ref"], quest_ref=question.quest_ref)["items"] == [version]


def test_dataset_identity_keyword_still_matches(runtime):
    graph = runtime.owners.research_graph
    question = _quest(runtime, "identity-search")
    dataset = _dataset(graph)
    referenced_version(runtime, dataset, question, "ordinary-version")
    assert graph.query_datasets(query="Qualitative", quest_ref=question.quest_ref)["items"] == [dataset]
    assert graph.query_datasets(query="not-in-any-record", quest_ref=question.quest_ref)["items"] == []


def test_version_keyword_cannot_match_another_quests_version_of_visible_dataset(runtime):
    graph = runtime.owners.research_graph
    one, two = _quest(runtime, "visible"), _quest(runtime, "other")
    shared = _dataset(graph, "shared")
    referenced_version(runtime, shared, one, "local-ordinary")
    referenced_version(runtime, shared, two, "other-version", notes="NDA-only-in-other-quest")
    hidden = _dataset(graph, "hidden")
    referenced_version(runtime, hidden, two, "other-dataset", meaning="NDA other material")
    assert graph.query_datasets(quest_ref=one.quest_ref)["items"] == [shared]
    assert graph.query_datasets(query="NDA", quest_ref=one.quest_ref)["items"] == []
    assert graph.query_datasets(query="NDA", quest_ref=two.quest_ref)["items"] == [shared, hidden]
    assert graph.query_datasets(query="NDA")["items"] == [shared, hidden]


def test_matching_versions_do_not_duplicate_cards_or_change_pagination(runtime):
    graph = runtime.owners.research_graph
    question = _quest(runtime, "pagination")
    first, second = _dataset(graph, "first"), _dataset(graph, "second")
    referenced_version(runtime, first, question, "first-a", notes="NDA first")
    referenced_version(runtime, first, question, "first-b", notes="NDA later")
    referenced_version(runtime, second, question, "second-a", notes="NDA second")
    page = graph.query_datasets(query="NDA", quest_ref=question.quest_ref, offset=0, limit=1)
    assert page["items"] == [first]
    assert page["total"] == 2
    assert page["next_offset"] == 1
    page = graph.query_datasets(query="NDA", quest_ref=question.quest_ref, offset=1, limit=1)
    assert page["items"] == [second]
    assert page["total"] == 2
    assert page["next_offset"] is None
