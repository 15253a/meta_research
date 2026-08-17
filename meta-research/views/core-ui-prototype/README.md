# vNext 核心主界面视觉原型

> THROWAWAY PROTOTYPE — 只用于选择配色、布局与信息密度，不是生产前端。

本页用同一组合成研究数据比较三套结构与气质明显不同的高保真主界面方向：

- `A — 光谱台`：明亮、柔和、留白充分；返场摘要居中，Companion 常驻右侧；包含首页问题路径缩略图、独立问题树视图、`create_question` 与材料 HumanRequest 窗口，以及实验启动时自动出现的 stdout / 硬件观测窗。
- `B — 夜航室`：深色沉浸式研究空间；用三件关键变化解释系统为何走到现在。
- `C — 研究工作室`：克制的编辑网格；Question、Stage 和三层事实以清晰文字层级呈现。

## 运行

在本 worktree 根目录执行：

```bash
python3 -m http.server 4173 --directory meta-research/views/core-ui-prototype
```

然后打开：

- <http://127.0.0.1:4173/?variant=A>
- <http://127.0.0.1:4173/?variant=A&view=questions&node=Q-38.2>
- <http://127.0.0.1:4173/?variant=A&panel=create-question>
- <http://127.0.0.1:4173/?variant=A&panel=human-request>
- <http://127.0.0.1:4173/?variant=B>
- <http://127.0.0.1:4173/?variant=C>

页面底部切换器与键盘 `←` / `→` 也可切换方案。顶部可切换“返场复核 / 局部等待 / 结果陈旧”三种代表性场景。

A 的左侧“树”入口和首页“打开完整问题树”会进入同一只读问题树。选择节点只改变浏览器内存中的查看上下文，并同步右侧 Companion，不会修改问题拓扑。

`create_question` 窗口先收集由人拥有的六字段 `CreationSeed`。字段可以只填一部分；确认前 Agent 不会改写或补全，确认后也只是冻结 Seed 并准备进入共同起草，并不代表 `QuestionProposal`、Formal Question、RM 接纳、RG 接纳或 Stage 推进已经发生。

材料 HumanRequest 窗口绑定一个精确的 `RequestId / revision` fixture，展示 Owner 定义的义务、验收条件、材料与局部等待投影。`provided / deferred / declined` 只记录 HC 响应；RM 接纳材料、请求 Owner 判定业务满足，以及受阻工作获得恢复资格仍是独立事实。

A 的代表性实验 `RUN-204 / attempt generation 2 / root Session SES-7` 处于 running 且 Execution Fence current 时，新 fence 会自动弹出一次黑色运行观测窗；同一 fence 按浏览器 session 去重，关闭后可从首页“当前实验”卡或问题树工具栏再次打开。若用户正在输入或审查 Command Draft，新 fence 只给非模态提示，不抢焦点或丢失草案。窗口每秒更新 synthetic GPU / VRAM / power / CPU fixture，并持续追加 synthetic stdout，用于验证实时信息的层级、密度、关闭与重开体验；它没有连接真实 Harness 或硬件。

## 边界

- 全部研究与运行数据均为 fixture，只在浏览器内存中变化；`sessionStorage` 仅记住本 session 已展示／关闭的 Execution Fence UI 偏好。`coreWrites = 0`、`networkWrites = 0`。
- 页面只表达 Snapshot / Projection 与 Human Collaboration 交互，不拥有任何领域状态。
- Companion 生命周期与 Writing 精确合同仍在开放票据中；对应位置只用于判断布局，均标记为 `PROTOTYPE ASSUMPTION`。
- 问题分解关系是代表性 fixture；原型只验证树的位置、密度与导航，不提前定义 Answer / Evidence 状态族或生产图查询合同。
- `create_question` 与材料请求中的身份、内容和合同标签也都是代表性 fixture；两个窗口只验证交互层级与视觉语言，不创建真实 Question、HumanRequest、AssetVersion 或恢复许可。
- stdout、Run / Attempt / root Session 身份与硬件遥测均是只读 Runtime 执行观察；只有 current Execution Fence 显示 `LIVE`，旧 fence / 断流会降级为 historical 或 stale。它们不表示 Target 已接纳、Question 已回答或 Stage 已推进。
- 硬件 fixture 明示 collector、device、scope、sampledAt、cadence、unit / denominator 与 freshness；GPU / VRAM / power 为整卡相关值，CPU 为 host-wide 相关值，不冒充某个 Run 的独占用量。生产遥测 transport 仍未在此原型中定义，原始秘密也不得进入日志。
- System Steward 不在此原型中。
