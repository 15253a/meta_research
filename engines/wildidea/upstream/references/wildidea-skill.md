---
name: wildidea
version: "1.3"
description: "WildIdea v1.3：按网页 v1.4 的 9 槽位、预设卡池、逐卡独立评分、最多重抽 2 次和未达标保底逻辑执行；标准 Skill 额外强制真实联网审查、独立判官与 HTML 校验。"
---

# WildIdea Skill v1.3 Full Spec

这是 Agent Skill 的完整执行规范。脚本只用于内部抽卡、搜索记录与产物校验，不要把 CLI 当成用户体验。

## Standard Pipeline

固定执行：

`输入问题与常见解法 -> 类型判断 -> 结构化为 problem_card（五键） -> 按模式抽 9 个槽位 -> 隔离常规答案 -> 逐槽冻结他山之石 -> 抽象无目标域词的方法 -> 映射为落地方案（对照 problem_card） -> 基础校验 -> 独立判官（同时核对 problem_card 对应关系） -> 未达标则重抽（最多 2 次） -> 真实联网审查 -> 形成通过/未达标保底/失败结果 -> 整批多样性检查（本质方向不足则换源重抽最冗余卡） -> 生成并校验 HTML -> problem_card 五键覆盖盘点 -> 返回路径`

六条纪律：

1. 必须先抽来源，再写方案；不能先写常规答案，再给它套一个外域名字。
2. 9 个槽位彼此独立。某张卡完成后可以立即报告，不必等待其余卡；最终 HTML 在全部槽位结束后生成。
3. 每个槽位最多 3 次生成，即初次生成加最多 2 次重抽。三次按三段式使用，不是盲目重抽：首次失败必须把具体命中的质量规则或判官维度/意见带进下一次尝试，只重写被点名的字段；连续两次同因失败，第三次须换一个结构角度重新映射同一锚点，不换锚点也不再是改措辞。不得无限重抽直至凑齐 9 张通过卡。
4. 高亮卡或全局排序只能在全部槽位结束后计算。
5. `problem_card` 只结构化问题本身，不产出解法；每张卡的映射与独立判官都要对照它，`desc` 须声明本卡主攻 `problem_card` 的哪一键；9 张卡全部完成后按五键盘点覆盖面，没有任何卡命中的键必须在最终交付中披露。
6. 9 张卡全部形成结果后，交付前须再做一次整批多样性检查（见 Batch Diversity Check）：判断这批卡是否有足够多本质不同的解法方向，而非表面措辞不同的同质卡；不足则换源域重抽最冗余的一张，不得直接交付同质批次。

## Mode Gate

默认进入标准模式。用户说“用 WildIdea”“帮我想方向”“给我方案”等仍是标准模式。

只有用户明确要求 `快速`、`速览`、`草稿`、`只要文本`、`不要 HTML`、`不要联网`、`别生成文件`、`先别查重` 时，才可进入非标准模式。非标准输出必须声明实际跳过了哪些步骤。

标准模式完成条件：

- 每个最终可见候选都有真实搜索证据，且没有 `needs_manual_check` 冒充通过；
- 每个候选都由独立子智能体或独立评价器完成评分：结构映射卡 6 维，`RANDOM_WORD` 卡改用独立的 4 维评分（见 Random Word 一节）；
- 已生成 HTML 与搜索 sidecar；
- 已使用 `validate_poster.py` 带禁词、sidecar 和实际卡片数校验通过；
- 最终回复给出本地路径和 `file:///` 地址。

任何要求不可用时，不得静默降级。

## Problem Card

在抽槽位之前，先把用户问题重述为 `problem_card`：五个字符串键 `actor`（受困的行动者）、`constraint`（当前限制其行动的约束）、`bottleneck_relation`（卡住的关键关系）、`desired_change`（期望达成的改变）、`trade_off`（必须在其间取舍的两难）。

- `problem_card` 只结构化问题本身，不写任何机制名、目标域常规解法或方案；这一步与 `mechanism-transfer.md` 里“源域锁定阶段禁止看目标域”并不冲突——详见该文件“目标域侧的对称要求”一节。
- 每张卡的映射步骤与独立判官都要对照 `problem_card`，而不是只对照原始问题文本。
- 每张卡的 `desc` 开头须声明本卡主攻 `problem_card` 的哪一键。
- 9 个槽位全部完成后，按五键盘点覆盖面：统计哪些键至少被一张卡命中，哪些键没有任何卡命中；未覆盖的键必须写进最终交付，不能略过不提。

## Web v1.4 Parity

以下生成逻辑必须与网页保持一致：

- 默认 9 个槽位；用户没有指定时不要生成 10 张。
- 一张卡独立经历 `生成 -> 基础校验 -> 独立评分 -> 完成/重抽`。
- 质量门槛分三档，由 `risk_profile` 选择，默认 `pragmatic`：
  - `pragmatic`（默认）：`Structural Depth >= 判官模型校准线`、`Novelty >= 7`、`Applicability >= 8`。
  - `explore`（用户要求“更野”“探索模式”时切换）：`Structural Depth >= 判官模型校准线`、`Novelty >= 7`、`Applicability >= 7`，且 `Domain Distance >= 7` 或 `Unexpectedness >= 8` 至少满足一项。
  - `research`（用户要把生成结果接入 auto-research/idea→实现→benchmark 自动验证全流程，或明确要求“研究探索优先于即时可行性”时切换）：`Structural Depth >= 判官模型校准线`、`Novelty >= 8`、`Applicability >= 6`、`Domain Distance >= 7`。这一档专为接入自动验证优化：新颖度与真跨域门槛都比 `pragmatic` 更严，用来压住“换皮老方法”混过 `NV>=7` 的情况；同时把可行性门槛从 `pragmatic` 的 `AP>=8` 主动放宽到 `AP>=6`，让结构扎实但暂不显而易见可行的候选（例如 Braess 悖论式网络机制这类 `NV9/DD9/AP7` 的候选）不会仅因即时可行性分数不够而被筛掉——可行性交给下游 auto-research 通过实现与 benchmark 去验证，不指望这道门先证明它。切到这一档需向用户/集成方披露。
  - 最终交付必须披露实际使用的档位（`pragmatic`、`explore` 或 `research`）。
  - `pragmatic` 档 `Applicability` 门槛由 9 下调到 8：判官提示词修正后 AP 评分不再集中打在 9 分，历史判官拒绝样本里近半数只差 1 分未达标、其中八成以上单独卡在 `AP>=9` 这一条；9 分的旧线是修正前打分虚高、扎堆在 9 分留下的惯性，已作废。
- 当前模型校准基线：Claude Sonnet 系列 SD 至少 6；DeepSeek v4 系列 SD 至少 6（2026-07 判官提示词修正后重校准，旧值 8 基于修正前的虚高打分，已作废）；DeepSeek R1 系列 SD 至少 9（旧提示词下测得、已知评分偏高，不建议作为判官模型，如用户坚持使用须先提醒）；其他模型优先使用已有校准值，没有校准记录时按网页代码默认线 `SD >= 6` 执行并披露。模型名带不带 provider 前缀均按同一基线处理。
- 三次尝试按三段式使用，不是盲目重抽：第 1 次未过质量门/判官门槛后，第 2 次必须把具体命中的规则名或判官维度/评委原话带入下一次生成提示，且只重写这些规则/维度指向的字段；若第 2 次仍命中同一原因（同一质量规则、或判官同一维度仍未达标），第 3 次必须换一个结构角度重新映射同一锚点——不换源锚点，也不是再改一次措辞；任何一次重抽都不得在不携带上一次具体失败原因的情况下盲目重新生成。
- 某次候选未过判官门槛时，记录 6 维评分与四项均分，然后按上一条的修复/换角度逻辑重抽同一槽位，排除本轮已用来源。
- 三次都未过判官分数门槛时，从有效候选中选择 `(SD + DD + NV + AP) / 4` 最高者，标记为 `未达标保底`，不得写成“通过”。
- 三次生成都没能通过质量门、从未进入判官时，从这三次草稿里选违规规则命中数最少的一版，同样标记为 `未达标保底` 交付，不再整槽判定失败。
- 直接重复已有实现、来源不真实、字段损坏或无法判断的候选不能作为保底。
- 若一个槽位三次都没有任何可用草稿（含上面两种保底路径都取不出结果），保留该槽位的失败状态，不得用无关内容补位。
- 网页对未达标保底/失败卡退款；本地 Skill 没有积分账本，只保留状态语义，不声称已退款。

## Type And Slots

先判断问题类型，再运行抽卡脚本。只读取抽样 JSON。

| 类型 | 命令 | 默认 9 卡配额 |
|---|---|---|
| algorithm | `python3 scripts/pick_domain_slots.py --type algorithm` | D1 x5, D2 x2, D3 x1, D6/毛选 x1 |
| research | `python3 scripts/pick_domain_slots.py --type research` | D1 x4, D2 x2, D3 x1, D5 x1, D6 x1 |
| product | `python3 scripts/pick_domain_slots.py --type product` | D1 x1, D2 x2, D3 x1, D4 x2, D5 x1, D6 x1, D7 x1 |
| strategy | `python3 scripts/pick_domain_slots.py --type strategy` | D1 x1, D2 x2, D3 x1, D4 x1, D5 x2, D6 x1, D7 x1 |

`algorithm`、`research` 两个题型的默认配额不再含 `D7/随机组词`（`RANDOM_WORD`）槽位——对硬科学题，随机组词是低信号噪声而非有效扰动源，原槽位已并入 `D1`；`product`、`strategy` 仍各保留 1 个 `RANDOM_WORD` 槽位不变。

槽位含义：

- `D1` 算法技术
- `D2` 学术机制
- `D3` 人文艺术
- `D4` 产品机制
- `D5` 社会政策
- `MAO`，卡面显示 `D6 毛选`
- `RANDOM_WORD`，卡面显示 `D7 随机组词`

D 数字表示顶层卡池，旁边的领域文字表示该来源的具体学科。相同 D 数字不得被解释成不同顶层卡池。

### Pool Modes

使用网站同款预设：

| 模式 | 命令 | 分布 |
|---|---|---|
| 默认分布 | 不传或 `--pool-mode default` | 按上表的问题类型配额 |
| 纯社会政策 | `--pool-mode social_policy` | D5 x9 |
| 纯算法 | `--pool-mode algorithm` | D1 x9 |
| 纯产品 | `--pool-mode product` | D4 x9 |

默认排除已标记为“已完成一次跨域映射的成品案例”的锚点条目：这类条目已经是别人包装好的现成类比，直接复用会形成双跳类比。仅当用户明确要求更野时，加 `--include-completed-analogies` 纳入，并在交付中披露已启用。

重抽统一调用：

```bash
python3 scripts/pick_domain_slots.py --reroll D1 --exclude D1-03 D1-11
python3 scripts/pick_domain_slots.py --reroll D5 --exclude D5-02
python3 scripts/pick_domain_slots.py --reroll mao --exclude MAO-07
python3 scripts/pick_domain_slots.py --reroll random_word --exclude 已用词
```

重抽必须尽量排除本轮已用或已淘汰的锚点。

## Source-First Contract

每张卡先形成这条链：

`具体源现象 -> 抽象方法 -> 目标域对象映射 -> 落地动作链`

字段要求：

- `source_phenomenon`：他山之石。写“谁/什么在什么情境下做了什么，产生什么结果或约束”，以中文为主，不得只是论文标题或术语名。
- `source`：机制中文名；必要时写中文名加英文缩写，如 `随机采样一致性（RANSAC）`。
- `proto`：抽象方法。不得出现用户问题、目标产品、目标数据或目标领域词。
- `claimed_method`：源机制对应的现实中真实存在的具名方法/算法/技术名，如卡尔曼滤波、岛屿生物地理学模型。确实找不到真实具名对应物时，如实写“抽象概括（无真实具名对应）”，严禁编造不存在的方法名。
- `desc`：落地方案。开头先声明本卡主攻 `problem_card` 五键中的哪一键，再用 2-4 个短句写清输入/材料、动作顺序、触发条件、改变结果以及可观察输出。
- `advantage`：以 `这种方案的优势在于，` 开头，尽量不超过 50 个中文字符，用人话说明价值。
- `fail`：具体失败前提，作为详情或内部元数据保留；紧凑卡面可隐藏。

如果锚点有 `source_scene`，优先把它作为他山之石。若有 `methods`，用 `input/process/output/key_insight` 理解结构，但不要照搬 `transfer_examples`。

算法/学术卡的主要卡面仍显示源现象；环境支持悬停详情时，同时补充通俗实际场景。

## Candidate Validation

基础校验必须在判官前完成：

- 标题短且可复述；
- 他山之石是具体事实，不是抽象品质；
- 抽象方法没有目标域词；
- 抽象方法须含至少一个因果/条件连接词（如果/当/一旦/触发/使得/随之/因此/才/否则任选其一），写成“如果/当/一旦 X，就会/导致 Y”的条件链，不能是静态名词短语——这条与 `quality.py` 的机检规则字面对齐，写卡时按这份词表自查；
- 落地方案至少有两句和明确动作链，动作链须能看到“第一步做什么、触发点是什么、产生什么可观察结果”三层，不能只是动作词罗列——同样对齐 `quality.py` 的机检口径；
- 产品题包含可见用户流程；
- 算法题包含输入数据、模型/特征步骤、输出和验证信号；
- 研究题包含变量、测量、对照或验证条件；
- 策略题包含行动者、激励/约束变化和可观察指标；
- 去掉外域机制后若只剩行业常识，淘汰；
- 不依赖品牌专名来制造具体感，除非用户明确要求竞品方案。

## Independent Judge And Retry

标准模式中，生成者不能自评。独立判官只接收用户问题、`problem_card`、源域、目标域、对象映射、共享关系与候选文本，不接收生成过程；除了给 6 维打分，还要核对候选是否真的命中它 `desc` 声明主攻的那个 `problem_card` 键。

评分维度为 0-10（`RANDOM_WORD` 卡不适用本节六维评分，改用 Random Word 一节的 4 维评分）：

- Structural Depth
- Domain Distance
- Applicability
- Novelty
- Unexpectedness
- Non-Obviousness

评 `Structural Depth` 时须显式核对：源域到目标域的各个对应点是否被同一条因果/功能关系串联起来（systematicity），还是彼此独立、只是表面属性的巧合。孤立的表面巧合即使对应点数量多，也必须打低分，不能等同于真正的结构映射。

每槽执行：

1. 生成并做基础校验；失败则记录具体命中的规则，按三段式（首次修复被点名字段、连续同因换结构角度、不得盲目重抽）安排下一次尝试，本次消耗一次尝试次数。
2. 独立判官评分，同时核对 `problem_card` 对应关系。
3. 达到质量门槛（见 Web v1.4 Parity 的 `pragmatic`/`explore`/`research` 三档）后，再做联网审查；查重开关生效时（默认随 `research` 档自动开启，也可被显式开关覆盖，见 Target Field Novelty Check 一节“生效条件”）还需过 Target Field Novelty Check。
4. 联网确认直接同机制实现时，本次淘汰；搜索无法判断时，标准模式不得将其标为通过。
5. 未过分数门槛的有效候选保留在该槽位的保底候选集中。
6. 最多三次；若曾进入判官，三次结束后按四项均分选择可搜索确认的最高保底；若三次都未通过质量门、从未进入判官，改选三次草稿中违规规则命中数最少的一版作为保底；两种保底都取不出结果时该槽位失败。

## Random Word

`algorithm`、`research` 题型默认不再抽到 `RANDOM_WORD` 槽位（见 Type And Slots 配额表）；`product`、`strategy` 抽到时仍按以下流程执行。

随机组词的目标是引入不可预测来源，但不得让模型随意换源：

1. 保留脚本抽到的原词。
2. 用 Agent 真实联网搜索该词。
3. 从真实结果中提取操作、规则、事件或边界。
4. 再抽象和映射。

没有可靠搜索落点时重抽或失败，不能凭空改成河流侵蚀、免疫系统等无关机制。

随机组词卡不适用六维结构映射评分（Structural Depth/Domain Distance/Applicability/Novelty/Unexpectedness/Non-Obviousness）。改用独立判官模板 `src/wildidea/prompts/judge_random_word.txt`，评 4 维（0-10）：

- `chain_clarity`：链路清晰度
- `actionability`：可执行性
- `novelty`：新颖度
- `unexpectedness`：意外度

通过线：`chain_clarity >= 6` 且 `actionability >= 6` 且 `novelty >= 7`（初始未校准值，待实际打分分布出来后再调整，此处先如实注明）。

## Search Audit

Skill 标准模式保留网页之外的增强审查：

- 产品/策略：查产品功能、竞品、专利和公开案例。
- 算法/研究：查论文、代码、基准、数据集和专利。
- 每个最终候选至少一条真实搜索记录。
- “未发现同名”只能写成粗查结论，不能宣称绝对原创。
- 产品、论文、代码或专利出现同机制实现时淘汰。
- 搜索失败或无法判断时，不得作为标准通过卡。

按 `references/search-integration.md` 写 `outputs/<topic>.search.json`。

## Target Field Novelty Check（独立查重开关）

候选通过独立判官门槛后，若本次运行的查重开关生效（不再要求必须处于 `risk_profile == "research"` 档，生效条件见下文“生效条件”一条），还要多做一次目标领域新颖度检查。这条检查和六维评分里的 `Novelty`/`Domain Distance` 不是一回事：六维评分判断的是“源域类比是否新颖”，这里核对的是候选的 `claimed_method` 落到**目标问题/目标领域**里是否已经是已知或标准做法——例如 MoE 用于跨被试 EEG 情绪识别在文献里已是 SOTA，源域类比可以显得很新颖，但目标领域里它并不新，六维评分容易被这种情况误导给出虚高的新颖度。

产物约定：`check_target_field_novelty(candidate, problem, llm, *, _search_backend=None) -> {"is_known": bool, "evidence": str, "confidence": "high"|"low"}`。

- 默认实现按题型分流检索源：`algorithm`/`research` 题默认走 OpenAlex 学术检索（arXiv 兜底）（`https://export.arxiv.org/api/query`，详见 `references/search-integration.md`“学术查重（arXiv）”一节），查“该具名方法用于该研究问题的具体组合是否已有发表工作”；`product`/`strategy` 题仍走内置搜狗通用网页检索（`core/search.py`）。两者拿到检索结果后都交给判官模型做同一次判断。
- 判定口径收紧：只有当检索证据显示“该方法用于该问题”这个具体组合已是既有发表工作时才算 `is_known=True`；命中很多与该方法相关但不是同一组合的论文（方法本身在其原生领域很有名，但没人把它用在这个目标问题上）不算已知。
- 可插拔：`_search_backend` 形参留给调用方注入自己的检索后端——auto-research 集成方可以换成自己的文献检索（例如内部论文库、Semantic Scholar），替换默认 arXiv/搜狗，接口签名不变。
- 降级纪律：检索失败、解析失败或结果不可判断时，必须优雅降级为 `is_known=False`——宁可漏检，也不能因为工具报错就误伤一个本来合格的候选。
- **查重驱动换卡（闭环）**：`is_known=True` 的候选不再只是打“领域内已知”标签被动展示——该槽位改为换一个新的源锚点（不是同一锚点换措辞）重新走完整流程：质量门槛 -> 独立判官 -> 再查一次目标领域新颖度，最多额外重抽 `max_novelty_rerolls` 次（与质量门/判官阶段最多 2 次重抽的预算各自独立计数，互不占用）。目的是把联网查重从“生成后打标”的被动过滤器，变成“生成中主动驱动新方向”的机制，让最终交付、以及喂给 auto-research 的候选尽量都不与已发表工作撞车。
- 达到 `max_novelty_rerolls` 上限仍判定 `is_known=True` 时，保留最后一版，标记为“领域内已知”，并在交付中披露已耗尽换锚点重抽额度，不得无限换锚点。
- 目的：避免把目标领域里已经是标准做法的方法当成新 idea 喂给自动验证流程，白白烧掉实现和 benchmark 的算力。
- 生效条件：是否运行这一步只由 `Config.research_novelty_check` 单独决定，不再要求 `risk_profile == "research"`。该字段类型是三态的 `Optional[bool]`，默认 `None`：`None`（未显式指定）时按档位自动——`risk_profile == "research"` 默认开，`pragmatic`/`explore` 默认关；显式传 `True` 时无论当前档位为何都强制开启；显式传 `False` 时无论档位为何都强制关闭。判定逻辑统一收敛到一个 helper：`_novelty_enabled(config) = config.research_novelty_check if config.research_novelty_check is not None else (config.risk_profile == "research")`；pipeline 里原先各自判断“`risk_profile == "research"` 且 `research_novelty_check`”的两处门（逐槽的目标领域新颖度检查、`is_known=True` 后的换锚点重抽闭环）现在都改调用这同一个 helper，不再各自重复条件、也不会出现两处判断口径不一致。CLI 提供 `--novelty-check`/`--no-novelty-check`（不传时为 `None`，按 `--profile` 自动）；网页新增“是否查重”开关（`CreateRunRequest.novelty_check`，默认 `False`，即网页默认不开查重，由用户按需勾选打开），该值随请求存入 `config_snapshot`，并作为显式 `True`/`False` 传给 `Config.research_novelty_check`（网页侧永远显式传布尔值，不传 `None`，因此网页发起的运行不会落入“按档位自动”分支）。
- 事件：检查结果作为 `novelty_flag` 事件披露，payload 含 `slot_id`、`claimed_method`、`is_known`、`confidence` 四个字段；闭环换锚点重抽时，每次重新检查都各自发一次 `novelty_flag` 事件，便于追踪一个槽位为摆脱“已知”标记换了几次锚点。

## Batch Diversity Check

9 个槽位全部形成最终结果（通过/未达标保底/失败）后，交付前再做一次整批层面的检查：

- 判断这 9 张卡在解决思路上是否有大约 6 个或以上本质不同的方向：核心机制或切入点不同，而不是同一改法换了措辞或表面领域。
- 即使标题、`source_phenomenon` 或卡池领域看起来不同，只要核心机制/切入点相同，也计为同一方向的重复卡。
- 本质不同方向数低于约 6 个时，判定为集合层的 mode collapse（生成收敛到同一类改法），而非单卡质量问题：把这批里最冗余（与其他卡本质方向重复度最高）的一张卡换一个源域重抽，不得直接交付一批同质卡。
- 重抽后的替代卡仍须完整走质量门槛与独立判官流程，不能免检直接放行。
- 最终交付需披露本轮统计出的本质不同方向数。

## Output Contract

最终保留 9 个槽位结果及其状态：

```text
01 通过
02 重抽 1 次后通过
03 未达标保底
04 失败
...
```

主卡面优先显示：问题、标题、D 槽位与具体领域、他山之石、抽象方法、落地方案、优势。6 维评分、失败边界、搜索证据和重抽详情可放详情区或 sidecar，不要挤占主卡面的可读性。

结果统计至少报告：通过数、未达标保底数、失败数、累计重抽数、搜索状态、通过卡的 SD/DD/NV/AP 均值、本轮使用的质量门槛档位（`pragmatic`/`explore`/`research`）、`problem_card` 五键覆盖盘点（哪些键无任何卡命中），以及整批多样性检查得到的本质不同方向数；查重开关生效时还须报告 `novelty_flag` 命中数（最终仍标记为“领域内已知”的候选数）以及查重驱动换锚点重抽的累计次数。

## HTML Pipeline

1. 读取 `references/poster-guide.md` 和 `templates/poster.html`。
2. 写入 `outputs/<topic>.html`。
3. 写入 `outputs/<topic>.search.json`。
4. 从用户输入和常见解法中提取中英文禁词。
5. 9 张可渲染卡时运行：

```bash
python3 scripts/validate_poster.py outputs/<topic>.html \
  --cards 9 \
  --forbid-proto-term <目标域禁词...> \
  --search-sidecar outputs/<topic>.search.json
```

6. 若有完全失败槽位导致可渲染卡少于 9 张，`--cards` 传实际渲染数，并在最终回复明确缺失槽位，不能伪造补齐。
7. 最终回复给出本地绝对路径和 `file:///` 地址。

## Algorithm And Research Additions

算法/科研题还要读 `references/mechanism-transfer.md`，内部保留源域原型、对象映射、共享关系、去锚点退化物、最近邻差异、最强反驳、最小可证伪实验与验证状态。

主卡面的落地方案仍然必须通俗：写拿什么数据，先后做什么，何时触发，最后用什么指标验证，不要只列变量名。

## Iteration

用户要求“迭代”“再野一点”或把上一轮产出也排除时：

- 将上一轮对应物加入禁用区；
- 不复用上一轮来源 ID 和主对应物；
- 仍按 9 个原始槽位、每槽最多 3 次执行；
- Soft-ban 可作为组件，但不能再次成为主创新。

## Optional Modes

| 触发词 | 行为 |
|---|---|
| 快速/速览/草稿 | 输出 1-2 条或用户指定数量，并声明未完成的标准步骤 |
| 精简/宁少勿多 | 正常抽样后只展示最强不超过 3 条，内部仍保留筛选事实 |
| 极端/一条/一杀 | 只展示 1 条，附最小启动成本和止损线 |
| 画像/角色 | 按工程师、设计、管理或研究者调整表达 |

## References

- `references/mechanism-transfer.md`
- `references/search-integration.md`
- `references/poster-guide.md`
- `references/poster-palettes.md`
- `references/output-innovation-recipes.md`
