# vNext 核心主界面视觉原型

> THROWAWAY PROTOTYPE — 只用于选择配色、布局与信息密度，不是生产前端。

本页用同一组合成研究数据比较三套结构明显不同的主界面方向：

- `A — 返场航图`：冷静、明亮的三栏研究驾驶舱，优先回答“我离开后发生了什么”。
- `B — 夜间观测台`：深色、连续的时间叙事工作台，优先回答“系统为何走到这里”。
- `C — 问题星图`：空间化研究画布，优先展示 Goal、Question、证据和交付物之间的关系。

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
