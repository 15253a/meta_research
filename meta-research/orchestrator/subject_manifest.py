"""subject manifest 确定性构造（§4.1.4 附注——双评审 DECISION 机械判据的 subject_hash 唯一定义）。

**由编排器确定性构造（judge 不自算）**：条目数组 `[{kind, ref, content_hash}, …]`，canonical JSON
（键排序、条目按 ref 排序）→ `subject_hash = sha256(canonical_json)`。产物集构成（规则=不引用未提交 DB 行）：
- **code_review** = plan 计划切片 hash + worktree 代码 diff hash + config/overrides 文件 hashes
  + identity 草稿 hash + smoke transcript ref+hash；
- **result_review** = 评估结果规范 artifact（fold+aggregate 指标值文件）hash + checkpoint content_hash(es)
  + 运行日志 staging 文件 ref+hash 集 + parser 观测 staging 文件 hash + identity 草稿 hash（eval 无则省）。

通过判据（gate_exec.review_passed 消费）= 存在 verdict=pass 的对应 DECISION 且 ① payload.subject_hash ==
编排器**当下重算**（本模块算出）② runner_call(success/audit/对应 purpose)。修复后产物变 → hash 变 → 旧 pass 自动失效。

hash 皆调用方先算好传入（本模块不读文件——保持纯函数、可测、与文件系统解耦；真流水在 CP5.4 编排器侧算 hash）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional


def subject_hash(entries: List[Dict[str, Any]]) -> str:
    """canonical JSON（条目按 (kind, ref) 排序、键排序、无空白差异）→ sha256。
    条目最小形态 {kind, ref, content_hash}；缺任一键 → ValueError（防静默漏项致 hash 不可比）。
    排序键取 (kind, ref)（附注原文「按 ref 排序」——加 kind 作首键消除同 ref 跨 kind 的并列歧义；
    构造与核验共用本函数，口径自洽）。"""
    for e in entries:
        missing = [k for k in ("kind", "ref", "content_hash") if not e.get(k)]
        if missing:
            raise ValueError(f"subject manifest 条目缺键 {missing}: {e!r}")
    canon = sorted(({"kind": e["kind"], "ref": e["ref"], "content_hash": e["content_hash"]} for e in entries),
                   key=lambda x: (x["kind"], x["ref"]))
    return hashlib.sha256(json.dumps(canon, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def code_review_manifest(*, plan_slice_hash: str, code_diff_hash: str,
                         config_hashes: Dict[str, str], identity_draft_hash: str,
                         smoke_transcript_ref: str, smoke_transcript_hash: str) -> List[Dict[str, str]]:
    """code_review 产物集配方（§4.1.4 附注）。config_hashes: {文件 ref → hash}（可空 dict）。"""
    entries = [
        {"kind": "plan_slice", "ref": "plan_slice", "content_hash": plan_slice_hash},
        {"kind": "code_diff", "ref": "worktree_diff", "content_hash": code_diff_hash},
        {"kind": "identity_draft", "ref": "identity_draft", "content_hash": identity_draft_hash},
        {"kind": "smoke_transcript", "ref": smoke_transcript_ref, "content_hash": smoke_transcript_hash},
    ]
    entries += [{"kind": "config", "ref": ref, "content_hash": h} for ref, h in config_hashes.items()]
    return entries


def result_review_manifest(*, metrics_artifact_hash: str, checkpoint_hashes: Dict[str, str],
                           run_log_hashes: Dict[str, str], parser_obs_hash: str,
                           identity_draft_hash: Optional[str] = None) -> List[Dict[str, str]]:
    """result_review 产物集配方（§4.1.4 附注）。checkpoint_hashes: {ckpt ref → content_hash}——**须 ≥1**
    （附注：build/exec 为本 target 新产、eval 为既有 legal checkpoint 的已提交 hash；checkpoint 是「可评
    target」本体，空集意味着换 checkpoint 不改 subject_hash → 篡改不可见，拒）；
    run_log_hashes: {log ref → hash}；identity_draft_hash 可省（eval 目标无 identity）。"""
    if not checkpoint_hashes:
        raise ValueError("result_review manifest 须 ≥1 checkpoint content_hash（附注；空集=checkpoint 不在防篡面）")
    entries = [
        {"kind": "metrics_artifact", "ref": "metrics_artifact", "content_hash": metrics_artifact_hash},
        {"kind": "parser_observation", "ref": "parser_observation", "content_hash": parser_obs_hash},
    ]
    entries += [{"kind": "checkpoint", "ref": ref, "content_hash": h} for ref, h in checkpoint_hashes.items()]
    entries += [{"kind": "run_log", "ref": ref, "content_hash": h} for ref, h in run_log_hashes.items()]
    if identity_draft_hash:
        entries.append({"kind": "identity_draft", "ref": "identity_draft", "content_hash": identity_draft_hash})
    return entries
