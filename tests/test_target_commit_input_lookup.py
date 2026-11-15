"""Input lookup verifies only the exact upstream Commits selected by the handle."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict
from test_target_root_frozen_inputs import _Graph, _fixture


def test_unrelated_corrupt_commit_cannot_block_selected_input(tmp_path, monkeypatch):
    handle, frozen, _, _, _, graph, finalizer = _fixture(tmp_path)
    lookups = []

    def query(quest_ref, *, target_commit_refs=None):
        assert quest_ref == "quest-frozen-inputs"
        lookups.append(target_commit_refs)
        selected = (frozen.target_commit_ref, "unrelated") if target_commit_refs is None else target_commit_refs
        if "unrelated" in selected:
            raise OwnerConflict("target_commit_invalid")
        return tuple(graph.commit for ref in selected if ref == frozen.target_commit_ref)

    monkeypatch.setattr(graph, "query_target_commits_for_quest", query)
    assert finalizer._resolve_target_commit_inputs(handle) == (frozen,)
    assert lookups == [(frozen.target_commit_ref,)]


@pytest.mark.parametrize("damage", ["corrupt", "missing", "other_quest", "receipt"])
def test_selected_commit_still_requires_available_exact_source(tmp_path, monkeypatch, damage):
    handle, frozen, _, _, _, graph, finalizer = _fixture(tmp_path)
    if damage == "receipt":
        graph.commit.receipt = replace(graph.commit.receipt, subject_ref="different-commit")

    def query(quest_ref, *, target_commit_refs=None):
        assert target_commit_refs == (frozen.target_commit_ref,)
        if damage == "corrupt":
            raise OwnerConflict("target_commit_invalid")
        source_quest = "other-quest" if damage == "other_quest" else "quest-frozen-inputs"
        return () if damage == "missing" or quest_ref != source_quest else (graph.commit,)

    monkeypatch.setattr(graph, "query_target_commits_for_quest", query)
    expected = "target_root_upstream_input_authority_invalid" if damage == "corrupt" else "target_root_upstream_input_invalid"
    with pytest.raises(OwnerConflict, match=expected):
        finalizer._resolve_target_commit_inputs(handle)


def test_selected_commit_batches_keep_original_input_order(tmp_path, monkeypatch):
    handle, template, workspace, _, memory, graph, finalizer = _fixture(tmp_path)
    frozen = tuple(replace(template,
        target_commit_ref=f"commit-{index:03d}", target_ref=f"target-{index:03d}",
        target_run_ref=f"run-{index:03d}", manifest_ref=f"manifest-{index:03d}")
        for index in range(258))
    sources = {item.target_commit_ref: _Graph(item) for item in frozen}
    transitions = {source.commit.target_ref: source.transition for source in sources.values()}
    by_ref = {item.target_commit_ref: item for item in frozen}
    manifests = {item.manifest_ref: SimpleNamespace(manifest_ref=item.manifest_ref) for item in frozen}
    required = tuple(reversed(tuple(by_ref))) + (frozen[0].target_commit_ref,)
    monkeypatch.setattr(workspace, "target_input_commit_sources", lambda _handle: required)
    batches = []

    def query(quest_ref, *, target_commit_refs=None):
        assert quest_ref == "quest-frozen-inputs"
        assert target_commit_refs is not None and len(target_commit_refs) <= 256
        batches.append(target_commit_refs)
        # Query row order must not replace the caller's selected input order.
        return tuple(sources[ref].commit for ref in sorted(target_commit_refs))

    monkeypatch.setattr(graph, "query_target_commits_for_quest", query)
    monkeypatch.setattr(graph, "query_target_frontier_commit_transition", transitions.get)
    monkeypatch.setattr(memory, "query", manifests.get)
    monkeypatch.setattr(memory, "materialize_target_commit_input",
                        lambda *, target_commit_ref, manifest: by_ref[target_commit_ref])
    assert finalizer._resolve_target_commit_inputs(handle) == tuple(by_ref[ref] for ref in required)
    assert [len(batch) for batch in batches] == [256, 2]
    assert tuple(ref for batch in batches for ref in batch) == required[:-1]
