# SKILL · bundle —— 产可执行包：代码 + identity + execution_manifest（一目标一调用）

> 版本：m7-2（步⑧：真执行契约 + plan-frozen GPU access mode）。按《第一部分》§3.4 与流程图 05-Bundle。
> 产物 schema = `schemas/execution_manifest.schema.json`（manifest）；代码/identity 为自由文件。
> **分层铁律**：计划（做什么）在上游 plan 已锁死并以「resolved 计划切片」给你（上下文包①固定锚）；
> 你产「怎么做」的**可执行包**；编排器交叉核对切片后由 harness **机械执行** manifest 命令——
> 你写错契约字段，包会被拒（目标 failed），所以照抄锚区给出的绑定值、不要自创。

## 通用

- **触发条件**：plan 产出 targets 非空（route=attack）后，编排器按 seq **严格串行**、逐目标调用你
  （一次一个 target、一信封一包；目标间不共上下文）。
- **读取**（上下文包①固定锚「本目标」节）：resolved 计划切片全文（target_key/target_kind/seq/spec_md/
  claim/budget…）+ **plan_slice_hash**（64 个十六进制字符的 sha256 串，照抄）+ **required 指标绑定**
  （形如 `1@1` 的 int 对，eval 输出用）+ 协议绑定 protocol_id/protocol_ver。
- **门禁与写入**：你的包经编排器三级校验（schema + 切片交叉核 + 围栏）后物化执行；执行事实与测量经
  gate 注册入库（**两段提交**：段(i) 执行事实随发生入账，段(ii) 结果评审过后才注册测量整包）——
  全部由编排器负责，你不写库。
- **失败语义**（P4 成败同记，编排器状态机执法，你须知其后果）：
  - manifest/包非法 → 目标 failed(artifact_invalid)；smoke 败 → failed(smoke)；训练/评估败 →
    failed(runtime)；评审否决 → failed(review_failed)；测量不满足协议 → failed(protocol_violation)；
    工程性无法推进 → engineering_blocked。
  - **critical 目标失败 → 早退**：本 cycle 剩余未执行的 pending 目标置 **skipped**（未执行旁路）；
    非 critical 失败 → 本目标 failed、继续下一目标。一切失败裁决权属轮尾 reasoning。
  - 隔离说明：canary 期代码物化在编排器管理的 staging（净土物化+哈希对账）；git worktree 级隔离与
    env lock 强校验属后续硬化步——不要在包里假设可写任意路径。

## 产出（一个信封装齐；文件名即信封 files 的键）

1. **代码文件**（≥1 个，通常 `train.py` / `eval.py` / `smoke.py` / 配置文件）：实现切片 `spec_md`
   声明的构建/训练/评估。代码/文本文件的值为字符串；`.json` 配置文件的值可为 JSON 对象（编排器物化
   为规范化 JSON）。可用子目录（如 `pkg/util.py`）；相对路径，禁 `.`/`..` 段。
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
     - 占位符：`{src}` = 代码物化目录（如 `["python", "{src}/train.py"]`）；`{ckpt}` = 训练产
       checkpoint 路径（**仅 eval 命令可用**，如 `["python", "{src}/eval.py", "{ckpt}"]`）。
     - 命令的 cwd 是独立的 run 目录（不是代码目录）：读代码走 `{src}`，写产物写 cwd 相对路径。
     - 路径围栏：绝对路径 token 只许指向运行区或运维放行的数据根；相对 token 禁 `..`。
   - `expected_outputs`: `{checkpoint: "<train 命令在其 cwd 产出的 checkpoint 相对路径>"}`（如
     `"ckpt.bin"`）。train 代码**必须**真产出该文件。
   - `repro_cmd_md`: 人可读复现命令块（与 identity.md 复现节一致）。
   - `env`: 附加环境变量（可整键省略；大写键；禁改 PATH/PYTHONPATH/PYTHONHOME/HOME/LD_*）。

## 执行流程契约（你的代码必须满足；编排器按此机械驱动 smoke→train→eval）

- **smoke**：秒级快速可跑判定（如小步前向）；退出码 0=过。失败 → 目标 failed(smoke)。
- **train**：真训练；向 stdout 打印 `loss: <float>` 轨迹行（观测 parser 消费）；结束前在 cwd 写出
  `expected_outputs.checkpoint`；退出码 0=成功。
- **eval**：读 `{ckpt}` 评估，向 stdout 对**锚区 required 的每个绑定**打印一行
  `metric_value: <metric_id>@<metric_ver>=<float>`（int 照抄，如 `metric_value: 1@1=0.93`）；
  退出码 0=成功。**漏打 required 指标 = 测量注册被拒（目标 failed）**。
- 失败语义（P4 成败同记，状态机由编排器负责）：训练/评估失败会如实入账（run/attempt failed），
  不要在代码里吞错误装成功；**指标值必须来自真计算**——硬编码/伪造会被结果评审 fail。

## 双评审（编排器另行调用独立判官，你无须产评审文件）

代码适配评审 **bundle_code_review**（smoke 后、训练前）与结果评审 **bundle_result_review**（评估后、
注册前）见 `prompts/skills/judge/SKILL.md`；被 fail 即目标 failed(review_failed)。
把 spec_md 忠实实现、让 log 与结果自洽，是过审的全部要诀。
