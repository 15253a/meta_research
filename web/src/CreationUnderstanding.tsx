import { useId } from "react";
import type { CreationResearchBasis } from "./api";

type CreationSource = CreationResearchBasis["sources"][number];

const understandingFields: Array<{
  field: keyof CreationResearchBasis["understanding"];
  label: string;
}> = [
  { field: "material_composition", label: "资料构成" },
  { field: "work_already_done", label: "已完成工作" },
  { field: "claims_and_conditions", label: "已有结论与条件" },
  { field: "conflicts", label: "冲突" },
  { field: "gaps", label: "证据缺口" },
  { field: "unfinished_questions", label: "尚未解决的问题" },
];

function sourceKind(source: CreationSource): string {
  switch (source.source?.kind) {
    case "original_file": return "原件";
    case "work_file": return "处理结果";
    case "material_reference": return "资料入口";
    default: return "资料";
  }
}

function custodyStatus(source: CreationSource): string {
  if (source.intake_state === "failed") return "正式接纳失败，尚未完成保管。";
  if (source.intake_state === "pending") return "已选择保管，正式接纳尚未完成。";
  if (source.binding?.version_ref) {
    if (source.custody === "managed") return "已由系统保管选中的精确版本。";
    if (source.custody === "linked_local") return "已建立选中版本的本地引用；来源变化会影响后续读取。";
    return "已接纳为正式资料。";
  }
  if (source.selection_reason) return "已选择保管，尚无已接纳的资料版本。";
  if (source.source?.kind === "work_file") return "未选择正式保管，试跑副本或结果不会自动入库。";
  if (source.source?.kind === "original_file" || source.source?.kind === "material_reference") {
    return "未选择正式保管，原件仍在原位置，未复制入库。";
  }
  return "未选择正式保管。";
}

export function CreationUnderstanding({ basis }: { basis: CreationResearchBasis }) {
  const titleId = useId();
  const materialSources: CreationSource[] = basis.sources;
  const sources = new Map(materialSources.map((source) => [source.material_key, source.relative_path]));
  return <section className="quest-journey-section quest-understanding" aria-labelledby={titleId}>
    <div className="quest-section-heading">
      <b id={titleId}>已有课题的理解</b>
      <small>{basis.kind === "literature_revised" ? "结合文献修正后" : "根据已有资料"}</small>
    </div>
    {basis.freshness === "stale" ? <p role="status">创建依据已经变化。下面保留当时的理解；新依据需要判断仍适用的认识并补查受影响部分，旧确认已失效。</p> : null}
    {basis.applicability ? <div>
      <h3>本次变化与适用范围</h3>
      <p>{basis.applicability.change_assessment}</p>
      <small>前驱依据：{basis.applicability.predecessor.basis_ref} · {basis.applicability.predecessor.basis_hash}</small>
      <ul>{basis.applicability.decisions.map((decision) => <li key={decision.prior_statement_ref}>
        <b>{{ retain: "仍适用", replace: "已替换认识", needs_recheck: "待补查", out_of_scope: "不在当前范围" }[decision.disposition]}</b>
        <p>{decision.prior_text}</p>
        <p>{decision.explanation}</p>
        {decision.applicable_conditions.length ? <small>适用条件：{decision.applicable_conditions.join("；")}</small> : null}
        {decision.affected_scope ? <p>受影响范围：{decision.affected_scope}</p> : null}
        {decision.creation_limit !== "none" ? <p>{decision.creation_limit === "required_first_question"
          ? "形成首题所需的依据仍需核对。" : "该范围保留为后续研究，尚未得到验证。"}</p> : null}
      </li>)}</ul>
      {basis.applicability.literature_decisions.length ? <div>
        <h3>已有文献的适用性</h3>
        <ul>{basis.applicability.literature_decisions.map((decision) => <li key={decision.snapshot_ref}>
          <b>{{ retain: "沿用已有快照", needs_recheck: "文献范围待补查", out_of_scope: "文献不在当前范围" }[decision.disposition]}</b>
          <small>原快照：{decision.snapshot_ref} · {decision.snapshot_hash}</small>
          {decision.applicable_conditions.length ? <p>适用条件：{decision.applicable_conditions.join("；")}</p> : null}
          {decision.affected_scope ? <p>{decision.affected_scope}</p> : null}
          <p>{decision.limitations}</p>
        </li>)}</ul>
      </div> : null}
    </div> : null}
    {understandingFields.map(({ field, label }) => <div key={field}>
      <h3>{label}</h3>
      {basis.understanding[field].length ? <ul>{basis.understanding[field].map((statement) => <li key={statement.ref}>
        <p>{statement.text}</p>
        {statement.conditions.length ? <small>条件：{statement.conditions.join("；")}</small> : null}
        <small>{statement.kind === "agent_inference" ? "系统推断" : "资料报告"}
          {statement.sources.map((citation) => ` · ${sources.get(citation.material_key) ?? citation.material_key} ${citation.location}`).join("")}</small>
      </li>)}</ul> : <p>当前资料未形成这一项结论。</p>}
    </div>)}
    <details open><summary>阅读覆盖与资料保管</summary>
      {materialSources.length ? <ul>{materialSources.map((source) => <li key={source.material_key}>
        <b>{source.relative_path}</b> · {sourceKind(source)} · {{ read: "已读", partial: "部分已读", unread: "未读" }[source.coverage.kind]}
        {source.coverage.unread_description ? <p>{source.coverage.unread_description}</p> : null}
        <p>{custodyStatus(source)}</p>
        {source.inherited_from ? <small>沿用原依据的精确资料版本：{source.inherited_from.basis_ref}。本次未重新接纳。</small> : null}
        {source.selection_reason ? <small>选择理由：{source.selection_reason}</small> : null}
        {source.binding?.version_ref ? <small>资料版本：{source.binding.version_ref}</small> : null}
      </li>)}</ul> : <p>当前未登记资料来源。</p>}
    </details>
    {basis.inherited_literature?.length ? <div>
      <h3>沿用的文献身份</h3>
      <p>这些快照保留原检索身份，不表示本次进行了新检索。</p>
      <ul>{basis.inherited_literature.map((item) => <li key={item.snapshot.snapshot_ref}>
        <small>{item.snapshot.snapshot_ref} · {item.snapshot.snapshot_hash}</small>
        <small>原检索：{item.snapshot.binding.run_ref}</small>
        <small>原绑定依据：{item.original_basis.basis_ref}</small>
      </li>)}</ul>
    </div> : null}
    {basis.kind === "literature_revised" ? <div>
      <h3>文献带来的修正</h3>
      {basis.corrections.length ? <ul>{basis.corrections.map((correction, index) => <li key={`${correction.prior_statement_ref}-${index}`}>
        <p>{correction.explanation}</p>
        <small>原资料：{correction.original_sources.map((key) => sources.get(key) ?? key).join("、")}</small>
        <small>文献：{correction.literature_sources.map((citation) => `${citation.paper_id} · ${citation.locator}`).join("；")}</small>
      </li>)}</ul> : <p>本次文献结果未支持新增修正。</p>}
      <p>{basis.search_assessment?.assessment}</p>
      {basis.search_assessment?.limitations.length ? <ul>{basis.search_assessment.limitations.map((limit) => <li key={limit}>{limit}</li>)}</ul> : null}
    </div> : null}
  </section>;
}
