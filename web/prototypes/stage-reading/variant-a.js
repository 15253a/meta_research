import {eventMarkup, rawMarkup} from './shared.js';

export function renderVariantA(ctx) {
  const historical=ctx.dispatch==='previous';
  const review=ctx.view==='review';
  const source=review?'审查':'主任务';
  const ended=historical||ctx.state==='done';
  const footer=historical?`历史${source}输出已结束；返回当前调度查看后续工作`:ctx.replaying?`正在回放${source}演示输出…`:ctx.state==='offline'?`连接中断；保留已收到的${source}输出`:ended?`本次${source}输出已结束；Bundle 阶段尚未完成`:review?(ctx.state==='running'?'跟随当前审查输出':'审查等待后续证据；未核验项继续保留'):ctx.response==='reject'?'已记录拒绝回应；等待主智能体调整方案':ctx.state==='needs_input'?'等待你的回应；已有记录保持可读':ctx.state==='waiting'?'等待下一条主任务活动；已有记录保持可读':'跟随当前主智能体的输出';
  return `<div class="variant-a"><section class="conversation-main" aria-label="Stage 主智能体对话流">
    <div class="conversation-title"><span class="agent-avatar" aria-hidden="true">✳</span><div><h2>${ctx.view==='review'?'Bundle 审查输出':'Bundle 策略主智能体'}</h2><p><span class="co-stream-cycle">Cycle ${ctx.cycleNumber} · 第 ${ctx.cycleNumber} 轮</span> · ${ctx.view==='review'?'核对材料和证据边界':'收集实验与证据'}</p></div><span class="tag">${historical?`历史 · ${source}`:review?'审查':'当前 Stage'}</span></div>
    <div class="stream-heading"><span>${ctx.view==='review'?'审查输出':'工作过程'}</span><span>按输出顺序</span></div>
    <div class="conversation-feed">${ctx.events.length?ctx.events.map(ev=>eventMarkup(ev)+(ev.id==='p4'?'<button class="handoff-link" data-action="task"><span class="task-square">T1</span><span>数据纳入与可行性审计<small>查看实验任务的独立执行记录</small></span><span>↗</span></button>':ev.id==='p6-request'?'<button class="button inline-request" data-action="panel" data-panel="requests">查看需要你处理的请求 · 1</button>':'')).join(''):'<p class="empty-message">这段记录中没有匹配的输出。</p>'}</div>
    <div class="feed-end"><span class="live-dot ${ctx.state==='offline'||ended?'off':''}"></span>${footer}${historical?'':`<button data-action="follow">${ctx.follow?'↓ 跟随最新':'↓ 回到最新'}</button>`}</div>
    ${rawMarkup(ctx)}
  </section></div>`;
}
