# 当前活跃运行：记忆、储存与入库设计反馈

采样完成时间：2026-09-24 17:02（Asia/Shanghai）。对象为用户指定的 8768 活跃 data-root。此报告只包含结构与状态聚合，不包含正文、实际资产路径、凭据或 provenance。

## 采样方式与边界

SQLite 通过 URI `mode=ro` 打开，显式设置 `PRAGMA query_only=ON`，并读回确认值为 `1`。每组聚合在短只读事务内执行，busy timeout 为 2 秒，单条查询设 2 秒中断上限。没有实例化服务 Owner、运行迁移或调用 API。仅对选定表计数、分组、联结引用；JSON 只用于统计记录数与提取版本引用，不输出内容。

受管对象存储只作目录遍历及文件 `stat` 总量统计，不读取正文、不计算 hash、不跟随符号链接；统计耗时约 0.063 秒，完整完成，无错误。数据库与文件统计不是同一原子快照；服务运行中数据可能变化。当前数据库迁移版本为 `0058_human_research_inputs`。

## 最有价值的 6 组反馈

### 1. 资产身份、内容重复与物理储存应分开理解

| 聚合 | 结果 |
|---|---:|
| Asset 身份 | 130 |
| AssetVersion | 136 |
| 不同 `content_hash` | 89 |
| `version_number > 1` 的版本 | 6 |
| 同 hash 出现多次的组 | 44 |
| 重复 hash 组内的版本总数 | 91 |
| 相对于每 hash 一个版本，多出的版本出现次数 | 47 |
| 版本逻辑字节总和 | 11,846,355,025 bytes，约 11.03 GiB |
| 最大单版本逻辑字节 | 3,279,930,156 bytes，约 3.05 GiB |
| 受管对象存储文件总数 | 430 |
| 受管对象存储 apparent size 总和 | 6,018,320,629 bytes，约 5.60 GiB |

按来源，14 个 `local_path` 版本贡献约 99.6% 的版本逻辑字节；113 个 `file` 版本合计约 42.22 MiB。其余为少量正式 Question、Idea、system artifact 与 text。

**设计反馈：** 当前规模已经要求区分语义身份、接纳版本、内容 hash 与物理对象。相同内容可以有多次接纳或不同研究归属，不能直接用“去重 hash”代替资产身份；大文件也使首次读取与复验的 I/O 预算成为实际问题。

**局限：** 内容 hash 相同不证明接纳重复是错误；逻辑字节可反复计算共享内容，而对象目录包含其他记忆对象。5.60 / 11.03 不能直接解释为去重率，也不是磁盘 allocated blocks。未作大文件 hash 或物理对象引用全量对账。

### 2. 当前入库与保管状态没有显示排队积压

| 聚合 | 结果 |
|---|---:|
| Intake jobs | 131 |
| `accepted` | 131 |
| 其他 intake status | 0 |
| 保留 `failure_code` 的 jobs | 0 |
| `attempt_count > 1` | 0 |
| 最大 attempt count | 1 |
| 已 scrub 请求 payload 的 jobs | 131 |
| managed custody 覆盖版本 | 136 / 136 |
| 其他 custody mode | 0 |

136 个版本与 131 个 intake 不矛盾：接纳类型聚合恰为 131 个普通 `asset_acceptance`，以及 1 个 Question、1 个 Idea、2 个 Plan、1 个 Reasoning 的正式内容接纳。

**设计反馈：** 入库已经存在普通资产 intake 和正式研究内容接纳两类来源。设计统一记忆入口时，应统一“如何被发现、解释、追踪”的规则，同时保留不同来源的接纳事实。

**局限：** 当前状态不是历史可靠性证明，也未覆盖业务仍在工作区、尚未正式接纳的产物。全部 managed 只描述当前样本，不能验证 linked_local 的真实生产表现。

### 3. 持久化校验观察完整，当前没有到期积压

| 聚合 | 结果 |
|---|---:|
| `verified` 且 `available` 的观察 | 136 / 136 |
| 缺少 observation 行的版本 | 0 |
| `next_verify_at` 已到期的版本 | 0 |
| 到期超过 1 小时 / 1 天 | 0 / 0 |

**设计反馈：** 现有后台验证投影在此采样点覆盖了所有已接纳版本，适合作为资产管理展示的一种状态。它仍应与“历史接纳有效”和“本次精确消费成功”分别解释。

**局限：** 这些是数据库保存的最近状态，不是本次重新验字节。源码在复验结果不变时保留旧 `observed_at`，因此本轮没有拿该字段计算“最近一次检查距离现在多久”。也未测量后台吞吐、故障恢复时长或首次读取延迟。

### 4. 单看 asset role 会严重低估已组织的产物

| 聚合 | 结果 |
|---|---:|
| 直接 RG asset role | 13 行，覆盖 13 版本 |
| role 类型 | 全为 `evidence`，涉及 1 Quest |
| 任一 Target completion manifest 涉及的版本 | 127 |
| 已有 TargetCommit 的 Target manifest 涉及的版本 | 127 |
| 直接 role 或已提交 Target manifest 覆盖的并集 | 131 / 136 |
| 绑定 DatasetVersion 的资产版本 | 9 |
| completion manifests / 对应 Target | 7 / 4 |
| TargetCommit | 4 |

并集以外的 5 个版本，按接纳类型分别为：Question 1、Idea 1、Plan 2、Reasoning 1。它们通过正式内容接纳路径存在，不能称作无归属文件或孤儿。

**设计反馈：** 当前产物归属至少通过直接 roles、Target completion manifests、Dataset bindings、阶段正式内容四类关系表达。统一资产索引若只读取 `rg_asset_roles`，会遗漏大量已经组织的内容；需要决定统一发现层如何呈现这些关系与它们各自的权威来源。

**局限：** 这里检查的是 DB 引用覆盖，未重新验证每份 receipt、fence 或 manifest 文件。7 份 manifest 对应 4 个 Target 可以涉及重接纳/重试等合法过程，单凭数量差异不能判定重复提交错误。

### 5. Dataset 语义登记仍是小样本，已有身份与版本分离的情况

| 聚合 | 结果 |
|---|---:|
| Dataset 身份 | 9 |
| 至少有一个 DatasetVersion 的身份 | 7 |
| 至少被 Question 引用的 Dataset 身份 | 7 |
| DatasetVersion / DatasetReference / DatasetDerivation | 7 / 7 / 6 |
| DatasetVersion 所绑定的不同资产版本 | 9 |

**设计反馈：** 当前至少 2 个 Dataset 身份尚无版本与 Question 引用。结合现有 scoped discovery 依赖 Question reference 的规则，资产已接纳、语义身份已登记、版本已形成、研究已引用，是实际存在的不同阶段，应在入库和检索设计中明确。

**局限：** 无版本身份可能是尚未完成的合理工作，不是失败。样本小，不能从 6 条派生关系推断跨 Quest 复用需求或完整数据谱系质量。

### 6. 记忆总体覆盖不能仅靠资产版本数衡量

| 记忆/研究对象聚合 | 结果 |
|---|---:|
| 当前 Question lifecycle | 1 个 active Question，1 Quest |
| LiteratureSnapshot | 1 |
| Question literature revision | 1 |
| 文献 revision 记录出现次数 | 19 |
| 非首版文献 revision | 0 |
| 专用 `rm_target_research_notes` 记录 | 0 |
| Idea / Plan / Reasoning 正式内容 | 1 / 2 / 1 |

**设计反馈：** 当前运行样本已产生大量文件型产物，但 Question、文献 revision 和阶段综合样本较少。设计梳理应同时覆盖长期研究记忆、研究过程内容、文献证据和大型数据产物；不能只优化文件列表后就认定记忆系统完整。

**局限：** 专用 notes 表为 0 不代表所有产物中没有笔记；相关内容可能位于其他正式文档或资产中，本轮按要求未读正文。单 Quest、单文献 revision 无法检验跨 Quest 检索或多 revision 发现行为，现有索引调查中的分页风险也未用业务内容做复现。

## 对决策地图的直接输入

这组运行数据支持优先讨论三个边界：**记忆的统一可发现范围；内容保管与研究归属的分离；普通资产 intake 与正式内容接纳如何在同一入口中解释。** 当前采样未显示 intake 或持久化验证积压，暂不能据此把重点设为修复排队故障。搜索质量、跨 Quest 复用、文献历史发现和重建/对账能力，仍需要用户预期与进一步的针对性证据。
