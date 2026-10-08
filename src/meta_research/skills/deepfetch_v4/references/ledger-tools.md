# 台账工具

示例中 `DEEPFETCH_ROOT` 为 `deepfetch-v4/` 的绝对目录，`OUTPUT_DIR` 为本次运行的绝对目录。具体参数以各子命令 `--help` 为准。

## 初始化与发现登记

```bash
python3 "$DEEPFETCH_ROOT/scripts/papers.py" init \
  --out-dir "$OUTPUT_DIR" --topic-file "/absolute/prompt.txt" \
  --interpretation "当前任务的简洁检索解释" \
  --concept "检索概念一" --concept "检索概念二" \
  --intensity medium
```

`upsert` 接受一项发现记录、数组、`{"papers": [...], "limitations": [...]}` 或完整 `deepfetch.openalex.v4` search／get 信封。新论文 intake 必须带已核实原论文 version provenance。OpenAlex get 结果先经原论文核验、补 provenance，再提交。工具移除雷达专属字段并写入元数据；主智能体仍须筛选实际保留条目，按[台账契约](papers-json.md)填写 `summary`、`evidence_level`、`basis`、`why_included`、`uncertainty`。`update-run` 保存单调递增的主动检索秒数、实际维度和停止理由。

用 `record-discovery --out-dir "$OUTPUT_DIR" --input "/absolute/discovery.json"` 提交恰含 receipts 与 unresolved_leads 数组的 JSON。先登记工具返回的全部原始回执，再用 upsert 的 provenance.discovery_refs 引用它们。不得改写宿主回执，宿主导入校验真实执行的一致性。失败回执自动加入 limitations。原学术身份和确认版本控制合并，重复标题不合并。

## 全文与 Reader

对已核实的 Acquisition 结果登记正文：

```bash
python3 "$DEEPFETCH_ROOT/scripts/papers.py" register-fulltext \
  --out-dir "$OUTPUT_DIR" --paper-id "已返回的版本 paper_id" \
  --file "/absolute/provider/result.pdf"
```

单次运行最多登记 10 篇不同论文的全文；额外发现仍可作为元数据占位。替换同篇已登记／分配论文的文件不另占名额，首次 Reader 分配后的名额规则见[角色契约](agents.md)。

```bash
python3 "$DEEPFETCH_ROOT/scripts/papers.py" prepare-readers \
  --out-dir "$OUTPUT_DIR" --task "当前研究问题"

python3 "$DEEPFETCH_ROOT/scripts/papers.py" apply-reader \
  --out-dir "$OUTPUT_DIR" --result "/absolute/reader-patch.json"
```

任务带精确 `patch_template` 和绝对 `reading_contract_path`。各 Reader 只修改自身 patch，由合并命令负责并发写入。

## 完成验证

主智能体写完 `summary.md` 后运行：

```bash
python3 "$DEEPFETCH_ROOT/scripts/papers.py" finalize --out-dir "$OUTPUT_DIR"
```

`validate` 检查进行中的稀疏台账；`validate --final` 只检查最终门槛，不清理。仅调试或需恢复时给 `finalize` 添加 `--keep-debug-state`。完成条件是公开文件与阅读状态一致、引用可解析，工具校验通过；命令成功不替代综述内容核查。
