import { ProductError, writeJson, type CompanionAgentProposal, type CompanionSoftConstraint, type CompanionViewContext, type GuidanceStrength } from "./api";
import type { WorkMaterialSubmission } from "./workMaterialApi";

export type GuidanceScope = { kind: "quest" | "question" | "cycle" | "target"; quest_ref: string; question_ref?: string; cycle_ref?: string; target_ref?: string };
export type ReviewedGuidance = {
  proposal_kind: "soft_constraint";
  text: string;
  assistant_understanding: string;
  applies_to: string[];
  semantic_scope: GuidanceScope;
  strength: GuidanceStrength;
  preserve_conditions: string[];
  work_materials: WorkMaterialSubmission | null;
};

export function sendGuidanceConversation(message: string, scopeRef: string, viewContext: CompanionViewContext | null, strength: GuidanceStrength, workMaterials: WorkMaterialSubmission | null) {
  return writeJson<Record<string, unknown>>("/api/v1/companion/messages", "POST", {
    scope_ref: scopeRef, message,
    ...(viewContext ? { view_context: viewContext } : {}),
    guidance_options: { strength, work_materials: workMaterials },
  });
}

function currentBasis(proposal: CompanionAgentProposal) {
  if (!proposal.proposal_ref || !proposal.scope_ref || !proposal.proposal_hash) throw new ProductError("agent_proposal_current_basis_required");
  return { expected_scope_ref: proposal.scope_ref, expected_proposal_hash: proposal.proposal_hash };
}

export function reviseGuidanceProposal(proposal: CompanionAgentProposal, guidance: ReviewedGuidance) {
  return writeJson<CompanionAgentProposal>(`/api/v1/human-collaboration/agent-proposals/${encodeURIComponent(proposal.proposal_ref ?? "")}/revisions`, "POST", {
    ...currentBasis(proposal), proposal: guidance,
  });
}

export function confirmGuidanceProposal(proposal: CompanionAgentProposal, strength: GuidanceStrength) {
  return writeJson<{ proposal: CompanionAgentProposal; soft_constraint: CompanionSoftConstraint }>(`/api/v1/human-collaboration/agent-proposals/${encodeURIComponent(proposal.proposal_ref ?? "")}/soft-constraint`, "POST", {
    ...currentBasis(proposal), strength,
  });
}

export function reviewedGuidance(value: Record<string, unknown> | undefined): ReviewedGuidance | null {
  if (!value || value.proposal_kind !== "soft_constraint" || typeof value.text !== "string"
    || typeof value.assistant_understanding !== "string" || !Array.isArray(value.applies_to) || value.applies_to.some(item => typeof item !== "string")
    || !Array.isArray(value.preserve_conditions) || value.preserve_conditions.some(item => typeof item !== "string") || !validGuidanceScope(value.semantic_scope)
    || typeof value.strength !== "number" || ![1, 2, 3, 4, 5].includes(value.strength) || !("work_materials" in value)) return null;
  if (value.work_materials !== null) {
    const materials = value.work_materials;
    if (typeof materials !== "object" || !materials || Array.isArray(materials)) return null;
    const submission = materials as Record<string, unknown>;
    if (!Array.isArray(submission.selections) || !submission.selections.length || typeof submission.receiver !== "object" || !submission.receiver) return null;
  }
  return value as ReviewedGuidance;
}

export function validGuidanceScope(value: unknown): value is GuidanceScope {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const scope = value as Record<string, unknown>;
  const fields: Record<string, string[]> = {
    quest: ["quest_ref"], question: ["quest_ref", "question_ref"],
    cycle: ["quest_ref", "question_ref", "cycle_ref"], target: ["quest_ref", "question_ref", "cycle_ref", "target_ref"],
  };
  const required = fields[String(scope.kind)];
  return Boolean(required && Object.keys(scope).length === required.length + 1
    && required.every(field => typeof scope[field] === "string" && scope[field].trim()));
}
