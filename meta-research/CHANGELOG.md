# Changelog

本项目采用语义化版本号。当前版本定位为可运行、可回放的本机开发测试版本；生产资格仍以
`QUALIFICATION.md` 和 `PRODUCTION_ACCEPTANCE.md` 的独立检查为准。

## 0.1.0 - 2026-07-20

首个版本化快照，主要包含：

- Web-first 多研究任务创建、运行控制、讲解员交互与可恢复 owner 生命周期。
- Idea、Plan、Bundle、Reasoning 四个常驻阶段主智能体，以及阶段内独立 reviewer。
- Bundle 主智能体驱动 smoke、train、eval、工程修复和结果提交；训练窗口展示真实实验日志，
  正式实验启动前展示 Bundle Codex 的实时构建活动。
- 本机 CPU/GPU 计算配置、多 GPU 选择、联网 Codex 与运行中可调整的评审强度。
- SQLite 权威状态、MCP 阶段提交/运行工具、路径化大产物、问题卡/基线卡/方法卡和池发布。
- WildIdea 发散与受控 novelty 检索适配、研究记忆召回及跨 cycle 推进。
- 执行 guardian、恢复回放、数据预检、日志归档和运行时状态可视化。

已知边界：

- 这是本机 development 版本，不代表 production qualification 已完成。
- Web 服务默认只监听远端 loopback；从 Windows 访问 SSH 主机时需建立本地端口转发。
- 当前仓库未配置发布远端，因此本版本先以本地 Git 标签和可校验归档交付。
