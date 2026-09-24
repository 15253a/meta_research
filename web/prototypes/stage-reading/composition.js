// Selected design: the Stage conversation is the main reader; existing work surfaces remain reachable.
import {escapeHtml as e, scenarios, summaryFindings, runtimeSummary} from './shared.js';
import {renderWorkspacePanels} from './workspace-panels.js';

const currentStatusOf = ctx => ctx.currentStatus || scenarios[ctx.state] || ctx.status || {label:'状态待确认',title:'当前研究状态待确认',detail:'已有记录仍可阅读。'};

export function renderQuestionContext(ctx) {
  return `<section class="co-question" aria-label="当前研究问题与阶段"><div class="co-question-title"><div><small>当前研究问题 <span class="co-cycle-label">Cycle ${ctx.cycleNumber} · 第 ${ctx.cycleNumber} 轮</span></small><h1>公开 EEG 数据能否支撑可靠的跨数据集验证？</h1></div><button class="co-link" data-action="panel" data-panel="question">问题详情 ↗</button></div><div class="co-stage-map" aria-label="本轮阶段，按研究需要进入">${[['idea','Idea','已有结果'],['plan','Plan','已有结果'],['bundle','Bundle','当前 · 收集实验与证据'],['reasoning','Reasoning','尚未进入']].map(([id,label,note])=>`<button class="${id==='bundle'?'current':id==='reasoning'?'future':'past'}" data-action="route" data-route="stage-${id}"><span>${label}</span><small>${note}</small></button>`).join('')}</div></section>`;
}

export function renderStatusSummary(ctx) {
  const live=runtimeSummary(ctx);
  return `<div class="co-status-strip co-discovery-strip" aria-label="研究发现、产物与运行状态">
    <button class="co-summary-card" data-action="route" data-route="quest-milestones"><span class="co-summary-label">研究进展 <span aria-hidden="true">↗</span></span><b>${e(summaryFindings.quest.text)}</b><small>整个研究 · 最近里程碑</small></button>
    <button class="co-summary-card co-question-finding" data-action="panel" data-panel="question"><span class="co-summary-label">本题发现 <span aria-hidden="true">↗</span></span><b>${e(summaryFindings.question.text)}</b><small>当前问题 · 待核验</small></button>
    <button class="co-summary-card" data-action="panel" data-panel="materials"><span class="co-summary-label">研究产物 <span aria-hidden="true">↗</span></span><b>0 份最终结果</b><small>${ctx.state==='done'?'1 份工作草稿 · 待核验':'草稿与最终结果'}</small></button>
    <button class="co-summary-card co-runtime-card" data-action="panel" data-panel="system"><span class="co-summary-label"><span class="co-heartbeat ${live.offline?'off':''}" aria-hidden="true"></span>运行状态 <span aria-hidden="true">↗</span></span><b>${e(live.text)}</b><small>${e(live.response)}</small></button>
  </div>`;
}

export function renderSupportRail(ctx) {
  const mode=ctx.rightMode||'panels';
  return `<aside class="lumen-companion-panel co-support-panel ${ctx.mobilePanel?'is-mobile-open':''}" aria-label="研究面板与研究助手"><section class="lumen-companion co-support"><header class="co-support-head"><div class="co-support-tabs" role="group" aria-label="右侧面板"><button data-action="right-mode" data-mode="panels" aria-pressed="${mode==='panels'}" class="${mode==='panels'?'active':''}">研究面板${ctx.pendingCount?'<i>1</i>':''}</button><button data-action="right-mode" data-mode="assistant" aria-pressed="${mode==='assistant'}" class="${mode==='assistant'?'active':''}"><span class="co-mini-orb" aria-hidden="true"></span>研究助手</button></div><button class="co-mobile-close" data-action="mobile-close" aria-label="关闭右侧面板">×</button></header><div class="co-support-body">${mode==='panels'?renderWorkspacePanels(ctx):renderAssistant(ctx)}</div></section></aside>`;
}

function renderAssistant(ctx) {
  const current=currentStatusOf(ctx);
  const messages = [{role:'user',text:'现在在做什么？'}, {role:'assistant',text:`${current.title}。${current.detail}`},...(ctx.chatMessages||[])];
  return `<section class="co-assistant-view"><div class="co-assistant-intro"><span class="lumen-orb" aria-hidden="true"></span><div><b>研究助手</b><small>交流与解释 · 与 Stage 执行记录分开</small></div></div><div class="co-chat" role="log" aria-label="研究助手对话">${messages.map(m=>`<div class="lumen-message ${m.role==='user'?'me':''}"><small>${m.role==='user'?'你':'研究助手 · 演示回复'}</small><p>${e(m.text)}</p></div>`).join('')}</div><details class="co-assistant-records"><summary>软约束、授权与研究控制记录</summary><p>本轮软约束示例：先完成数据纳入审计，再安排后续实验。</p><p>常规研究授权与每次具体请求分别保留记录。</p><button class="co-link" data-action="panel" data-panel="requests">查看人工协作记录 ↗</button><button class="co-link" data-action="route" data-route="control">研究控制与草案 ↗</button></details><div class="companion-composer"><label class="sr-only" for="assistant-input">给研究助手发消息</label><textarea id="assistant-input" placeholder="询问或补充这轮研究…">${e(ctx.chatDraft||'')}</textarea><div><small>Enter 发送 · Shift+Enter 换行</small><button data-action="chat-send" aria-label="发送演示消息" ${!ctx.chatDraft?.trim()?'disabled':''}>↑</button></div></div></section>`;
}

export function renderTargetDock(ctx) {
  const offline=ctx.state==='offline';
  if(ctx.targetDock==='closed')return '';
  if(ctx.targetDock==='minimized')return `<div class="co-target-min"><button data-action="target-open"><span class="live-dot ${offline?'off':''}"></span>T1 ${offline?'断开前记录':'执行记录'} <span>展开 ↗</span></button><button data-action="target-close" aria-label="关闭实验日志">×</button></div>`;
  const output=ctx.targetPage===0?[
    {n:'#T1-01',title:'已接收数据审计任务',body:'逐项检查数据访问与许可、受试者唯一性、诊断标签和跨库重合。'},
    {n:'#T1-02',title:'读取审计清单',body:'已整理本轮要求。接下来需要为每一项补齐可复核的依据。'}
  ]:[
    {n:'#T1-03',title:'正在等待可核对的材料',body:'当前尚无可认定为最终实验结果的证据。任务记录与主智能体的调度说明分别保留。'},
    {n:'#T1-04',title:'保留后续检查项',body:'待核对：可访问文件、适用许可、受试者去重依据、标签说明。'}
  ];
  return `<section class="co-target-dock" aria-label="T1 独立执行记录"><header><div><span class="task-square">T1</span><div><b>数据纳入与可行性审计</b><small>实验任务 · 独立来源 · 演示记录</small></div></div><nav><button data-action="target-minimize" aria-label="最小化实验日志">−</button><button data-action="target-close" aria-label="关闭实验日志">×</button></nav></header><div class="co-target-toolbar"><span class="live-dot ${offline?'off':''}"></span><span>${offline?'连接中断 · 保留断开前记录':'已有运行记录 · 尚无最终结果'}</span><label><input type="checkbox" id="target-raw" ${ctx.targetRaw?'checked':''}> 原始 stdout</label></div><div class="co-target-body">${ctx.targetRaw?`<pre>${e(output.map(x=>JSON.stringify({type:'item.completed',item:{type:'agent_message',id:x.n,text:x.body}})).join('\n'))}</pre>`:output.map(x=>`<article><small>${x.n} · Target 输出</small><h3>${x.title}</h3><p>${x.body}</p></article>`).join('')}<details><summary>运行身份与核验详情</summary><dl><dt>Target</dt><dd>T1_dataset_admission_feasibility_audit</dd><dt>结果提交</dt><dd>尚未形成最终结果</dd><dt>来源顺序</dt><dd>T1 的独立输出序号，不与 Stage 输出排序合并</dd></dl></details></div><footer><button class="button" data-action="target-page" data-page="0" ${ctx.targetPage===0?'disabled':''}>上一页</button><span>第 ${ctx.targetPage+1} / 2 页 · 示例</span><button class="button" data-action="target-page" data-page="1" ${ctx.targetPage===1?'disabled':''}>下一页</button><button class="co-target-follow ${ctx.targetFollow?'active':''}" data-action="target-follow">${ctx.targetFollow?'✓ 跟随最新':'↓ 跟随最新'}</button></footer></section>`;
}

export function routeBody(route,ctx) {
  const block=(label,title,body)=>`<div class="eyebrow">${label}</div><h2 id="dialog-title">${title}</h2>${body}`;
  const authorizationNote=ctx.response==='reject'?'已记录拒绝回应（示例）。本次请求已结束，等待主智能体调整执行方案。':ctx.state==='needs_input'&&!ctx.response?'本次数据审计执行范围请求尚待回应（示例）。可从“需要你”查看范围并直接接受或拒绝。':ctx.response==='accept'||ctx.state==='waiting'?'已收到接受回应（示例）。等待系统核对当前任务、范围和回应版本。':ctx.state==='offline'?'当前保留断开前的授权记录（示例）；后续处理状态暂不可观测。':'当前没有待处理授权请求。既有回应与本轮任务范围分别保留记录（示例）。';
  const stageInfo={idea:['Idea · 形成候选解释','研究工具与候选材料','<p>本轮已形成研究方向和数据证据缺口，作为当前 Plan 与 Bundle 的输入。</p><details open><summary>已形成的材料</summary><p>研究问题：公开 EEG 数据能否支撑可靠的跨数据集验证？优先核对数据纳入与验证范围。</p></details>'],plan:['Plan · 设计验证路线','验证方案','<p>已形成数据纳入审计方案。先核对许可、受试者标识、标签和跨库重合，再安排实验。</p><details open><summary>方案与验证条件</summary><p>逐项记录证据与限制；未取得依据的项目保留“待核验”，不把任务安排计作检查通过。</p></details>'],bundle:['Bundle · 当前阶段','实验与训练','<p>当前 T1 数据纳入与可行性审计已有运行记录，尚无最终结果。</p><button class="button" data-action="dialog-task">打开独立执行记录 ↗</button>'],reasoning:['Reasoning · 本轮尚未进入','综合判断','<p>尚未形成综合研究判断。当前可继续查看 Idea、Plan 材料与 Bundle 的实验活动。</p><p>获得实际结果和相关核验记录后，这里会呈现判断、证据边界与下一研究动作。</p>']};
  if(route.startsWith('stage-')){const d=stageInfo[route.slice(6)];return block(d[0],d[1],d[2]+'<details><summary>阶段身份与核验记录</summary><p>本原型使用演示材料；阶段按实际研究需要进入或跳过，不以进度百分比表示。</p></details>');}
  const routes={
    'quest-milestones':block('整个研究 · Quest','研究进展',`<p class="dialog-lead">跨 MDD、AD 与精神分裂症的可变导联 EEG 基础模型</p><div class="co-milestone"><small>最近里程碑 · 演示</small><h3>${e(summaryFindings.quest.text)}</h3><p>已有研究方向与验证方案将数据可比性列为前提：先明确许可、受试者标识与标签口径，再安排跨数据集验证。</p></div><div class="co-doc-row"><span class="task-square">计</span><div><b>依据：已有 Idea / Plan 材料</b><p>当前明确的是研究前提；数据是否实际满足条件，仍需本题审计。</p></div></div><button class="button" data-action="dialog-panel" data-panel="question">查看本题发现 ↗</button>`),
    menu:block('工作区导航','研究入口','<div class="co-menu">'+[['tree','问题树'],['assets','研究资料'],['writing','写作'],['history','历史'],['create','创建研究任务'],['control','研究控制']].map(([id,label])=>`<button class="button" data-action="route" data-route="${id}">${label} ↗</button>`).join('')+'</div>'),
    tree:block('问题树 · 保留原有导航','本轮研究与问题关系','<div class="co-tree"><div>研究目标 · 跨疾病 EEG 基础模型</div><div class="co-tree-child"><b>当前问题</b><p>公开 EEG 数据能否支撑可靠的跨数据集验证？</p><span class="tag">Bundle · 当前</span><div class="co-tree-grandchild">后续问题 · 尚未形成候选</div></div></div><button class="button" data-action="dialog-panel" data-panel="question">查看当前问题详情</button>'),
    assets:block('研究资料 · 既有材料与阶段产物','这轮研究的资料','<div class="co-doc-row"><span class="task-square">想</span><div><b>Idea · 研究方向</b><p>已形成 · 本轮输入</p></div></div><div class="co-doc-row"><span class="task-square">计</span><div><b>Plan · 数据纳入审计方案</b><p>已形成 · 当前研究依据</p></div></div><button class="button" data-action="dialog-panel" data-panel="materials">查看材料、草稿与最终结果</button>'),
    writing:block('写作工作台 · 保留原有入口','写作与研究报告','<p>写作依据与研究执行过程分别保留。当前可使用已有 Idea / Plan 材料，实验最终结果尚未形成。</p><div class="co-doc-row"><span class="task-square">文</span><div><b>研究报告</b><p>本轮尚未形成可展示的报告。</p></div></div><button class="button" disabled>当前没有报告可打开</button><button class="co-link" data-action="dialog-panel" data-panel="materials">查看已有材料 ↗</button>'),
    history:block('历史 · 保留原有入口','本轮研究记录','<div class="co-doc-row"><span class="task-square">5</span><div><b>当前调度 · dispatch-5</b><p>主任务与审查分别可读</p></div></div><button class="button" data-action="history-current">查看当前调度</button><div class="co-doc-row"><span class="task-square">4</span><div><b>上一次调度 · dispatch-4</b><p>本次输出已结束，所属 Stage 尚未完成</p></div></div><button class="button" data-action="history-previous">查看上次输出</button>'),
    create:block('新建研究 · 独立入口','创建研究任务','<p>在独立表单中准备新研究，已有研究的输出仍保留。</p><label class="co-form-label">研究目标<textarea id="create-goal" placeholder="描述研究问题与预期成果…"></textarea></label><label class="co-form-label">已有资料<input id="create-material" placeholder="文献、数据或已有线索"></label><button class="button" data-action="create-preview">预览草稿</button><p id="create-feedback" role="status"></p>'),
    control:block('研究控制与授权记录','当前研究控制','<p>暂停、继续及授权记录保留在原有顶栏与助手中。实际执行仍绑定具体研究范围。</p><div class="co-doc-row"><span class="task-square">围</span><div><b>当前 Cycle · Bundle</b><p>研究界面与执行任务分别管理</p></div></div><details><summary>现有草案与授权记录</summary><p>'+e(authorizationNote)+'</p></details><button class="button" data-action="dialog-panel" data-panel="requests">查看人工协作记录</button>'),
    acquisition:block('资料获取 · 独立来源','DeepFetch / Acquisition','<p>当前没有本轮资料获取任务。已有研究材料仍可从“研究资料”查看。</p><p>获取任务出现后，在此保留自己的命令、输出和结果，不并入 Stage 主智能体的来源顺序。</p><button class="button" data-action="dialog-panel" data-panel="materials">查看已有材料</button>'),
    artifact:block('工作产物 · 示例文档','数据审计工作小结','<span class="tag">草稿 · 待核验</span><h3>已安排的工作</h3><p>已建立 T1 数据纳入与可行性审计任务，覆盖许可、受试者标识、诊断标签和跨库重合。</p><h3>仍需取得的证据</h3><p>可访问数据文件、去重依据、标签说明与适用许可。当前不能认定这些项目均已通过审计。</p><h3>下一步</h3><p>等待实验任务回报，再核对是否满足研究计划中的纳入条件。</p>')
  };
  return routes[route]||block('研究面板','当前研究','<p>从研究面板选择需要查看的内容。</p>');
}
