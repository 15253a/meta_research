"""Research-question admission contract shared by both StateStore implementations.

A question is a research object, not a queue item for repository or deployment work.
Every admitted question therefore carries an ``evidence_closure_v1`` predicate whose
evidence vocabulary is exactly the one accepted by ``answer.schema.json``.  The frozen
DDL already provides ``question.predicate_json``; callers must not create another table
or smuggle this contract into free-form text.

The legacy fallback is deliberate.  Older trusted callers only supplied ``text``.  We
materialise a conservative all-evidence contract for those calls so existing durable
runs remain resumable, while new reasoning output is instructed to provide an explicit
contract.  Engineering-task rejection applies to both explicit and legacy calls.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Optional, Tuple


QUESTION_CONTRACT_KIND = "evidence_closure_v1"
ALLOWED_QUESTION_EVIDENCE = (
    "evaluation",
    "literature",
    "child_answer",
    "human",
)

_CONTRACT_KEYS = {
    "kind",
    "allowed_evidence",
    "answer_criterion_md",
    "refute_criterion_md",
}

# These expressions intentionally target *operational imperatives*, not words such as
# “environment” or “deployment” in isolation: both may be legitimate research factors.
# The rejection reason is stable and audit/test friendly; prompts carry the richer rule.
_ENGINEERING_TASK_PATTERNS = (
    (
        "directory_inventory",
        re.compile(
            r"(?:盘点|清点|列出|罗列|扫描|遍历|查看|检查|整理).{0,12}"
            r"(?:目录|文件夹|文件(?:清单)?|路径清单|仓库结构|代码结构|资产清单)"
            r"|(?:目录|文件夹|文件|仓库结构|资产).{0,12}"
            r"(?:有哪些|是什么|清单|盘点|缺失|缺少|需要补齐|是否齐全)"
            r"|(?:缺失|缺少|补齐).{0,12}(?:目录|文件夹|文件|资产)",
            re.IGNORECASE,
        ),
    ),
    (
        "code_or_error_repair",
        re.compile(
            r"(?:修复|修正|修改|调试|排查|解决|处理).{0,16}"
            r"(?:代码|脚本|程序|bug|缺陷|报错|错误|异常|崩溃|构建失败|运行失败)"
            r"|(?:代码|脚本|程序|bug|报错|错误|异常).{0,16}(?:怎么修|如何修|修复|调试|排查)"
            r"|(?:报错|错误|异常|崩溃).{0,12}(?:原因|为什么|为何|是什么|如何处理|怎么处理)",
            re.IGNORECASE,
        ),
    ),
    (
        "deployment_or_environment",
        re.compile(
            r"(?:部署|上线|发布|重启|启动|停止).{0,12}(?:服务|系统|网站|应用|容器|进程)"
            r"|(?:如何|怎么|怎样).{0,8}(?:部署|上线|发布)"
            r"|(?:部署|上线|发布).{0,12}(?:是否|有没有|能否).{0,8}(?:成功|完成|可用)"
            r"|(?:配置|搭建|修复|准备|更新|切换|排查).{0,12}"
            r"(?:运行环境|开发环境|部署环境|权限|依赖|驱动|服务器|网络|端口|GPU|CUDA)"
            r"|(?:运行环境|开发环境|部署环境|权限|依赖|驱动).{0,12}"
            r"(?:是否|有没有).{0,8}(?:配置|安装|正确|齐全|可用)"
            r"|(?:安装|升级|降级|卸载).{0,12}(?:依赖|软件|包|驱动|CUDA)",
            re.IGNORECASE,
        ),
    ),
    (
        "filesystem_operation",
        re.compile(
            r"(?:创建|删除|移动|复制|重命名|清理).{0,12}(?:目录|文件夹|文件|路径)",
            re.IGNORECASE,
        ),
    ),
    (
        "directory_inventory",
        re.compile(
            r"\b(?:inventory|list|scan|walk|inspect)\b.{0,40}"
            r"\b(?:director(?:y|ies)|folders?|files?|file\s+tree|repository\s+tree|asset\s+list)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "code_or_error_repair",
        re.compile(
            r"\b(?:fix|patch|debug|troubleshoot|resolve|handle)\b.{0,40}"
            r"\b(?:code|scripts?|bugs?|errors?|exceptions?|crashes?|build\s+failure)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "deployment_or_environment",
        re.compile(
            r"\b(?:deploy|restart|launch|stop)\b.{0,40}\b(?:service|system|app|container|process)\b"
            r"|\bhow\s+to\s+(?:deploy|release|launch)\b"
            r"|\b(?:configure|set\s*up|repair)\b.{0,40}"
            r"\b(?:environment|permissions?|dependencies|drivers?|server|network|ports?|cuda)\b"
            r"|\b(?:install|upgrade|downgrade|uninstall)\b.{0,40}"
            r"\b(?:dependencies|packages?|drivers?|cuda)\b",
            re.IGNORECASE,
        ),
    ),
)


class QuestionAdmissionError(ValueError):
    """The proposed node is not an admissible evidence-closeable research question."""


def _nonempty_text(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise QuestionAdmissionError(f"{field} 须为非空字符串")
    normalized = " ".join(value.split())
    if not normalized:
        raise QuestionAdmissionError(f"{field} 须为非空字符串")
    if len(normalized) > max_length:
        raise QuestionAdmissionError(f"{field} 超过 {max_length} 字符")
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
        raise QuestionAdmissionError(f"{field} 含控制字符")
    return normalized


def _reject_engineering_task(text: str) -> None:
    for category, pattern in _ENGINEERING_TASK_PATTERNS:
        if pattern.search(text):
            raise QuestionAdmissionError(
                "question 准入拒绝工程任务"
                f"（{category}）：目录盘点、修代码/处理报错、部署/环境工作应留在对应工程阶段，"
                "不得进入研究问题树"
            )


def _legacy_contract(text: str) -> Dict[str, Any]:
    # This is intentionally explicit in storage; ``NULL`` would erase the distinction
    # between a resumable legacy call and an admitted evidence contract.
    return {
        "kind": QUESTION_CONTRACT_KIND,
        "allowed_evidence": list(ALLOWED_QUESTION_EVIDENCE),
        "answer_criterion_md": f"至少一条允许证据支持对“{text}”的肯定回答。",
        "refute_criterion_md": f"至少一条允许证据支持对“{text}”的否定回答。",
    }


def normalize_question_contract(
    text: Any,
    predicate_json: Optional[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any], str]:
    """Validate a question and return ``(normalised_text, contract, source)``.

    ``source`` is ``explicit`` or ``legacy_default`` and belongs in the admission
    decision.  The returned dict is canonicalisable JSON and safe for
    ``question.predicate_json``.
    """

    normalized_text = _nonempty_text(text, "question.text", max_length=4096)
    _reject_engineering_task(normalized_text)

    source = "explicit"
    if predicate_json is None:
        source = "legacy_default"
        contract = _legacy_contract(normalized_text)
    elif not isinstance(predicate_json, dict):
        raise QuestionAdmissionError("question.predicate_json 须为 object")
    else:
        contract = dict(predicate_json)

    missing = _CONTRACT_KEYS - set(contract)
    extra = set(contract) - _CONTRACT_KEYS
    if missing:
        raise QuestionAdmissionError(
            f"question.predicate_json 缺 evidence closure 字段: {sorted(missing)}")
    if extra:
        raise QuestionAdmissionError(
            f"question.predicate_json 含未知字段: {sorted(extra)}")
    if contract.get("kind") != QUESTION_CONTRACT_KIND:
        raise QuestionAdmissionError(
            f"question.predicate_json.kind 必须为 {QUESTION_CONTRACT_KIND}")

    evidence = contract.get("allowed_evidence")
    if not isinstance(evidence, list) or not evidence:
        raise QuestionAdmissionError("question.allowed_evidence 须为非空数组")
    if any(not isinstance(kind, str) or kind not in ALLOWED_QUESTION_EVIDENCE
           for kind in evidence):
        raise QuestionAdmissionError(
            "question.allowed_evidence 只能使用 answer evidence 四类："
            + "/".join(ALLOWED_QUESTION_EVIDENCE))
    if len(set(evidence)) != len(evidence):
        raise QuestionAdmissionError("question.allowed_evidence 不得重复")

    answer_criterion = _nonempty_text(
        contract.get("answer_criterion_md"),
        "question.answer_criterion_md",
        max_length=4096,
    )
    refute_criterion = _nonempty_text(
        contract.get("refute_criterion_md"),
        "question.refute_criterion_md",
        max_length=4096,
    )
    normalized_contract = {
        "kind": QUESTION_CONTRACT_KIND,
        "allowed_evidence": list(evidence),
        "answer_criterion_md": answer_criterion,
        "refute_criterion_md": refute_criterion,
    }
    return normalized_text, normalized_contract, source


def admission_payload(
    *,
    qid: str,
    operation: str,
    text: str,
    contract: Dict[str, Any],
    contract_source: str,
) -> Dict[str, Any]:
    """Build the durable, content-addressable admission audit payload."""

    canonical = json.dumps(
        {"text": text, "predicate_json": contract},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "qid": qid,
        "operation": operation,
        "contract_source": contract_source,
        "contract_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "predicate_json": contract,
    }
