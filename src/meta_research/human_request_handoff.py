"""Human-readable handoff built solely from one recorded HumanRequest revision."""
from __future__ import annotations

from typing import Any
from html import escape
import re
from io import BytesIO


def build_human_request_handoff(request: dict[str, Any]) -> dict[str, Any]:
    target = request["target_assertion"]
    condition = target.get("help_context", target.get("condition", {})) if isinstance(target, dict) else {}
    if not isinstance(condition, dict):
        condition = {}

    def recorded_text(key: str, missing: str) -> str:
        value = condition.get(key)
        return value if isinstance(value, str) and value.strip() else missing

    attempts = condition.get("attempted_work")
    if isinstance(attempts, list) and not attempts:
        attempted_work = ["发起时明确记录：尚未开展尝试。"]
    elif isinstance(attempts, list) and all(
        isinstance(attempt, dict)
        and all(isinstance(attempt.get(key), str) for key in ("action", "result"))
        for attempt in attempts
    ):
        attempted_work = []
        for number, attempt in enumerate(attempts, 1):
            attempted_work.extend([f"尝试 {number}：{attempt['action']}", f"结果：{attempt['result']}"])
    else:
        attempted_work = ["发起时未单独记录已尝试工作及结果；请依据原研究说明核对，不能视为已经尝试。"]

    system_help = request["kind"] == "system_operation_help"
    capability_authorization = request["kind"] == "capability_authorization"
    if system_help:
        return_instructions = [
            f"本材料绑定请求 {request['request_ref']}（修订 {request['revision']}）。",
            "请回到本求助页面确认当前修订，然后点击“重试”以恢复当前精确操作；无需填写答复表单或上传材料。",
            "生成、预览和下载材料不会执行重试；无需启动研究助手。",
        ]
    elif capability_authorization:
        return_instructions = [
            f"本材料绑定请求 {request['request_ref']}（修订 {request['revision']}）。",
            "请回到本求助页面确认当前修订和精确授权范围，点击“接受”提交本次授权，或选择“拒绝”“稍后处理”。",
            "生成、预览和下载材料不会提交授权；无需启动研究助手。",
        ]
    else:
        return_instructions = [
            recorded_text("safe_response", "请在 Meta Research 的本求助页面填写处理说明，并按需附上材料。"),
            f"本材料绑定请求 {request['request_ref']}（修订 {request['revision']}）。",
            "请回到本求助页面确认当前修订，选择实际要交回的说明、事实或材料后正式提交；也可以明确拒绝或暂缓。",
            "生成、预览和下载材料不会提交答复；无需启动研究助手，助手产物也须由人选择后正式提交。",
        ]
    if not request["current"]:
        return_instructions.insert(0, "这是历史请求修订，请查看当前请求后再重试。" if system_help else "这是历史请求修订，请查看当前请求后再决定正式回应。")

    sections = [
        ("background", "背景与研究目的", [recorded_text("background", "发起时未单独记录背景。"), "研究目的：" + request["business_purpose"]]),
        ("attempted_work", "已尝试工作及结果", attempted_work),
        ("problem", "当前困难或缺少条件", [recorded_text("problem", "发起时未单独记录当前困难或缺少条件。")]),
        ("requested_delivery", "需要您提供什么", [recorded_text("requested_delivery", request["obligation"]), "验收条件：", *request["acceptance_conditions"]]),
        ("impact", "对后续研究的影响", [recorded_text("impact", "发起时未单独记录后续影响。")]),
        ("return_instructions", "如何重试当前操作" if system_help else "如何处理本次授权" if capability_authorization else "如何交回回应", return_instructions),
    ]
    return {
        "schema_ref": "meta-research/human-request-handoff/v1",
        "request_ref": request["request_ref"], "revision": request["revision"],
        "is_current": request["current"], "status": request["status"],
        "title": request["obligation"],
        "sections": [{"key": key, "title": title, "paragraphs": paragraphs} for key, title, paragraphs in sections],
    }


def handoff_markdown(handoff: dict[str, Any]) -> bytes:
    def literal(value: str) -> str:
        return re.sub(r"([\\`*_{}\[\]<>#+.!|\-])", r"\\\1", value).replace("\n", "  \n")

    parts = ["# " + literal(handoff["title"])]
    for section in handoff["sections"]:
        parts.extend(["## " + literal(section["title"]), *[literal(value) for value in section["paragraphs"]]])
    return ("\n\n".join(parts) + "\n").encode("utf-8")


def handoff_html(handoff: dict[str, Any]) -> bytes:
    sections = []
    for section in handoff["sections"]:
        paragraphs = "".join("<p>" + escape(value) + "</p>" for value in section["paragraphs"])
        sections.append("<section><h2>" + escape(section["title"]) + "</h2>" + paragraphs + "</section>")
    document = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + escape(handoff["title"]) + "</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 24px;line-height:1.65}"
        "h1,h2,p{white-space:pre-wrap;overflow-wrap:anywhere}h2{margin-top:1.7em;font-size:1.25em}"
        "@media print{body{margin:0;max-width:none}}</style></head><body><main><h1>"
        + escape(handoff["title"]) + "</h1>" + "".join(sections) + "</main></body></html>"
    )
    return document.encode("utf-8")


def handoff_pdf(handoff: dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    # The predefined Unicode CID mapping supports Chinese without depending on
    # a font installed on the service host. Text remains extractable as Unicode.
    font_name = "STSong-Light"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
    body_style = ParagraphStyle("handoff-body", fontName=font_name, fontSize=10.5, leading=17, wordWrap="CJK", spaceAfter=8)
    heading_style = ParagraphStyle("handoff-heading", parent=body_style, fontSize=13, leading=21, spaceBefore=10, keepWithNext=True)
    title_style = ParagraphStyle("handoff-title", parent=body_style, fontSize=18, leading=27, spaceAfter=16)

    def paragraph(value: str, style: ParagraphStyle) -> Paragraph:
        return Paragraph(escape(value).replace("\n", "<br/>"), style)

    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, title=handoff["title"], author="Meta Research",
        leftMargin=44, rightMargin=44, topMargin=44, bottomMargin=44,
    )
    content = [paragraph(handoff["title"], title_style)]
    for section in handoff["sections"]:
        content.append(paragraph(section["title"], heading_style))
        content.extend(paragraph(value, body_style) for value in section["paragraphs"])
        content.append(Spacer(1, 4))
    document.build(content)
    return output.getvalue()
