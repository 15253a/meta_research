"""CP1.2 流程层实读校验：prompt / skill 是行为本体（措辞即行为），逐份核要素与锚词。

校验三层：
1. 通用骨架六要素（《第二部分》§6.4）：触发条件 / 读取 / Codex 任务节 / 产物 schema 指向 / 门禁与写入 / 失败语义；
2. 每阶段关键流程锚词（对齐流程图 02–05 与本阶段设计要点）；
3. skill 引用的 schemas/ 文件必须真实存在（防提示词指向不存在的契约）。
"""
import re

from conftest import SCHEMAS_DIR, SYSTEM_ROOT

PROMPTS = SYSTEM_ROOT / "prompts"
SKILLS = {
    name: (PROMPTS / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    for name in ("idea", "plan", "bundle", "reasoning")
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
        "phase=idea", "phase=audit",  # 两次独立 runner_call
        "判官", "Structural Depth",
        "防重复造轮",
        "联网粗查已启用",             # novelty 诚实纪律固定文本
        "idea_set", "idea_audit",
        "全部候选不合格",             # 失败语义：阶段失败=轮正常收尾
        "inconclusive",
    ],
    "plan": [
        "verification needs", "复用判定",
        "锁死", "I1", "I2",           # 协议锁死（bundle 只照办）
        "可回答性",                   # 独立评审 ≤2 轮
        "eval_action", "evaluation_id", "eval_key", "evaluation_source",
        "attempt_purpose",
        "target_key", "spec_md", "claim",   # target 必填字段必须教给工人（内审 Critical#1）
        "canonical_key", "variant_key", "config_json",
        "聚合轮",                     # needs=[] 特例
        "import_defer",               # M0 不产的显式声明
        "plan.json", "plan_review",
        "dependency_wait",
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
        "四元打分", "B(t)",
        "不写 route",                     # selection 与 route 分离
        "terminate", "inconclusive",
        "answer.json", "tree_ops.json", "selection.json",
        "cycle_report",
    ],
}

SYSTEM_PROMPT_ANCHORS = [
    "无状态",                # 角色锁定
    "不得臆造",
    "三级校验",
    "```json",              # 输出信封
    "\"files\"",
    "resource_request.json",  # sidecar 出口
    "中文",                  # 落盘语言
    "不得执行任何 shell 命令",  # 本机 bwrap 约束
]


def test_system_prompt_anchors():
    for anchor in SYSTEM_PROMPT_ANCHORS:
        assert anchor in SYSTEM_PROMPT, f"system_prompt 缺要素锚词: {anchor}"


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


def test_skill_schema_references_exist():
    """skill 里出现的 schemas/xxx.schema.json 必须真实存在。"""
    pattern = re.compile(r"schemas/([a-z_]+)\.schema\.json")
    for name, text in list(SKILLS.items()) + [("system_prompt", SYSTEM_PROMPT)]:
        for ref in set(pattern.findall(text)):
            assert (SCHEMAS_DIR / f"{ref}.schema.json").exists(), (
                f"{name} 引用了不存在的 schema: {ref}"
            )


def test_skills_are_chinese():
    """落盘语言硬约定（§5.9）：提示词正文须以中文为主。本断言只为逮住"整篇英文"的违规
    ——英文骨架 + 中文点缀的文档 <0.1；正常中文文档因必须点名大量英文字段名/枚举
    （schema 契约要求），实测落在 0.25–0.45，故阈值取 0.2 留字段名余量。"""
    han = re.compile(r"[一-鿿]")
    word = re.compile(r"[^\s`*#>|\-=~]")
    for name, text in list(SKILLS.items()) + [("system_prompt", SYSTEM_PROMPT)]:
        ratio = len(han.findall(text)) / max(1, len(word.findall(text)))
        assert ratio > 0.2, f"{name} 中文占比过低（{ratio:.2f}），疑违反落盘语言约定"
