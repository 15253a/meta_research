# #273：前端追赶、HR 回执与自动恢复

范围对应 [#273](https://github.com/15253a/meta_research/issues/273)，基底为 `origin/v1-test@eb25f660047cf4274bc0bca7108f8e3c27621cf4`。实现使用独立 worktree；不包含合并、部署或 #274 的真实科研 Cycle 验收。

## 行为与接口

- `projectionRecovery` 合并当前实例内单调目标，以当前范围及必需内容实际交付后的 React commit 核对追赶；deferred 或读取失败的必需内容不能确认完成。事件 gap、重连、后台返回和同 revision 健康变化触发核对。切范围丢弃旧完成判定；切数据实例重建基准。
- 单次 snapshot／HR 交接说明读取及 HR 回应投递有 22 秒退出上限。503 交回调度，尊重 Retry-After／退避。旧可用内容、阅读状态及未提交输入保持；普通读取失败是局部提示，事件通道中断与 worker 故障分别显示。
- 新 HR 只提示数量，由人点击打开。回应回执及后继状态留在原请求；未知结果显示“提交结果待确认”，沿原封存正文、材料、request_ref、response_path、幂等键和写槽安全核对。明确拒绝可修改输入；迟到回执不覆盖另一个请求或草稿。
- Snapshot coordinator 合并所需 sections，保留一个共享计算和超时后的原结果。retained 返回先选择再复制，保持独立返回对象。数据库既有持久 correlation identity 提供 revision_domain；无新迁移或第二投影权威。
- Bundle Skipped 使用准确已接纳 Plan 的引用呈现支持、缺口和未安排理由，不推断 Question 完成。

## #271／#269 交互边界

#271 的 `sendCompanionMessage(message, scopeRef, view_context)`、显式审阅后 `respondToHumanRequest(request_ref, reviewedBody)` 和 `onChanged()` 刷新通知保持。新增本地 delivery subscription 只表示请求绑定的 pending／recorded／rejected 投递观察；Owner 投影继续唯一决定 evaluation／disposition／resume。

Companion／Writing 可改变任务装配与用户选择的助手材料，但正式提交仍传最后审阅的准确正文及材料身份；不能替换封存中的提交、自动改选另一个 HR，或将普通交流／文件交付当作正式回应。未新增科研 SemanticOperation 或 root grant，#269 无新增 MCP 合同。

## 验证方法

```text
npm run build
npm test
npx playwright test tests/projection-recovery.spec.ts tests/human-request-recovery.spec.ts tests/human-request-v4.spec.ts tests/research-overview-refresh.spec.ts tests/root-domain-recovery.spec.ts tests/companion-session.spec.ts --project=functional
```

Chrome 以真实编译 React 页面和固定 HTTP／SSE 序列验证渲染，而非只检查 Python 或 hook。专项覆盖旧 cut 无新事件追平、503／挂起恢复、deferred 展开、旧范围迟到响应、同 revision 健康、断流、后台返回、跨实例相同引用、HR 未知／拒绝／迟到回执、草稿和交接说明保留、准确 Plan 和 #271 正式审阅接口。

观察样本：旧 cut 3 次读取约 6.2 秒追平；连续两次 503 后第 3 次读取约 8.3 秒恢复；挂起读取约 22,010 ms 退出后自动恢复。读取最大并发为 1。HR 一次样本点击到待确认 722 ms、再到回执 1,023 ms，2 次 POST 使用同一个幂等键。这些是受控轨迹，22 秒是退出上限，不是正常响应目标。

Python 在独立临时 Linux 数据目录运行，避免 Windows 既有 fcntl 平台依赖；未访问研究目录。针对 coordinator、真实 projection／status、准确 Plan 的检查，以及 HR HTTP 幂等、回执／原根恢复检查，均保留归档哈希、JUnit 与日志。

同一受控输入以 SQLAlchemy engine hook 计实际 SQL：轻量 316、完整 354、三个并发请求共 356、retained 1 次 feed 查询。复制约 30.1／33.6 KB；deferred 返回复制的 assets/history items 为 0。源查询、SQL 和复制计时分开；冷热差异使完整请求样本耗时更短，不能据此宣称速度提升，也未将回调次数冒充 SQL。

## 验证限制与证据

本地证据目录为工作区 `.scratch/issue-273-20261101/`，包含实际浏览器 traces／截图、backend-01／backend-02／hr-api-01、SQL cost observation、基底失败复现及真实 CC 终审回执。完整运行记录不发布到仓库。

全量旧 Python suite 尝试产生 5 个 artifact batching 失败，均在原始基底源码和测试上逐项复现；按失败提前结束，21 项通过，其余未运行。HR 专项的单个旧断言（AE revision 预期 1、实为 2）也已在原始基底复现。全量 Windows browser suite 受 uv／fcntl 平台依赖阻断，不作为全绿证据；本票使用真实 Chrome 专项和独立 Linux 后端补验。未把这些限制等同于 #274 集成验收。
