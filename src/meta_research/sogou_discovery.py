from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
from http.client import HTTPException
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from meta_research.skills.deepfetch_v4.scripts.ledger_contract import parse_receipt

SOGOU_OPERATION_IDS = ("deepfetch.sogou.search", "deepfetch.sogou.open")
_HOSTS = {"weixin.sogou.com", "mp.weixin.qq.com"}
_MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class HttpObservation:
    requested_url: str
    final_url: str | None
    redirects: tuple[str, ...]
    status: int | None
    body: str
    error: str | None = None


class _SiteRedirects(HTTPRedirectHandler):
    def __init__(self):
        self.hops = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _site_url(newurl) or len(self.hops) >= 8:
            raise ValueError("unsupported_redirect")
        self.hops.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _site_url(value):
    parsed = urlsplit(value)
    try:
        return parsed.scheme == "https" and parsed.hostname in _HOSTS and parsed.port in {None, 443} and not parsed.username and not parsed.password
    except ValueError:
        return False


class HttpSession:
    def __init__(self):
        self.redirects = _SiteRedirects()
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()), self.redirects)

    def get(self, url, referer=None):
        url = url.replace(" ", "%20")
        if not _site_url(url):
            return HttpObservation(url, None, (), None, "", "unsupported_redirect")
        self.redirects.hops = []
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html"}
        if referer:
            headers["Referer"] = referer
        try:
            try:
                response = self.opener.open(Request(url, headers=headers), timeout=20)
            except HTTPError as error:
                response = error
            with response:
                content = response.read(_MAX_BYTES + 1)
                if len(content) > _MAX_BYTES:
                    raise ValueError("response_too_large")
                encoding = response.headers.get_content_charset() or "utf-8"
                return HttpObservation(url, response.geturl(), tuple(self.redirects.hops), response.status, content.decode(encoding, errors="replace"))
        except (OSError, URLError, HTTPException, ValueError, LookupError) as error:
            reason = "unsupported_redirect" if str(error) == "unsupported_redirect" else "unavailable"
            return HttpObservation(url, None, tuple(self.redirects.hops), None, "", reason)


class _Markup(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = {}
        self.visible = []
        self.cards = []
        self.card = None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "li" and attributes.get("id", "").startswith("sogou_vr_"):
            self.card = {"title": "", "url": None, "account_or_author": "", "excerpt": "", "published_at": None}
        label = " ".join(filter(None, (attributes.get("id"), attributes.get("class"))))
        self.stack.append((tag, label))
        if self.card is not None and tag == "a" and any(t == "h3" for t, _ in self.stack):
            self.card["url"] = attributes.get("href")

    def handle_endtag(self, tag):
        if tag == "li" and self.card is not None:
            if self.card["url"]:
                self.cards.append(self.card)
            self.card = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if any(tag in {"script", "style"} for tag, _ in self.stack):
            if self.card is not None:
                published = re.fullmatch(r"document.write\(timeConvert\('([0-9]{10})'\)\)", data.strip())
                if published:
                    self.card["published_at"] = datetime.fromtimestamp(int(published.group(1)), timezone.utc).isoformat()
            return
        self.visible.append(data)
        for _, label in self.stack:
            if label:
                for name in label.split():
                    self.parts.setdefault(name, []).append(data)
        if self.card is not None:
            if any(t == "h3" for t, _ in self.stack):
                self.card["title"] += data
            if any("txt-info" in label for _, label in self.stack):
                self.card["excerpt"] += data
            if any("account" in label or "all-time-y2" in label for _, label in self.stack):
                self.card["account_or_author"] += data

    def text(self, label):
        return " ".join("".join(self.parts.get(label, [])).split()) or None


def classify(observation):
    if observation.error:
        return observation.error
    body = " ".join(_Markup(observation.body).visible)
    address = observation.final_url or ""
    if observation.status == 429 or "访问过于频繁" in body:
        return "rate_limited"
    if any(marker in address for marker in ("/antispider", "/mp/wappoc_appmsgcaptcha")) or any(marker in body for marker in ("环境异常", "请输入验证码")):
        return "captcha"
    if observation.status == 401 or any(marker in body for marker in ("请登录", "登录后查看", "login_required")):
        return "login_required"
    if observation.status in {404, 410} or any(marker in body for marker in ("链接已过期", "该内容已被发布者删除", "内容已删除")):
        return "expired"
    if observation.status != 200:
        return "unavailable"
    return None


def fragment_destination(body):
    match = re.search(r"var\s+url\s*=\s*''\s*;(?P<fragments>(?:\s*url\s*\+=\s*'[^'\\]*'\s*;)+)\s*url\.replace\(\"@\",\s*\"\"\);\s*window\.location\.replace\(url\)", body)
    if match is None:
        return None
    destination = "".join(re.findall(r"url\s*\+=\s*'([^'\\]*)'", match.group("fragments")))
    parsed = urlsplit(destination)
    if not _site_url(destination) or parsed.hostname != "mp.weixin.qq.com" or parsed.path != "/s":
        return None
    return destination


@dataclass
class SearchSession:
    transport: HttpSession
    query: str
    requested_url: str
    receipt_ref: str
    created: float


class SogouDiscovery:
    def __init__(self, receipt_root: Path | None = None, *, session_factory=HttpSession, clock=time.monotonic):
        self._receipt_root = receipt_root
        self._session_factory = session_factory
        self._clock = clock
        self._sessions = {}
        self._cards = {}
        self._receipts = {}
        self._lock = threading.RLock()

    def receipts(self, scope):
        with self._lock:
            return json.loads(json.dumps(self._receipts.get(scope, [])))

    def _path(self, scope):
        return self._receipt_root / (hashlib.sha256(scope.encode()).hexdigest() + ".json")

    def _record(self, scope, receipt):
        parse_receipt(receipt)
        values = self.receipts(scope)
        values.append(receipt)
        self._receipts[scope] = values
        if self._receipt_root is not None:
            self._receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination = self._path(scope)
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            temporary.replace(destination)
        return receipt

    def _receipt(self, query, action, parent, observation, outcome, *, returned=None, title=None, author=None, published=None, excerpt=None, body=None):
        limitation = None if outcome in {"opened", "results", "empty"} else "sogou_wechat_" + outcome
        return {"receipt_ref": "discovery:" + uuid4().hex, "channel": "sogou_wechat", "action": action, "query": query,
                "parent_receipt_ref": parent, "observed_at": datetime.now(timezone.utc).isoformat(), "outcome": outcome,
                "observation": {"requested_url": observation.requested_url, "returned_url": returned, "final_url": observation.final_url,
                                "redirects": list(observation.redirects), "http_status": observation.status,
                                "title": title, "account_or_author": author, "published_at": published,
                                "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest() if body else None},
                "evidence_kind": "opened_article_body" if body else "result_snippet", "excerpt": excerpt, "limitation": limitation}

    def search(self, scope, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise ValueError("sogou_query_invalid")
        query = query.strip()
        with self._lock:
            now = self._clock()
            expired = {ref for ref, session in self._sessions.items() if now - session.created > 1200}
            self._sessions = {ref: session for ref, session in self._sessions.items() if ref not in expired}
            self._cards = {ref: value for ref, value in self._cards.items() if value[1] not in expired}
            address = "https://weixin.sogou.com/weixin?" + urlencode({"type": "2", "query": query})
            transport = self._session_factory()
            observation = transport.get(address)
            restriction = classify(observation)
            markup = _Markup(observation.body)
            if restriction is None and not markup.cards and not any(marker in observation.body for marker in ("没有找到", "暂无结果", "未找到", "news-list")):
                restriction = "unavailable"
            cards = []
            receipt = self._record(scope, self._receipt(query, "search", None, observation, restriction or ("results" if markup.cards else "empty")))
            if restriction is None:
                self._sessions[receipt["receipt_ref"]] = SearchSession(transport, query, address, receipt["receipt_ref"], self._clock())
                for card in markup.cards:
                    address = urljoin("https://weixin.sogou.com", html.unescape(card["url"]))
                    if not _site_url(address) or urlsplit(address).hostname != "weixin.sogou.com" or urlsplit(address).path != "/link":
                        continue
                    candidate_ref = "sogou-card:" + uuid4().hex
                    card_receipt = self._record(scope, self._receipt(query, "search", receipt["receipt_ref"], observation, "results", returned=address,
                        title=card["title"].strip() or None, author=card["account_or_author"].strip() or None, published=card["published_at"], excerpt=card["excerpt"].strip() or None))
                    self._cards[candidate_ref] = (scope, receipt["receipt_ref"], address, card_receipt["receipt_ref"])
                    cards.append({"candidate_ref": candidate_ref, "receipt": card_receipt})
            return {"receipt": receipt, "candidates": cards, "article_body": None}

    def open(self, scope, candidate_ref):
        with self._lock:
            card = self._cards.get(candidate_ref)
            if card is None or card[0] != scope:
                observation = HttpObservation("https://weixin.sogou.com/", None, (), None, "")
                return {"receipt": self._record(scope, self._receipt(None, "open", None, observation, "expired")), "candidates": [], "article_body": None}
            _, search_ref, address, parent = card
            session = self._sessions.get(search_ref)
            if session is None or self._clock() - session.created > 1200:
                observation = HttpObservation(address, None, (), None, "")
                return {"receipt": self._record(scope, self._receipt(None, "open", parent, observation, "expired", returned=address)), "candidates": [], "article_body": None}
            observation = session.transport.get(address, session.requested_url)
            restriction = classify(observation)
            if restriction is None and urlsplit(observation.final_url or "").hostname == "weixin.sogou.com":
                destination = fragment_destination(observation.body)
                if destination:
                    article = session.transport.get(destination, observation.final_url)
                    observation = HttpObservation(address, article.final_url, (*observation.redirects, observation.final_url, destination, *article.redirects), article.status, article.body, article.error)
                    restriction = classify(observation)
                else:
                    restriction = "unsupported_redirect"
            markup = _Markup(observation.body)
            body = markup.text("js_content")
            final = urlsplit(observation.final_url or "")
            if restriction is None and (final.hostname != "mp.weixin.qq.com" or final.path != "/s" or not body or len(body) < 40):
                restriction = "unavailable"
            if restriction:
                body = None
            receipt = self._record(scope, self._receipt(session.query, "open", parent, observation, restriction or "opened", returned=address,
                title=markup.text("activity-name") if body else None, author=markup.text("js_name") if body else None,
                published=markup.text("publish_time") if body else None, excerpt=body[:2000] if body else None, body=body))
            return {"receipt": receipt, "candidates": [], "article_body": body}


def sogou_operations(owner):
    from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation
    from meta_research.semantic_owner_gateway import _root_agent_acquisition_session_ref
    from meta_research.owners.common import OwnerConflict

    def invoke(context, arguments, action):
        if context.root_kind != "deepfetch":
            raise SemanticMcpError("sogou_scope_unauthorized")
        session_ref = _root_agent_acquisition_session_ref(owner, context, allow_suspended=False)
        session = owner.query_acquisition_session(session_ref=session_ref)
        if session is None or session.mode == "provided_only":
            raise SemanticMcpError("sogou_provided_only_denied")
        scope = context.run_ref + ":" + context.attempt_ref
        try:
            return getattr(owner.sogou_discovery, action)(scope, arguments["query" if action == "search" else "candidate_ref"])
        except (ValueError, OwnerConflict) as error:
            raise SemanticMcpError(str(error)) from error

    schema = discovery_output_schema()
    return tuple(SemanticOperation(name, "agent_runtime", description,
        lambda context, arguments, action=action: invoke(context, arguments, action),
        {"type": "object", "properties": {field: {"type": "string", "minLength": 1, "maxLength": 512}}, "required": [field], "additionalProperties": False}, schema)
        for name, action, field, description in (
            (SOGOU_OPERATION_IDS[0], "search", "query", "Search public WeChat Sogou as a peer discovery channel. Returns host receipts and selected-card refs. Restrictions are local limitations. Denied for provided_only."),
            (SOGOU_OPERATION_IDS[1], "open", "candidate_ref", "Open one returned Sogou card in its issuing session. Returns actual article body or honest captcha/login/expired limitation. Cannot fetch arbitrary URLs. Article body is a research lead, not independent paper reading.")))


def discovery_output_schema():
    from meta_research.skills.deepfetch_v4.scripts.ledger_contract import OBSERVATION_KEYS, RECEIPT_KEYS, OUTCOMES
    nullable = {"type": ["string", "null"]}
    observation = {"type": "object", "properties": {key: nullable for key in OBSERVATION_KEYS}, "required": sorted(OBSERVATION_KEYS), "additionalProperties": False}
    observation["properties"].update(redirects={"type": "array", "items": {"type": "string"}}, http_status={"type": ["integer", "null"]})
    observation["properties"]["requested_url"] = {"type": "string", "minLength": 1}
    receipt = {"type": "object", "properties": {key: nullable for key in RECEIPT_KEYS}, "required": sorted(RECEIPT_KEYS), "additionalProperties": False}
    receipt["properties"].update(observation=observation, outcome={"type": "string", "enum": sorted(OUTCOMES)}, channel={"type": "string", "enum": ["sogou_wechat"]}, action={"type": "string", "enum": ["search", "open"]}, evidence_kind={"type": "string", "enum": ["result_snippet", "opened_article_body"]})
    for field in ("receipt_ref", "observed_at"):
        receipt["properties"][field] = {"type": "string", "minLength": 1}
    candidate = {"type": "object", "properties": {"candidate_ref": {"type": "string"}, "receipt": receipt}, "required": ["candidate_ref", "receipt"], "additionalProperties": False}
    return {"type": "object", "properties": {"receipt": receipt, "candidates": {"type": "array", "items": candidate}, "article_body": nullable}, "required": ["receipt", "candidates", "article_body"], "additionalProperties": False}
