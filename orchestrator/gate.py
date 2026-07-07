"""StubGate —— 三级校验 + 门禁的 M0 桩（《第二部分》§6.10 替换矩阵：M1 换真、签名不变）。

M0 真做的两级：① JSON schema（逐文件按 ARTIFACT_SCHEMA_MAP）② 引用完整性（产物内
引用的 id 在本轮上下文里真实存在——metric_result_id / question_id / evaluation_id 等）。
③ 业务门禁（不变量 I1–I6）**放过**（M0 只验流程契约、不验不变量，第三部分 §7.1 M0 行），
但拒绝行为的接缝保留：返回 CommitResult，拒因清单化，M1 换真时只改第三级。

commit 的副作用 = 把产物 JSON + md 落到轮目录 artifacts/（staging 写入后原子改名——
**逐文件原子**（§5.4）；阶段级原子提交（§4.2.5 phase_commit 单事务）是 M1 的事，M0
崩溃可能留半套文件、由重跑覆盖），并登记进内存 ArtifactIndex 供引用完整性检查与上下文
编译。Codex 永不直接写这里——写入执行者是驱动器进程内的本 Gate（M0 单进程即"daemon 侧"）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .interfaces import Artifact, CommitResult, Stage, ValidationResult
from .schemas import ARTIFACT_SCHEMA_MAP, SchemaSet


class ArtifactIndex:
    """本进程内的产物登记（M0 内存版；M1 起由 DB 行替代）。

    键 = (cycle_id, stage, target_id, filename)；另维护证据可引用的 metric_result_id 集
    与产物级 evaluation_id 集（引用完整性第②级的数据源）。
    """

    def __init__(self):
        self.records: Dict[tuple, Dict[str, Any]] = {}
        self.metric_result_ids: set = set()
        self.evaluation_ids: set = set()

    def register(self, *, cycle_id: str, stage: Stage, target_id: Optional[str],
                 filename: str, payload: Dict[str, Any]) -> None:
        self.records[(cycle_id, stage, target_id, filename)] = payload
        if filename == "bundle_target.json":
            for mr in payload.get("metric_results") or []:
                self.metric_result_ids.add(mr["metric_result_id"])
            ev = payload.get("evaluation")
            if ev:
                self.evaluation_ids.add(f"eval:{ev['eval_key']}@{ev['protocol_name']}@{ev['protocol_ver']}")

    def get(self, cycle_id: str, stage: Stage, filename: str,
            target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self.records.get((cycle_id, stage, target_id, filename))


class StubGate:
    def __init__(self, schema_set: SchemaSet, index: ArtifactIndex,
                 statestore, artifacts_root: Path):
        self.schemas = schema_set
        self.index = index
        self.state = statestore
        self.artifacts_root = artifacts_root

    # -- Gate Protocol ---------------------------------------------------------
    def preview(self, artifact: Artifact) -> ValidationResult:
        errors = self._level1_schema(artifact) or self._level2_refs(artifact)
        return ValidationResult(ok=not errors, errors=errors)

    def commit(self, artifact: Artifact, *, cycle_id: str, stage: Stage,
               target_id: Optional[str] = None) -> CommitResult:
        errors = self._level1_schema(artifact)
        if not errors:
            errors = self._level2_refs(artifact, target_id)
        # 第③级 业务门禁：M0 放过（M1 落 I1–I6；接缝在此）
        if errors:
            self.state._record("gate", "reject", {"cycle": cycle_id, "stage": stage, "errors": errors[:5]})
            return CommitResult(ok=False, errors=errors)
        committed = []
        cycle_dir = self.artifacts_root / f"cycles/{cycle_id}/artifacts"
        for filename, payload in artifact.files.items():
            self._atomic_write_json(cycle_dir, self._fs_name(filename, target_id), payload)
            self.index.register(cycle_id=cycle_id, stage=stage, target_id=target_id,
                                filename=filename, payload=payload)
            committed.append(filename)
        if artifact.md:
            self._atomic_write_text(cycle_dir, self._fs_name(f"{stage}.md", target_id), artifact.md)
        return CommitResult(ok=True, committed_files=committed)

    # -- 三级校验 ---------------------------------------------------------------
    def _level1_schema(self, artifact: Artifact) -> List[str]:
        errors: List[str] = []
        if not artifact.files:
            return ["产物为空（files 无内容）"]
        for filename, payload in artifact.files.items():
            if filename == "resource_request.json":
                errors.append("resource_request.json: sidecar 非 Gate 产物（§6.11）——经 "
                              "interaction_request_create 落请求单，不入研究库；驱动器须在 commit 前摘出")
                continue
            if filename not in ARTIFACT_SCHEMA_MAP:
                errors.append(f"{filename}: 未知产物文件名")
                continue
            v = self.schemas.validator_for_artifact(filename)
            for e in v.iter_errors(payload):
                errors.append(f"{filename}: {e.json_path} {e.message}")
                # 展平 oneOf/anyOf 的子错误：顶层「不 valid under any …」不含具体键名，
                # 工人拿不到可行动反馈会反复撞同一堵墙（重试收敛性的关键）
                for sub in self._iter_context(e, depth=0):
                    errors.append(f"{filename}: {sub.json_path} {sub.message}")
        return errors

    @staticmethod
    def _iter_context(err, depth: int):
        if depth > 3:
            return
        for sub in err.context or []:
            yield sub
            yield from StubGate._iter_context(sub, depth + 1)

    def _level2_refs(self, artifact: Artifact, target_id: Optional[str] = None) -> List[str]:
        """引用完整性（M0 范围）：产物所引 id 在状态 / 产物索引 / 同批 local_key 中真实存在。"""
        errors: List[str] = []
        local_keys = self._batch_local_keys(artifact)

        def known_q(qid: Optional[str]) -> bool:
            return qid is not None and (qid in self.state.questions or qid in local_keys)

        for filename, payload in artifact.files.items():
            if filename == "answer.json":
                if payload["question_id"] not in self.state.questions:
                    errors.append(f"answer.question_id 不存在: {payload['question_id']}")
                for ev in payload["evidence"]:
                    mrid = ev.get("metric_result_id")
                    if mrid and mrid not in self.index.metric_result_ids:
                        errors.append(f"evidence.metric_result_id 无成功测量对应: {mrid}")
                    cq = ev.get("child_question_id")
                    if cq and cq not in self.state.questions:
                        errors.append(f"evidence.child_question_id 不存在: {cq}")
            elif filename == "tree_ops.json":
                for op in payload["ops"]:
                    pid = op.get("parent_question_id")
                    if pid and pid not in self.state.questions:
                        errors.append(f"tree_ops.{op['op']}.parent_question_id 不存在: {pid}")
                    if op.get("op") == "propose_prune" and op["question_id"] not in self.state.questions:
                        errors.append(f"propose_prune.question_id 不存在: {op['question_id']}")
                    if op.get("op") == "mark_answer_applicability":
                        if op["answer_id"] not in self.state.answers:
                            errors.append(f"mark_answer_applicability.answer_id 不存在: {op['answer_id']}")
                        sq = op.get("spawned_question_ref")
                        if sq and not known_q(sq):
                            errors.append(f"spawned_question_ref 既非既有问题也非同批 local_key: {sq}")
                    if op.get("op") == "seed_applicability_audit":
                        for aid in op.get("answer_ids", []):
                            if aid not in self.state.answers:
                                errors.append(f"seed_applicability_audit 引用悬空 answer: {aid}")
            elif filename == "selection.json":
                qid = payload.get("next_question_id")
                if qid is not None and not known_q(qid):
                    errors.append(f"selection.next_question_id 既非既有问题也非同批 local_key: {qid}")
                for row in payload.get("scores", []):
                    if not known_q(row.get("question_id")):
                        errors.append(f"selection.scores 引用悬空问题: {row.get('question_id')}")
            elif filename == "plan.json":
                for t in payload.get("targets", []):
                    ev_id = t.get("evaluation_id")
                    if ev_id and ev_id not in self.index.evaluation_ids:
                        errors.append(f"plan.targets[{t['target_key']}].evaluation_id 无既有格子: {ev_id}")
                for r in payload.get("reuse_evidence", []):
                    if r.get("evaluation_id") and r["evaluation_id"] not in self.index.evaluation_ids:
                        errors.append(f"reuse_evidence.evaluation_id 无既有格子: {r['evaluation_id']}")
                    if r.get("metric_result_id") and r["metric_result_id"] not in self.index.metric_result_ids:
                        errors.append(f"reuse_evidence.metric_result_id 无成功测量: {r['metric_result_id']}")
                    if r.get("answer_id") and r["answer_id"] not in self.state.answers:
                        errors.append(f"reuse_evidence.answer_id 不存在: {r['answer_id']}")
            elif filename == "bundle_target.json":
                if target_id is not None and payload.get("target_key") != target_id:
                    errors.append(f"bundle_target.target_key（{payload.get('target_key')}）"
                                  f"与 commit target_id（{target_id}）不一致")
        return errors

    @staticmethod
    def _batch_local_keys(artifact: Artifact) -> set:
        keys = set()
        ops = (artifact.files.get("tree_ops.json") or {}).get("ops", [])
        for op in ops:
            if op.get("local_key"):
                keys.add(op["local_key"])
            for ch in op.get("children", []) or []:
                if ch.get("local_key"):
                    keys.add(ch["local_key"])
        return keys

    # -- 落盘（staging → 原子改名，§5.4） ---------------------------------------
    @staticmethod
    def _fs_name(filename: str, target_id: Optional[str]) -> str:
        return f"{target_id}.{filename}" if target_id else filename

    @staticmethod
    def _atomic_write_json(dir_: Path, name: str, payload: Any) -> None:
        dir_.mkdir(parents=True, exist_ok=True)
        tmp = dir_ / (name + ".staging")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, dir_ / name)

    @staticmethod
    def _atomic_write_text(dir_: Path, name: str, text: str) -> None:
        dir_.mkdir(parents=True, exist_ok=True)
        tmp = dir_ / (name + ".staging")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, dir_ / name)
