import type { HumanRequestItem } from "./api";

// Presentation only: the original request remains authoritative and is available
// in technical details. Never derive permissions or response facts from this text.
export function plainRequestText(value: string): string {
  // Exact references must stay usable, even when they contain domain terms.
  const references = /(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`|"(?:[A-Za-z]:[\\/]|\/|\.\.?[\\/])[^"\n]*"|'(?:[A-Za-z]:[\\/]|\/|\.\.?[\\/])[^'\n]*'|(?:https?:\/\/|[A-Za-z]:[\\/]|\/|\.\.?[\\/])[^\s<>"'，。；、）)]+|\b[A-Za-z0-9_-]+(?:[\\/][^\s<>"'，。；、）)]+|(?:\.[A-Za-z0-9_-]+)+))/g;
  return value.split(references).map((part, index) => index % 2 ? part : plainProse(part)).join("");
}

function plainProse(value: string): string {
  return value
    .replace(/\bHumanRequest\b/g, "协助请求")
    .replace(/\bTarget\b/g, "研究任务")
    .replace(/\bQuest\b/g, "研究项目")
    .replace(/\bBaseline\b/g, "参考方法")
    .replace(/\bVariant\b/g, "方法方案")
    .replace(/\bDataset\b/g, "数据集")
    .replace(/\bEnvironment\b/g, "运行环境")
    .replace(/\bOwner\b/g, "负责此项工作的系统模块")
    .replace(/\bSkill\b/g, "操作说明")
    .replace(/冻结(?:输入)?\s*manifest|\bfrozen_input_manifest_path\b/g, "本次任务的输入文件清单")
    .replace(/\bmanifest\b/g, "文件清单")
    .replace(/\bfence\b/g, "执行标识")
    .replace(/\bexec_command\b/g, "命令执行工具")
    .replace(/\bapply_patch\b/g, "文件编辑工具")
    .replace(/\bcompletion_binding\b/g, "成果提交要求")
    .replace(/冻结合同/g, "本次任务要求")
    .replace(/已接纳/g, "已确认")
    .replace(/精确依赖/g, "所需条件")
    .replace(/同一操作链/g, "这项操作的处理记录");
}

function conditionText(request: HumanRequestItem, key: string): string {
  const condition = request.target_assertion?.condition;
  if (!condition || typeof condition !== "object" || Array.isArray(condition)) return "";
  const value = (condition as Record<string, unknown>)[key];
  return typeof value === "string" ? value.trim() : "";
}

export function requestIssueKey(request: HumanRequestItem): string | null {
  // Match the specific reported failure, not a generic permission request.
  const reported = [request.business_purpose, conditionText(request, "impact"),
    conditionText(request, "safe_response")].join("\n");
  return request.kind === "system_operation_help"
    && /bwrap:\s*Failed to make \/ slave:\s*Permission denied/.test(reported)
    ? "workspace-command-startup-denied"
    : null;
}

const kindGuidance: Record<HumanRequestItem["kind"], {
  handler: string; summary: string; responseHint: string;
}> = {
  library_reconnect: {
    handler: "有图书馆访问权限的人",
    summary: "文献访问遇到了问题，需要你协助选择可用的获取方式。",
    responseHint: "可以说明已恢复访问、改用公开文献，或提供已有全文。",
  },
  external_material_api_access: {
    handler: "能提供所需材料或服务访问方式的人",
    summary: "研究需要补充材料或访问条件。",
    responseHint: "写下你能提供什么、有哪些限制；也可以附上文件或提出替代办法。",
  },
  offline_action: {
    handler: "能提供所需判断、帮助或现场操作的人",
    summary: "这项工作需要人的判断或实际行动，请按下面的要求回应。",
    responseHint: "可以提供你的判断、操作结果或观察；如果暂时做不到，也可以直接说明原因。",
  },
  capability_authorization: {
    handler: "由你决定是否授权",
    summary: "继续这项工作需要你的明确授权，请先查看具体范围。",
    responseHint: "确认授权范围后再决定；也可以提出更小的范围或拒绝。",
  },
  system_operation_help: {
    handler: "负责相关系统或设备的维护者",
    summary: "一项系统操作遇到了问题，相关工作正在等待处理。",
    responseHint: "不清楚如何处理时，可以在旁边询问具体步骤，或说明你无法处理的部分。",
  },
};

export function humanRequestPresentation(request: HumanRequestItem) {
  const issueKey = requestIssueKey(request);
  if (issueKey === "workspace-command-startup-denied") {
    return {
      issueKey,
      title: "研究程序无法运行命令或保存文件",
      summary: "服务器拒绝了研究程序的操作，相关研究暂时无法继续。",
      handler: "服务器维护者",
      action: "请让服务器维护者检查并修复研究程序的运行权限。修复后，点击“检查是否恢复”。",
      reason: "当前请求报告运行环境阻止了操作启动，研究程序无法通过这些操作读取文件或保存成果。需要先修复运行环境。",
      checks: ["能在原研究目录运行简单命令。", "能读取所需文件，并创建、读回一个测试文件。", "保留已有研究文件和记录，让原任务继续。"],
      responseHint: "如果你不负责维护服务器，可以在旁边说明“我无法处理服务器配置，请说明需要维护者做什么”。",
      retryLabel: "检查是否恢复",
    };
  }
  return {
    issueKey,
    title: plainRequestText(request.obligation),
    ...kindGuidance[request.kind],
    action: plainRequestText(conditionText(request, "safe_response") || request.obligation),
    reason: plainRequestText(conditionText(request, "impact") || request.business_purpose || "处理后，系统会检查相关工作是否可以继续。"),
    checks: (request.acceptance_conditions ?? []).map(plainRequestText),
    retryLabel: "重试",
  };
}
