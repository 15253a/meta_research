# SKILL · bundle —— 产包并执行：代码 + smoke/train/eval + 日志修复（整 cycle 一个连续 turn）

> 版本：m7-3（步⑧：真执行契约 + plan-frozen GPU access mode + 多 checkpoint/fold）。按《第一部分》§3.4 与流程图 05-Bundle。
> 产物 schema = `schemas/execution_manifest.schema.json`（manifest）；代码/identity 为自由文件。
> **分层铁律**：计划（做什么）在上游 plan 已锁死并以「resolved 计划切片」给你（上下文包①固定锚）；
> 你产「怎么做」的**可执行包**，并在当前 Bundle 主 turn 内调用 runtime MCP 完成受控
> smoke/train/eval、观察日志、修复与结果审查。不存在后续 event 顶层 turn，也不产
> `bundle_operator_action.json`。harness/guardian 仍负责机械隔离与生命周期；你写错契约字段时，
> MCP 将在同一 turn 返回精确错误，须就地修正。

## 通用

- **触发条件**：plan 产出 targets 非空（route=attack）后，整个 cycle 的 Bundle 阶段只启动一个顶层
  Codex turn/process。你在这一 turn 内用 `bundle_next_target` 按 seq **严格串行**地获取/绑定每个 target
  的最新 ContextPack，完成其产包、smoke/train/eval、日志观察与工程修复，然后继续下一个。
  不得在 target 边界退出后依赖 `resume`；只有进程灾难恢复才可用耐久 provider id。
- **读取**（上下文包①固定锚「本目标」节）：resolved 计划切片全文（target_key/target_kind/seq/spec_md/
  claim/budget…）+ **plan_slice_hash**（64 个十六进制字符的 sha256 串，照抄）+ **required 指标绑定**
  （形如 `1@1` 的 int 对，eval 输出用）+ 协议绑定 protocol_id/protocol_ver。
- **门禁与写入**：完成包后调用 `meta_research_runtime.submit_stage_artifact`。MCP 在本 turn 内校验 schema、
  文件闭包和冻结 plan 绑定并把正文存入文件管理；错误会直接返回给你，须就地修改并重提。成功后 SQL 只记
  path/hash 回执，编排器再消费回执并执行围栏及核心 gate。执行事实与测量仍按原**两段提交**核心事务注册。
- **失败语义**（P4 成败同记，编排器状态机执法，你须知其后果）：
  - manifest/包非法、smoke/训练/评估失败、评审否决或测量违约都会携精确
    `failure_kind` 回传本会话，要求在同一冻结 plan 内重出完整实现。当前第一版
    `flow.retry.bundle_repair=null`，工程修复不设轮次上限；只有你判断是冻结 plan 本身的问题才 `replan`
    并交给必经 Reasoning，或由用户明确停止；普通工程错误不能因轮次计数直接变成 `engineering_blocked`。
  - **critical 目标失败 → 早退**：本 cycle 剩余未执行的 pending 目标置 **skipped**（未执行旁路）；
    非 critical 失败 → 本目标 failed、继续下一目标。一切失败裁决权属轮尾 reasoning。
  - 隔离说明：canary 期代码物化在编排器管理的 staging（净土物化+哈希对账）；git worktree 级隔离与
    env lock 强校验属后续硬化步——不要在包里假设可写任意路径。

## 产出（文件管理优先；路径清单即交接）

在 `local_tools_enabled` 下，先把完整包写入当前 cwd 的 `submission/`，然后调用
`submit_stage_artifact(files={}, workspace_files=[...])`，路径数组只列必要相对路径；不要把源码正文再次
复制进 `files`。MCP 会流式托管并计算 path+hash，后续仍走同一 manifest 交叉核、staging、Gate 与 SQL
核心入库链。没有本机文件工具的兼容调用才把小型文件内联进该 MCP 的 `files` 参数。

1. **代码文件**（≥1 个，通常 `train.py` / `eval.py` / `smoke.py` / 配置文件）：实现切片 `spec_md`
   声明的构建/训练/评估。代码/文本文件的值为字符串；`.json` 配置文件的值可为 JSON 对象（编排器物化
   为规范化 JSON）。可用子目录（如 `pkg/util.py`）；相对路径，禁 `.`/`..` 段。
   - `gpu_required=true` 时，代码须在容器内从框架运行时发现已分配设备数（例如
     `torch.cuda.device_count()`），不得硬编码宿主物理编号或改写 `CUDA_VISIBLE_DEVICES`。分配数大于 1 时，
     真训练应采用与框架/算法相容的 DDP、DataParallel、数据并行或逐卡并行折叠，尽可能使用全部已分配
     GPU；若算法确实只能单卡，须在 `identity.md` 明示限制，不能伪称已经多卡运行。启动时向日志打印实际
     发现和使用的 GPU 数，供同一 Codex operator 核对。
2. **`identity.md`**：该 baseline 的人可读身份——名称、用途、结构/机制摘要、能力摘要，**必含
   「## 复现命令」节**（注册入池后它就是池资产的身份文档）。
3. **`execution_manifest.json`**（机器执行契约，逐字段规则）：
   - `manifest_version`: 1。
   - `target_ref`: `{target_key, target_kind, seq, plan_slice_hash}` —— 全部**照抄锚区切片与
     plan_slice_hash**（编排器重算核对，抄错即拒）。
   - `protocol_ref`: `{protocol_id, protocol_ver}` —— 照抄锚区（int）。
   - `gpu_required`: **逐字照抄锚区从 plan target 冻结的 bool**；bundle 不得根据部署库存或自己生成的代码改写。
   - `env_hash`: **逐字照抄锚区按 gpu_required 给出的 pinned workload sha256**；不得自造环境名、tag 或隐式
     pull。CPU/GPU hash 不同，防止两种执行模式互相复用；GPU 为 true 但启动 canary 未证明 fixed allocation
     时，编排器会在创建执行 session 前拒绝。
   - `python_requirements`: 可选数组，只在实现实际需要且 pinned 本机环境缺包时填写，优先使用
     `package==version`。不要为了“完整”重装 torch/numpy 等已可 import 的大包；先使用基础环境，若 smoke
     日志出现 `ModuleNotFoundError`，在同一 Bundle 主 turn 的 repair 中补入精确缺包。系统会在每个 phase
     的正式 Docker capability 内用 `python -m pip` 非 shell 安装到该 phase cwd 的
     `.mr-python-deps/`，并把安装过程写入同一 execution log；不会修改宿主 Conda、代码或数据目录。
     安装时间计入该 phase 的 `timeout_s`，联网能力取锚区 `network=...`，manifest 无权另行扩大网络。
   - `config_json`: 代码实际使用的配置对象。**切片 claim.config_json 非空时须与之完全一致**
     （计划是配置的决定者）；切片未给则你自定（并让代码真用它）。
   - `code_files`: 信封中全部代码/配置文件名的清单（**不含**保留名 identity.md / execution_manifest.json /
     _staged.ok）。
   - `commands`: `{smoke, train, eval}` 三个都要（build **与 exec** 目标同——exec 是既有 baseline 的
     新变体，一样要 smoke/训练/评估）。每个 = `{"argv": [程序, 参数…], "timeout_s": 秒}`
     （timeout_s 可整键省略；**键名逐字，不要加 `?` 等多余字符**）：
     - **argv 数组、禁 shell**：不得用 `bash -c` / `sh` / `env` 作程序名；一个 token 一个参数。
     - 程序名必须存在于锚区 pinned image；默认 bootstrap 镜像用 `python`，不得写 host 的
       `/usr/bin/python`、虚拟环境绝对路径或假定未锁定的系统工具。
     - 占位符：`{src}` = 代码物化目录（如 `["python", "{src}/train.py"]`）。checkpoint 占位符
       **仅 eval 命令可用**：仅有一个 checkpoint 时可用 `{ckpt}`；多个 checkpoint 必须按身份使用
       `{ckpt:<ckpt_key>}`，同一 eval argv 可放多个（如 `"{ckpt:fold0}"`、`"{ckpt:fold1}"`）。
       用户上传的单文件只用上下文给出的 `{asset:<完整 user-file-request ref>}`。Web 已登记的本机
       数据/参考目录只用 `{local_source:<清单中的 source_id>}`，例如
       `"{local_source:e06eaa405c6047b685ef6e1ec0d4eef2}/DEAP"`；系统把它解析到已冻结、只读挂载的
       exact 根目录，**不会复制数据**。不得把裸 source_id 写成 `{asset:...}`，也不得猜或硬编码宿主绝对路径。
     - 命令的 cwd 是独立的 run 目录（不是代码目录）：读代码走 `{src}`，写产物写 cwd 相对路径。
     - 路径围栏：绝对路径 token 只许指向运行区或运维放行的数据根；相对 token 禁 `..`。
   - `expected_outputs`: 新包一律声明规范数组
     `{"checkpoints": [{"ckpt_key": "<唯一身份>", "path": "<train cwd 下的相对路径>"}, ...]}`，
     例如单模型为 `{"checkpoints":[{"ckpt_key":"final","path":"ckpt.bin"}]}`，LODO 可逐折写
     `fold0`、`fold1` 等。`ckpt_key` 在数组内唯一，仅用 ASCII 字母数字及 `._-`（最长 128）；path
     禁绝对路径、反斜杠及 `.`/`..` 段。旧 `{checkpoint:"ckpt.bin"}` 仅供已有 manifest 回放，
     不用于新产物。train 代码**必须**真产出数组中的每个文件。
   - `repro_cmd_md`: 人可读复现命令块（与 identity.md 复现节一致）。
   - `env`: 附加环境变量（可整键省略；大写键；禁改 PATH/PYTHONPATH/PYTHONHOME/HOME/LD_*）。

## 执行流程契约（【Bundle 主任务】；同一 cycle turn 串行全部 target）

- 进入 Bundle 后**第一个工具动作必须是 `bundle_next_target`**。它返回 `cycle_complete=false` 时，只使用
  其 `context_pack` 与身份字段处理当前 target；不得沿用上一 target 的 plan slice/refs。
- 写完当前 target 的完整包，让本 turn 唯一的干净 reviewer 做代码审查并记录 `bundle_code` review，修复后调用
  `submit_stage_artifact`。保存成功返回的 exact `submission_ref`。
- 立即在同一 turn 调用 `bundle_execute(submission_ref=...)`。该调用只异步启动服务端依精确 manifest、
  target、owner、guardian 和 gate 串行执行的 smoke/train/eval，必须快速返回；你不能更换
  服务端绑定的 argv/image/GPU/env，也不能直接写 SQL。
- 用 `bundle_status` 轮询 `worker_running`、`live_logs`、权威 execution logs、`latest_repair` 和终态。
  发现 traceback、OOM、NaN/Inf、发散、
  错误数据/shape、缺产物或伪成功时，在当前 turn 修改完整包，再次提交并调用
  `bundle_execute`从 smoke 重跑。若错误已在部分日志中确凿出现且命令仍在运行，先用
  `bundle_repair(diagnosis_md=...)` 请求 guardian 取消并排空，轮询到 worker 结束再修复。
  `controller_error` 非空是核心管线异常，不得吞掉或假装 target 完成。
- 当前 target 终态后必须再调用 `bundle_next_target`。它返回下一个 target 时重复上述流程；
  **只有 `cycle_complete=true` 时才可结束这一个顶层 Bundle turn**。不得结束 turn 等外层事件唤醒。
- `dependency_install_failed` 或 `ModuleNotFoundError` 属工程问题：优先修正 `python_requirements`/版本兼容并
  从 smoke 重跑，不得把包缺失提升成研究问题或要求用户手工安装。
- 当服务端固定分配多张 GPU 时，若训练日志显示只发现/使用其中一张且实现没有明确的算法限制，也属于工程
  配置问题：返回 `repair`，让同一 session 修正多卡启动或并行实现后从 smoke 重跑。

- **smoke**：秒级快速可跑判定（如小步前向）；退出码 0=过。失败会作为本 turn 的精确 repair 反馈；
  主智能体明确放弃修复或服务端证明不可恢复后，才终态为 failed(smoke)。
- **train**：真训练；向 stdout 打印 `loss: <float>` 轨迹行（观测 parser 消费）；结束前在 cwd 写出
  `expected_outputs.checkpoints` 声明的全部文件；退出码 0=成功。
- **eval**：读相应 `{ckpt}` / `{ckpt:<ckpt_key>}` 评估。对**锚区 required 的每个指标绑定**，每折打印
  `metric_value: <metric_id>@<metric_ver>[checkpoint=<ckpt_key>]=<float>`（如
  `metric_value: 1@1[checkpoint=fold0]=0.91`），并打印一行无后缀的 aggregate：
  `metric_value: <metric_id>@<metric_ver>=<float>`。单 checkpoint 也可同时给 fold 与 aggregate；
  退出码 0=成功。**漏打 required aggregate、折 key 不属于声明 checkpoint 或折集合不完整 = 测量注册被拒
  （目标 failed）**。
- 失败语义（P4 成败同记，状态机由编排器负责）：训练/评估失败会如实入账（run/attempt failed），
  不要在代码里吞错误装成功；**指标值必须来自真计算**——硬编码/伪造会被结果评审 fail。

## 主会话内的独立评审

在本 Bundle 主 turn 中启动**恰好一个**干净子智能体，并保留同一可定址子会话直到整个
`cycle_complete=true`。每个 target 的代码提交前让
这同一子智能体做 **bundle_code** review（审计索引 `code_review`）；eval terminal 时只把服务端的权威
日志、退出码、测量和产物回执再发给**这同一个**子智能体做 **bundle_result** review（索引
`result_review`）。切换 target 时也不得重建 reviewer，更不得为结果审查启动第二个子智能体。子智能体只返回问题，不能提交或改库；
你吸收意见、修复并分别用 `record_review(review_kind=bundle_code)` 和
`record_review(review_kind=bundle_result)` 留下索引。review fail 是修改信号，不得另开顶层 Bundle Codex。

只有权威执行反馈证明冻结 plan/协议本身不可执行，且继续改代码、依赖或运行环境也无法解决时，
才调用 `bundle_replan(diagnosis_md=...)`。该工具只记录 replan 意图；必须转入本 cycle 的 Reasoning 主 turn，
只有 Reasoning 核心事务可以终态化问题/轮次与决定后续。
