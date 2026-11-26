export type ProposalActivity = {
  key: string;
  label: string;
  status: "running" | "completed" | "notice" | "failed";
};

export type ProposalStreamState = {
  activities: ProposalActivity[];
  reconnecting: string | null;
};

const MAX_ACTIVITIES = 200;

function truncate(text: string, maxLength: number): string {
  const normalized = text.trim();
  if (normalized.length <= maxLength) return normalized;
  return normalized.slice(0, maxLength - 1).trimEnd() + "…";
}

function compactPath(raw: string): string {
  const cleaned = raw.trim().replace(/^['"`]+|['"`]+$/g, "").replace(/[;,]+$/g, "");
  if (!cleaned) return "";
  const parts = cleaned.split("/").filter(Boolean);
  if (parts.length <= 2) return cleaned;
  return parts.slice(-2).join("/");
}

function unwrapShellCommand(command: string): string {
  let inner = command.trim();
  const wrapper = inner.match(/^(?:\/usr\/bin\/env\s+)?(?:\/bin\/|\/usr\/bin\/)?(?:bash|sh|zsh)\s+-(?:l?c|cl)\s+([\s\S]+)$/);
  if (wrapper) {
    inner = wrapper[1].trim();
    if (
      (inner.startsWith("'") && inner.endsWith("'")) ||
      (inner.startsWith('"') && inner.endsWith('"'))
    ) {
      inner = inner.slice(1, -1).trim();
    }
  }
  return inner.replace(/\s+/g, " ").trim();
}

function extractTargetArgument(tokens: string[], startIndex: number): string {
  for (let i = startIndex; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (!token || token.startsWith("-")) {
      if (token === "-n" || token === "-c" || token === "-maxdepth" || token === "-name" || token === "-d" || token === "-m") {
        i += 1;
      }
      continue;
    }
    if (token === "|" || token === "&&" || token === "||" || token === ";" || token.startsWith(">") || token.startsWith("2>")) {
      break;
    }
    return compactPath(token);
  }
  return "";
}

function extractLastPathArgument(tokens: string[], startIndex: number): string {
  let candidate = "";
  for (let i = startIndex; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (!token) continue;
    if (token === "|" || token === "&&" || token === "||" || token === ";" || token.startsWith(">") || token.startsWith("2>")) {
      break;
    }
    if (token.startsWith("-")) {
      if (token === "-n" || token === "-c" || token === "-maxdepth" || token === "-name" || token === "-e" || token === "-f" || token === "-g" || token === "-m") {
        i += 1;
      }
      continue;
    }
    candidate = compactPath(token);
  }
  return candidate;
}

function describeCommand(rawCommand: string): string {
  const inner = unwrapShellCommand(rawCommand);
  if (!inner) return "执行资料准备检查";
  const tokens = inner.match(/(?:[^\s'"]+|'[^']*'|"[^"]*")+/g) ?? [];
  const head = (tokens[0] ?? "").split("/").pop()?.toLowerCase() ?? "";

  if (["ls", "find", "tree", "du", "stat", "pwd"].includes(head)) {
    const target = extractTargetArgument(tokens, 1);
    return target ? `浏览目录：${target}` : "浏览目录结构";
  }
  if (["cat", "head", "tail", "sed", "awk", "wc", "less", "more", "file", "nl", "cut", "sort", "uniq"].includes(head)) {
    const target = extractLastPathArgument(tokens, 1);
    return target ? `查看文件：${target}` : "查看文件内容";
  }
  if (["grep", "rg", "egrep", "fgrep", "ag"].includes(head)) {
    const target = extractLastPathArgument(tokens, 1);
    return target ? `检索文件内容：${target}` : "检索文件内容";
  }
  if (["cp", "rsync", "tar", "unzip", "gzip", "gunzip"].includes(head)) {
    const target = extractTargetArgument(tokens, 1);
    return target ? `复制临时副本：${target}` : "复制临时副本";
  }
  if (["python", "python3", "pytest", "node", "uv", "rscript", "julia"].includes(head)) {
    const inlineIdx = tokens.findIndex(token => token === "-c" || token === "-e");
    if (inlineIdx >= 0) {
      return `运行脚本检查（${head.startsWith("python") ? "Python" : head}）`;
    }
    const target = extractTargetArgument(tokens, 1);
    return target ? `运行脚本检查：${target}` : "运行脚本检查";
  }
  if (["mkdir", "touch", "mv", "rm"].includes(head)) {
    const target = extractLastPathArgument(tokens, 1);
    return target ? `整理临时工作文件：${target}` : "整理临时工作文件";
  }
  return `执行检查：${truncate(inner, 84)}`;
}

function extractReasoningText(item: Record<string, unknown>): string {
  const summaryParts: string[] = [];
  if (typeof item.summary === "string" && item.summary.trim()) {
    summaryParts.push(item.summary.trim());
  } else if (Array.isArray(item.summary)) {
    for (const entry of item.summary) {
      if (typeof entry === "string" && entry.trim()) {
        summaryParts.push(entry.trim());
      } else if (entry && typeof entry === "object" && typeof (entry as Record<string, unknown>).text === "string") {
        const text = String((entry as Record<string, unknown>).text).trim();
        if (text) summaryParts.push(text);
      }
    }
  }
  const parts: string[] = [];
  const seen = new Set<string>();
  const addUnique = (value: string) => {
    const normalized = value.replace(/\s+/g, " ").trim();
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    parts.push(value.trim());
  };
  if (summaryParts.length > 0) {
    for (const part of summaryParts) addUnique(part);
  } else if (typeof item.text === "string" && item.text.trim()) {
    addUnique(item.text);
  } else if (Array.isArray(item.content)) {
    for (const entry of item.content) {
      if (entry && typeof entry === "object" && typeof (entry as Record<string, unknown>).text === "string") {
        const text = String((entry as Record<string, unknown>).text).trim();
        if (text) addUnique(text);
      }
    }
  }
  if (!parts.length) return "";
  const joined = parts
    .join(" ")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\r?\n+/g, " · ")
    .replace(/\s+/g, " ")
    .trim();
  return truncate(joined, 240);
}

function describeMcpCall(item: Record<string, unknown>): string {
  const rawTool = typeof item.tool === "string" ? item.tool.trim() : "";
  const rawArgs = item.arguments && typeof item.arguments === "object"
    ? (item.arguments as Record<string, unknown>)
    : {};
  let opId = rawTool;
  let opArgs = rawArgs;
  if (rawTool === "tools.call" && typeof rawArgs.operation_id === "string") {
    opId = rawArgs.operation_id.trim();
    if (rawArgs.arguments && typeof rawArgs.arguments === "object") {
      opArgs = rawArgs.arguments as Record<string, unknown>;
    } else {
      opArgs = {};
    }
  }
  const pathArg = typeof opArgs.path === "string" && opArgs.path.trim() ? compactPath(opArgs.path) : "";
  const refArg = typeof opArgs.reference_ref === "string" && opArgs.reference_ref.trim() ? opArgs.reference_ref.trim() : "";
  const target = pathArg || refArg;

  if (opId === "research_workspace.materials.discover") {
    return target ? `查看资料目录：${target}` : "查看已挂载资料目录";
  }
  if (opId === "research_workspace.materials.read") {
    return target ? `读取资料内容：${target}` : "读取资料内容";
  }
  if (opId === "research_workspace.materials.copy" || opId === "research_workspace.materials.copy.reconcile") {
    return target ? `复制资料到工作区：${target}` : "复制资料到工作区";
  }
  if (opId === "research_memory.creation_basis.read") {
    const view = typeof opArgs.view === "string" && opArgs.view.trim() ? `（${opArgs.view.trim()}）` : "";
    return `读取研究依据${view}`;
  }
  if (opId === "research_memory.content.read") {
    return "读取已保存材料正文";
  }
  if (opId === "tools.catalog.page" || opId === "tools.contracts.read") {
    return "检查可用工具清单";
  }
  return opId ? `调用工具：${opId}` : "调用资料工具";
}

function describeFileChange(item: Record<string, unknown>): string {
  const changes = Array.isArray(item.changes) ? item.changes : [];
  const paths = changes
    .map(entry => (entry && typeof entry === "object" && typeof (entry as Record<string, unknown>).path === "string"
      ? compactPath(String((entry as Record<string, unknown>).path))
      : ""))
    .filter(Boolean);
  if (!paths.length && typeof item.path === "string" && item.path.trim()) {
    paths.push(compactPath(item.path));
  }
  if (!paths.length) return "工作文件";
  if (paths.length === 1) return `工作文件：${paths[0]}`;
  return `工作文件：${paths[0]} 等 ${paths.length} 项`;
}

function describeCollabTool(item: Record<string, unknown>): string {
  const tool = typeof item.tool === "string" ? item.tool.trim() : "";
  if (tool === "spawn_agent") return "委派研究子任务";
  if (tool === "wait" || tool === "wait_agent") return "等待研究子任务结果";
  if (tool === "send_input") return "向研究子任务补充说明";
  if (tool === "close_agent") return "收尾研究子任务";
  return "研究子任务";
}

export function proposalStreamState(text: string): ProposalStreamState {
  const rows: ProposalActivity[] = [];
  const indexByKey = new Map<string, number>();
  let reconnecting: string | null = null;
  let reconnectEpoch = 0;
  let segment = 0;

  const upsert = (
    key: string,
    label: string,
    status: ProposalActivity["status"],
    coalesce: boolean,
  ) => {
    if (!label) return;
    if (coalesce && indexByKey.has(key)) {
      rows[indexByKey.get(key)!] = { key, label, status };
      return;
    }
    if (coalesce) indexByKey.set(key, rows.length);
    rows.push({ key, label, status });
  };

  for (const [index, line] of text.split("\n").entries()) {
    if (!line.trim()) continue;
    let event: Record<string, unknown>;
    try {
      const parsed = JSON.parse(line);
      if (!parsed || typeof parsed !== "object") continue;
      event = parsed as Record<string, unknown>;
    } catch {
      continue;
    }

    const type = event.type;
    if (type === "thread.started") {
      segment += 1;
      reconnecting = null;
      reconnectEpoch += 1;
      upsert(String(index), "生成会话已启动", "running", false);
      continue;
    }
    if (type === "turn.started") {
      segment += 1;
      reconnecting = null;
      reconnectEpoch += 1;
      upsert(String(index), "正在结合研究目标和可用资料准备第一问", "running", false);
      continue;
    }
    if (type === "turn.completed") {
      reconnecting = null;
      reconnectEpoch += 1;
      upsert(String(index), "本轮准备已完成，正在核对问题草稿", "completed", false);
      continue;
    }
    if (type === "turn.failed") {
      reconnecting = null;
      reconnectEpoch += 1;
      upsert(String(index), "本次生成未完成", "failed", false);
      continue;
    }
    if (type === "error") {
      const rawMessage = typeof event.message === "string" ? event.message : "";
      const match = rawMessage.match(/reconnecting(?:\.{0,3}|\s)*(\d+\s*\/\s*\d+)?/i);
      if (match) {
        const ratio = match[1] ? match[1].replace(/\s+/g, "") : "重试中";
        reconnecting = ratio;
        const label = match[1] ? `网络连接重试中（${ratio}）…` : "网络连接重试中…";
        upsert(`reconnect:${reconnectEpoch}`, label, "notice", true);
      } else {
        reconnecting = "重试中";
        upsert(`reconnect:${reconnectEpoch}`, "运行连接暂时中断，正在等待恢复", "notice", true);
      }
      continue;
    }

    if (type === "item.started" || type === "item.updated" || type === "item.completed") {
      const item = event.item;
      if (!item || typeof item !== "object") continue;
      const record = item as Record<string, unknown>;
      reconnecting = null;
      reconnectEpoch += 1;

      const rawId = typeof record.id === "string" && record.id.trim() ? record.id.trim() : null;
      const key = rawId !== null ? `s${segment}:${rawId}` : String(index);
      const coalesce = rawId !== null;
      const done = type === "item.completed";
      const nonZeroExit = typeof record.exit_code === "number" && record.exit_code !== 0;
      const itemFailed = record.status === "failed" || nonZeroExit;

      if (record.type === "reasoning") {
        const summary = extractReasoningText(record);
        if (summary) {
          upsert(key, `思路：${summary}`, done ? "completed" : "running", coalesce);
        }
        continue;
      }

      if (record.type === "command_execution") {
        const command = typeof record.command === "string" ? record.command : "";
        const summary = describeCommand(command);
        if (itemFailed) {
          upsert(key, `检查命令返回非零状态（已继续）：${summary}`, "notice", coalesce);
        } else if (done) {
          upsert(key, `已${summary}`, "completed", coalesce);
        } else {
          upsert(key, `正在${summary}`, "running", coalesce);
        }
        continue;
      }

      if (record.type === "mcp_tool_call") {
        const summary = describeMcpCall(record);
        if (itemFailed) {
          upsert(key, `工具调用返回提示（已继续）：${summary}`, "notice", coalesce);
        } else if (done) {
          upsert(key, `已${summary}`, "completed", coalesce);
        } else {
          upsert(key, `正在${summary}`, "running", coalesce);
        }
        continue;
      }

      if (record.type === "web_search") {
        const query = typeof record.query === "string" ? record.query.trim() : "";
        const suffix = query ? `：${truncate(query, 80)}` : "";
        if (itemFailed) {
          upsert(key, `检索未命中（已继续）${suffix}`, "notice", coalesce);
        } else if (done) {
          upsert(key, `已完成资料检索${suffix}`, "completed", coalesce);
        } else {
          upsert(key, `正在检索相关资料${suffix}`, "running", coalesce);
        }
        continue;
      }

      if (record.type === "file_change") {
        const summary = describeFileChange(record);
        upsert(key, done ? `已更新${summary}` : `正在更新${summary}`, done ? "completed" : "running", coalesce);
        continue;
      }

      if (record.type === "collab_tool_call") {
        const action = describeCollabTool(record);
        if (itemFailed) {
          upsert(key, `${action}返回提示（已继续）`, "notice", coalesce);
        } else if (done) {
          upsert(key, `${action}已完成`, "completed", coalesce);
        } else {
          upsert(key, `正在${action}`, "running", coalesce);
        }
        continue;
      }

      if (
        record.type === "agent_message" &&
        done &&
        (record.role === undefined || record.role === "assistant") &&
        (record.channel === undefined || ["commentary", "final", "final_answer"].includes(String(record.channel))) &&
        (record.phase === undefined || ["commentary", "final", "final_answer"].includes(String(record.phase)))
      ) {
        const message = typeof record.text === "string" ? record.text.trim() : "";
        if (!message || /^[{[]/.test(message)) {
          continue;
        }
        upsert(key, truncate(message, 1200), "completed", coalesce);
      }
    }
  }

  return {
    activities: rows.slice(-MAX_ACTIVITIES),
    reconnecting,
  };
}

export function proposalActivities(text: string): ProposalActivity[] {
  return proposalStreamState(text).activities;
}
