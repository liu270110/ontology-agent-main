# benchmarks/ — 基准对比体系（docs/Agent/16 §1/§2 落地）

> 设计权威：`docs/Agent/16-基准对比体系设计.md`（v1.0，用户裁决 2026-10-06）；问题输入源：
> `docs/评审/红队攻击性审查-2026-10-06.md` F 域（RAG 可观测性与指标反馈族）。
> 本波落地：**rag suite v0**（骨架 + naive 基线 + 六维指标 + 全链实测）。agent-core /
> intent / ontology-scale 随各自波次落地。

## 运行指南

统一入口（仓库根执行）：

```bash
# 全链冒烟（rag v0）：语料装载 → 进私库 50 文档（M2-lite 直驱索引）→ 20 查询双 harness
# → 六维对比 → results/rag/<date>/run-*.json + results/rag/SUMMARY.md（追加式曲线）
python benchmarks/run.py --suite rag --smoke

# 指定嵌入端点（ASGI 进程内实例时注入平台环境；本机 TEI 形态）
python benchmarks/run.py --suite rag --smoke --embed-base-url http://127.0.0.1:18002 --embed-protocol tei

# 优化后换 tag 重跑（SUMMARY.md 留曲线，不覆盖历史）
python benchmarks/run.py --suite rag --tag v0.2.0

# 只跑 ours / 复跑检索侧（索引幂等，跳过进库）
python benchmarks/run.py --suite rag --ours-only
python benchmarks/run.py --suite rag --skip-ingest --kb-id <uuid>
```

可变参数全部走 Settings（`benchmarks/suites/rag/config.py`，env 前缀 `BENCH_RAG_`）；
平台参数（嵌入端点等）仍归 `services.platform.config`（bench 仅在 ASGI 分支按 CLI 显式
注入 OA_ 环境变量，None=不动平台配置）。

### harness 面向

| harness | 说明 |
| ------- | ---- |
| `ours_kb` | 我们 kb 现检索（A0）。检索经 kb API（`POST /kb/search`）；harness=auto 时 8364 在跑走 http，否则进程内 `create_app()` + `httpx.ASGITransport` 直连（不起端口）。索引进库走 M2-lite 步序直驱（`business/kb_pipeline.run_pipeline`：preprocess→chunk→embed→bm25_index，零 LLM/零外呼——M2-full 含抽取的索引对比随 A1 波次） |
| `naive_bm25` | 全文 Okapi BM25 基线（字符二元组分词，零依赖，`competitors/naive_rag.py`） |
| `naive_vector` | 简单稠密向量基线（与平台同源 bge-m3 端点 + 余弦；刻意同嵌入模型，使 ours vs naive_vector 差异只剩检索策略本身） |

令牌：平台 security 模块本地签发（tools/drawing-probe 同款，随机 bench 租户，令牌不落
盘不打印）；数据隔离：每轮 run 独立随机租户 + 独立 collection（共享 PG 只追加，
不触碰既有数据）。

## 指标口径字典（冻结实现 = `suites/rag/metrics.py`）

| 指标 | 定义 | 采集点 |
| ---- | ---- | ---- |
| recall@k | 宏平均 \|top_k 文档集 ∩ 金标相关集\| / \|金标相关集\|；k=`BENCH_RAG_RECALL_K`（默认 5） | 金标 queries.jsonl 确定性计算 |
| MRR | 宏平均 1/rank（首个相关文档名次，1 起；无命中 0） | 同上 |
| faithfulness | 规则版：答案句按。！？；;\n切分，逐句算字符 bigram 支撑率 max_over_citations(\|∩\|/\|句 bigram\|)≥`min_support`(0.5) 记支撑句，faithfulness=支撑句数/总句数；无答案记 None（no_answer 单列）。**LLM judge 接口已留**（metrics.py 模块头注释：本地 vLLM@18001 RAGAS 式判定，第二波并列 `faithfulness_llm` 双列） | ours=kb 抽取式 answers[0].summary；naive=top-1 文档前 3 句拼装 |
| latency_p50/p95 | 客户端墙钟毫秒，nearest-rank 分位（含 HTTP/进程内开销，双 harness 同口径可比） | 每查询计时 |
| cost_per_query | token 计数 = tokenize(query) + tokenize(top-k 引用拼接)。后端：`vllm`（本地 vLLM /tokenize 真分词器，默认）/ `heuristic`（CJK 字符×1 + ASCII 词×1 确定性估算） | 每查询 |
| ontology_gain | A1（本体约束档）− A0 于 recall/MRR/faithfulness 三项差值。**本波 A1 未实现 → 恒 null 并注明**（结果 JSON `ontology_gain` 块） | A1 接入后同语料回填 |

## 语料（corpora/）

- `corpora/v0/`：50 文档 × 20 查询金标集 v0（文档-查询-相关片段三元组 JSONL）——
  电力停电分析 wedge 场景，四文档型：停电工单 ×20 / 设备档案 ×15 / 检修记录 ×10 /
  调控规程 ×5；查询覆盖 11 单相关 / 6 双相关 / 3 三相关。
- 生成器 `corpora/build_v0.py`（确定性模板零随机，重跑逐字节稳定）；引文由渲染文档的
  同一模板函数拼出，装载时断言 quote ∈ content（金标漂移即失败）。
- 语料指纹 sha256 随每轮结果 JSON 落盘（ORSI capability_fingerprint 场景集哈希同源，
  docs/Agent/16 §3）。

## 结果（results/）

- `results/rag/<date>/run-<HHMMSS>-<tag>.json`：全量指标 + 环境指纹（commit/python/
  嵌入端点/语料哈希）+ 配置快照 + 逐查询明细；机器写，人审。
- `results/rag/SUMMARY.md`：追加式曲线表（每次优化后重跑，不覆盖历史）。

## 已知边界（本波如实声明）

1. A1 本体档未实现 → ontology_gain 列留空（null+注明），非 0；
2. 索引面走 M2-lite（零 LLM 依赖）；M2-full（extract/align/validate）与图谱增强
   （KbFact 图路扩展）的质量贡献随 A1 波次补测；
3. faithfulness 为规则版（n-gram 支撑率），LLM judge 第二波并列双列；
4. `ours` 经 http harness 测量外部后端时，嵌入端点以该后端自身配置为准（环境指纹
   记录的是 bench 进程侧配置）。
