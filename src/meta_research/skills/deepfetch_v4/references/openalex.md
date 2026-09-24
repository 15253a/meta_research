# 文献发现入口

OpenAlex 提供可复核的元数据查询、精确身份解析和引文邻域；Web Search 补充术语、近期或弱索引工作、综述、基准、命名方法及台账中显现的缺口。两者都不直接判定质量。主智能体负责查询、锚点、方向、深度、相关性与停止判断。

## OpenAlex

`scripts/openalex.py` 不保存运行状态，返回规范元数据和明确引文方向。按研究语言和领域选择实际查询；例：

```bash
python3 "$DEEPFETCH_ROOT/scripts/openalex.py" search \
  --query "unsupervised domain adaptation EEG emotion recognition" \
  --query "cross-subject EEG affective computing transfer" \
  --limit 25 --output "$OUTPUT_DIR/.deepfetch/openalex-search.json"

python3 "$DEEPFETCH_ROOT/scripts/openalex.py" get \
  "doi:10.xxxx/example" "W1234567890" \
  --output "$OUTPUT_DIR/.deepfetch/openalex-works.json"

python3 "$DEEPFETCH_ROOT/scripts/openalex.py" citations \
  --seed "W1234567890" --seed "doi:10.xxxx/example" \
  --direction both --limit 50 \
  --output "$OUTPUT_DIR/.deepfetch/openalex-citations.json"
```

明确维度需要时使用 `--from-year`、`--to-year`、重复 `--work-type` 或 `--sort FIELD:asc|desc`，精确参数查 `--help`。search／get 信封为 `deepfetch.openalex.v4`；向 `papers.py upsert` 传精确 get 或筛选后保留的 works，不把整页噪声直接登记。最终交接前补足有限预理解，引文邻域仍是供选择的线索。

已有 `OPENALEX_API_KEY` 时由运行环境提供，客户端在输出和错误中隐去；不要把密钥写进研究材料。

## Web 覆盖检查

发现陌生术语或稀疏结果时用 Web Search 补查。冻结精读集合前，对当前台账和已见候选做一次有界检查：先重新考虑相关但未登记的候选，再查询重要缺口，避免重复宽查询。维度可包括任务设置、方法族、同义术语、综述／分类／教程／基准／数据集、参考文献中的经典方法、精确方法名、近期预印本和反证或比较研究。

关键缺口已有代表性占位，且新增表达主要返回重复或无关内容时可停止；候选数量本身不证明覆盖。主动检索预算约束工作，不另设查询次数门槛。

## 接纳一条 Web 线索

1. 核实精确学术标题及至少一项身份来源，例如 DOI、arXiv、OpenAlex、PubMed、会议录、仓储或出版商论文页。
2. 可行时用 OpenAlex 规范元数据；没有记录仍可保留已核实 DOI、arXiv 或标题身份的论文。
3. 优先按 DOI，再按 arXiv、OpenAlex 或明确的标题／作者证据去重。
4. 仅填写标题、摘要、引文上下文或元数据实际支持的预理解。
5. 入选正文交给 Acquisition 和 Reader，不在主上下文展开全文科学阅读。

博客、新闻、实验室列表和搜索片段可指向论文，不能代替学术记录；不作为论文登记，也不把片段伪装成 `metadata.abstract`。有身份价值的 URL 放入 `metadata.source_urls`。
