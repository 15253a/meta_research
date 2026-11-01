"""Canonical packaged references, disclosed through role-scoped pointers."""
from importlib.resources import files
from pathlib import Path

from meta_research.owners.common import canonical_hash
from meta_research.resource_preparation import prepared_resource

SHARED_REFERENCE_NAMES = (
    "research-guidance.md", "human-guidance.md", "research-navigation.md",
    "cycle-work.md", "materials-and-custody.md",
)
SHARED_REFERENCE_PATHS = frozenset(f"references/{name}" for name in SHARED_REFERENCE_NAMES)

CYCLE_ROLE_DIRECTORIES = {
    "idea": "idea_stage", "plan": "plan_stage", "bundle": "bundle_stage",
    "target": "target-execution", "reasoning": "reasoning_stage",
}


@prepared_resource
def cycle_role_skill_text(root_kind: str) -> str:
    """Share one prepared read between adapter binding and Harness delivery."""
    return (shared_reference_root() / CYCLE_ROLE_DIRECTORIES[root_kind] / "SKILL.md").read_text(encoding="utf-8")


@prepared_resource
def cycle_role_instructions(root_kind: str) -> str:
    """Maintain the selected role method in Harness instructions, not turn data."""
    directory = CYCLE_ROLE_DIRECTORIES.get(root_kind)
    if directory is None:
        return ""
    source = shared_reference_root() / directory / "SKILL.md"
    return (f"Role skill source: {source}\n"
            "Resolve role reference links relative to this source directory.\n"
            + cycle_role_skill_text(root_kind))


@prepared_resource
def target_role_reference_hash() -> str:
    """Bind Target's required references through its existing Harness profile.

    The other Cycle adapters already bind their complete role resource bundles.
    Target runs directly through the Harness, so its references belong here.
    """
    root = shared_reference_root() / "target-execution" / "references"
    return canonical_hash({name: (root / name).read_text(encoding="utf-8")
                           for name in ("formal-work.md", "data-preservation.md")})


@prepared_resource
def prepare_cycle_role_resources() -> None:
    """Prepare the shared role identity inside each adapter's resource boundary."""
    for root_kind in CYCLE_ROLE_DIRECTORIES:
        cycle_role_instructions(root_kind)
    target_role_reference_hash()


@prepared_resource
def shared_reference_root() -> Path:
    """Installed file root used by native tools, independent of their cwd."""
    return Path(__file__).with_name("skills").resolve()


@prepared_resource
def shared_reference_resources() -> dict[str, str]:
    package = files("meta_research.skills") / "references"
    return {f"references/{name}": (package / name).read_text(encoding="utf-8")
            for name in SHARED_REFERENCE_NAMES}


@prepared_resource
def shared_reference_bundle_hash() -> str:
    return canonical_hash({"references": shared_reference_resources(),
                           "writing": shared_writing_resources()})


@prepared_resource
def shared_writing_resources() -> dict[str, str]:
    """Bind the one shared method and its required references, never a Writing root."""
    root = shared_reference_root() / "writing-report"
    names = ("SKILL.md", "references/context-and-handoff.md", "references/scientific-writing.md",
             "references/report-writing.md", "references/paper-v1-advisory.md",
             "references/presentation-v1-advisory.md", "agents/openai.yaml")
    return {name: (root / name).read_text(encoding="utf-8") for name in names}


def shared_writing_instructions(root_kind: str) -> str:
    if root_kind not in {*CYCLE_ROLE_DIRECTORIES, "companion"}:
        return ""
    # Validate all linked resources at the existing preparation boundary. A
    # missing installed reference must not silently select an older skill.
    shared_writing_resources()
    return ("\nShared writing skill source: " + str(shared_reference_root() / "writing-report" / "SKILL.md")
            + "\n写作或修改论文、报告、配套 PPT、提纲或局部稿件时，读取这份共用 writing-report Skill，"
            "按任务读取其中的文体、证据与交接参考；在当前任务、目录和权限内完成写作。"
            "正式文件需独立审阅、实际渲染及视觉检查；局部工作按请求范围检查。\n")


def companion_reference_instructions(root_kind: str) -> str:
    if root_kind != "companion":
        return ""
    root = shared_reference_root()
    return ("\nCompanion role source: " + str(root / "companion-work.md")
            + "\nCreation role source: " + str(root / "first_creation" / "SKILL.md")
            + "\n按本次业务分支读取并遵循相应主指导：日常交流及独立写作使用 Companion；"
            "创建理解、重评、首题父子任务及 manual_question_creation 使用 first_creation 与相应上下文合同。"
            "续轮或压缩后缺少适用规则时从这些准确入口恢复；始终使用本次实际提供的响应 schema，"
            "manual 窗口仅起草六字段 Proposal，用户正式确认由原入口处理。\n")


def shared_reference_instructions(root_kind: str) -> str:
    """Point native readers at the sole packaged source without inlining it."""
    purposes = {
        "research-guidance.md": ("安排投入、持续会话或委派审阅", "判断研究价值、协作需要与连续工作"),
        "human-guidance.md": ("主动求助、接收指导或处理人类材料", "把人的参与接入研究并保持协作连续性"),
        "research-navigation.md": ("发现积累、追溯依据或复用成果", "从六个研究视角理解过程、内容、用途与来源"),
        "cycle-work.md": ("阶段接续、建题、后继选题或正式目标演化", "处理问题发展、目标重审与实际工作衔接"),
        "materials-and-custody.md": ("接收材料、加工文件、登记保管或清理工作区", "安排阅读、保存与生命周期"),
    }
    cycle_roles = {"idea", "plan", "bundle", "target", "reasoning"}
    names = SHARED_REFERENCE_NAMES
    if root_kind == "acquisition":
        names = ("human-guidance.md", "materials-and-custody.md")
    elif root_kind not in cycle_roles:
        names = tuple(name for name in names if name != "cycle-work.md")
    return (
        f"Shared reference root: {shared_reference_root()}\n"
        "公共参考中的 references/... 相对此安装目录；文件内链接相对该文件。"
        "已有完整且仍有效的正文时直接使用；触发相关工作时，通过原生文件工具读取对应部分。"
        "读取失败如实报告缺少的文件，不使用其他版本替代。\n"
        f"当前角色：{root_kind}。仅履行本角色及当前授权范围内的职责。"
        "Cycle 阶段提交、正式指导确认及目标演化规则仅适用于获得相应职责和工具的研究根；"
        "Acquisition 负责指定获取，Companion／Writing 按各自请求协作，不因此接管 Cycle。\n"
        + "\n".join(f"- **{purposes[name][0]}**：读取 `references/{name}`，{purposes[name][1]}。" for name in names)
        + shared_writing_instructions(root_kind)
        + companion_reference_instructions(root_kind)
    )
