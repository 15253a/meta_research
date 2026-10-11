export type ProposalActivity = { key: string; label: string; status: "running" | "completed" | "failed" };

// Only public lifecycle summaries are shown. Provider reasoning, commands,
// tool arguments and raw errors stay out of the activity feed.
export function proposalActivities(text: string): ProposalActivity[] {
  const rows: ProposalActivity[] = [];
  for (const [index, line] of text.split("\n").entries()) {
    let event;
    try { event = JSON.parse(line); } catch { continue; }
    if (!event || typeof event !== "object") continue;
    let label = "";
    let status: ProposalActivity["status"] = "running";
    if (event.type === "thread.started") label = "生成会话已启动";
    if (event.type === "turn.started") label = "正在结合研究目标和可用资料准备第一问";
    if (event.type === "turn.completed") { label = "本轮准备已完成，正在核对问题草稿"; status = "completed"; }
    if (event.type === "error") label = "运行连接暂时中断，正在等待恢复";
    if (event.type === "turn.failed") { label = "本次生成未完成"; status = "failed"; }
    if (["item.started", "item.completed", "item.updated"].includes(event.type)) {
      const item = event.item;
      if (!item || typeof item !== "object") continue;
      const done = event.type === "item.completed";
      const failed = item.status === "failed";
      status = failed ? "failed" : done ? "completed" : "running";
      if (item.type === "command_execution") label = failed ? "资料准备操作未完成" : done ? "资料准备操作已完成" : "正在执行资料准备操作";
      if (item.type === "mcp_tool_call") label = failed ? "资料工具调用未完成" : done ? "资料工具调用已完成" : "正在调用资料工具";
      if (item.type === "web_search") label = failed ? "检索操作未完成" : done ? "检索操作已完成" : "正在检索相关资料";
      if (item.type === "file_change") label = done ? "工作资料已更新" : "正在更新工作资料";
      if (item.type === "collab_tool_call") label = failed ? "研究子任务未完成" : done ? "研究子任务已返回" : "研究子任务正在执行";
      if (item.type === "agent_message" && done && [undefined, "assistant"].includes(item.role)
        && [undefined, "commentary", "final", "final_answer"].includes(item.channel)
        && [undefined, "commentary", "final", "final_answer"].includes(item.phase)) {
        const message = typeof item.text === "string" ? item.text.trim() : "";
        label = message && !/^[{[]/.test(message)
          ? message.slice(0, 1200) + (message.length > 1200 ? "…" : "")
          : "当前工作说明已更新";
      }
    }
    if (label) rows.push({ key: String(index), label, status });
  }
  return rows.slice(-12);
}
