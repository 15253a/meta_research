"""schema 加载与校验器构造（schemas/ 目录的唯一程序入口）。

产物文件名 → schema 短名的映射也定义在此（Gate 逐文件校验的依据）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012

# 产物文件名 → schema 短名（信封 files 的键按此校验；不在表内的文件名 = 未知产物、当场拒）。
# 注意：resource_request.json（sidecar）不在此表——它非 Gate 产物（§6.11：schema 校验后经
# interaction_request_create 落请求单、不入研究库）；其 schema 经 validator("resource_request") 取。
ARTIFACT_SCHEMA_MAP: Dict[str, str] = {
    "idea_set.json": "idea_set",
    "idea_set.draft.json": "idea_set_draft",
    "idea_audit.json": "idea_audit",
    "plan.json": "plan",
    "plan_review.json": "plan_review",
    "bundle_target.json": "bundle_target",
    "answer.json": "answer",
    "tree_ops.json": "tree_ops",
    "selection.json": "selection",
}


class SchemaSet:
    def __init__(self, schemas_dir: Path):
        self.schemas_dir = schemas_dir
        self._schemas: Dict[str, dict] = {}
        resources = []
        for path in sorted(schemas_dir.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            name = path.name[: -len(".schema.json")]
            self._schemas[name] = schema
            resources.append((schema["$id"], DRAFT202012.create_resource(schema)))
        self._registry = Registry().with_resources(resources)
        self._validators: Dict[str, Draft202012Validator] = {}   # 实例内缓存（不用 lru_cache 挂方法：会按 self 长持实例）

    def validator(self, name: str) -> Draft202012Validator:
        if name not in self._validators:
            self._validators[name] = Draft202012Validator(self._schemas[name], registry=self._registry)
        return self._validators[name]

    def validator_for_artifact(self, filename: str) -> Draft202012Validator:
        name = ARTIFACT_SCHEMA_MAP.get(filename)
        if name is None:
            raise KeyError(f"未知产物文件名: {filename}")
        return self.validator(name)
