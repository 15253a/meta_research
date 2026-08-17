# vNext 核心主界面视觉原型

> THROWAWAY PROTOTYPE — 只用于选择配色、布局与信息密度，不是生产前端。

本页用同一组合成研究数据比较三套结构与气质明显不同的高保真主界面方向：

- `A — 光谱台`：明亮、柔和、留白充分；返场摘要居中，Companion 常驻右侧。
- `B — 夜航室`：深色沉浸式研究空间；用三件关键变化解释系统为何走到现在。
- `C — 研究工作室`：克制的编辑网格；Question、Stage 和三层事实以清晰文字层级呈现。

## 运行

在本 worktree 根目录执行：

```bash
python3 -m http.server 4173 --directory meta-research/views/core-ui-prototype
```

然后打开：

- <http://127.0.0.1:4173/?variant=A>
- <http://127.0.0.1:4173/?variant=B>
- <http://127.0.0.1:4173/?variant=C>

页面底部切换器与键盘 `←` / `→` 也可切换方案。顶部可切换“返场复核 / 局部等待 / 结果陈旧”三种代表性场景。

## 边界

- 全部数据均为 fixture，状态只在浏览器内存中变化，`coreWrites = 0`、`networkWrites = 0`。
- 页面只表达 Snapshot / Projection 与 Human Collaboration 交互，不拥有任何领域状态。
- Companion 生命周期与 Writing 精确合同仍在开放票据中；对应位置只用于判断布局，均标记为 `PROTOTYPE ASSUMPTION`。
- System Steward 不在此原型中。
