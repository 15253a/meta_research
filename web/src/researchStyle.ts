import type { ResearchStyle } from "./api";

export const RESEARCH_STYLES: { value: ResearchStyle; label: string; description: string }[] = [
  { value: "focus", label: "聚焦攻关", description: "集中攻克当前目标，优先安排直接推动目标的研究。" },
  { value: "balanced", label: "均衡探索", description: "推进当前目标，同时适度探索有价值的相邻问题与线索。" },
  { value: "open", label: "开放探索", description: "主动探索新问题与意外发现，允许研究方向随证据演化。" },
];

export function researchStyleLabel(value: ResearchStyle): string {
  return RESEARCH_STYLES.find(style => style.value === value)!.label;
}
