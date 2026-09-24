// Prototype fixtures only. These are not observations of the 8767 service.
import { escapeHtml as e, summaryFindings, runtimeSummary } from './shared.js';

const questionTitle = '公开 EEG 数据能否支撑可靠的跨数据集验证？';
const panelButton = (panel, text, extra = '') => `<button type="button" class="wp-link ${extra}" data-action="panel" data-panel="${e(panel)}">${e(text)}<span aria-hidden="true">↗</span></button>`;
const sourceButton = (source, text) => `<button type="button" class="wp-button" data-action="stream" data-stream="${e(source)}">${e(text)}<span aria-hidden="true">↗</span></button>`;
const badge = (text, tone = '') => `<span class="wp-badge ${tone}">${e(text)}</span>`;
const detailRow = (label, text) => `<div><dt>${e(label)}</dt><dd>${e(text)}</dd></div>`;

function taskStatus(ctx) {
  if (ctx.state === 'offline') return '断开前有运行记录';
  if (ctx.state === 'running') return '任务已建立';
  return '已有运行记录';
}

function targetCard(ctx, full = false) {
  return `<article class="wp-target">
    <div class="wp-target-head"><span class="wp-task-mark">T1</span><div><b>数据纳入与可行性审计</b><small>${e(taskStatus(ctx))} · 尚无最终结果</small></div></div>
    ${full ? '<p>核对数据访问与许可、受试者去重、诊断标签和跨库重合；检查完成后返回可复核的依据。</p><div class="wp-tags"><span>许可与访问</span><span>受试者去重</span><span>标签一致性</span></div>' : ''}
    <div class="wp-target-actions"><button type="button" class="wp-link" data-action="task">运行详情<span aria-hidden="true">↗</span></button><button type="button" class="wp-link" data-action="stream" data-stream="target">独立执行记录<span aria-hidden="true">↗</span></button></div>
  </article>`;
}

function artifactCard() {
  return `<button type="button" class="wp-artifact" data-action="artifact"><span class="wp-document-mark" aria-hidden="true">稿</span><span><b>数据审计工作小结</b><small>Bundle · 草稿，待核验</small></span><span aria-hidden="true">↗</span></button>`;
}

function terminalPreview() {
  return `<div class="wp-terminal-preview"><small>T2 · 训练与评估日志示例</small><div><button type="button" class="wp-link" data-action="terminal-open" data-mode="train">训练日志 <span aria-hidden="true">↗</span></button><button type="button" class="wp-link" data-action="terminal-open" data-mode="eval">评估日志 <span aria-hidden="true">↗</span></button></div></div>`;
}

function overview(ctx) {
  const needsInput = ctx.state === 'needs_input' && !ctx.response;
  return `<div class="wp-overview">
    <div class="wp-question-short"><span>Cycle ${ctx.cycleNumber} · Bundle</span>${panelButton('question', '问题详情')}</div>
    <section class="wp-card"><div class="wp-card-head"><h3>实验与训练</h3>${panelButton('experiments', '查看全部')}</div>${targetCard(ctx)}${terminalPreview()}</section>
    <section class="wp-card"><div class="wp-card-head"><h3>资料获取</h3>${panelButton('materials', '查看材料')}</div><p class="wp-muted">当前没有资料获取任务。</p><div class="wp-inline-state"><span class="wp-soft-dot" aria-hidden="true"></span><span>已有 Idea、Plan 材料可查阅</span></div></section>
    <section class="wp-card"><div class="wp-card-head"><h3>研究产物</h3>${badge('0 份最终结果')}</div>${ctx.state === 'done' ? artifactCard() : '<p class="wp-muted">等待实验回报与阶段核验。</p>'}<div class="wp-card-footer">${panelButton('materials', '材料与草稿')}${panelButton('next', '后续与收口')}</div></section>
    <section class="wp-card ${needsInput ? 'wp-needs-input' : ''}"><div class="wp-card-head"><h3>需要你${needsInput ? ' · 1' : ''}</h3>${panelButton('requests', needsInput ? '处理请求' : '查看')}</div><div class="wp-empty-line"><span class="wp-check ${needsInput ? 'is-attention' : ''}" aria-hidden="true">${needsInput ? '!' : '✓'}</span><div><b>${needsInput ? '数据审计需要一次授权' : '当前没有待你处理的事项'}</b><small>${needsInput ? '查看准确范围，然后直接接受或拒绝。' : ctx.response ? '演示回应已记录，无需重复提交。' : ctx.state === 'waiting' ? '授权已收到，正在等待系统核验。' : '出现需要你回应的事项时会在此提醒。'}</small></div></div></section>
    <section class="wp-system-glance"><details><summary><span>系统核验</span><span class="wp-summary-status">${ctx.state === 'offline' ? '连接中断' : '来源记录可追溯'}</span></summary><p>${ctx.state === 'offline' ? '当前保留断开前的记录，无法判断服务之后的状态。' : '主智能体、资料获取与实验任务分别保留自己的顺序和来源。'} 本次模型输出结束，不表示整个研究阶段完成。</p>${panelButton('system', '连接与核验详情')}</details></section>
  </div>`;
}

function question(ctx) {
  return `<section class="wp-card wp-detail-intro"><span class="wp-eyebrow">当前研究问题</span><h3>${questionTitle}</h3><p>先确认数据是否适合纳入，再确定能被实验回答的范围。</p></section>
    <section class="wp-card"><div class="wp-card-head"><h3>本题发现</h3>${badge('待核验')}</div><p><b>${e(summaryFindings.question.text)}</b></p><p class="wp-caption">当前材料能说明审计范围，还不足以确认数据已满足纳入条件。T1 将继续补齐依据。</p></section>
    <section class="wp-card"><div class="wp-card-head"><h3>仍需补齐什么</h3>${badge('3 项未知')}</div><ul class="wp-evidence-list"><li>数据访问与使用许可是否满足研究用途</li><li>受试者能否唯一识别，跨库样本是否重合</li><li>诊断标签与评估口径能否对齐</li></ul></section>
    <section class="wp-card"><div class="wp-card-head"><h3>预期回答</h3></div><p>一份数据纳入审计清单，逐项记录证据、限制和是否可进入实验。</p><dl class="wp-facts">${detailRow('研究范围', '本轮聚焦数据可行性；尚未确认模型效果。')}${detailRow('当前阶段', 'Bundle · 组织实验与训练')}${detailRow('此前材料', 'Idea 已形成研究方向；Plan 已形成研究计划。')}</dl></section>
    <section class="wp-card"><details class="wp-details"><summary>研究身份与关联</summary><dl class="wp-facts">${detailRow('Quest', 'EEG 跨数据集验证（原型示例）')}${detailRow('Cycle', '第 1 轮研究')}${detailRow('Question', '当前问题 · question_demo_01')}${detailRow('Bundle', 'bundle_demo_01')}${detailRow('关联实验', 'T1_dataset_admission_feasibility_audit')}</dl></details></section>
    ${panelButton('materials', '查看本题研究材料', 'wp-bottom-link')}`;
}

function experiments(ctx) {
  const tracking = ctx.response === 'reject' ? '已记录拒绝回应；等待主智能体调整执行方案。' : ctx.state === 'offline' ? '保留断开前的任务记录；当前进展暂不可观测。' : ctx.state === 'needs_input' && !ctx.response ? '保留已有任务；本次执行范围请求等待你的回应。' : ctx.response === 'accept' || ctx.state === 'waiting' ? '接受回应已收到；当前等待系统核验。' : '沿用 T1 的独立运行记录，等待证据回报。';
  return `<section class="wp-card wp-detail-intro"><span class="wp-eyebrow">Bundle 阶段</span><h3>实验与训练</h3><p>查看任务的目标、运行记录，以及哪些结果已通过核验。</p><div class="wp-inline-state"><span class="wp-soft-dot" aria-hidden="true"></span><span>1 个实验任务 · 0 份最终实验结果</span></div></section>
    <section class="wp-card">${targetCard(ctx, true)}${terminalPreview()}</section>
    <section class="wp-card"><div class="wp-card-head"><h3>阶段进展</h3></div><ol class="wp-stage-list"><li class="is-complete"><span>✓</span><div><b>研究计划已承接</b><small>数据纳入条件与审计范围已明确。</small></div></li><li class="is-current"><span>2</span><div><b>${ctx.state === 'offline' ? '任务记录暂未更新' : '任务正在接受跟踪'}</b><small>${tracking}</small></div></li><li><span>3</span><div><b>结果尚未冻结</b><small>收到证据并通过核验后才会形成最终产物。</small></div></li></ol></section>
    <section class="wp-card"><details class="wp-details"><summary>系统如何核验这段研究</summary><dl class="wp-facts">${detailRow('任务边界', 'T1 保留自己的身份与执行顺序。')}${detailRow('结果提交', '尚未形成 TargetCommit。')}${detailRow('阶段提交', '尚未形成 Bundle StageCommit。')}${detailRow('后续衔接', '满足本阶段条件后，才进入 Reasoning。')}</dl><p class="wp-muted">一次 Provider 输出结束，仅说明本次调用结束。</p></details></section>`;
}

function materials(ctx) {
  return `<section class="wp-card wp-detail-intro"><span class="wp-eyebrow">材料与产物</span><h3>这轮研究的依据</h3><p>已有阶段材料、过程草稿与最终结果分别标明。</p></section>
    <section class="wp-card"><div class="wp-card-head"><h3>资料获取</h3>${badge('暂无任务')}</div><p class="wp-muted">DeepFetch / Acquisition 暂无本轮可展示的获取记录。</p>${sourceButton('acquisition', '查看资料获取记录')}</section>
    <section class="wp-card"><div class="wp-card-head"><h3>已有阶段材料</h3></div><details class="wp-material"><summary><span class="wp-document-mark">想</span><span><b>Idea · 研究方向</b><small>已形成 · 本轮输入</small></span><span aria-hidden="true">⌄</span></summary><p>比较公开 EEG 数据在跨数据集验证中的可用性，先梳理研究问题与证据缺口。</p></details><details class="wp-material"><summary><span class="wp-document-mark">计</span><span><b>Plan · 研究计划</b><small>已形成 · 本轮输入</small></span><span aria-hidden="true">⌄</span></summary><p>先进行数据纳入审计：许可与访问、受试者去重、标签一致性及跨库重合；审计通过后再安排实验。</p></details></section>
    <section class="wp-card"><div class="wp-card-head"><h3>本阶段工作草稿</h3>${badge(ctx.state === 'done' ? '1 份' : '尚未形成')}</div>${ctx.state === 'done' ? artifactCard() : '<p class="wp-muted">主智能体交出工作小结后，会在这里保留可阅读的草稿。</p>'}<p class="wp-caption">草稿可帮助理解过程；尚未通过阶段核验。</p></section>
    <section class="wp-card"><div class="wp-card-head"><h3>最终实验结果</h3>${badge('0 份')}</div><p class="wp-muted">T1 尚未提交可冻结的最终结果。此处不会把命令输出或工作草稿计作最终产物。</p></section>`;
}

function requests(ctx) {
  if (ctx.state === 'needs_input' || ctx.response) {
    const responded = Boolean(ctx.response);
    return `<section class="wp-card wp-detail-intro"><span class="wp-eyebrow">人工协作 · 演示请求</span><h3>${responded ? '回应已记录' : '数据审计需要你的授权'}</h3><p>${responded ? '该操作仅更新本页演示状态，未向 8767 提交任何请求。' : 'T1 需要在本次研究的指定目录执行数据审计命令，并保存核验材料。'}</p>${badge(responded ? ctx.response === 'accept' ? '已接受 · 演示' : '已拒绝 · 演示' : '待你回应', responded ? 'green' : 'amber')}</section>
      <section class="wp-card"><div class="wp-card-head"><h3>本次请求的范围</h3></div><dl class="wp-facts">${detailRow('发起方', 'Bundle 策略主智能体')}${detailRow('关联任务', 'T1 · 数据纳入与可行性审计')}${detailRow('允许事项', '仅在本轮指定的研究工作目录执行 Plan 约束内的数据审计命令，并写入审计材料。')}${detailRow('用途', '核验许可、受试者去重、诊断标签与跨库重合。')}${detailRow('范围限制', '不包含创建额外实验、扩大数据访问范围或执行模型训练。')}</dl><details class="wp-details"><summary>影响说明与请求版本</summary><p>接受后，系统仍需核对当前任务与授权范围。只有核对通过且没有其他阻碍，工作才会继续。</p><dl class="wp-facts">${detailRow('请求', 'human_request_demo:r1')}${detailRow('版本', 'Bundle generation 3 · 原型示例')}${detailRow('绑定任务', 'target_demo_T1')}${detailRow('影响', '可能执行审计命令并写入该任务的工作材料。')}</dl></details>
      ${responded ? `<div class="wp-notice"><b>${ctx.response === 'accept' ? '已演示接受，等待系统核验' : '已演示拒绝，等待主智能体处理'}</b><p>无需再确认或重复提交。真实系统会把这次回应交还负责该请求的智能体。</p></div>` : '<div class="wp-response-actions"><button type="button" class="wp-response-accept" data-action="demo-response" data-choice="accept">接受 · 演示</button><button type="button" class="wp-response-reject" data-action="demo-response" data-choice="reject">拒绝 · 演示</button></div><p class="wp-caption">点选即可记录演示回应；影响说明无需先展开。</p>'}</section><p class="wp-footnote">这是交互原型中的精确请求示例，不代表当前服务中有待授权事项。</p>`;
  }
  return `<section class="wp-card wp-detail-intro"><span class="wp-eyebrow">人工协作</span><h3>需要你</h3><div class="wp-empty-state"><span class="wp-check" aria-hidden="true">✓</span><b>当前没有待你处理的事项</b><p>新的请求会说明原因和影响，并在这里直接显示回应入口。</p></div></section>
    <section class="wp-card"><div class="wp-card-head"><h3>已回应的事项</h3>${badge('示例记录')}</div><details class="wp-details" ${ctx.state === 'waiting' ? 'open' : ''}><summary>数据审计授权已收到</summary><div class="wp-notice"><b>等待系统核验</b><p>你的回应已经提交。核验通过且其他条件满足后，工作会从现有任务继续。</p></div><dl class="wp-facts">${detailRow('你的回应', '接受（演示数据）')}${detailRow('对应范围', '当前 T1 数据纳入与可行性审计')}${detailRow('系统处理', '核对当前任务、授权范围与回应版本')}${detailRow('当前是否需要操作', '无需重复提交同一授权')}</dl></details></section>
    <p class="wp-footnote">此面板仅演示布局和状态，没有连接真实授权提交接口。</p>`;
}

function system(ctx) {
  const offline = ctx.state === 'offline';
  const live=runtimeSummary(ctx);
  return `<section class="wp-card wp-detail-intro"><span class="wp-eyebrow">运行状态</span><h3>系统现在在做什么</h3><div class="wp-notice ${offline ? 'is-offline' : ''}"><b>${e(live.text)}</b><p>${e(live.detail)}</p></div></section>
    <section class="wp-card"><div class="wp-card-head"><h3>最近响应与工作状态</h3></div><dl class="wp-facts">${detailRow('系统响应', live.response)}${detailRow('当前工作', live.text)}${detailRow('数据来源', '本页演示状态与模拟心跳；没有订阅 8767 后端。')}</dl><p class="wp-muted">系统有响应时，任务仍可能在等待核验、材料或你的回应。响应时间不等于任务进度。</p></section>
    <section class="wp-card"><div class="wp-card-head"><h3>核验与来源</h3></div><dl class="wp-facts">${detailRow('核验版本', '当前调度 · 演示版本')}${detailRow('主智能体', 'Bundle 策略；主任务与审查分别存档')}${detailRow('实验任务', 'T1；独立执行记录')}${detailRow('资料获取', '当前无获取记录')}</dl><p class="wp-muted">每条输出按自己的来源顺序展示，保留原始记录入口。</p></section>
    <section class="wp-card"><details class="wp-details"><summary>完成状态如何判断</summary><p>一次模型调用结束，只代表这次输出已收齐。实验最终结果与阶段完成，需要各自的提交记录和核验依据。</p><dl class="wp-facts">${detailRow('本次模型输出', ctx.state === 'done' ? '已结束' : offline ? '暂不可观测' : '按当前来源记录显示')}${detailRow('最终实验结果', '尚未形成')}${detailRow('Bundle 阶段', '尚未完成')}${detailRow('Reasoning 阶段', '尚未形成结果')}</dl></details></section>`;
}

function next(ctx) {
  return `<section class="wp-card wp-detail-intro"><span class="wp-eyebrow">后续与收口</span><h3>从已有结果继续</h3><p>后续问题与结束研究的入口保留在这里，开放条件会随阶段进展更新。</p></section>
    <section class="wp-card"><div class="wp-card-head"><h3>后续问题</h3>${badge('等待研究判断')}</div><p>当前 Bundle 尚未完成，也尚未形成 Reasoning 结果。获得研究判断后，可在这里查看下一问与其依据。</p><button type="button" class="wp-button" disabled>尚无可选择的后续问题</button></section>
    <section class="wp-card"><div class="wp-card-head"><h3>结束研究</h3>${badge('条件未满足')}</div><p>研究收口需要明确的判断与可审阅的结果。本次主智能体输出结束不会自动结束整个研究。</p><button type="button" class="wp-button" disabled>等待研究收口条件</button></section>
    <section class="wp-card"><div class="wp-card-head"><h3>现在可以查看</h3></div>${panelButton('experiments', '实验与训练的当前进展', 'wp-row-link')}${panelButton('materials', '已有研究材料与草稿', 'wp-row-link')}${panelButton('system', '阶段完成的核验依据', 'wp-row-link')}</section>`;
}

export function renderWorkspacePanels(ctx) {
  const panels = { overview, question, experiments, materials, requests, system, next };
  const panel = panels[ctx.panel] ? ctx.panel : 'overview';
  const titles = { question: '当前问题', experiments: '实验与训练', materials: '材料与产物', requests: '需要你', system: '系统核验', next: '后续与收口' };
  return `<div class="workspace-panels" data-workspace-panel="${e(panel)}">
    ${panel !== 'overview' ? `<header class="wp-navigation"><button type="button" class="wp-link" data-action="panel" data-panel="overview"><span aria-hidden="true">←</span>研究面板</button><span>${e(titles[panel])}</span></header>` : ''}
    <div class="wp-demo-label"><span aria-hidden="true">◇</span> 原型演示 · 非当前服务状态</div>
    ${panels[panel](ctx)}
  </div>`;
}
