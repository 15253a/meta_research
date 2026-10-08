import { useId, type ReactNode } from "react";
import "./spectrum.css";
import { MetaTrace } from "./MetaTrace";
import { useResearchMotion } from "./ResearchMotion";

export const spectrumStages = [
  { id: "idea", name: "Idea", label: "研究思路", description: "探索问题，形成假设", color: "#3ecfb2" },
  { id: "plan", name: "Plan", label: "验证计划", description: "设计路径，定义验证", color: "#5a97f5" },
  { id: "bundle", name: "Bundle", label: "实验与证据", description: "开展实验，汇集证据", color: "#9183f9" },
  { id: "reasoning", name: "Reasoning", label: "研究判断", description: "综合发现，形成判断", color: "#f0a96c" },
] as const;

export type SpectrumStage = typeof spectrumStages[number]["id"];
export function spectrumStage(value: string | null | undefined): SpectrumStage | null {
  return spectrumStages.find(stage => stage.id === value?.toLowerCase())?.id ?? null;
}

const curves = [
  "M0,72 C55,72 95,10 158,8 S240,72 370,72",
  "M170,72 C230,72 275,5 335,3 S415,72 530,72",
  "M390,72 C450,72 495,2 558,1 S635,72 740,72",
  "M590,72 C648,72 685,9 745,7 S793,72 800,72",
];

/** A decorative spectrum, never a measurement or a completion percentage. */
function SpectrumChart({ current }: { current: SpectrumStage | null }) {
  const id = useId().replace(/:/g, "");
  return <svg className="spectrum-chart" viewBox="0 0 800 76" preserveAspectRatio="none" aria-hidden="true" focusable="false">
    <defs>{spectrumStages.map(stage => <linearGradient key={stage.id} id={`${id}-${stage.id}`} x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stopColor={stage.color} stopOpacity={stage.id === current ? ".23" : ".1"} />
      <stop offset="1" stopColor={stage.color} stopOpacity=".015" />
    </linearGradient>)}</defs>
    <line x1="0" y1="72" x2="800" y2="72" stroke="#dce3ee" strokeWidth="1" />
    {spectrumStages.map((stage, index) => <g key={stage.id}>
      <path d={`${curves[index]}Z`} fill={`url(#${id}-${stage.id})`} />
      <path d={curves[index]} stroke={stage.color} strokeOpacity={stage.id === current ? ".8" : ".45"} strokeWidth="1.2" fill="none" vectorEffect="non-scaling-stroke" />
      {stage.id === current && <path className="spectrum-flow" d={curves[index]} stroke={stage.color} strokeWidth="2.8" fill="none" strokeLinecap="round" vectorEffect="non-scaling-stroke" />}
    </g>)}
  </svg>;
}

export function SpectrumStages({ current, labels, onSelect, compact = false, stale = false, stageContent, selected, onResult, running = false }: {
  current: SpectrumStage | null;
  labels?: Partial<Record<SpectrumStage, string>>;
  onSelect?: (stage: SpectrumStage) => void;
  compact?: boolean;
  stale?: boolean;
  stageContent?: (stage: SpectrumStage) => ReactNode;
  selected?: SpectrumStage | null;
  onResult?: (stage: SpectrumStage) => void;
  running?: boolean;
}) {
  const motion = useResearchMotion<HTMLElement>(running && !stale);
  return <section ref={motion.ref} data-spectrum-choice="D" data-motion-active={motion.active} className={`research-spectrum${compact ? " is-compact" : ""}`} aria-label="研究光谱">
    <div className="spectrum-caption"><span>棱镜谱带 <i /> 研究光谱</span><small>四个阶段 · 随研究推进</small></div>
    <SpectrumChart current={current} />
    <nav className="spectrum-stages" aria-label="本轮阶段与历史结果">{spectrumStages.map((stage, index) => {
      const active = current === stage.id;
      const label = labels?.[stage.id] ?? (active ? stale ? "上次记录的阶段" : "当前阶段" : "查看阶段");
      const content = <><span className="spectrum-stage-top"><span className="spectrum-stage-number">0{index + 1}</span><span className="spectrum-stage-state"><i />{label}</span></span>
        <span className="spectrum-stage-name">{stage.name}{active ? <MetaTrace variant="spectrum" active={motion.active} /> : <span aria-hidden="true">↗</span>}</span>
        <span className="spectrum-stage-label">{stage.label}</span><span className="spectrum-stage-description">{stage.description}</span></>;
      const props = { className: "spectrum-stage", "data-stage": stage.id, "data-current": active, "aria-current": active ? "step" as const : undefined, "aria-label": `${stage.name} · ${stage.label}，${label}` };
      if (stageContent) return <div key={stage.id} {...props} className="spectrum-stage has-root-sessions" data-selected={selected === stage.id}>
        <button type="button" className="spectrum-stage-open" aria-label={props["aria-label"]} aria-pressed={selected === stage.id} onClick={() => onSelect?.(stage.id)}>{content}</button>
        {stageContent(stage.id)}
        {onResult ? <button type="button" className="spectrum-stage-result" onClick={() => onResult(stage.id)} aria-label={`查看 ${stage.name} 结果与历史`}>查看结果 ↗</button> : null}
      </div>;
      return onSelect ? <button key={stage.id} type="button" {...props} onClick={() => onSelect(stage.id)}>{content}</button>
        : <a key={stage.id} {...props} href={`/?workspace=1&stage=${stage.id}`}>{content}</a>;
    })}</nav>
  </section>;
}

const iconPaths: Record<string, ReactNode> = {
  "⌂": <><path d="m3 10 9-7 9 7v10H3Z" /><path d="M9 20v-7h6v7" /></>,
  "树": <><rect x="9" y="3" width="6" height="5" rx="1" /><rect x="2" y="16" width="6" height="5" rx="1" /><rect x="16" y="16" width="6" height="5" rx="1" /><path d="M12 8v4M5 16v-4h14v4" /></>,
  "▤": <><path d="M3 7h7l2 2h9v11H3Z" /><path d="M3 7V4h7l2 3h7v2" /></>,
  "✎": <><path d="M13 5H4v16h16v-9M10 14l1-5 8-7 3 3-8 8Z" /></>,
  "↺": <><path d="M3 5v5h5M3 10a9 9 0 1 1 1 8M12 7v6l4 2" /></>,
  "!": <><path d="M8 19h8M5 16h14l-2-4V8a5 5 0 0 0-10 0v4ZM10 22h4" /></>,
  "＋": <path d="M12 4v16M4 12h16" />,
};
export function ResearchIcon({ glyph }: { glyph: string }) {
  return <svg className="research-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{iconPaths[glyph] ?? iconPaths["▤"]}</svg>;
}
