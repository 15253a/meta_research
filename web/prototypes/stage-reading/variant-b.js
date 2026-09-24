// Throwaway prototype B: a step-indexed reading workspace for one Stage agent.
import { escapeHtml as e, eventMarkup, statusMarkup, rawMarkup, relatedMarkup } from './shared.js';

export function renderVariantB(ctx) {
  const groups = ctx.view === 'review' ? [
    { id: 1, title: '读取审查材料', subtitle: '回报、结论与核对范围', description: '先读主智能体收到了哪些材料，以及本轮审查关注的问题。' },
    { id: 2, title: '核对证据边界', subtitle: '证据范围与适用条件', description: '阅读证据核对过程，了解哪些判断有依据，以及哪些边界仍需保留。' },
    { id: 3, title: '形成审查意见', subtitle: '审查判断与后续建议', description: '查看主智能体形成的审查意见，以及下一步需要补充或确认的内容。' },
  ] : [
    { id: 1, title: '核对研究材料', subtitle: '目标、范围与现有线索', description: '先读主智能体如何理解已有材料，以及它准备核对的问题。' },
    { id: 2, title: '建立审计任务', subtitle: '具体操作与委派说明', description: '查看主智能体做了什么、为什么这样做，以及交给实验任务的问题。' },
    { id: 3, title: '等待实验回报', subtitle: '当前判断与下一步', description: '阅读最新的判断，了解现在还在等待什么，以及回报后会继续做什么。' },
  ];
  const current = Number(ctx.step) || 1;
  const selected = groups.find(group => group.id === current) || groups[0];
  const events = (ctx.events || []).filter(event => Number(event.step) === selected.id);
  const entryCount = events.length;

  return `<section class="variant-b" aria-label="步骤导航版研究活动原型">
    <div class="b-workspace-heading">
      <div class="b-workspace-name"><span class="b-agent-mark" aria-hidden="true">S</span><div><span class="eyebrow">STAGE WORKSPACE</span><h2>主智能体的工作过程</h2></div></div>
      <span class="b-source-label"><span aria-hidden="true"></span>${ctx.view === 'review' ? 'Bundle 审查 · 独立来源' : 'Bundle 策略 · 当前来源'}</span>
    </div>

    <div class="b-workspace">
      <aside class="b-directory" aria-label="本轮活动目录">
        <div class="b-directory-heading"><span class="section-label">本轮阅读目录</span><span class="b-index-count">03</span></div>
        <p class="b-directory-note">示例活动分组 · 选择后展开阅读</p>
        <nav class="b-steps" aria-label="示例活动分组">
          ${groups.map(group => {
            const count = (ctx.events || []).filter(event => Number(event.step) === group.id).length;
            return `<button type="button" class="b-step ${selected.id === group.id ? 'is-selected' : ''}" data-action="step" data-step="${group.id}" aria-pressed="${selected.id === group.id}">
              <span class="b-step-number">${String(group.id).padStart(2, '0')}</span>
              <span class="b-step-copy"><strong>${group.title}</strong><span>${group.subtitle}</span><span class="b-step-count">${count ? `${count} 条可见记录` : '暂无可见记录'}</span></span>
              <span class="b-step-arrow" aria-hidden="true">↗</span>
            </button>`;
          }).join('')}
        </nav>
        <div class="b-directory-bottom">
          <span class="b-directory-key" aria-hidden="true">≡</span>
          <p>这里按活动整理输出。<br><span>分组为原型示例，<br>不表示已验证的执行计划。</span></p>
        </div>
      </aside>

      <div class="b-reading-workspace">
        <div class="b-state-strip">${statusMarkup(ctx)}</div>
        <article class="b-reader" aria-labelledby="b-reading-title">
          <header class="b-reader-heading">
            <div class="b-reader-kicker"><span class="b-part-label">活动 ${String(selected.id).padStart(2, '0')}</span><span>${ctx.view === 'review' ? '审查输出' : '主智能体输出'}</span></div>
            <div class="b-reader-title-row"><h3 id="b-reading-title">${selected.title}</h3><span class="b-record-count">${entryCount} 条记录</span></div>
            <p>${selected.description}</p>
          </header>

          <div class="b-reading-content">
            ${entryCount ? `<div class="b-events">${events.map(event => `<div class="b-entry b-entry-${e(event.kind)}">${eventMarkup(event)}</div>`).join('')}</div>` : `<div class="b-empty"><span aria-hidden="true">≡</span><h4>这个活动还没有可见输出</h4><p>可以从左侧选择其他活动，查看已经出现的记录。</p></div>`}
          </div>

          <footer class="b-reader-footer"><span class="b-end-mark" aria-hidden="true"></span><span>当前活动的可见记录已读至此</span><span class="b-source-order">记录编号仅表示本来源顺序</span></footer>
        </article>

        <div class="b-context-area">
          <div class="b-context-heading"><span class="section-label">关联上下文</span><span>实验任务与研究产物</span></div>
          ${relatedMarkup(ctx)}
        </div>
        <div class="b-raw-area">${rawMarkup(ctx)}</div>
      </div>
    </div>
  </section>`;
}
