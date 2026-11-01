# #271 普通助手与共用 Writing

实现基线：`v1-test@6a8767e9037eb8166edec89cfc9134618c005ac7`。范围以 [#271](https://github.com/15253a/meta_research/issues/271) 和 [#262](https://github.com/15253a/meta_research/issues/262) 为准。

## 实际接口

`POST /api/v1/companion/messages` 保留普通消息合同，写作入口额外传 `writing_request`。它包含完整要求、文体、配套演示选择、工作范围、准确所选材料版本、模型配置引用与相对输出目录。HC 复用普通 `/new` 和消息幂等回执，首轮科学内容与材料入口封存在该消息中。后续普通对话和 `/new` 继续使用 Quest 助手目录，不创建 Writing Run、审批或专用工作区。

`GET /api/v1/companion/files` 提供当前助手目录的分页列表与准确哈希下载链接。下载复用目录边界和一次完整读取，文件变化返回冲突，不能将旧链接解释为新版本。它不接纳研究资产、不提交指导、不发布文件。

Companion 的研究目录 grant 只含读取与发现；本身的原生文件制作能力保持。跨页面选择的研究读取使用 `source_quest_ref`，沿目标 Quest 现有、当前有效的 HC 授权核验。用户明确选入写作的模板、前稿等沿现有 RM 导出能力提供准确原生文件，用户选择不会改变研究对象归属。

## 定稿资源

Companion / first_creation 来源为 #256 独立定稿；Writing 来自共享设计 checkout 已合流的 #259 定稿。正式包包含 Companion 方法、first_creation 主文件及三份参考、Writing 主文件及全部五份文体／上下文参考。实际 `research_guidance`、creation adapter 与 Harness 提供准确本地读取路径，研究主链和普通助手使用同一 Writing Skill。参考按任务需要读取，必要资源缺失失败。

## 用户 API 内容模型

`assistant.content.generate` 使用服务环境的 `META_RESEARCH_CONTENT_MODELS` 路径读取模型配置。配置文件示例：

```json
{
  "science-model": {
    "endpoint": "https://provider.example/v1/chat/completions",
    "model": "selected-model",
    "api_key_env": "SCIENCE_MODEL_API_KEY"
  }
}
```

当前接通 OpenAI 兼容的 Chat Completions 文本协议。API 密钥由服务环境的命名变量提供，任务只传 `config_ref`。助手显式组织本次所需 `messages`，服务不附加整个目录、历史或 Quest；返回仅含内容及配置引用。失败返回可识别错误，不回退其他模型。内容仍须由助手核对、修订并制作所需文件。当前模型选项直接由普通 Codex 助手完成工作。

## 清理与验证口径

删除专用 Writing service、adapter、worker、Owner Run / execution / citation adjudication / external delivery、专用 HTTP 路由及前端确认链。研究总览沿 `AcceptedResearchReader` 继续读取原已接纳科研内容。历史数据库迁移文件保留，不添加旧任务运行或转换路径。

验证记录在本工作区 `.scratch/issue-271-20261101/`：每次 Linux 检查有冻结源码压缩包、SHA256、命令、pytest 日志与 JUnit。受控 provider／HTTP 测试、真实模型调用及文档渲染分别记录，不能互相替代。实现不代表 8770 已部署，也不代表 #274 的单 Cycle 验收完成。

最终测试结果、真实文件验收及与 #270 的合流边界待本次验证结束填写。
