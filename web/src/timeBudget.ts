const LEGACY_HOURS: Record<string, string> = { "7d": "168", "30d": "720", "90d": "2160" };

export function timeBudgetHours(value: string): string {
  return Object.hasOwn(LEGACY_HOURS, value) ? LEGACY_HOURS[value] : (value.endsWith("h") ? value.slice(0, -1) : "");
}

export function validTimeBudget(value: string): boolean {
  if (value === "open" || Object.hasOwn(LEGACY_HOURS, value)) return true;
  if (value.length > 24 || !/^[0-9]+(?:\.[0-9]{1,2})?h$/.test(value)) return false;
  const seconds = Math.round(Number(value.slice(0, -1)) * 3600);
  return seconds > 0 && Number.isSafeInteger(seconds);
}

export function timeBudgetLabel(value: string): string {
  if (value === "open") return "不设硬截止";
  return validTimeBudget(value) ? `${timeBudgetHours(value)} 小时` : "请输入有效小时数";
}
