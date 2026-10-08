from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meta_research.sogou_discovery import HttpObservation, SogouDiscovery, fragment_destination, sogou_operations
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.skills.deepfetch_v4.scripts import papers
from meta_research.skills.deepfetch_v4.scripts.ledger_contract import ContractError, canonical_paper_url, verify_host_receipts

CARD = '<li id="sogou_vr_1"><h3><a href="/link?url=actual&amp;type=2">Calibration research</a></h3><p class="txt-info" id="summary_1">A research lead.</p><span class="all-time-y2">Research account</span><script>document.write(timeConvert(\'1708941779\'))</script></li>'
FRAGMENTS = 'var url = \'\'; url += \'https://mp.\'; url += \'weixin.qq.com/s?actual=1\'; url.replace("@", ""); window.location.replace(url)'
BODY = '<h1 id="activity-name">Article title</h1><a id="js_name">Actual author</a><div id="js_content">An article describes uncertainty calibration and links to the original academic paper. This is commentary.</div>'


class Transport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def get(self, url, referer=None):
        self.requests.append((url, referer))
        status, body, final, redirects = next(self.responses)
        return HttpObservation(url, final or url, redirects, status, body)


def service(tmp_path, tail):
    transport = Transport([(200, CARD, None, ()), *tail])
    discovery = SogouDiscovery(tmp_path / "host", session_factory=lambda: transport)
    result = discovery.search("run:attempt", "uncertainty calibration")
    return discovery, transport, result


def test_selected_card_opens_same_session_and_preserves_evidence(tmp_path):
    discovery, transport, search = service(tmp_path, [(200, FRAGMENTS, None, ()), (200, BODY, "https://mp.weixin.qq.com/s?actual=1", ())])
    card = search["candidates"][0]
    assert card["receipt"]["observation"]["title"] == "Calibration research"
    assert card["receipt"]["observation"]["account_or_author"] == "Research account"
    assert card["receipt"]["excerpt"] == "A research lead."
    assert card["receipt"]["observation"]["published_at"] == "2024-02-26T10:02:59+00:00"
    opened = discovery.open("run:attempt", card["candidate_ref"])
    assert opened["receipt"]["outcome"] == "opened"
    assert opened["receipt"]["evidence_kind"] == "opened_article_body"
    assert opened["receipt"]["observation"]["account_or_author"] == "Actual author"
    assert opened["receipt"]["observation"]["published_at"] is None
    assert opened["receipt"]["observation"]["content_sha256"] == hashlib.sha256(opened["article_body"].encode()).hexdigest()
    assert opened["receipt"]["observation"]["redirects"] == ["https://weixin.sogou.com/link?url=actual&type=2", "https://mp.weixin.qq.com/s?actual=1"]
    assert transport.requests[1][1] == transport.requests[0][0]
    restarted = SogouDiscovery(tmp_path / "host")
    assert json.loads(next((tmp_path / "host").glob("*.json")).read_text())[-1] == opened["receipt"]
    assert restarted.receipts("run:attempt") == []
    assert restarted.open("run:attempt", card["candidate_ref"])["receipt"]["outcome"] == "expired"


@pytest.mark.parametrize("status,body,final,outcome", [
    (200, "环境异常 当前环境异常，完成验证后即可继续访问", "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha", "captcha"),
    (200, "请登录后查看", None, "login_required"),
    (429, "too many requests", None, "rate_limited"),
    (410, "gone", None, "expired"),
    (200, "var url = getUnknownTarget(); location.href=url", None, "unsupported_redirect"),
    (503, "unavailable", None, "unavailable"),
])
def test_restrictions_never_supply_article_body(tmp_path, status, body, final, outcome):
    discovery, _, search = service(tmp_path, [(status, body, final, ())])
    opened = discovery.open("run:attempt", search["candidates"][0]["candidate_ref"])
    assert opened["receipt"]["outcome"] == outcome
    assert opened["receipt"]["observation"]["http_status"] == status
    assert opened["receipt"]["evidence_kind"] == "result_snippet"
    assert opened["article_body"] is None
    assert opened["receipt"]["observation"]["content_sha256"] is None
    assert opened["receipt"]["limitation"] == "sogou_wechat_" + outcome
    assert discovery.receipts("run:attempt")[1]["excerpt"] == "A research lead."


def test_empty_search_and_cross_attempt_card_expiry(tmp_path):
    transport = Transport([(200, "<html>没有找到相关结果</html>", None, ())])
    discovery = SogouDiscovery(session_factory=lambda: transport)
    result = discovery.search("scope", "query")
    assert result["receipt"]["outcome"] == "empty"
    assert result["receipt"]["query"] == "query"
    assert result["candidates"] == []
    discovery, _, result = service(tmp_path, [])
    opened = discovery.open("different:attempt", result["candidates"][0]["candidate_ref"])
    assert opened["receipt"]["outcome"] == "expired"
    assert opened["article_body"] is None


def test_expired_selected_card_preserves_returned_address(tmp_path):
    discovery, _, result = service(tmp_path, [])
    discovery._clock = lambda: 10**12
    opened = discovery.open("run:attempt", result["candidates"][0]["candidate_ref"])
    assert opened["receipt"]["outcome"] == "expired"
    assert opened["receipt"]["observation"]["returned_url"] == "https://weixin.sogou.com/link?url=actual&type=2"


def test_fragment_parser_refuses_arbitrary_script_and_external_hosts():
    assert fragment_destination(FRAGMENTS) == "https://mp.weixin.qq.com/s?actual=1"
    assert fragment_destination(FRAGMENTS.replace("mp.", "evil.")) is None
    assert fragment_destination(FRAGMENTS.replace("'https://mp.'", "getDestination()")) is None


class Owner:
    def __init__(self, discovery, mode):
        self.sogou_discovery = discovery
        self.mode = mode

    def verify_root_agent_runtime_scope(self, **scope):
        assert scope["root_kind"] == "deepfetch"
        return {"quest_ref": "quest"}

    def query_acquisition_session(self, **scope):
        return SimpleNamespace(session_ref="acquisition", mode=self.mode)


def gateway_call(gateway, connection, name, arguments, call_id=1):
    _, response = gateway.dispatch(connection.token, {"jsonrpc": "2.0", "id": call_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return response


def issue(gateway, operations):
    connection, binding = gateway.issue_channel(run_ref="run", attempt_ref="attempt", root_session_ref="root", fence_ref="fence", capability_binding_hash="a" * 64, operation_ids=operations, root_kind="deepfetch", phase="radar")
    return SimpleNamespace(connection=connection, binding=binding)


def test_semantic_gateway_executes_exact_tools_and_denies_provided_only(tmp_path):
    transport = Transport([(200, CARD, None, ()), (200, BODY, "https://mp.weixin.qq.com/s?actual=1", ())])
    owner = Owner(SogouDiscovery(session_factory=lambda: transport), "oa_only")
    gateway = SemanticMcpGateway(sogou_operations(owner))
    channel = issue(gateway, ("deepfetch.sogou.search", "deepfetch.sogou.open"))
    result = gateway_call(gateway, channel.connection, "deepfetch.sogou.search", {"query": "query"})
    search = result["result"]["structuredContent"]
    assert search["receipt"]["outcome"] == "results"
    opened = gateway_call(gateway, channel.connection, "deepfetch.sogou.open", {"candidate_ref": search["candidates"][0]["candidate_ref"]}, 2)
    assert opened["result"]["structuredContent"]["receipt"]["outcome"] == "opened"
    owner.mode = "provided_only"
    denied = gateway_call(gateway, channel.connection, "deepfetch.sogou.search", {"query": "query"}, 3)
    assert denied["result"]["isError"] is True
    assert "sogou_provided_only_denied" in json.dumps(denied)
    forged = gateway_call(gateway, channel.connection, "deepfetch.sogou.open", {"candidate_ref": "https://evil.example", "url": "https://evil.example"}, 4)
    assert forged["result"]["isError"] is True
    assert len(transport.requests) == 2


def version(revision=1, *, kind="preprint", url=None):
    original = url or f"https://arxiv.org/abs/2401.01234v{revision}"
    return {"discovery_refs": [], "version": {"kind": kind, "arxiv_version": revision if kind == "preprint" else None, "canonical_url": original, "verified_at": "2026-10-08T12:00:00Z", "verification_urls": [original]}, "related_paper_ids": []}


def intake(provenance=None, **changes):
    return {"title": "Calibration research", "arxiv_id": "2401.01234", "source_urls": ["https://arxiv.org/abs/2401.01234v1"], "provenance": provenance or version(), **changes}


def initialize(tmp_path):
    assert papers.main(["init", "--out-dir", str(tmp_path), "--topic", "calibration"]) == 0


def upsert(tmp_path, payload):
    path = tmp_path / "input.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = papers.main(["upsert", "--out-dir", str(tmp_path), "--input", str(path)])
    path.unlink()
    return result


def test_same_confirmed_revision_merges_all_discovery_sources(tmp_path):
    initialize(tmp_path)
    discovery, _, search = service(tmp_path, [])
    sogou = search["candidates"][0]["receipt"]
    web = json.loads(json.dumps(sogou))
    web.update(receipt_ref="native:actual", channel="native_web", evidence_kind="academic_source")
    web["parent_receipt_ref"] = None
    web["observation"]["requested_url"] = "https://arxiv.org/abs/2401.01234v1"
    ledger = papers.load_ledger(tmp_path)
    ledger["discovery"]["receipts"] = [search["receipt"], sogou, web]
    papers.atomic_write_json(tmp_path / "papers.json", ledger)
    a, b = version(), version()
    a["discovery_refs"] = [sogou["receipt_ref"]]
    b["discovery_refs"] = ["native:actual"]
    assert upsert(tmp_path, [intake(a), intake(b, title="A corrected academic title", openalex_id="W123")]) == 0
    ledger = papers.load_ledger(tmp_path)
    assert len(ledger["papers"]) == 1
    paper = ledger["papers"][ledger["paper_order"][0]]
    assert paper["provenance"]["discovery_refs"] == [sogou["receipt_ref"], "native:actual"]
    assert paper["reading"]["status"] == "not_read"
    assert canonical_paper_url(paper) == "https://arxiv.org/abs/2401.01234v1"
    verify_host_receipts(ledger, discovery.receipts("run:attempt"))


def test_distinct_revisions_and_publication_stay_separate_and_related(tmp_path):
    initialize(tmp_path)
    assert upsert(tmp_path, intake()) == 0
    first = papers.load_ledger(tmp_path)["paper_order"][0]
    second = version(2)
    second["related_paper_ids"] = [first]
    published = version(kind="published", url="https://doi.org/10.1234/calibration")
    published["related_paper_ids"] = [first]
    assert upsert(tmp_path, [intake(second, source_urls=["https://arxiv.org/abs/2401.01234v2"]), intake(published, doi="10.1234/calibration", source_urls=["https://doi.org/10.1234/calibration"])]) == 0
    ledger = papers.load_ledger(tmp_path)
    assert len(ledger["papers"]) == 3
    assert [p["provenance"]["version"]["kind"] for p in ledger["papers"].values()] == ["preprint", "preprint", "published"]
    assert ledger["papers"][ledger["paper_order"][1]]["provenance"]["related_paper_ids"] == [first]
    assert len(set(ledger["paper_order"])) == 3


def test_title_only_does_not_create_paper_and_same_title_does_not_merge(tmp_path):
    initialize(tmp_path)
    assert upsert(tmp_path, {"title": "A project lead", "source_urls": ["https://mp.weixin.qq.com/s?article"]}) == 2
    assert papers.load_ledger(tmp_path)["paper_order"] == []
    assert upsert(tmp_path, [intake(), intake(version(url="https://arxiv.org/abs/2402.00001v1"), arxiv_id="2402.00001", source_urls=["https://arxiv.org/abs/2402.00001v1"])]) == 0
    assert len(papers.load_ledger(tmp_path)["papers"]) == 2


def test_unknown_identifiers_use_verified_original_address(tmp_path):
    initialize(tmp_path)
    assert upsert(tmp_path, intake(version(kind="published", url="https://proceedings.example.org/paper/123"), arxiv_id=None, source_urls=["https://proceedings.example.org/paper/123"])) == 0
    ledger = papers.load_ledger(tmp_path)
    paper = ledger["papers"][ledger["paper_order"][0]]
    assert paper["identity"]["doi"] is None
    assert paper["identity"]["openalex_id"] is None
    assert canonical_paper_url(paper) == "https://proceedings.example.org/paper/123"


def test_forged_receipts_and_commentary_originals_fail(tmp_path):
    initialize(tmp_path)
    provenance = version()
    provenance["discovery_refs"] = ["forged:success"]
    assert upsert(tmp_path, intake(provenance)) == 2
    assert upsert(tmp_path, intake(version(kind="published", url="https://mp.weixin.qq.com/s?article"))) == 2
    assert upsert(tmp_path, intake(version(1), arxiv_id="2401.01234v2")) == 2
    discovery, _, search = service(tmp_path, [])
    ledger = papers.load_ledger(tmp_path)
    ledger["discovery"]["receipts"] = [search["receipt"]]
    with pytest.raises(ContractError, match="host observation"):
        verify_host_receipts(ledger, [])
    verify_host_receipts(ledger, discovery.receipts("run:attempt"))


def test_snapshot_consumers_preserve_discovery_and_distinct_version_records(tmp_path):
    from meta_research.owners.research_memory import _proposal_ledger_evidence, _question_literature_records, _validated_v4_ledger
    initialize(tmp_path)
    assert upsert(tmp_path, [intake(doi="10.1234/shared"), intake(version(2), doi="10.1234/shared", source_urls=["https://arxiv.org/abs/2401.01234v2"])]) == 0
    ledger = papers.load_ledger(tmp_path)
    assert _validated_v4_ledger(ledger, expected_count=2) == 2
    projection = _proposal_ledger_evidence(ledger)
    assert projection["discovery"] == {"receipts": [], "unresolved_leads": []}
    first, second = ledger["paper_order"]
    assert projection["papers"][second]["provenance"]["version"]["arxiv_version"] == 2
    records = _question_literature_records({"papers_ledger": ledger, "papers": [{"doi": "10.1234/shared", "url": "https://arxiv.org/abs/2401.01234v1"}, {"doi": "10.1234/shared", "url": "https://arxiv.org/abs/2401.01234v2"}], "fulltexts": []})
    assert records == [{"ref": "paper:" + first, "evidence_basis": "title_lead", "evidence_basis_ref": "paper:" + first}, {"ref": "paper:" + second, "evidence_basis": "title_lead", "evidence_basis_ref": "paper:" + second}]


@pytest.mark.parametrize("forged", [False, True])
def test_provider_import_retains_host_receipt_and_original_url(tmp_path, forged):
    from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable
    from test_deepfetch_adapter import SequencedPrototypeRunner, PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL, RecordingAcquisitionClient, _bind_acquisition, _execute
    discovery, _, search = service(tmp_path, [])
    observations = discovery.receipts("run:attempt")

    class ProvenanceRunner(SequencedPrototypeRunner):
        def __call__(self, argv, prompt, timeout):
            result = super().__call__(argv, prompt, timeout)
            if "web_evidence_gate=v1" not in prompt:
                root = Path(next(line.removeprefix("public_output_root=") for line in prompt.splitlines() if line.startswith("public_output_root=")))
                ledger = json.loads((root / "papers.json").read_text())
                ledger["discovery"] = {"receipts": copy.deepcopy(observations), "unresolved_leads": []}
                if forged:
                    ledger["discovery"]["receipts"][0]["query"] = "fabricated query"
                paper = ledger["papers"]["doi:10.1000/example"]
                paper["provenance"] = version(kind="published", url="https://doi.org/10.1000/example")
                paper["provenance"]["discovery_refs"] = [search["candidates"][0]["receipt"]["receipt_ref"]]
                paper["metadata"]["source_urls"] = ["https://openalex.org/W123", "https://example.org/paper"]
                (root / "papers.json").write_text(json.dumps(ledger), encoding="utf-8")
            return result

    runner = ProvenanceRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    adapter = _bind_acquisition(CodexDeepFetchAdapter(tmp_path / "provider", model_ref="gpt-test", process_runner=runner), RecordingAcquisitionClient(tmp_path / "acquisition"))
    adapter.bind_sogou_discovery(SimpleNamespace(receipts=lambda scope: observations))
    if forged:
        with pytest.raises(DeepFetchUnavailable, match="deepfetch_discovery_provenance_invalid"):
            _execute(adapter)
    else:
        result = _execute(adapter)
        assert result.papers[0]["url"] == "https://doi.org/10.1000/example"
        assert result.fulltexts[0]["paper_url"] == "https://doi.org/10.1000/example"
        assert result.papers_ledger["discovery"]["receipts"] == observations
        assert result.papers_ledger["papers"]["doi:10.1000/example"]["reading"]["status"] == "complete"


def test_discovery_never_substitutes_for_native_web_gate(tmp_path):
    from meta_research.deepfetch import CodexDeepFetchAdapter, DeepFetchUnavailable
    from test_deepfetch_adapter import RecordingRunner, PROTOTYPE_EMPTY_FINAL, _execute
    discovery, _, _ = service(tmp_path, [])
    class NoNativeWeb(RecordingRunner):
        def __call__(self, argv, prompt, timeout):
            result = super().__call__(argv, prompt, timeout)
            result.stdout = json.dumps({"type": "thread.started", "thread_id": "native-web-research-1"})
            return result
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", model_ref="gpt-test", process_runner=NoNativeWeb(PROTOTYPE_EMPTY_FINAL, emit_web_evidence=False))
    adapter.bind_sogou_discovery(discovery)
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_web_evidence"):
        _execute(adapter)


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("mode", ["oa_only", "provided_only"])
def test_provider_paths_grant_reachable_discovery_or_deny_provided_only(tmp_path, durable, mode):
    from meta_research.deepfetch import CodexDeepFetchAdapter
    from meta_research.harness import ResidentMcpChannel
    from meta_research.owners.common import canonical_hash
    from meta_research.root_resident_mcp import RootResidentMcpChannels
    from test_deepfetch_adapter import _DeepFetchResidentMcpAuthority, ResidentRecordingRunner, DurableSegmentSequenceRunner, PROTOTYPE_EMPTY_FINAL, _request

    transport = Transport([(200, CARD, None, ())])
    owner = Owner(SogouDiscovery(session_factory=lambda: transport), mode)
    discovery_gateway = SemanticMcpGateway(sogou_operations(owner))

    class Authority(_DeepFetchResidentMcpAuthority):
        def require_operation_binding(self, **values):
            base = super().require_operation_binding(**{**values, "required_operation_ids": self.operation_ids})
            self.selected_ids = values["required_operation_ids"]
            bindings = tuple(item for item in self.operation_bindings if item["semantic_operation_id"] in self.selected_ids)
            return replace(base, required_operation_ids=self.selected_ids, semantic_mcp_operation_bindings_hash=canonical_hash(list(bindings)))

        def issue_resident_mcp_channel(self, **scope):
            base = super().issue_resident_mcp_channel(**scope)
            selected = tuple(item for item in self.operation_bindings if item["semantic_operation_id"] in scope["operation_ids"])
            if "deepfetch.sogou.search" in scope["operation_ids"]:
                self.discovery_channel = issue(discovery_gateway, ("deepfetch.sogou.search", "deepfetch.sogou.open"))
            return replace(base, binding=replace(base.binding, operation_bindings=selected))

    authority = Authority()
    outcomes = []

    def observe(argv):
        assert 'mcp_servers.meta_research.url="http://127.0.0.1:8799/mcp"' in argv
        if mode == "oa_only":
            response = gateway_call(discovery_gateway, authority.discovery_channel.connection, "deepfetch.sogou.search", {"query": "query"})
            outcomes.append(response["result"]["structuredContent"]["receipt"]["outcome"])
        else:
            outcomes.append("discovery_not_granted" if "deepfetch.sogou.search" not in authority.issued[-1]["operation_ids"] else "wrong_grant")

    workspace = tmp_path / "provider"
    if durable:
        class Runner(DurableSegmentSequenceRunner):
            def run_durable_job(self, job_ref, argv, prompt, timeout, stdout_path, pid_path, supervisor_request_path, environment=None):
                observe(argv)
                return super().run_durable_job(job_ref, argv, prompt, timeout, stdout_path, pid_path, supervisor_request_path)
        runner = Runner(workspace, stopped_segments=0)
    else:
        class Runner(ResidentRecordingRunner):
            def __call__(self, argv, prompt, timeout, environment=None):
                observe(argv)
                return super().__call__(argv, prompt, timeout, environment)
        runner = Runner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(workspace, model_ref="gpt-test", process_runner=runner)
    adapter.bind_resident_mcp_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8799")
    request = _request()
    request = replace(request, scope={"literature_mode": mode}, runtime_binding=adapter.runtime_binding(), job_ref="deepfetch:test" if durable else None)
    result = adapter._invoke(request, "web_evidence_gate=v1", phase="turn-0")
    assert result[0] == {"status": "web_evidence_ready"}
    assert outcomes == (["results"] if mode == "oa_only" else ["discovery_not_granted"])
