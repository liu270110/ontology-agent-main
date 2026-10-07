# benchmarks/ — 基准对比体系（docs/Agent/16 §1/§2 落地）

> 设计权威：`docs/Agent/16-基准对比体系设计.md`（v1.0，用户裁决 2026-10-06）；问题输入源：
> `docs/评审/红队攻击性审查-2026-10-06.md` F 域（RAG 可观测性与指标反馈族）。
> 本波落地：**rag suite v0**（骨架 + naive 基线 + 六维指标 + 全链实测）。agent-core /
> intent / ontology-scale 随各自波次落地。
>
> **2026-10-07 增补：intent suite v0 落地**（双档对照验证本体约束核心卖点，红队 E2）——
> 运行指南与口径见下方「intent suite」节。
> 本波落地：**rag suite v0**（骨架 + naive 基线 + 六维指标 + 全链实测）、**agent-core**
> （红队 A/B/C/H 六场景）、**ontology-scale**（G1/F3 重型本体规模梯度三档×三指标）。
> intent 随各自波次落地。

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

# ontology-scale（G1/F3）：确定性合成三档 10²/10³/10⁴ × 三指标，零网络依赖
# （vLLM@18001 仅 token 计数；10⁴ 档全链 ~7min，超时预算 tier_timeout_s 内 partial 如实落盘）
python benchmarks/run.py --suite ontology-scale --smoke
python benchmarks/run.py --suite ontology-scale --tiers 100,1000 --tier-timeout 300 --onto-token-counter heuristic
```

可变参数全部走 Settings（rag=`suites/rag/config.py` 前缀 `BENCH_RAG_`；ontology-scale=
`suites/ontology-scale/config.py` 前缀 `BENCH_ONTO_SCALE_`）；平台参数（嵌入端点等）仍归
`services.platform.config`（bench 仅在 ASGI 分支按 CLI 显式注入 OA_ 环境变量，None=不动
平台配置）。

### harness 面向

| harness | 说明 |
| ------- | ---- |
| `ours_kb` | 我们 kb 现检索（A0）。检索经 kb API（`POST /kb/search`）；harness=auto 时 8364 在跑走 http，否则进程内 `create_app()` + `httpx.ASGITransport` 直连（不起端口）。索引进库走 M2-lite 步序直驱（`business/kb_pipeline.run_pipeline`：preprocess→chunk→embed→bm25_index，零 LLM/零外呼——M2-full 含抽取的索引对比随 A1 波次） |
| `naive_bm25` | 全文 Okapi BM25 基线（字符二元组分词，零依赖，`competitors/naive_rag.py`） |
| `naive_vector` | 简单稠密向量基线（与平台同源 bge-m3 端点 + 余弦；刻意同嵌入模型，使 ours vs naive_vector 差异只剩检索策略本身） |

令牌：平台 security 模块本地签发（tools/drawing-probe 同款，随机 bench 租户，令牌不落
盘不打印）；数据隔离：每轮 run 独立随机租户 + 独立 collection（共享 PG 只追加，
不触碰既有数据）。

## ontology-scale 指标口径字典（冻结实现 = `suites/ontology-scale/metrics.py`）

问题源=红队审查 G1（重型本体性能）/F3（TBox 改版漂移检出）；生成器 `generator.py`
（电力停电域词汇合成，确定性种子，同种子同输出；三族=设备类型/工单类型/故障模式族，
每类 2 个 PropertyShape，实例 10N，违例实例占比 `violation_ratio` 按族均衡分摊）。

| 指标 | 定义 | 采集点 |
| ---- | ---- | ---- |
| validate_latency | pySHACL 校验时延（平台 `services/ontology/core/shacl.validate` 原路径：advanced=True/inference=none）。干净 ABox（应 conforms）与带违例 ABox（应非 conforms）两形态各 `latency_repeats` 次：p50/p95（nearest-rank）+ 膨胀比（违例/干净均值）。`violation_hit_exact`=违例命中数与生成器解析期望恰等（shapes 有效性的运行时证据） | 每档每形态逐次计时（ValidationReport.elapsed_ms） |
| assemble_token_cost | TBox 注入上下文 token 成本（grounding 组装口径，`ContextBlock.tokens` 求和同语义）。两模式：summary=类名+属性清单摘要（tier=1 稳定知识块合成形态）/ full=全量 schema（类公理+SHACL shapes Turtle）。`compression_ratio`=full/summary（>1=摘要面净收益）。后端 `vllm`（本地 /tokenize 真分词器，按批合并计数）/ `heuristic`（CJK×1+ASCII 词×1） | 每档两模式各计一次（单元=每类一条） |
| reindex_consistency | F3 漂移检出率：改 TBox 属性定义（三契约型变异：range_tighten/enum_narrow/required_add，保证可检出）→ 旧实例（干净 ABox）重校验，`detection_rate`=检出数/契约型布靶数。TBox-only 对照（仅改 rdfs:range 不动 shapes）在 inference=none 下 pySHACL 不读 rdfs:range，属已知盲区——`tbox_only_visible` 单列不计分母 | 每档 4 处变异（`mutation_targets` 确定性选靶） |

基准断言（每档五条，失败即 assert_failed 如实落盘）：violation_hit_exact / clean_conforms /
violations_nonconforms / detection_rate==1.0 / compression_ratio>1。

## rag 指标口径字典（冻结实现 = `suites/rag/metrics.py`）

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

## intent suite（2026-10-07 增补：双档对照，红队 E2）

「ontology 对意图的约束效果」从未被数据验证（红队审查 E2）——本套件以同一金标集跑
**A0/A1 双档对照**产出差值列 `ontology_constraint_gain`（负值同样如实落档）。

```bash
# 双档×100 金标全链（本地 vLLM@18001，qwen3-4b-awq；不用外网）
python benchmarks/run.py --suite intent --smoke
# 换 tag 重跑留曲线 / 调试截前 N 条
python benchmarks/run.py --suite intent --tag v0.1.0
python benchmarks/run.py --suite intent --smoke --limit 5
```

### 金标集（datasets/golden_v0.jsonl）

100 条意图×行动类映射（平台 17 静态行动类词表，mcp 动态族不入候选集）：
**明确直射 40 / 歧义近义 30 / 需澄清 20 / 越界 10**。字段：`id/query/expected_action/
ambiguity_level/notes/expectation`（expectation=map/clarify/reject 与层级一一对应）。
生成器 `datasets/build_golden_v0.py`：**人工规则构造，零 LLM、零随机**（金标本身即审计
对象，notes 逐条给依据）；重跑逐字节稳定（sha256 随结果 JSON 落档）。

### 双档口径（同金标集同输出契约，仅候选集表达不同）

| 档 | system 提示注入面 |
| ---- | ---- |
| A0 直觉档 | 行动类**名称清单**（无标注无约束条），模型凭直觉选 |
| A1 本体约束档 | 候选集封闭约束条 + **语义标注常驻段**（action_iri/execution_mode/capability/channel/描述/参数 schema），经平台 `render_tool_schema_section`（services/agent/business/adapters/builtin.py:55）同源渲染 |

**A1 近似口径声明（如实）**：平台 A1 档（OntRAG AgenticRAG 三档装配：A0 服务端代跑/A1
细粒度工具循环/A2 内核原生）尚未在意图面落地——本套件 A1 为「语义标注增强提示」近似，
约束注入面取平台真实语义标注（action_catalog.py 全量从平台常量导入，tests 交叉对账），
**非端到端本体推理管线**；E2 差值在此口径下成立，外推须带此口径。逐类标注来源
（平台原文 13 类 vs bench 侧补注 4 类：chat_answer/run_terminal/web_fetch/web_search）
随结果 JSON `a1_mode.annotation_provenance` 落档。

### 指标口径字典（冻结实现 = `suites/intent/metrics.py`）

| 指标 | 定义 | 分母 |
| ---- | ---- | ---- |
| intent_accuracy | expectation=map 条（clear 40+ambiguous 30）中 action 与金标一致比例；整体+分层两列 | map 条，parse_error 留在分母内 |
| clarification_trigger_rate | need_clarification 20 条中选 ask_user（平台澄清行动类）比例 | clarify 条 |
| out_of_scope_reject_rate | 越界 10 条判拒比例；判拒=action="out_of_scope" 或 confidence<`BENCH_INTENT_OUT_OF_SCOPE_CONFIDENCE_FLOOR`（0.5，映射最近+低置信亦算诚实出路） | reject 条 |
| ontology_constraint_gain | A1−A0 于上述三率的逐列差值（E2 核心产出，负值保留） | — |

解析口径：输出须为 JSON `{"action","confidence"}`；剥 `<think>` 后取首个平衡 JSON 对象；
解析失败记 parse_error（计入分母、各率按败计，不静默重试）。

### 结果（results/intent/）

- `results/intent/<date>/run-<HHMMSS>-<tag>.json`：双档四指标+逐条明细（含 raw 截断）+
  环境指纹（commit/模型/采样/金标 sha256）+ `a1_mode` 近似口径块；
- `results/intent/SUMMARY.md`：追加式双档曲线表。

### 已知边界（本波如实声明）

1. A1 为语义标注增强提示近似（见上），端到端本体约束（OntRAG 检索面 A1/内核 A2）随波次接入后同金标重跑；
2. 候选集=17 静态行动类；mcp_bridge 动态族（`action/mcp/{全名}`）无静态清单，不入候选集；
3. web 两类与 run_terminal/chat_answer 的执行级/描述按上文来源登记（web 绑定未声明执行级，bench 按「只读出网」记 READ）；
4. 受评面为本地 vLLM 裸模型直选（temperature=0），非平台 chat 主管线（E1：意图层现由 LLM 充当，JEV 未接线）——套件测的是「约束注入对意图判别的增益」，非平台端到端路由准确率。
