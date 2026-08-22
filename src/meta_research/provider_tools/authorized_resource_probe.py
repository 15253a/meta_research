#!/usr/bin/env python3
"""Conservatively verify an institution-authorized scholarly browser target.

The helper talks only to the loopback CDP bridge.  It returns compact booleans
and a target id; page text, cookies, storage, and credentials never leave the
browser process.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _loopback_proxy(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("proxy must be an unauthenticated loopback HTTP URL")
    return value.rstrip("/")


def _json_request(url: str, *, body: str | None = None) -> Any:
    request = urllib.request.Request(
        url,
        data=None if body is None else body.encode("utf-8"),
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read(2_000_000).decode("utf-8"))


def _authorization_expression(institution: str) -> str:
    """Build the in-browser entitlement check without exporting page text."""

    normalized_institution = " ".join(institution.casefold().split())
    return f"""(()=>{{
      const institution = {json.dumps(normalized_institution)};
      const text = String(document.body?.innerText || '').slice(0, 250000);
      const title = String(document.title || '').toLowerCase();
      const url = String(location.href || '').toLowerCase();
      const login = /(?:sign[ -]?in|log[ -]?in|single sign[ -]?on|carsi|shibboleth|openathens|统一身份认证|登录)/i.test(`${{url}} ${{title}} ${{text.slice(0, 8000)}}`);
      const scholarly = document.contentType === 'application/pdf' ||
        Boolean(document.querySelector('meta[name="citation_title"], meta[name="citation_doi"], meta[property="og:type"][content*="article" i], article'));
      const entitlement = /(?:access (?:is )?(?:provided|enabled|granted) (?:by|through|via)|institutional access (?:is )?(?:provided|enabled|granted) (?:by|through|via|for|to)|signed in (?:via|through|with)|licensed (?:to|through|via|by)|(?:访问|授权).{{0,24}}(?:由|通过).{{0,24}}(?:机构|大学|图书馆)|(?:机构|大学|图书馆).{{0,24}}(?:访问|授权).{{0,24}}(?:启用|提供|许可))/i;
      const contexts = text.split(/\\n+/)
        .map((value) => value.replace(/\\s+/g, ' ').trim())
        .filter((value) => value.length > 0 && value.length <= 1000);
      const institutionEntitlement = Boolean(
        institution && institution.length >= 3 && contexts.some((context) => {{
          const normalized = context.toLowerCase();
          return normalized.includes(institution) && entitlement.test(normalized);
        }})
      );
      return {{login, scholarly, institution: institutionEntitlement}};
    }})()"""


def _inspect(proxy: str, target_id: str, institution: str) -> dict[str, object]:
    query = urllib.parse.urlencode({"target": target_id})
    info = _json_request(f"{proxy}/info?{query}")
    if not isinstance(info, dict):
        raise ValueError("invalid target info")
    expression = _authorization_expression(institution)
    evaluated = _json_request(f"{proxy}/eval?{query}", body=expression)
    value = evaluated.get("value") if isinstance(evaluated, dict) else None
    if not isinstance(value, dict):
        raise ValueError("invalid target evaluation")
    return {
        "target_id": target_id,
        "login": bool(value.get("login")),
        "scholarly": bool(value.get("scholarly")),
        "institution": bool(value.get("institution")),
        "ready": info.get("ready") in {"complete", "interactive"},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default="http://127.0.0.1:3456")
    parser.add_argument("--target")
    parser.add_argument("--institution", default="")
    args = parser.parse_args(argv)
    try:
        proxy = _loopback_proxy(args.proxy)
        if args.target:
            target_ids = [args.target]
        else:
            targets = _json_request(f"{proxy}/targets")
            if not isinstance(targets, list):
                raise ValueError("invalid targets response")
            target_ids = [
                str(item["targetId"])
                for item in targets
                if isinstance(item, dict)
                and item.get("type", "page") == "page"
                and isinstance(item.get("targetId"), str)
                and item["targetId"]
            ]
        login_target: str | None = None
        for target_id in target_ids:
            observed = _inspect(proxy, target_id, args.institution)
            if (
                observed["ready"]
                and observed["scholarly"]
                and observed["institution"]
                and not observed["login"]
            ):
                print(json.dumps({"ok": True, "status": "verified", "targetId": target_id}))
                return 0
            if observed["login"] and login_target is None:
                login_target = target_id
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "login_required" if login_target else "unverified",
                    "targetId": login_target,
                }
            )
        )
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(json.dumps({"ok": False, "status": "unavailable", "error": type(error).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
