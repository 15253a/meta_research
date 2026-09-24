// PROTOTYPE: authored CLI log fixtures only. No backend, subprocess or network access.
import { escapeHtml } from './shared.js';

const e = escapeHtml;
const count = value => Math.max(0, Math.min(50, Math.floor(Number(value) || 0)));

export function terminalLines(mode, extra = 0) {
  const more = count(extra);
  if (mode === 'eval') {
    const lines = [
      '# 演示日志 · T2 · 评估 · 非真实后端输出',
      '$ python evaluate.py --checkpoint checkpoints/epoch_026.pt --split held_out',
      '[config] experiment=cross_dataset_demo  seed=42  device=cuda:0',
      '[data] held_out split loaded; subject overlap check: 0',
      '[model] checkpoint loaded: checkpoints/epoch_026.pt',
      '[eval] batch_size=32  batches=160  metrics=loss,accuracy,macro_f1',
      '',
    ];
    for (let batch = 1; batch <= 80 + more; batch += 1) {
      const progress = Math.floor(batch / 160 * 100);
      const loss = (0.67 - batch * 0.0016 + (batch % 4) * 0.0008).toFixed(4);
      const acc = (0.702 + batch * 0.00061).toFixed(4);
      const f1 = (0.683 + batch * 0.00062).toFixed(4);
      lines.push(`[eval] batch ${String(batch).padStart(3, '0')}/160  ${String(progress).padStart(2)}%  loss=${loss}  running_accuracy=${acc}  running_macro_f1=${f1}  samples=${batch * 32}  elapsed=${(batch * 0.43).toFixed(1)}s`);
    }
    return lines;
  }
  const lines = [
    '# 演示日志 · T2 · 训练 · 非真实后端输出',
    '$ python train.py --config configs/cross_dataset_demo.yaml --epochs 80',
    '[config] experiment=cross_dataset_demo  seed=42  device=cuda:0',
    '[data] training and validation splits loaded; subject overlap check: 0',
    '[model] EEGEncoder  parameters=1,248,512  precision=fp32',
    '[train] batch_size=64  optimizer=AdamW  lr=0.000300  epochs=80',
    '',
  ];
  for (let epoch = 1; epoch <= 26 + more; epoch += 1) {
    const trainLoss = (0.86 / (1 + epoch * 0.054)).toFixed(4);
    const valLoss = (0.91 / (1 + epoch * 0.043)).toFixed(4);
    const f1 = (0.604 + 0.245 * (1 - Math.exp(-epoch / 21))).toFixed(4);
    const lr = (0.0003 * (1 - epoch / 95)).toFixed(6);
    lines.push(`[train] epoch ${String(epoch).padStart(2, '0')}/80  step 128/128  loss=${trainLoss}  lr=${lr}  grad_norm=0.842  elapsed=${(epoch * 38.2).toFixed(1)}s`);
    lines.push(`[valid] epoch ${String(epoch).padStart(2, '0')}/80  loss=${valLoss}  macro_f1=${f1}  accuracy=${(Number(f1) + 0.022).toFixed(4)}`);
    lines.push(`[save]  checkpoint=checkpoints/epoch_${String(epoch).padStart(3, '0')}.pt  monitor=val_macro_f1  best=${f1}`);
  }
  return lines;
}

export function renderTargetTerminal(ctx) {
  if (!ctx.terminalDock || ctx.terminalDock === 'closed') return '';
  const mode = ctx.terminalMode === 'eval' ? 'eval' : 'train';
  const label = mode === 'train' ? '训练' : '评估';
  const cycle = Math.max(1, Math.floor(Number(ctx.cycleNumber) || 1));
  if (ctx.terminalDock === 'minimized') {
    return `<aside class="co-terminal-min" aria-label="已最小化的 Target 黑窗口日志"><button type="button" data-action="terminal-open" aria-label="恢复 T2 ${label}日志"><span aria-hidden="true">&gt;_</span> T2 · ${label}日志 <small>Cycle ${cycle} · 演示</small></button><button type="button" data-action="terminal-close" aria-label="关闭 Target 黑窗口日志">×</button></aside>`;
  }
  const lines = terminalLines(mode, ctx.terminalExtra);
  return `<section class="co-terminal-window" role="region" aria-label="T2 训练与评估黑窗口日志">
    <header class="co-terminal-header"><div><strong><span aria-hidden="true">&gt;_</span> T2 · 训练与评估（日志示例）</strong><small>Cycle ${cycle} · 演示日志，未连接后端</small></div><div class="co-terminal-window-actions"><button type="button" data-action="terminal-minimize" aria-label="最小化 Target 黑窗口日志" title="最小化">−</button><button type="button" data-action="terminal-close" aria-label="关闭 Target 黑窗口日志" title="关闭">×</button></div></header>
    <div class="co-terminal-toolbar"><div class="co-terminal-modes" role="group" aria-label="日志类型"><button type="button" data-action="terminal-mode" data-mode="train" aria-pressed="${mode === 'train'}">训练日志</button><button type="button" data-action="terminal-mode" data-mode="eval" aria-pressed="${mode === 'eval'}">评估日志</button></div><span>${lines.length} 行 · 标准输出</span></div>
    <div class="co-terminal-output${ctx.terminalWrap ? ' is-wrapped' : ''}" tabindex="0" role="log" aria-live="off" aria-label="${label}命令行输出，可滚动查看"><pre>${e(lines.join('\n'))}</pre></div>
    <footer class="co-terminal-footer"><span class="co-terminal-fixture">${label}输出示例</span><div><button type="button" data-action="terminal-wrap" aria-pressed="${Boolean(ctx.terminalWrap)}">自动换行</button><button type="button" data-action="terminal-follow" aria-pressed="${Boolean(ctx.terminalFollow)}">${ctx.terminalFollow ? '跟随最新 ✓' : '跟随最新'}</button><button type="button" data-action="terminal-append"${count(ctx.terminalExtra) >= 50 ? ' disabled' : ''}>追加示例日志</button></div></footer>
  </section>`;
}
