#!/usr/bin/env python3
"""WildIdea 联网搜索辅助脚本。

用法:
  python scripts/search_helper.py "搜索词"
  python scripts/search_helper.py "搜索词" --top 5
  python scripts/search_helper.py "搜索词" --fetch-first   # 同时抓取第一条结果的正文

默认从搜狗搜索提取结果（无需 API key，curl 直接可用）。
输出 JSON，供 WildIdea skill 的联网验证和随机组词使用。

设计原则:
  - 零依赖（只用 Python 标准库 + curl）
  - 搜狗作为默认引擎（Bing 302 跳转、百度反爬，搜狗最稳定）
  - 随机组词场景下，不要对结果做"合理性"过滤——搜到什么就用什么
"""
import argparse
import html as html_mod
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request


_SEARCH_URL = "https://www.sogou.com/web?query={query}"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _fetch(url: str, timeout: int = 10) -> str:
    """用 urllib 直接抓取，失败时回退到 curl。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    # 回退到 curl
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", str(timeout), "-H", f"User-Agent: {_UA}", url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return result.stdout
    except Exception:
        return ""


def search_sogou(query: str, top: int = 5) -> list[dict]:
    """搜狗搜索，返回 [{title, url, snippet}] 列表。"""
    url = _SEARCH_URL.format(query=urllib.parse.quote(query))
    raw = _fetch(url)
    if not raw:
        return []
    # 搜狗结果在 <h3> 里的 <a href="...">标题</a>
    # 摘要在 <p class="str_info"> 或 <div class="space-txt">
    results = []
    h3_blocks = re.findall(r'<h3[^>]*>(.*?)</h3>', raw, re.S)
    for block in h3_blocks[:top]:
        link = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        href = link.group(1)
        title = re.sub(r"<[^>]+>", "", link.group(2)).strip()
        title = html_mod.unescape(title)
        # 搜狗的链接可能是 /link?url=... 格式
        if href.startswith("/link?"):
            href = "https://www.sogou.com" + href
        results.append({"title": title, "url": href})
    # 尝试提取摘要
    snippets = re.findall(r'<p[^>]*class="[^"]*(?:str_info|space-txt)[^"]*"[^>]*>(.*?)</p>', raw, re.S)
    snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippets]
    for i, r in enumerate(results):
        if i < len(snippets):
            r["snippet"] = html_mod.unescape(snippets[i])[:200]
    return results


def fetch_page_text(url: str, max_chars: int = 3000) -> str:
    """抓取页面正文（去 HTML 标签后的纯文本）。"""
    raw = _fetch(url, timeout=15)
    if not raw:
        return ""
    # 去 script/style
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.S | re.I)
    # 去标签
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    # 压缩空白
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def main():
    parser = argparse.ArgumentParser(description="WildIdea 联网搜索（搜狗引擎，无需 API key）")
    parser.add_argument("query", help="搜索词")
    parser.add_argument("--top", type=int, default=5, help="返回前 N 条结果（默认 5）")
    parser.add_argument("--fetch-first", action="store_true", help="同时抓取第一条结果的正文")
    args = parser.parse_args()

    results = search_sogou(args.query, top=args.top)

    output = {
        "query": args.query,
        "engine": "sogou",
        "status": "found" if results else "no_result",
        "hits": results,
    }

    if args.fetch_first and results:
        first_url = results[0]["url"]
        text = fetch_page_text(first_url)
        output["first_page"] = {
            "url": first_url,
            "text_preview": text[:1000] if text else "(抓取失败或页面无文本内容)",
        }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
