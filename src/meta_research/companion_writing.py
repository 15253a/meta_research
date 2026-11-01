"""Writing is an ordinary Companion request, with exact selected inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meta_research.owners.common import OwnerConflict, canonical_hash


class WritingMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_ref: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    role: Literal["source", "draft", "feedback", "document_template", "presentation_template", "dependency"]


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["current", "configured_api"] = "current"
    config_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def configuration(self):
        if (self.kind == "configured_api") != (self.config_ref is not None):
            raise ValueError("content_model_configuration_required")
        return self


class WritingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_type: Literal["report", "paper", "presentation"] = "report"
    include_presentation: bool = False
    task_scope: Literal["full_document", "outline", "revision"] = "full_document"
    requirements: str = Field(min_length=1, max_length=12000)
    materials: list[WritingMaterial] = Field(default_factory=list, max_length=100)
    model: ContentModel = Field(default_factory=ContentModel)
    output_directory: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def relative_output(self):
        if self.output_directory is not None:
            path = PurePosixPath(self.output_directory)
            if (not self.output_directory or path.is_absolute() or ".." in path.parts
                    or "\\" in self.output_directory or ":" in self.output_directory
                    or any(part.startswith(".") for part in path.parts)):
                raise ValueError("writing_output_directory_invalid")
        return self


def _selected_material_path(memory, asset, workspace_root: Path, input_key: str) -> Path:
    """Export explicit exact selections using RM's existing verified byte copy."""
    root = workspace_root.resolve()
    description = memory.describe_asset_export(asset.version_ref)
    destination = (root / "writing-inputs" / canonical_hash({"request": input_key})
                   / canonical_hash({"version_ref": asset.version_ref}) / description.file_name)
    if not destination.resolve().is_relative_to(root):
        raise OwnerConflict("writing_input_path_invalid")
    # Refuse symlink aliases, including aliases to another file inside the
    # workspace: an input copy must not overwrite or impersonate earlier work.
    if any(path.is_symlink() for path in (destination, *destination.parents) if path != root):
        raise OwnerConflict("writing_input_path_invalid")
    try:
        return memory.export_asset(asset.version_ref, destination).path
    except OwnerConflict as error:
        if error.code != "asset_export_destination_exists":
            raise
    # A lost enqueue can retry after the export completed. Reuse only the exact
    # exported content; edited copies remain untouched and are reported.
    try:
        expected = set()
        for entry in description.entries:
            path = destination / entry.path if description.kind == "directory" else destination
            expected.add(path)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != entry.size:
                raise ValueError()
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry.sha256:
                    raise ValueError()
        if description.kind == "directory":
            if any(path.is_symlink() for path in destination.rglob("*")):
                raise ValueError()
            if {path for path in destination.rglob("*") if path.is_file()} != expected:
                raise ValueError()
    except (OSError, ValueError):
        raise OwnerConflict("writing_input_changed") from None
    return destination


def writing_message(request: WritingRequest, message: str, memory, *, science=None,
                    workspace_root: Path | None = None, input_key: str | None = None) -> str:
    # This is the same authenticated user's accepted-asset read boundary used
    # by the asset browser. Page selection does not narrow their asset access.
    materials = []
    for selected in request.materials:
        asset = memory.query_asset_version(selected.version_ref)
        if asset is None or asset.content_hash != selected.content_hash:
            raise OwnerConflict("writing_material_version_unavailable")
        memory.verify_asset_binding(asset_ref=asset.asset_ref,
            version_ref=asset.version_ref, content_hash=asset.content_hash,
            manifest_hash=asset.manifest_hash, receipt=asset.receipt)
        if workspace_root is None:
            raise OwnerConflict("writing_workspace_unavailable")
        path = _selected_material_path(memory, asset, workspace_root,
            input_key or canonical_hash({"task": request.model_dump(), "message": message}))
        materials.append({**selected.model_dump(), "asset_ref": asset.asset_ref,
            "media_type": asset.media_type, "manifest_hash": asset.manifest_hash,
            "path": str(path), "source_receipt": asset.receipt.as_public_dict()})
    task = request.model_dump()
    task["materials"] = materials
    task["scientific_context"] = science
    task["output_directory"] = request.output_directory or "deliverables"
    return (message + "\n\n写作任务（普通助手会话）：\n"
        + json.dumps(task, ensure_ascii=False, sort_keys=True)
        + "\n使用共用 writing-report/SKILL.md。保留以上完整要求及准确版本；"
          "所选模板、前稿及依赖先实际读取，不可用时说明缺项。"
          "所选材料的 path 是本次准确版本的原生文件入口，修改时另存到输出目录，保留输入副本。"
          "结合本次 scientific_context 的科学结论、结果、反证和限制；"
          "所需完整研究材料通过准确 reader 按需读取。"
          "在本 Quest 持久助手目录内制作文件，交付源文件和必要依赖；正式成品安排独立审阅、"
          "修订、真实渲染和视觉检查。提纲或局部修改只检查所需范围。"
          "独立写作交付现有证据支撑的版本，实验建议另列，不向 Cycle 派工。"
          "current 使用当前助手模型；configured_api 必须调用 "
          "assistant.content.generate，传所选 config_ref 及 messages:[{role,content}]；"
          "消息仅含本任务所需且获授权的相关材料。"
          "配置和凭据沿受限环境提供；调用失败如实报告，不能改用当前模型代写。"
          "外部返回文本仍须由助手核对、修订并制作文件。正文采用正常学术引用，内部来源、"
          "计算和审阅记录另存。交付不触发研究提交、投稿、发布或第三方发送。")
