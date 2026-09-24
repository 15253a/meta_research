// PROTOTYPE: authored scenario fixtures, not a snapshot or live service data.
export const escapeHtml = (value = '') => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const e = escapeHtml;

// Authored findings and heartbeat fixtures for this prototype; no backend subscription.
export const summaryFindings = {
  quest: {label:'研究进展', scope:'整个研究 · Quest', text:'跨库验证需先统一数据标准。'},
  question: {label:'本题发现', scope:'当前问题 · Question', text:'去重依据和标签说明仍未补齐。'},
};
export function runtimeSummary(ctx) {
  const offline=ctx.state==='offline';
  const text=offline?'连接中断，状态待确认':ctx.replaying?'正在输出新的研究记录':ctx.response==='reject'?'系统在线，等待方案调整':ctx.state==='needs_input'?'系统在线，等待你的回应':ctx.state==='waiting'?'系统在线，等待核验':ctx.state==='done'?'T1 审计任务仍在执行':'正在核对数据纳入条件';
  return {text, offline, response:offline?'最近响应暂不可更新':'4 秒前收到响应 · 演示', detail:offline?'连接断开后，保留最近的研究记录；当前是否仍在执行暂不可确认。':'响应时间来自演示心跳，用于表达系统仍在线；具体在执行还是等待，以卡片中的工作状态为准。'};
}

export const scenarios = {
  running: {label:'正在工作', title:'正在核对数据纳入条件', detail:'主智能体正在整理审计范围；最新输出显示在下方。', tone:'green'},
  waiting: {label:'等待核验', title:'授权已收到，等待系统核验', detail:'核验通过且其他条件满足后，再从现有任务继续。目前无需再次操作。', tone:'amber'},
  needs_input: {label:'需要你', title:'有 1 项执行范围请求需要你处理', detail:'主智能体保留已有材料，等待你的选择。可以从右侧“需要你”处理。', tone:'amber'},
  done: {label:'本次输出结束', title:'本次调度结束，审计任务仍在继续', detail:'主智能体已交出这次工作小结；整个 Bundle 阶段尚未完成。', tone:'green'},
  offline: {label:'连接中断', title:'暂时无法接收新的活动', detail:'以下保留断开前的记录。连接中断不代表研究已经停止。', tone:'neutral'},
};

const primary = [
  {id:'p1',kind:'message',time:'#01',step:1,title:'先核对这轮研究的数据基础',body:'我会先读取 Plan 中的数据纳入条件，重点核对许可、受试者标识和诊断标签。只有通过审计的数据集，才会进入后续实验。'},
  {id:'p2',kind:'tool',time:'#02',step:1,title:'读取研究计划与数据纳入清单',body:'已读取 2 份材料',command:'read plan.md\nread dataset_admission.yaml',output:'plan.md · 研究设计与验证约束\ndataset_admission.yaml · 数据纳入条件\n状态：读取完成'},
  {id:'p3',kind:'message',time:'#03',step:2,title:'把证据缺口转成一个可执行的审计任务',body:'当前需要补齐三类信息：\n• 数据是否可访问、许可是否适用；\n• 受试者是否唯一，重复访视如何处理；\n• 诊断标签与跨数据集重合是否明确。\n我会把这些检查交给 T1，并保留它的独立执行记录。'},
  {id:'p4',kind:'tool',time:'#04',step:2,title:'建立数据可用性审计任务',body:'已关联实验任务 T1',command:'dispatch_target · T1_dataset_admission_feasibility_audit',output:'目标：数据纳入与可行性审计\n范围：许可 / 受试者去重 / 标签 / 跨库重合\n派发结果：已关联现有任务，不重复创建'},
  {id:'p5',kind:'message',time:'#05',step:3,title:'审计交给 T1 后，我会等待它回报',body:'T1 已有运行记录。我会沿用这个任务，等它返回审计证据后再判断是否进入后续实验。当前还没有可认定为最终结果的产物。'},
  {id:'p6',kind:'decision',time:'#06',step:3,title:'授权已收到；目前等待系统核验',body:'当前仍不能派发下一步。授权回应已收到，系统正在核验和消费这次回应；我会保留现有任务，不重复创建，也不再向你重复索要同一授权。',raw:{action:'wait',rationale:'Target 已有运行记录；当前 dispatch_allowed=false。授权回应已收到，等待 Owner 核验和 waiter 消费，保持同一 Target 运行边界。',selected_target_ref:null}},
];
const review = [
  {id:'r1',kind:'message',time:'#01',step:1,title:'我会独立核对这次调度的依据',body:'先检查主任务交出的材料是否覆盖研究计划中的数据纳入条件，再核对结论是否超出已有证据。这一段属于审查输出。'},
  {id:'r2',kind:'tool',time:'#02',step:1,title:'读取主任务材料与验证约束',body:'已读取 2 份审查材料',command:'read bundle_materials.md\nread acceptance_criteria.yaml',output:'主任务材料：可读取\n验证约束：可读取\n审查范围：当前调度材料'},
  {id:'r3',kind:'message',time:'#03',step:2,title:'任务已建立，但审计结果还没有返回',body:'材料说明了数据访问、许可和受试者去重的检查范围。目前只能确认任务已安排，不能把这些检查写成已经通过。'},
  {id:'r4',kind:'decision',time:'#04',step:3,title:'保留未核验项，等待后续证据',body:'建议保留“待核验”标记，收到实验任务的证据后继续审查。当前没有足够依据宣告 Bundle 阶段完成。'},
];

export function scenarioEvents(state, view) {
  if (view === 'review') return (state === 'running' ? review.slice(0,3) : review).map(ev=>({...ev,source:'review'}));
  if (state === 'needs_input') return [...primary.slice(0,5),{id:'p6-request',kind:'decision',time:'#06',step:3,title:'下一步执行范围需要你确认',body:'这一步需要确认具体执行范围。我已把请求放到“需要你”，会保留当前材料，收到回应后再核对后续执行条件。'}];
  if (state === 'running') return primary.slice(0,4);
  if (state === 'done') return [...primary.slice(0,5), {id:'p7',kind:'artifact',time:'#06',step:3,title:'这次调度的小结已整理',body:'已列出数据审计范围与后续检查项。T1 仍在执行；这是一份待核验的工作小结。'}];
  return primary;
}

export function eventMarkup(ev) {
  const raw = ev.raw ? JSON.stringify(ev.raw,null,2) : ev.body;
  const source = `<details class="event-source"><summary>原文</summary><pre>${e(raw)}</pre></details>`;
  if (ev.kind === 'tool') return `<article class="event tool-event" data-event-id="${e(ev.id)}"><details class="tool-row"><summary><span class="seq">${e(ev.time)}</span><span class="tool-mark" aria-hidden="true">⌘</span><span class="tool-text">${e(ev.title)}<small>${e(ev.body)}</small></span><span class="tool-result">已完成</span><span class="chevron" aria-hidden="true">⌄</span></summary><div class="tool-expanded"><label>命令 / 操作</label><pre>${e(ev.command)}</pre><label>返回结果</label><pre>${e(ev.output)}</pre></div></details></article>`;
  return `<article class="event ${e(ev.kind)}-event" data-event-id="${e(ev.id)}"><div class="event-meta"><span class="seq">${e(ev.time)}</span><span>${ev.source==='review'?'审查输出':ev.kind === 'decision' ? '策略决定' : ev.kind === 'artifact' ? '工作产物' : '主智能体输出'}</span>${source}</div><h3>${e(ev.title)}</h3><div class="event-body">${e(ev.body).split('\n').map(l=>`<p>${l}</p>`).join('')}</div>${ev.kind==='artifact'?'<button class="artifact-link" data-action="artifact"><span aria-hidden="true">▤</span><span>数据审计工作小结<small>Markdown · 草稿，待核验</small></span><span aria-hidden="true">↗</span></button>':''}</article>`;
}

export function statusMarkup(ctx) {
  return `<div class="status-note ${e(ctx.status.tone)}"><span class="status-symbol" aria-hidden="true">${ctx.state==='offline'?'↻':ctx.state==='waiting'?'Ⅱ':'◉'}</span><div><strong>${e(ctx.status.title)}</strong><p>${e(ctx.status.detail)}</p></div></div>`;
}

export function relatedMarkup(ctx) {
  return `<section class="related-block"><div class="section-label">关联实验 <span>1</span></div><button class="related-task" data-action="task"><span class="task-square">T1</span><span><b>数据纳入与可行性审计</b><small>${ctx.state==='running'?'任务已建立':'有运行记录 · 尚无最终结果'}</small></span><span aria-hidden="true">↗</span></button><p class="quiet-note">实验任务保留自己的执行记录。</p></section><section class="related-block"><div class="section-label">研究产物</div>${ctx.state==='done'?'<button class="related-task" data-action="artifact"><span class="task-square">稿</span><span><b>数据审计工作小结</b><small>草稿 · 待核验</small></span><span aria-hidden="true">↗</span></button>':'<div class="empty-product"><span aria-hidden="true">▱</span><p>最终结果尚未形成<small>任务回报后会显示在这里</small></p></div>'}</section>`;
}

export function rawMarkup(ctx) {
  const jsonl = [{type:'thread.started',thread_id:'prototype-thread'},...ctx.events.map(ev=>({type:'item.completed',item: ev.kind==='tool'?{id:ev.id,type:'command_execution',command:ev.command,aggregated_output:ev.output,status:'completed',exit_code:0}:{id:ev.id,type:'agent_message',text:ev.raw?JSON.stringify(ev.raw):ev.body}}))].map(x=>JSON.stringify(x)).join('\n');
  return `<details class="raw-record"><summary>查看原始记录 <span>JSONL · ${ctx.view==='review'?'审查':'主任务'} · 演示数据</span></summary><pre>${e(jsonl)}</pre><p>输出按该来源的记录顺序排列。此处为原型示例。</p></details>`;
}
