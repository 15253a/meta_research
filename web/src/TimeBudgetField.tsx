import { useEffect, useRef } from "react";
import { timeBudgetHours, validTimeBudget } from "./timeBudget";
import "./time-budget.css";

export function TimeBudgetField({ value, onChange, onBlur, disabled = false, className = "" }: {
  value: string; onChange: (value: string) => void; onBlur?: () => void; disabled?: boolean; className?: string;
}) {
  const invalid = !validTimeBudget(value);
  const previousHours = useRef(value !== "open" && !invalid ? value : "720h");
  useEffect(() => {
    if (value !== "open" && !invalid) previousHours.current = value;
  }, [value, invalid]);
  return <div className={`time-budget-field ${className}`}>
    <label><span>时间预算（小时）</span><input aria-label="时间预算" type="number" min="0.01" step="0.01"
      placeholder="输入小时数，例如 2.5" value={timeBudgetHours(value)} disabled={disabled || value === "open"}
      aria-invalid={invalid || undefined} onChange={event => onChange(`${event.currentTarget.value}h`)} onBlur={onBlur} /></label>
    <button type="button" className="time-budget-open" aria-pressed={value === "open"} disabled={disabled}
      onClick={() => onChange(value === "open" ? previousHours.current : "open")}>不设硬截止</button>
    {invalid ? <small role="alert">请输入大于 0 的小时数，最多两位小数。</small> : null}
  </div>;
}
