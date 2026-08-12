# Writing deliverable prototype — THROWAWAY

这是 [“原型验证 Writing 交付物结构与修订体验”](https://github.com/15253a/meta_research/issues/76) 的抛弃式、只读 UI 原型，不是生产实现或正式规格。

## 视觉方向

本轮使用 Anthropic 的 [`frontend-design`](https://skills.sh/anthropics/skills/frontend-design) skill 重构为“证据光台”：对象是绑定冻结研究快照的可追溯 Writing 交付物，受众是负责核验来源与改稿边界的研究负责人，页面的单一任务是在不改写研究事实的前提下审阅一个精确版本并提出局部修订。

冷色实验台、稿件层与 mono 坐标来自研究核验现场；交互式“证据光路”把正文的 `C-01 / E-02 / L-03` 坐标连接到精确 Resolution 及其支持边界。信号橙只用于修订或越界警告，C 中的编号只表示真实验收顺序。旧版暖纸张视觉仍保留在分支历史中，可随时比较或回退。

## 要回答的问题

paper、报告和 PPT 是否应共享 `claim + evidence + limitation` 这一可追踪语义单元，只在组织与叙事方式上分化？用户能否清楚地查来源、识别陈旧与不完整、对精确交付物版本和输入 Snapshot 提出局部修订，并理解“修改交付物不等于改变研究事实”？

## 运行

```bash
python -m http.server 8776 --bind 127.0.0.1 --directory /tmp/meta-research-issue76.KEMFQI/meta-research/views
```

访问：

- `http://127.0.0.1:8776/writing-prototype/?variant=A` — 语义脊柱审稿台
- `http://127.0.0.1:8776/writing-prototype/?variant=B` — 交付物谱系地图
- `http://127.0.0.1:8776/writing-prototype/?variant=C` — 聚焦式验收流程

页面顶部 prototype bar 或键盘左右方向键可以切换方案；输入框聚焦时不会拦截方向键。所有操作只更新浏览器内存，`coreWrites = 0`，不使用持久化。

## 建议验收任务

1. 在 5 秒内找到交付物精确版本、父版本、冻结 Snapshot、陈旧风险和不完整项。
2. 在 paper、报告和 PPT 间切换，判断共享语义与类型特有组织是否自然。
3. 找到“完成执行，不等于接纳结果”的精确来源与支持边界。
4. 对一个段落或 slide 发起绑定 `W-v3 + Snapshot r184` 的局部修订。
5. 在内容、证据和 citation 三个轴上比较 `W-v2 → W-v3`。
6. 当认为底层结论有误时，正确离开 Writing 修订路径，选择“提出研究变更”。

## 尚未决定

本原型不决定 Writing Run／attempt／恢复合同、formal citation 接纳、renderer、导出、发布或投稿能力，也不作正式 stale 裁决。`claim + evidence + limitation` 是供用户判断的候选，不是已经通过的领域规范。
