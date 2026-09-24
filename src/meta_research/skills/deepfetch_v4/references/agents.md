# DeepFetch 角色契约

## 主智能体

负责解释任务、选择查询和引文方向、维护元数据与候选、选择全文、协调任务及撰写 `summary.md`。台账广度由相关性和主动检索时钟约束；精读总上限见主 Skill。按组合后的新增证据价值排序，获取顺序和访问方便程度不决定入选。书目、摘要和 Web 页面用于发现与核实，全文科学阅读交给 Reader；汇合后从台账读取全部 Reader 结果。

## 预检与 Acquisition

Quest 级 Acquisition Root 负责当前访问模式、合法路线、浏览器状态和私有存储。沿当前配置预检：`oa_only` 无需机构浏览器，`provided_only` 仅核实已提供材料，`oa_then_institution` 核实所需机构路线。每次请求使用当前明确模式，不继承上回合的临时判断。

通过共用 `agent_runtime.acquisition.request` effect 提交 1–10 个目标的有界批次；请求结果不明时以同一 `effect_id` 对账，再判断是否重试。当前适配器的 `action=acquire` 请求体形状为：

```json
{
  "effect_id": "fulltext-openalex-W123",
  "targets": [
    {
      "paper_id": "openalex:W123",
      "title": "已核实的精确论文标题",
      "source_urls": []
    }
  ]
}
```

已知 DOI、arXiv ID 或 URL 时一并传入。获取服务逐目标返回 typed 结果：`obtained` 带已核实路径、格式与内容证明；`waiting_user`／`missing` 说明状态和原因。Reader 只能接收同一 `paper_id` 的核实正文。provider 等待只是获取结果，具体人类义务需显式提出 HumanRequest。

未决获取项不超过当前精读目标的空余容量。截止前、首次 Reader 分配前的 `missing` 或明确放弃可释放预留并替补；截止后不晋升候补。`waiting_user` 保留预留直至解决或放弃。登录过期时及时返回受影响论文，同时推进独立的 OpenAlex、OA 与阅读；给出重新登录后重试、用户提供正文或放弃该文的选项。没有其他检索活动的纯等待不计时。

## Reader

每个逻辑 Reader 只读一篇已分配全文。首次分配占用该论文运行级名额，重试仍是同一名额。立即填充实际可用容量：

```text
readers_to_start = min(queued jobs, max(0, 10 - active Readers), runtime slots currently free for Readers)
```

10 是上限，不是必须达到的峰值；空闲 Acquisition Root 不预留 Reader 槽位。宿主容量不足时按波次处理已接纳论文或其重试，不新增第 11 篇。

Reader 输入必须包括研究任务、论文记录、单篇全文绝对路径与 SHA-256、`papers-json.md` 的绝对路径、单次 assignment ID 和 patch 模板。先打开任务，完整读取 `reading_contract_path` 的阅读部分，再读取指定正文。依据全文填写理解、方法、实验设置、主张、材料、局限和可信度；不另行检索、下载、读其他论文、修元数据或写总报告。

通过 `scripts/papers.py apply-reader` 提交自身 patch，仅向主智能体返回 `paper_id`、assignment ID、终态和简短错误。正文未读到的内容保持未知。

失败处理：

- `reader_failed`、`timeout`、`invalid_output`：保留有效全文，发布空的 `failed` 阅读；值得重试时重试同文，否则该名额以失败结束。
- `file_invalid`、`paper_mismatch`：由合并工具隔离文件、清空公开路径并恢复 `not_read`；可修正并重试同文，名额仍已占用。
- Reader 确实失联且达到运行时超时条件时，主智能体可用原 assignment 数据提交 `timeout` 失败 patch；尚在运行或等待时继续观察，不用主观等待时长伪造超时或阅读结论。
