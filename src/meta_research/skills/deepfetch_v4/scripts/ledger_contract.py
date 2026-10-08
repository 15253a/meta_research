from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

BASE_TOP_KEYS = {"schema_version", "topic", "run", "paper_order", "papers", "missing_fulltexts", "limitations"}
BASE_PAPER_KEYS = {"identity", "metadata", "pre_understanding", "fulltext_path", "reading"}
VERSION_KEYS = {"kind", "arxiv_version", "canonical_url", "verified_at", "verification_urls"}
PROVENANCE_KEYS = {"discovery_refs", "version", "related_paper_ids"}
OBSERVATION_KEYS = {"requested_url", "returned_url", "final_url", "redirects", "http_status", "title", "account_or_author", "published_at", "content_sha256"}
RECEIPT_KEYS = {"receipt_ref", "channel", "action", "query", "parent_receipt_ref", "observed_at", "outcome", "observation", "evidence_kind", "excerpt", "limitation"}
OUTCOMES = {"results", "opened", "empty", "captcha", "login_required", "rate_limited", "expired", "unavailable", "unsupported_redirect"}


class ContractError(ValueError):
    pass


def exact(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ContractError(f"{name} has invalid fields")
    return value


def text(value, name, *, nullable=False):
    if value is None and nullable:
        return value
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a nonempty string")
    return value


def strings(value, name):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value) or len(set(value)) != len(value):
        raise ContractError(f"{name} must contain unique strings")
    return value


def url(value, name, *, nullable=False):
    if value is None and nullable:
        return None
    text(value, name)
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ContractError(f"{name} must be a credential-free HTTP URL")
    return value


def timestamp(value, name, *, nullable=False):
    if value is None and nullable:
        return None
    text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except ValueError as error:
        raise ContractError(f"{name} must include a timezone") from error
    return value


def academic_url(value):
    url(value, "academic URL")
    parsed = urlsplit(value)
    host = parsed.hostname.lower()
    if host in {"weixin.sogou.com", "mp.weixin.qq.com"} or any(part in parsed.path.lower() for part in ("/search", "/blog", "/news")):
        raise ContractError("discovery or commentary URL is not an original paper")
    return value


def parse_version(value):
    exact(value, VERSION_KEYS, "version")
    if value["kind"] not in {"preprint", "published", "unknown"}:
        raise ContractError("version kind invalid")
    revision = value["arxiv_version"]
    if revision is not None and (type(revision) is not int or revision < 1 or value["kind"] != "preprint"):
        raise ContractError("arxiv_version invalid")
    academic_url(value["canonical_url"])
    timestamp(value["verified_at"], "verified_at")
    sources = strings(value["verification_urls"], "verification_urls")
    if not sources:
        raise ContractError("version needs original academic verification")
    for source in sources:
        academic_url(source)
    arxiv = re.fullmatch(r"/(?:abs|pdf)/(.+?)(?:\.pdf)?", urlsplit(value["canonical_url"]).path)
    if urlsplit(value["canonical_url"]).hostname == "arxiv.org" and arxiv:
        if value["kind"] != "preprint":
            raise ContractError("arXiv original must be a preprint manifestation")
        observed = re.search(r"v([1-9]\d*)$", arxiv.group(1))
        if (int(observed.group(1)) if observed else None) != revision:
            raise ContractError("canonical arXiv revision disagrees with version")
    return value


def parse_provenance(value):
    exact(value, PROVENANCE_KEYS, "provenance")
    strings(value["discovery_refs"], "discovery_refs")
    strings(value["related_paper_ids"], "related_paper_ids")
    parse_version(value["version"])
    return value


def parse_receipt(value):
    exact(value, RECEIPT_KEYS, "receipt")
    text(value["receipt_ref"], "receipt_ref")
    if value["channel"] not in {"sogou_wechat", "openalex", "native_web", "provided"} or value["action"] not in {"search", "open"} or value["outcome"] not in OUTCOMES:
        raise ContractError("receipt action, channel or outcome invalid")
    timestamp(value["observed_at"], "observed_at")
    for field in ("query", "parent_receipt_ref", "excerpt", "limitation"):
        text(value[field], field, nullable=True)
    observation = exact(value["observation"], OBSERVATION_KEYS, "observation")
    url(observation["requested_url"], "requested_url")
    for field in ("returned_url", "final_url"):
        url(observation[field], field, nullable=True)
    if not isinstance(observation["redirects"], list):
        raise ContractError("redirects must be an array")
    for hop in observation["redirects"]:
        url(hop, "redirect")
    status = observation["http_status"]
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ContractError("http_status invalid")
    for field in ("title", "account_or_author", "published_at"):
        text(observation[field], field, nullable=True)
    digest = observation["content_sha256"]
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ContractError("content_sha256 invalid")
    kind = value["evidence_kind"]
    if kind not in {"result_snippet", "opened_article_body", "academic_source"}:
        raise ContractError("evidence_kind invalid")
    if kind == "opened_article_body" and (value["outcome"] != "opened" or not digest or not value["excerpt"] or urlsplit(observation["final_url"] or "").hostname != "mp.weixin.qq.com" or urlsplit(observation["final_url"] or "").path != "/s"):
        raise ContractError("opened article requires actual article body evidence")
    if value["outcome"] not in {"results", "opened", "empty"} and value["limitation"] is None:
        raise ContractError("failed access needs a limitation")
    return value


def parse_extensions(ledger, *, historical_read=False):
    if not isinstance(ledger, dict):
        raise ContractError("ledger must be an object")
    expanded = "discovery" in ledger
    exact(ledger, BASE_TOP_KEYS | ({"discovery"} if expanded else set()), "ledger")
    if not expanded:
        if not historical_read:
            raise ContractError("new ledger needs discovery provenance")
        return ledger
    discovery = exact(ledger["discovery"], {"receipts", "unresolved_leads"}, "discovery")
    if not isinstance(discovery["receipts"], list) or not isinstance(discovery["unresolved_leads"], list):
        raise ContractError("discovery collections must be arrays")
    receipts = {}
    for receipt in discovery["receipts"]:
        parse_receipt(receipt)
        ref = receipt["receipt_ref"]
        if ref in receipts:
            raise ContractError("duplicate receipt")
        receipts[ref] = receipt
    for receipt in receipts.values():
        parent = receipt["parent_receipt_ref"]
        if parent is not None and (parent not in receipts or parent == receipt["receipt_ref"] or receipts[parent]["action"] != "search"):
            raise ContractError("unknown search parent receipt")
    papers = ledger["papers"]
    if not isinstance(papers, dict):
        raise ContractError("papers must be an object")
    def refs(values):
        strings(values, "discovery_refs")
        if set(values) - set(receipts):
            raise ContractError("unknown discovery receipt ref")
    for paper_id, paper in papers.items():
        exact(paper, BASE_PAPER_KEYS | {"provenance"}, "paper")
        if not isinstance(paper["identity"], dict) or not isinstance(paper["metadata"], dict) or not isinstance(paper["metadata"].get("source_urls"), list):
            raise ContractError("paper identity, metadata or source URLs invalid")
        provenance = parse_provenance(paper["provenance"])
        refs(provenance["discovery_refs"])
        if set(provenance["related_paper_ids"]) - (set(papers) - {paper_id}):
            raise ContractError("unknown or self paper relation")
        for source in paper["metadata"]["source_urls"]:
            academic_url(source)
        canonical = provenance["version"]["canonical_url"]
        doi = paper["identity"].get("doi")
        if doi and urlsplit(canonical).hostname in {"doi.org", "dx.doi.org"} and urlsplit(canonical).path.lstrip("/").lower() != doi:
            raise ContractError("canonical DOI identity conflicts")
        arxiv_id = paper["identity"].get("arxiv_id")
        if arxiv_id and urlsplit(canonical).hostname == "arxiv.org":
            base = re.sub(r"v\d+$", "", urlsplit(canonical).path.removeprefix("/abs/").removeprefix("/pdf/").removesuffix(".pdf"))
            if base != arxiv_id:
                raise ContractError("canonical arXiv identity conflicts")
    leads = set()
    for lead in discovery["unresolved_leads"]:
        exact(lead, {"lead_ref", "discovery_refs", "kind", "title", "url", "limitation"}, "lead")
        text(lead["lead_ref"], "lead_ref")
        if lead["lead_ref"] in leads or lead["kind"] not in {"article", "project", "possible_paper"}:
            raise ContractError("lead identity or kind invalid")
        leads.add(lead["lead_ref"])
        refs(lead["discovery_refs"])
        text(lead["title"], "lead.title", nullable=True)
        url(lead["url"], "lead.url", nullable=True)
        text(lead["limitation"], "lead.limitation")
    return ledger


def equivalent_version(left, right, *, identities=None):
    a, b = left["version"], right["version"]
    if a["kind"] == "unknown" or b["kind"] == "unknown" or a["kind"] != b["kind"]:
        return False
    if a["kind"] == "preprint":
        shared_arxiv = identities is not None and identities[0].get("arxiv_id") and identities[0].get("arxiv_id") == identities[1].get("arxiv_id")
        return a["arxiv_version"] is not None and a["arxiv_version"] == b["arxiv_version"] and bool(shared_arxiv or a["canonical_url"] == b["canonical_url"])
    shared_doi = identities is not None and identities[0].get("doi") and identities[0].get("doi") == identities[1].get("doi")
    if shared_doi:
        return True
    return a["canonical_url"] == b["canonical_url"]


def verify_host_receipts(ledger, observations):
    host = {r["receipt_ref"]: r for r in observations}
    for receipt in ledger.get("discovery", {}).get("receipts", []):
        if receipt["channel"] == "sogou_wechat" and host.get(receipt["receipt_ref"]) != receipt:
            raise ContractError("Sogou receipt is not a matching host observation")


def canonical_paper_url(paper):
    return parse_provenance(paper["provenance"])["version"]["canonical_url"]
