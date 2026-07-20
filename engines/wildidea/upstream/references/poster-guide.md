# WildIdea HTML 海报生成指南

每次 WildIdea 标准模式完成默认 9 个槽位后，必须使用本文件生成横版 HTML。用户不需要额外说"生成 HTML"。如果用户明确说"不要 HTML/只要文本"，本轮只能标记为非标准草稿模式。

默认输出是横版静态 HTML（无固定宽高比，高度由内容自适应），风格为 Claude 式白底米黄：浅暖背景、低对比边框、棕色强调、信息密度高但不花。除非用户明确要求，不再使用深色霓虹、标题渐变、大面积装饰背景。

具体色值见 `references/poster-palettes.md`。

## 模板

读取 `templates/poster.html`，替换全部占位符后写入 `outputs/<topic>.html`。静态 HTML 可以直接用 `file:///` 打开，不需要启动本地服务；只有用户要求截图或浏览器验证时，才打开浏览器检查。

命名规则：优先使用短英文 slug，例如 `toc-skill-ideas.html`、`photo-album-app.html`。无法稳定命名时，使用 `wildidea-YYYYMMDD-HHMM.html`。旧输出文件名里即使带 `16x9`，版式也必须按自由窗口规则生成。

## 占位符

| 占位符 | 含义 | 示例 |
|--------|------|------|
| `{TITLE}` | 主标题 | EEG domain adaptation 创新方法 |
| `{BADGE_TEXT}` | 顶部小徽章 | WILDIDEA V5 |
| `{FOCUS}` | 副标题 | 9 个外域槽位，逐卡独立生成与评分 |
| `{META_HTML}` | 右上统计条 | 见下方 |
| `{QUARANTINE_HTML}` | 隔离区 | 见下方 |
| `{REJECTED_HTML}` | 本轮淘汰/重抽记录 | 见下方 |
| `{CARD_ROWS}` | 默认 9 张卡片区 | 外层必须是 `.cards` |
| `{SUMMARY}` | 底部一句主线 | 见下方 |

生成前后都要检查：最终 HTML 不得残留 `{PLACEHOLDER}`。

## 统计条

`{META_HTML}` 使用 `.stats`，不要用旧版 `.meta/.ok/.warn`。

```html
<div class="stats">
  <div class="stat"><b>8/9</b><span>候选通过</span></div>
  <div class="stat"><b>0</b><span>页面占位符</span></div>
  <div class="stat"><b>待验证</b><span>文献级查重</span></div>
</div>
```

## 隔离区

`{QUARANTINE_HTML}` 使用 `.ban`。这里只放被禁止的常规方向、上一轮主机制、明显重复路线。内容要短，别把所有推理塞进隔离区。

```html
<section class="ban">
  <strong>隔离区</strong>
  <div class="tags">
    <span>普通 DANN</span>
    <span>Transformer 通道注意力</span>
    <span>上一轮主机制名</span>
  </div>
</section>
```

## 淘汰/重抽记录

`{REJECTED_HTML}` 使用 `.ban.rejected`。每次标准模式都要生成这一段；没有淘汰时写“无重抽，9 个槽位一次通过”。这里记录抽卡过程中被 ban 的来源、候选或随机词，以及为什么重抽。

```html
<section class="ban rejected">
  <strong>本轮淘汰/重抽</strong>
  <div class="tags">
    <span>随机词“提个”：口语短语，缺真实机制边界</span>
    <span>某候选：去锚点后退化为普通 attention 加权</span>
  </div>
</section>
```

## 卡片结构

`{CARD_ROWS}` 必须自带外层 `.cards`，默认 9 张卡片，适配 3 列 x 3 行。每张卡片只放读者最需要先看懂的内容，不塞完整审稿表。

```html
<section class="cards">
  <article class="card">
    <div class="id">
      <em>P01</em>
      <span class="slot">D5 社会政策</span>
    </div>
    <div class="source">来源：<strong>Hyperband</strong></div>
    <div class="name">候选机制名</div>
    <div class="proto"><strong>源域原型：</strong>映射到用户领域前冻结出的通用操作机制。</div>
    <div class="desc">用户领域怎么干（通俗展开）：把做法写成读者脑中能出现画面的 2 到 3 句。</div>
    <div class="fail">失败条件：如果这个现象没有发生，说明方向不成立。</div>
  </article>
</section>
```

### 卡片写法

- `.slot` 必须写清楚远域类别，例如 `D1 算法技术`、`D2 学术机制`、`D3 人文艺术`、`D4 产品机制`、`D5 社会政策`、`MAO 毛选`、`RANDOM_WORD 随机组词`。
- `.source` 必须写具体来源机制名，例如 `Hyperband`、`Kalman NIS`、`V2G`，不要只写"算法技术"。
- `.proto` 必须写“源域原型/外域抽象结果”，也就是映射到用户领域之前最后冻结出的通用机制。不能出现用户领域术语。
- `.name` 是候选方法名，尽量短。
- `.desc` 是最重要的正文，写"用户领域怎么干（通俗展开）"。要比普通结果更口语一点，让用户能马上想象实施动作。
- `.desc` 不要写干巴巴变量串。例如不要只写"以 subject embedding 进行条件化路由"，要写成"先给每个被试留一个小档案，模型每次看到新 trial 时先判断这个人现在像哪类状态，再选择对应的小模块处理"。
- `.fail` 只保留最小可证伪条件。完整的最近邻、退化物、最强反驳可以放在正文回答或另一个 markdown，不强塞进海报卡片。

## 底部总结

`{SUMMARY}` 使用 `.bottom`。左边一句话讲本轮主线，右边放版本和验证状态。

```html
<section class="bottom">
  <div><strong>主线：</strong>这一轮不是再堆域适配模块，而是把"何时允许适配、何时拒绝适配"做成可审查机制。</div>
  <div class="footer">wildIdea V5 · model self-audit only · literature audit pending</div>
</section>
```

## 版式约束

- 默认横版，`.slide` 最大宽度 1600px 居中，高度由内容自适应，不锁比例。
- 默认 9 张卡片，3 列自适应：≤780px → 2列，≤500px → 1列。不要做成单列长页，除非用户要求。
- `.card` 使用 flex 列布局，不裁切内容（`overflow: visible`），不高宽压缩（无 `min-height: 0`）。
- `.proto`、`.desc` 和 `.fail` 必须设置 `word-break: break-word; overflow-wrap: break-word` 防止长英文串溢出。
- 卡片内下方留空要少，`.desc` 字体要明显大于 `.fail`。
- 页面主色保持白、米黄、浅棕，避免单一深蓝/紫色/黑色主题。
- 不使用旧版 `.anchor/.match/.badge-row/.insight/.container/.its-says/.you-try`。

## 生成与验证

1. 生成 HTML 到 `outputs/<topic>.html`。
2. 运行 `python3 scripts/validate_poster.py outputs/<topic>.html --cards 9 --forbid-proto-term <用户领域禁词...> --search-sidecar outputs/<topic>.search.json`（`--cards 9`、`--forbid-proto-term` 和 `--search-sidecar` 在默认标准模式下均为必填）。
3. 若脚本失败，先修 HTML 再交付。
4. 最终答复必须提示文件路径和 `file:///` 地址；如用户要截图，再使用浏览器打开并目测文字是否溢出。

校验脚本会检查两类问题：

- 结构：占位符、9 张卡片、`.slot/.source/.proto/.name/.desc/.fail`、`.ban.rejected`、旧版类名、固定比例裁切、文字溢出防护。
- 候选契约（内容）：`.source` 非空且不能是槽位名（必须写具体来源机制，如 `Hyperband`）；`.name/.proto/.desc/.fail` 不得残留模板样例文字。
- 必填去锚点审查：`--forbid-proto-term <用户领域术语...>` 是必填项，不传直接 FAIL。脚本自动做 NFKC+大小写归一并展开中英同义词。检查 `.proto` 是否泄露了用户领域术语，同时检测 `.proto` 与 `.desc` 的相似度（疑似马后炮）。禁词需覆盖任务名、数据类型、指标，例如：
  `python3 scripts/validate_poster.py outputs/eeg.html --cards 9 --forbid-proto-term EEG 脑电 域适应 --search-sidecar outputs/eeg.search.json`
- 非标准模式或完全失败槽位导致卡片数不是 9 时，用 `--cards N` 指定实际渲染数量，并明确报告缺失槽位。
