"""CP1.2 流程层实读校验：prompt / skill 是行为本体（措辞即行为），逐份核要素与锚词。

校验三层：
1. 通用骨架六要素（《第二部分》§6.4）：触发条件 / 读取 / Codex 任务节 / 产物 schema 指向 / 门禁与写入 / 失败语义；
2. 每阶段关键流程锚词（对齐流程图 02–05 与本阶段设计要点）；
3. skill 引用的 schemas/ 文件必须真实存在（防提示词指向不存在的契约）。
"""
import json
import re

from conftest import SCHEMAS_DIR, SYSTEM_ROOT
from orchestrator.schemas import SchemaSet

PROMPTS = SYSTEM_ROOT / "prompts"
SKILLS = {
    name: (PROMPTS / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    for name in ("idea", "plan", "bundle", "reasoning")
}
CONTROL_SKILLS = {
    name: (PROMPTS / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    for name in ("adapter_generation", "adapter_review")
}
SYSTEM_PROMPT = (PROMPTS / "system_prompt.md").read_text(encoding="utf-8")

# 通用骨架六要素（《二》§6.4）：触发/读取/Codex 任务/产物 schema 指向/门禁写入/失败语义。
# 「Codex 任务」节名随阶段（【生成任务】【计划任务】【判官任务】【评审任务】/ bundle 的
# 「执行流程」——M0 由驱动器代跑，任务主体是驱动器），故用 TASK_SECTION_MARKERS 单独校验。
COMMON_ANCHORS = ["触发条件", "读取", "门禁与写入", "失败语义", "schemas/"]
TASK_SECTION_MARKERS = ["任务】", "执行流程"]

# 每阶段关键锚词（缺一即流程要点缺失）
STAGE_ANCHORS = {
    "idea": [
        "需要全新创新",              # NEED 分支（图 03）
        "wildidea", "旁路",
        "wildidea_expand", "wildidea_search",  # Idea-only MCP，无第二个顶层模型
        "判官", "Structural Depth",
        "防重复造轮",
        "联网粗查已启用",             # 受控后端启用态仍须保留的诚实文案
        "idea_set.json", "record_review",
        "全部候选不合格",             # 失败语义：阶段失败=轮正常收尾
        "inconclusive",
    ],
    "plan": [
        "最小可执行计划", "Codex 自主决定",
        "Web 已登记", "ContextPack",
        "targets", "target_kind", "spec_md",
        "protocol", "scope_spec", "metric_defs",
        "三问决策树", "机械复用判定", "规范 selector", "gpu_required",
        "不得替模型补研究语义",
        "targets=[]", "reuse_evidence", "import_defer", "plan.json",
    ],
    "bundle": [
        "串行", "target_kind",
        "smoke", "worktree",
        "code_review", "result_review",   # 双评审
        "engineering_blocked",
        # （步⑧ m7-1：删 "fake"/"synthetic" 锚——它们是 M0 造假桩必标；真执行契约起不再造假）
        "两段提交",
        "早退", "skipped",                # RFAIL 分支
        "critical",
        "execution_manifest", "plan_slice_hash", "metric_value", "{src}", "{ckpt}",   # 步⑧真执行契约锚
    ],
    "reasoning": [
        "R1", "R2", "R3", "R4",
        "bootstrap", "decompose", "goal_amend",
        "verdict", "evidence",
        "最小键", "B(t)",
        "不写 route",                     # selection 与 route 分离
        "terminate", "inconclusive",
        "answer.json", "tree_ops.json", "selection.json",
        "cycle_report",
    ],
}

SYSTEM_PROMPT_ANCHORS = [
    "当前事实包权威",          # 常驻工作上下文不扩大事实面
    "不得臆造",
    "当前 turn 修正并重提",
    "```json",              # 输出信封
    "\"files\"",
    "resource_request.json",  # sidecar 出口
    "中文",                  # 落盘语言
    "不得执行任何 shell 命令",  # 本机 bwrap 约束
]


def test_system_prompt_anchors():
    for anchor in SYSTEM_PROMPT_ANCHORS:
        assert anchor in SYSTEM_PROMPT, f"system_prompt 缺要素锚词: {anchor}"


def test_system_prompt_inlines_minimal_schema_valid_resource_request_skeleton():
    """Workers receive the user-facing minimum, not an audit worksheet."""
    match = re.search(
        r"resource_request\.json`? 最小骨架.*?```json\n(.*?)\n```",
        SYSTEM_PROMPT, re.DOTALL)
    assert match is not None, "system_prompt 未内联 resource_request exact skeleton"
    payload = json.loads(match.group(1))
    SchemaSet(SCHEMAS_DIR).validator("resource_request").validate(payload)
    assert set(payload) == {"summary_md", "items"}
    assert set(payload["items"][0]) == {"kind", "desc"}


def test_plan_keeps_science_and_delegates_bookkeeping():
    plan = SKILLS["plan"]
    for anchor in (
            "前向逻辑或算法结构是否变化", "必须重新训练或产生新的可评对象",
            "只改变评估数据、协议、指标或重测", "target_key", "budget_estimate",
            "required metrics", "规范 selector", "不得替你生成",
            "禁止 `auto-cN-tN`"):
        assert anchor in plan, f"plan/SKILL.md 缺自主计划语义: {anchor}"
    assert "独立 plan reviewer" in plan
    assert "编排器会用保守默认值确定性补齐" not in plan


def test_reasoning_copies_exact_measurement_ref_and_labels_fake_only_from_provenance():
    reasoning = SKILLS["reasoning"]
    for anchor in (
            "evidence_ref=mrN", "只复制 `mrN`", "evaluation.source=fake",
            "source=fake", "synthetic=true", "successful_measurements",
            "不得称为 fake"):
        assert anchor in reasoning, f"reasoning/SKILL.md 缺 measurement 引用语义: {anchor}"
    assert "M0 假执行的测量可作流程性证据" not in reasoning


def test_skill_common_skeleton():
    for name, text in SKILLS.items():
        for anchor in COMMON_ANCHORS:
            assert anchor in text, f"{name}/SKILL.md 缺通用骨架要素: {anchor}"
        assert any(m in text for m in TASK_SECTION_MARKERS), (
            f"{name}/SKILL.md 缺「任务」节（六要素之 Codex 任务）"
        )


def test_skill_stage_anchors():
    for name, anchors in STAGE_ANCHORS.items():
        text = SKILLS[name]
        missing = [a for a in anchors if a not in text]
        assert not missing, f"{name}/SKILL.md 缺流程锚词: {missing}"


def test_idea_skill_uses_pinned_adapter_contract_not_m0_simulation():
    idea = SKILLS["idea"]
    assert "M0 骨架说明" not in idea
    assert "在会话内执行" not in idea
    for anchor in (
            "wildidea@6ff66ada15b0047b2e03d229f2e9543c542df598",
            "problem_card", "source-first", "claimed_method", "systematicity",
            "repair", "reangle", "batch diversity", "严禁生成 HTML",
            "联网查重未启用·文献级待验证", "绝不猜 engine/hash/model/sampling",
            "wildidea_expand", "wildidea_search", 'files={"idea_set.json": ...}'):
        assert anchor in idea, f"idea/SKILL.md 缺 pinned adapter 契约: {anchor}"
    assert "phase=idea" not in idea
    assert "phase=audit" not in idea
    assert 'files["idea_set.draft.json"]' not in idea
    assert 'files["idea_audit.json"]' not in idea


def test_skill_schema_references_exist():
    """skill 里出现的 schemas/xxx.schema.json 必须真实存在。"""
    pattern = re.compile(r"schemas/([a-z_]+)\.schema\.json")
    for name, text in (list(SKILLS.items()) + list(CONTROL_SKILLS.items())
                       + [("system_prompt", SYSTEM_PROMPT)]):
        for ref in set(pattern.findall(text)):
            assert (SCHEMAS_DIR / f"{ref}.schema.json").exists(), (
                f"{name} 引用了不存在的 schema: {ref}"
            )


def test_adapter_control_skills_keep_isolation_and_fail_closed_contracts():
    generation = CONTROL_SKILLS["adapter_generation"]
    review = CONTROL_SKILLS["adapter_review"]
    for anchor in (
            "untrusted data", "no tools", "import-adapter.json",
            "adapter-generation-failure.json", "dependency_contract",
            "instead of guessing"):
        assert anchor in generation, f"adapter_generation/SKILL.md 缺边界锚词: {anchor}"
    for anchor in (
            "independent reviewer", "do not receive the generator transcript",
            "no tools", "import-adapter-review.json", "identity_hash",
            "projection_hash", "adapter_sha256"):
        assert anchor in review, f"adapter_review/SKILL.md 缺边界锚词: {anchor}"


def test_skills_are_chinese():
    """落盘语言硬约定（§5.9）：提示词正文须以中文为主。本断言只为逮住"整篇英文"的违规
    ——英文骨架 + 中文点缀的文档 <0.1；正常中文文档因必须点名大量英文字段名/枚举
    （schema 契约要求），实测落在 0.25–0.45，故阈值取 0.2 留字段名余量。"""
    han = re.compile(r"[一-鿿]")
    word = re.compile(r"[^\s`*#>|\-=~]")
    for name, text in list(SKILLS.items()) + [("system_prompt", SYSTEM_PROMPT)]:
        ratio = len(han.findall(text)) / max(1, len(word.findall(text)))
        assert ratio > 0.2, f"{name} 中文占比过低（{ratio:.2f}），疑违反落盘语言约定"
