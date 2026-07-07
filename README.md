# meta-research 元循环系统（施工中）

依据 `../reference/`（三部分施工标准 + 流程图）实现；当前进度见仓库根 `ROADMAP.md`。
本 README 只说明目录布局与当前里程碑边界，**设计真相唯一在 `../reference/第一部分-系统架构设计.md`**。

## 目录布局（对齐《第二部分》§6.3；随里程碑逐步补齐）

```
meta-research/
├── policies/policy.yaml     # 全部可调旋钮（P2；全量注册表 = 第一部分附录 C，每旋钮带默认值）
├── prompts/                  # system_prompt + 四阶段 skill（CP1.2 落地）
├── schemas/                  # 四阶段产物 JSON Schema + sidecar + policy schema（Gate 校验对象，§6.11）
├── orchestrator/             # 确定性编排器（Python；M0 = 接口 + 桩 + 最小驱动器）
│   └── interfaces.py         #   流程层↔资产层唯一缝（§6.10 Protocol；桩与真实现共用签名）
├── input/
│   └── goal_brief.md         # 启动输入①：研究目标书（YAML frontmatter 含 predicate_json，§4.6.7）
├── tests/                    # 自验（pytest）：schema 元校验 + 正/负例 + policy / goal_brief 解析
├── engines/wildidea/         # vendored idea 引擎（M0 仅 adapter 骨架位，后续里程碑落）
├── db/                       # SQLite 唯一真相（M1 落地；M0 禁建——见下）
├── views/ baselines/ protocols/ questions/ uploads/ connectors/   # 后续里程碑落地
```

## 自验

```bash
python -m pip install -r requirements-dev.txt   # pytest / jsonschema / PyYAML
python -m pytest tests/ -q                       # 在本目录（meta-research/）下运行
```

## 当前里程碑边界（M0，验收 = 第三部分 §7.1 M0 行）

- **只验流程契约、不验不变量**：Gate 桩只做 schema + 引用完整性两级，业务门禁放过。
- **不建 DB**：不得写 M1 才存在的真实 DB 表（`db/` 目录保持为空）。
- **假执行必须显式标记**：驱动器造假的 evaluation 标 `source=fake`；execution_log / execution_observation 标 `synthetic=true`。
- **Runner 从 M0 起即真**（`codex exec` 一次性调用，无状态工人）。
