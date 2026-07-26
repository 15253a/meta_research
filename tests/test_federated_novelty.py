"""Contract tests for broad, replayable multi-source novelty retrieval."""
from __future__ import annotations

import json
import urllib.error

from orchestrator.federated_novelty import FederatedNoveltySearchProvider


POLICY_HASH = "sha256:" + "a" * 64
CONFIG = {
    "enabled": True,
    "status": "controlled_backend_enabled",
    "provider": "literature_federated_v1",
    "endpoints": {
        "crossref": "https://api.crossref.org/works",
        "openalex": "https://api.openalex.org/works",
    },
    "queries_per_candidate": 1,
    "max_results_per_query": 30,
    "timeout_s": 20,
    "max_response_bytes": 4 * 1024 * 1024,
    "min_interval_s": 0,
    "retry_attempts": 1,
    "retry_initial_delay_s": 0,
    "retry_max_delay_s": 0,
}


class _Response:
    status = 200
    headers = {}

    def __init__(self, raw):
        self.raw = raw

    def read(self, _limit):
        return self.raw

    def close(self):
        pass


def _work(tmp_path):
    work = tmp_path / "quest"
    work.mkdir(mode=0o700)
    return work


def test_federated_results_are_merged_deduped_frozen_and_replayed(tmp_path):
    calls = []
    crossref = {"message": {"items": [{
        "DOI": "10.1/shared", "title": ["Shared EEG paper"],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "published": {"date-parts": [[2024, 2, 3]]},
        "type": "journal-article",
    }, {
        "DOI": "10.1/second", "title": ["Random subspace EEG"],
        "published": {"date-parts": [[2023]]},
        "type": "proceedings-article",
    }]}}
    openalex = {"results": [{
        "id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/shared",
        "title": "Shared EEG paper", "publication_year": 2024,
        "authorships": [], "abstract_inverted_index": None,
    }, {
        "id": "https://openalex.org/W2", "doi": "https://doi.org/10.1/third",
        "title": "Cross-subject emotion recognition", "publication_year": 2022,
        "authorships": [], "abstract_inverted_index": None,
    }]}

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        payload = crossref if "crossref" in request.full_url else openalex
        return _Response(json.dumps(payload).encode())

    work = _work(tmp_path)
    provider = FederatedNoveltySearchProvider(CONFIG, work, opener=opener)
    first = provider.search(
        "DEAP cross subject EEG random subspace ensemble",
        policy_hash=POLICY_HASH)

    assert first["status"] == "complete"
    assert len(first["results"]) == 3
    assert {row["source_provider"] for row in first["results"]} == {
        "crossref", "openalex"}
    assert len(calls) == 2
    assert first["final_ref"]["provider"] == "literature_federated_v1"
    assert (work / first["final_ref"]["snapshot_ref"]).is_file()

    replay = FederatedNoveltySearchProvider(
        CONFIG, work,
        opener=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("replay must not use network")))
    assert replay.search(
        "DEAP cross subject EEG random subspace ensemble",
        policy_hash=POLICY_HASH) == first


def test_all_sources_unavailable_returns_pending_snapshot_not_exception(tmp_path):
    def unavailable(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "busy", {}, None)

    provider = FederatedNoveltySearchProvider(
        CONFIG, _work(tmp_path), opener=unavailable)
    result = provider.search(
        "DEAP cross subject EEG random subspace ensemble",
        policy_hash=POLICY_HASH)

    assert result["status"] == "unavailable"
    assert result["results"] == []
    assert result["final_ref"]["ranking"] == []
    assert result["final_ref"]["snapshot_hash"].startswith("sha256:")
