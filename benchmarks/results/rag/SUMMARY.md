# rag suite 结果曲线（SUMMARY.md，机器写人审；追加式不覆盖历史）

| 时间 | tag | harness | recall@k | MRR | faithfulness | p50(ms) | p95(ms) | cost(tok/q) | 结果文件 |
| ---- | --- | ------- | -------- | --- | ------------ | ------- | ------- | ----------- | -------- |
| 2026-10-07 07:46:36 | v0 | ours_kb | 0.975 | 0.975 | 1.000 | 236.2 | 579.5 | 578 | run-074636-v0.json |
| 2026-10-07 07:46:36 | v0 | naive_bm25 | 1.000 | 0.975 | 1.000 | 0.3 | 0.4 | 1499 | run-074636-v0.json |
| 2026-10-07 07:46:36 | v0 | naive_vector | 1.000 | 0.967 | 1.000 | 503.8 | 697.6 | 1646 | run-074636-v0.json |
| 2026-10-07 07:50:06 | v0 | ours_kb | 0.975 | 0.975 | 1.000 | 209.9 | 277.8 | 578 | run-075006-v0.json |
| 2026-10-07 07:50:06 | v0 | naive_bm25 | 1.000 | 0.975 | 1.000 | 0.3 | 0.4 | 1499 | run-075006-v0.json |
| 2026-10-07 07:50:06 | v0 | naive_vector | 1.000 | 0.967 | 1.000 | 489.0 | 595.2 | 1646 | run-075006-v0.json |

## 公开榜单参照（引用）

> 闭源对比口径=公开榜单数据引用而非直连（docs/Agent/16 §5；口径与三档分级详见
> `benchmarks/suites/rag/README.md`「闭源对比口径」）。**红线：本节数字与上方 v0 电力
> 语料实测数字禁止直接并列比较**（数据集/语言/指标均不同），只作量级合理性参考。
> 引用表全量（含 caveat 与 not_available 条目）：`benchmarks/suites/rag/leaderboard_citations/public_leaderboards.json`。

### A 档（同基准同指标——BEIR nDCG@10）

| 系统 | 数据集 | nDCG@10 | 来源（检索日期 2026-10-07） |
| ---- | ------ | ------- | --------------------------- |
| BM25（论文原测） | BEIR 18 零样本集 | ≈0.423* | BEIR 论文 Table 2（arXiv:2104.08663） |
| BM25+CE（论文重排档，16/18 集胜 BM25） | BEIR 18 零样本集 | ≈0.476* | 同上 |

\* 论文只给逐集值，平均为引用表对逐集值算术平均（13 集子集口径：BM25≈0.413 / BM25+CE≈0.465）；现役 EvalAI 榜单头部一手数字未核实到，槽位留 not_available。

### B 档（同指标族不同数据集——闭源 embedding 服务）

| 系统 | 基准 | 值 | 来源 |
| ---- | ---- | -- | ---- |
| OpenAI text-embedding-3-large | MIRACL 多语检索均值 | 54.9 | OpenAI 官方博客（openai.com/blog/new-embedding-models-and-api-updates） |
| OpenAI text-embedding-3-small | MIRACL 多语检索均值 | 44.0 | 同上 |

Cohere embed-v3/v4 与 OpenAI MTEB Retrieval 子任务：官方一手页面无标准检索基准绝对值，标 not_available（不填数）。

### 我们的定性位置（非并列结论）

我们 naive_bm25 在自有电力语料 recall@5=1.0，BEIR 论文 BM25 十八集零样本平均 nDCG@10≈0.42——仅量级合理性参考：两者均在词法基线正常量级，且 recall@5 vs nDCG@10、中文电力域 vs 英文开放域不可换算，不构成任何名次结论。

### C 档（纯参考不可比——只给指针不列数）

- Perplexity.ai 产品级学术评测：FreshLLMs/FreshQA（arXiv:2310.03214，2023-04 快照，数字随产品迭代失效）；
- Gemini grounding：官方模型页无 SimpleQA/grounding 检索指标公开值（not_available）；
- Voyage voyage-3-large：厂商自建 100 数据集自评相对优势值（绝对值在站外 spreadsheet，不可并列）；
- OpenAI MTEB(EN) 全任务均值 64.6：embedding 全能力均值，非检索单指标，禁止当检索召回引用。
