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
    {basis.freshness === "stale" ? <p role="status">创建依据已经变化。下面保留生成时的理解；请重新生成以阅读当前资料。</p> : null}
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
        {source.selection_reason ? <small>选择理由：{source.selection_reason}</small> : null}
        {source.binding?.version_ref ? <small>资料版本：{source.binding.version_ref}</small> : null}
      </li>)}</ul> : <p>当前未登记资料来源。</p>}
    </details>
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
