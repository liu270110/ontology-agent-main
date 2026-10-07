# benchmarks/suites/rag — 闭源对比口径（README）

> 本文登记 rag suite 的「闭源 RAG 对比」口径。设计权威：`docs/Agent/16-基准对比体系设计.md`
> §5——「闭源 RAG API 对比（外呼成本+不可复现，登记后续；**对比闭源=用公开榜单数据引用
> 而非直连**）」。本文件即该口径的落地说明（2026-10-07 建，此前无 README）。
>
> 引用表本体：[`leaderboard_citations/public_leaderboards.json`](leaderboard_citations/public_leaderboards.json)
> （纯文献引用，零外呼零 API 调用；每条带 `source_url` + `retrieved_at` + `caveat`，
> 核实不到的确切数字一律 `value=null` + `status=not_available`，不凭记忆填数）。

## 闭源对比口径

### 三档可比性分级

引用表每条目带 `tier` 字段，分级如下：

| 档 | 定义 | 判据 | 当前条目 |
| ---- | ---- | ---- | ---- |
| **A** | 同基准同指标，可直接比 | BEIR nDCG@10 族（论文原测 BM25 / BM25+CE） | BEIR BM25 基线、BM25+CE 重排档（现役 EvalAI 榜单头部未取到一手数字，槽位留 not_available） |
| **B** | 同指标族、不同数据集 | MTEB Retrieval / MIRACL 等标准检索基准上的闭源 embedding 服务 | OpenAI text-embedding-3-large/small 的 MIRACL 均值（Cohere v3/v4、OpenAI MTEB 检索子任务：not_available） |
| **C** | 纯参考不可比（产品级或指标口径不同） | 产品级端到端 QA 评测、非检索单指标、厂商自评 | Perplexity FreshQA（学术评测引用）、OpenAI MTEB 全任务均值、Voyage 厂商自评相对优势、Gemini grounding（not_available） |

### 红线（禁止事项）

**本仓 `benchmarks/results/rag/` 的 v0 电力语料实测数字（recall@5 / MRR / faithfulness 等）
与上述榜单数字禁止直接并列比较。**原因（缺任一即不可并列）：

1. **数据集不同**：我们=中文电力停电域自有语料 50 文档 20 查询；榜单=英文开放域标准集
   （BEIR 18 集百万级文档）；
2. **指标不同**：我们主指标 recall@5（文档级命中）；BEIR 榜=nDCG@10（含分级相关性与
   折扣）；两者数值不可换算；
3. **语言不同**：中文分词（字符二元组）与英文 BM25 分词行为不可迁移。

合法用法只有一种：**量级合理性参考**——如「我们 naive_bm25 在自有语料 recall@5=1.0
（金标覆盖率高的产物），BEIR 论文 BM25 十八集零样本平均 nDCG@10≈0.42——只说明双方
都在『词法基线的正常量级』，不构成任何名次结论」。A 档亦受此红线约束（同基准同指标，
但仍非同数据集）；B/C 档只进叙述、不进任何表格并列。

### not_available 规则

- 检索不到可核实确切数字 → `value=null`、`status=not_available`，`source_url` 指向本次
  核查过的页面，`caveat` 写明未取到的原因（动态 JS 页/官方未公布/仅二手转述）；
- 网络二手转述（聚合站/评测博客转引）一律不采信、不填数；
- 各条 `caveat` 必须写清「口径不可直接比」的具体原因（数据集不同/语言不同/指标定义差异）。

### 与 metrics 流的关系（orsi_link 兼容）

引用表是**非实测数据**，不进 metrics 流：不落 `results/`、不进 SUMMARY 曲线表、
`benchmarks/orsi_link.py` 不读它（`leaderboard_citations/` 在 suites 侧而非 results 侧；
且文件带 `nature=citation_only` 标记，orsi_link 的 load_results 对该标记显式跳过，
测试锁定此行为——见 `tests/benchmarks/test_rag_leaderboard_citations.py`）。
引用数字只作 README（本文件）与 `benchmarks/results/rag/SUMMARY.md`「公开榜单参照
（引用）」节的叙述附件。
