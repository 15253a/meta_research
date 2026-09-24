// Throwaway UI prototype C: a reading-first research journal, selected with ?variant=C.
import { escapeHtml as e, eventMarkup, rawMarkup, relatedMarkup } from './shared.js';

export function renderVariantC(ctx) {
  const reviewing = ctx.view === 'review';
  const titles = reviewing
    ? ['读取审查材料', '核对证据边界', '形成审查意见']
    : ['核对研究材料', '建立审计任务', '等待实验回报'];
  const chapters = titles.map((title, i) => ({
    number: i + 1,
    title,
    events: ctx.events.filter(event => Number(event.step) === i + 1),
  }));
  const visibleChapters = chapters.filter(chapter => chapter.events.length);
  const status = ctx.status;

  return `<section class="variant-c" aria-label="研究日志原型">
    <div class="c-now c-now--${e(status.tone || 'neutral')}" role="status">
      <span class="c-now-label"><span class="c-status-dot" aria-hidden="true"></span>此刻</span>
      <strong>${e(status.title)}</strong>
      <span class="c-now-state">${e(status.label)}</span>
    </div>

    <header class="c-masthead">
      <div class="c-edition"><span>${reviewing ? 'REVIEW NOTES' : 'RESEARCH NOTES'}</span><span>STAGE / ${reviewing ? '审查视图' : '主任务视图'}</span></div>
      <div class="c-heading-line"><h2>${reviewing ? '审查手记' : '研究手记'}</h2><span class="c-record-count">${ctx.events.length} 条工作记录</span></div>
      <p>${e(status.detail)}</p>
    </header>

    <div class="c-reading-layout">
      <div class="c-manuscript" aria-label="主智能体工作记录">
        ${visibleChapters.length ? visibleChapters.map(chapter => `
          <section class="c-chapter" id="journal-chapter-${chapter.number}">
            <div class="c-chapter-number" aria-hidden="true">${String(chapter.number).padStart(2, '0')}</div>
            <div class="c-chapter-content">
              <header class="c-chapter-heading">
                <h3>${e(chapter.title)}</h3>
                <span>${chapter.events.length} 条记录</span>
              </header>
              <div class="c-chapter-entries">${chapter.events.map(event => `
                <div class="c-entry c-entry--${e(event.kind)}">${eventMarkup(event)}</div>
              `).join('')}</div>
            </div>
          </section>
        `).join('') : `<div class="c-empty"><span class="c-empty-rule" aria-hidden="true"></span><h3>这里将记下研究的下一步。</h3><p>${ctx.query ? '没有匹配当前搜索的记录。' : '收到主智能体的可读说明后，工作记录会显示在这里。'}</p></div>`}

        <div class="c-endnote"><span class="c-endnote-dot" aria-hidden="true"></span><span>${ctx.state === 'done' ? '本次调度输出已结束' : ctx.state === 'offline' ? '当前显示最后收到的记录' : ctx.replaying ? '正在演示工作记录逐条到达' : '已读至当前最新记录'}</span></div>
        <section class="c-source-notes" aria-label="原始记录"><span class="section-label">来源与原始记录</span><p>上方按工作阶段组织记录。每条记录均可展开原文，顺序号表示来源内的记录顺序。</p>${rawMarkup(ctx)}</section>
      </div>

      <aside class="c-marginalia" aria-label="阅读索引和研究上下文">
        <div class="c-margin-inner">
          <nav class="c-index" aria-label="手记章节">
            <h3 class="section-label">阅读索引</h3>
            ${chapters.map(chapter => `<a class="c-index-link${chapter.number === Number(ctx.step) ? ' is-current' : ''}${!chapter.events.length ? ' is-empty' : ''}" href="#journal-chapter-${chapter.number}" ${!chapter.events.length ? 'aria-disabled="true" tabindex="-1"' : ''}><span>${String(chapter.number).padStart(2, '0')}</span><span>${e(chapter.title)}${!chapter.events.length ? '<small>暂无记录</small>' : ''}</span></a>`).join('')}
          </nav>
          <div class="c-context-note"><span class="section-label">正在阅读</span><strong>${reviewing ? 'Stage 审查记录' : 'Stage 主智能体'}</strong><p>${reviewing ? '审查说明、证据核对与审查意见，集中在同一份记录里。' : '主智能体的说明、执行动作与决定，集中在同一份记录里。'}</p></div>
          <div class="c-related"><span class="section-label">关联工作</span>${relatedMarkup(ctx)}</div>
          <p class="c-order-note">按阶段阅读 · 按原文核对<br>记录顺序不代表精确发生时间。</p>
        </div>
      </aside>
    </div>
  </section>`;
}
