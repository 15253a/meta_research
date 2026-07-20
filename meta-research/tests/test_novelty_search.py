"""Host-controlled, replayable arXiv novelty search boundary."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

import orchestrator.novelty_search as novelty_module
from orchestrator.novelty_search import (
    ArxivNoveltySearchProvider,
    NoveltySearchError,
    NoveltySearchProviderError,
)


POLICY_HASH = "sha256:" + "a" * 64
CONFIG = {
    "enabled": True,
    "status": "controlled_backend_enabled",
    "provider": "arxiv_api_v1",
    "endpoint": "https://export.arxiv.org/api/query",
    "queries_per_candidate": 1,
    "max_results_per_query": 2,
    "timeout_s": 20,
    "max_response_bytes": 4096,
    "min_interval_s": 0,
    # Most contract tests deliberately exercise one request.  Dedicated retry
    # tests below opt into the production-style recovery loop without sleeping.
    "retry_attempts": 1,
    "retry_initial_delay_s": 0,
    "retry_max_delay_s": 0,
}


def _entry(number: int = 1, *, entry_id: str | None = None,
           include_summary: bool = True) -> str:
    summary = (
        "<summary> First line.\n  Second line. </summary>"
        if include_summary else "")
    return f"""
  <entry>
    <id>{entry_id or f'http://arxiv.org/abs/2607.{number:05d}v1'}</id>
    <updated>2026-07-{number:02d}T12:30:00Z</updated>
    <published>2026-07-{number:02d}T10:00:00+00:00</published>
    <title>  Robust   EEG method {number}  </title>
    {summary}
    <author><name>Alice {number}</name></author>
    <author><name>张 三</name></author>
    <category term="cs.LG"/>
    <category term="eess.SP"/>
  </entry>"""


def _atom(*entries: str) -> bytes:
    body = "".join(entries or (_entry(),))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<title>arXiv Query: test</title>' + body + "</feed>"
    ).encode("utf-8")


class _Response:
    def __init__(self, url: str, raw: bytes, *, status: int = 200,
                 declared: int | str | None = None,
                 content_encoding: str | None = None):
        self._url = url
        self._raw = raw
        self.status = status
        self.closed = False
        self._offset = 0
        self.headers = {}
        if declared is not False:
            self.headers["Content-Length"] = str(
                len(raw) if declared is None else declared)
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding

    def geturl(self):
        return self._url

    def read(self, limit: int):
        block = self._raw[self._offset:self._offset + limit]
        self._offset += len(block)
        return block

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, raws=None, *, response_factory=None):
        self.raws = list(raws or [_atom()])
        self.response_factory = response_factory
        self.calls = []
        self.responses = []

    def __call__(self, request, timeout):  # noqa: ANN001
        self.calls.append((request, timeout))
        raw = self.raws.pop(0) if self.raws else _atom()
        response = (self.response_factory(request, raw)
                    if self.response_factory is not None
                    else _Response(request.full_url, raw))
        self.responses.append(response)
        return response


def _work(tmp_path: Path) -> Path:
    work = tmp_path / "quest"
    work.mkdir(mode=0o700)
    return work


def _provider(tmp_path: Path, opener, *, config=None, guard=None,
              work=None) -> ArxivNoveltySearchProvider:
    return ArxivNoveltySearchProvider(
        dict(CONFIG if config is None else config), work or _work(tmp_path),
        owner_guard=guard, opener=opener)


def test_search_freezes_content_addressed_atom_snapshot_and_replays_after_restart(
        tmp_path):
    work = _work(tmp_path)
    opener = _Opener([_atom(_entry(1), _entry(2))])
    guarded = []
    provider = _provider(
        tmp_path, opener, work=work, guard=lambda: guarded.append("owned"))

    query = "EEG robustness & cross-dataset generalization?"
    first = provider.search(query, policy_hash=POLICY_HASH)

    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert request.get_method() == "GET"
    assert timeout == 20
    parsed = urllib.parse.urlsplit(request.full_url)
    assert (parsed.scheme, parsed.hostname, parsed.path) == (
        "https", "export.arxiv.org", "/api/query")
    params = urllib.parse.parse_qs(parsed.query)
    assert params == {
        "search_query": [f'all:"{query}"'],
        "start": ["0"], "max_results": ["2"],
        "sortBy": ["relevance"], "sortOrder": ["descending"],
    }
    assert opener.responses[0].closed is True

    final_ref = first["final_ref"]
    assert set(final_ref) == {
        "query", "provider", "snapshot_hash", "snapshot_ref",
        "raw_content_hash", "result_content_hashes", "ranking", "policy_hash",
    }
    assert final_ref["provider"] == "arxiv_api_v1"
    assert final_ref["ranking"] == final_ref["result_content_hashes"]
    assert len(final_ref["ranking"]) == 2
    assert first["results"][0]["rank"] == 1
    assert first["results"][0]["title"] == "Robust EEG method 1"
    assert first["results"][0]["summary"] == "First line. Second line."
    assert first["results"][0]["authors"] == ["Alice 1", "张 三"]

    snapshot_path = work / final_ref["snapshot_ref"]
    raw_path = next((work / "state/novelty/raw/sha256").glob("*.atom"))
    receipt_path = next((work / "state/novelty/queries/sha256").glob("*.json"))
    assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o400
    assert stat.S_IMODE(raw_path.stat().st_mode) == 0o400
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert final_ref["snapshot_hash"] == (
        "sha256:" + hashlib.sha256(snapshot_path.read_bytes()).hexdigest())
    assert final_ref["raw_content_hash"] == (
        "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest())

    # A new owner object must consume the same quest receipt without network.
    network_called = []
    restarted = _provider(
        tmp_path, lambda *_a, **_k: network_called.append(True), work=work)
    second = restarted.search(query, policy_hash=POLICY_HASH)
    assert second == first
    assert network_called == []
    assert guarded


@pytest.mark.parametrize("asset", ["raw", "snapshot", "receipt"])
def test_replay_rejects_symlink_substitution_without_network(tmp_path, asset):
    work = _work(tmp_path)
    provider = _provider(tmp_path, _Opener(), work=work)
    provider.search("bounded novelty query", policy_hash=POLICY_HASH)
    patterns = {
        "raw": "state/novelty/raw/sha256/*.atom",
        "snapshot": "state/novelty/snapshots/sha256/*.json",
        "receipt": "state/novelty/queries/sha256/*.json",
    }
    target = next(work.glob(patterns[asset]))
    replacement = work / "replacement.bin"
    replacement.write_bytes(b"not trusted")
    target.unlink()
    target.symlink_to(replacement)
    called = []
    replay = _provider(
        tmp_path, lambda *_a, **_k: called.append(True), work=work)
    with pytest.raises(NoveltySearchError, match="安全打开|authority"):
        replay.search("bounded novelty query", policy_hash=POLICY_HASH)
    assert called == []


def test_replay_rejects_same_size_raw_tamper_by_hash(tmp_path):
    work = _work(tmp_path)
    provider = _provider(tmp_path, _Opener(), work=work)
    provider.search("bounded novelty query", policy_hash=POLICY_HASH)
    raw_path = next((work / "state/novelty/raw/sha256").glob("*.atom"))
    raw = raw_path.read_bytes()
    os.chmod(raw_path, 0o600)
    raw_path.write_bytes(raw[:-1] + (b"X" if raw[-1:] != b"X" else b"Y"))
    os.chmod(raw_path, 0o400)
    with pytest.raises(NoveltySearchError, match="content hash"):
        provider.search("bounded novelty query", policy_hash=POLICY_HASH)


@pytest.mark.parametrize("query", [
    "", "abcd", " leading text", "trailing text ", "bad\nquery",
    'bad "query"', "bad\\query", "e\u0301 normalized query", "x" * 513,
    "format\u202equery",
])
def test_query_is_bounded_plain_nfc_text_before_network(tmp_path, query):
    called = []
    provider = _provider(
        tmp_path, lambda *_a, **_k: called.append(True))
    with pytest.raises(NoveltySearchError, match="query"):
        provider.search(query, policy_hash=POLICY_HASH)
    assert called == []


@pytest.mark.parametrize("field,value", [
    ("provider", "ambient_web"),
    ("endpoint", "http://export.arxiv.org/api/query"),
    ("endpoint", "https://evil.example/api/query"),
    ("queries_per_candidate", 2),
    ("max_results_per_query", 0),
    ("max_results_per_query", 51),
    ("max_response_bytes", 100),
    ("max_response_bytes", 16 * 1024 * 1024 + 1),
    ("timeout_s", 0),
    ("min_interval_s", 61),
    ("retry_attempts", 0),
    ("retry_attempts", 21),
    ("retry_initial_delay_s", -1),
    ("retry_initial_delay_s", 121),
    ("retry_max_delay_s", -1),
    ("retry_max_delay_s", 601),
])
def test_policy_fixed_endpoint_and_bounds_are_fail_closed(tmp_path, field, value):
    config = dict(CONFIG)
    config[field] = value
    with pytest.raises(NoveltySearchError, match="policy|provider|endpoint"):
        _provider(tmp_path, _Opener(), config=config)


def test_retry_delay_range_must_be_coherent(tmp_path):
    config = dict(CONFIG)
    config.update(retry_initial_delay_s=2, retry_max_delay_s=1)
    with pytest.raises(NoveltySearchError, match="retry_max_delay_s"):
        _provider(tmp_path, _Opener(), config=config)


@pytest.mark.parametrize("policy_hash", [
    "", "a" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 63,
])
def test_policy_hash_must_be_prefixed_lowercase_sha256(tmp_path, policy_hash):
    called = []
    provider = _provider(
        tmp_path, lambda *_a, **_k: called.append(True))
    with pytest.raises(NoveltySearchError, match="policy_hash"):
        provider.search("valid novelty query", policy_hash=policy_hash)
    assert called == []


@pytest.mark.parametrize("failure", [
    "redirect", "status", "declared_oversize", "stream_oversize",
    "length_mismatch", "compressed", "http_error",
])
def test_http_redirect_status_host_and_byte_bounds_are_rejected(tmp_path, failure):
    config = dict(CONFIG)
    config["max_response_bytes"] = 1024

    if failure == "http_error":
        def opener(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 429, "rate limited", {}, None)
    else:
        def factory(request, raw):
            kwargs = {}
            url = request.full_url
            if failure == "redirect":
                url = "https://export.arxiv.org/api/query?changed=1"
            elif failure == "status":
                kwargs["status"] = 503
            elif failure == "declared_oversize":
                kwargs["declared"] = 1025
            elif failure == "length_mismatch":
                kwargs["declared"] = len(raw) + 1
            elif failure == "compressed":
                kwargs["content_encoding"] = "gzip"
            payload = b"x" * 1025 if failure == "stream_oversize" else raw
            return _Response(url, payload, **kwargs)
        opener = _Opener(response_factory=factory)

    provider = _provider(tmp_path, opener, config=config)
    with pytest.raises(NoveltySearchProviderError, match="arXiv novelty"):
        provider.search("valid novelty query", policy_hash=POLICY_HASH)


def test_transient_network_http_429_and_5xx_retry_until_success(
        monkeypatch, caplog, tmp_path):
    now = [100.0]
    sleeps = []
    guards = []
    calls = []
    outcomes = ["timeout", "rate_limit", "server_error", "success"]

    def monotonic():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    def opener(request, timeout):
        calls.append((request, timeout))
        outcome = outcomes.pop(0)
        if outcome == "timeout":
            raise TimeoutError("temporary timeout")
        if outcome == "rate_limit":
            raise urllib.error.HTTPError(
                request.full_url, 429, "rate limited",
                {"Retry-After": "7"}, None)
        if outcome == "server_error":
            return _Response(request.full_url, b"busy", status=503)
        return _Response(request.full_url, _atom(_entry(1)))

    monkeypatch.setattr(novelty_module.time, "monotonic", monotonic)
    monkeypatch.setattr(novelty_module.time, "sleep", sleep)
    config = dict(CONFIG)
    config.update(
        retry_attempts=4, retry_initial_delay_s=1, retry_max_delay_s=10)
    provider = _provider(
        tmp_path, opener, config=config, guard=lambda: guards.append("owned"))

    result = provider.search("recoverable novelty query", policy_hash=POLICY_HASH)

    assert result["results"][0]["title"] == "Robust EEG method 1"
    assert len(calls) == 4
    assert [timeout for _request, timeout in calls] == [20.0] * 4
    assert sum(sleeps) == 12  # 1s, Retry-After 7s, then exponential 4s.
    assert len(guards) > len(calls)
    assert caplog.text.count("秒后重试") == 3


def test_transient_failure_exhaustion_reports_attempt_count(tmp_path):
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        raise TimeoutError("still unavailable")

    config = dict(CONFIG)
    config.update(
        retry_attempts=3, retry_initial_delay_s=0, retry_max_delay_s=0)
    provider = _provider(tmp_path, opener, config=config)
    with pytest.raises(NoveltySearchProviderError, match="3 次尝试"):
        provider.search("exhausted novelty query", policy_hash=POLICY_HASH)
    assert len(calls) == 3


@pytest.mark.parametrize("raw,pattern", [
    (b"not XML", "畸形"),
    (b'<!DOCTYPE feed [<!ENTITY x "x">]><feed>&x;</feed>', "DTD"),
    (_atom(_entry(1, include_summary=False)), "summary"),
    (_atom(_entry(1), _entry(1)), "重复 entry id"),
    (_atom(_entry(1, entry_id="https://evil.example/abs/1")), "authority"),
])
def test_malformed_or_ambiguous_atom_is_rejected(tmp_path, raw, pattern):
    provider = _provider(tmp_path, _Opener([raw]))
    with pytest.raises(NoveltySearchProviderError, match=pattern):
        provider.search("valid novelty query", policy_hash=POLICY_HASH)


def test_result_count_is_rejected_instead_of_silently_truncated(tmp_path):
    config = dict(CONFIG)
    config["max_results_per_query"] = 1
    provider = _provider(
        tmp_path, _Opener([_atom(_entry(1), _entry(2))]), config=config)
    with pytest.raises(NoveltySearchProviderError, match="entry 数"):
        provider.search("valid novelty query", policy_hash=POLICY_HASH)


def test_distinct_real_requests_observe_minimum_interval(monkeypatch, tmp_path):
    now = [100.0]
    sleeps = []

    def monotonic():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(novelty_module.time, "monotonic", monotonic)
    monkeypatch.setattr(novelty_module.time, "sleep", sleep)
    config = dict(CONFIG)
    config["min_interval_s"] = 3
    opener = _Opener([_atom(_entry(1)), _atom(_entry(2))])
    provider = _provider(tmp_path, opener, config=config)

    provider.search("first novelty query", policy_hash=POLICY_HASH)
    provider.search("second novelty query", policy_hash=POLICY_HASH)

    assert len(opener.calls) == 2
    assert sleeps == [3.0]


def test_existing_objects_are_reused_after_crash_before_receipt(tmp_path):
    work = _work(tmp_path)
    raw = _atom(_entry(1))
    first = _provider(tmp_path, _Opener([raw]), work=work)
    first.search("crash replay novelty", policy_hash=POLICY_HASH)
    raw_path = next((work / "state/novelty/raw/sha256").glob("*.atom"))
    snapshot_path = next((work / "state/novelty/snapshots/sha256").glob("*.json"))
    identities = ((raw_path.stat().st_dev, raw_path.stat().st_ino),
                  (snapshot_path.stat().st_dev, snapshot_path.stat().st_ino))
    receipt = next((work / "state/novelty/queries/sha256").glob("*.json"))
    receipt.unlink()  # Simulate owner death after objects but before receipt fsync.

    opener = _Opener([raw])
    retry = _provider(tmp_path, opener, work=work)
    retry.search("crash replay novelty", policy_hash=POLICY_HASH)

    assert len(opener.calls) == 1
    assert identities == (
        (raw_path.stat().st_dev, raw_path.stat().st_ino),
        (snapshot_path.stat().st_dev, snapshot_path.stat().st_ino),
    )
