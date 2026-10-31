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
    return canonical_hash(shared_reference_resources())


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
    )
