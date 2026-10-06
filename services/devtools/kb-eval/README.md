# services/devtools/kb-eval — M2.5 知识库评估工具包

PoC①②③ 的执行与冻结工具（锚点：docs/architecture/01 §7 PoC 出口条件）。全部脚本在**仓库根目录**执行，Python ≥3.11（本仓 3.13），依赖 rdflib/owlrl/pyshacl/httpx/psutil（psutil 可缺省）。

| 脚本 | 作用 | 前置 | 输出 |
| --- | --- | --- | --- |
| `poc1_inference_benchmark.py` | OWL 推理基准：电力模板合成图 1k/5k/10k/100k 三元组，分阶段计时（rdflib 解析 / owlrl 物化 / pySHACL 校验）+ 内存峰值；每档硬超时（10k≤10min、100k≤30min，超时即终止并幂律外推） | 无外部服务 | stdout markdown 矩阵；`poc1_results.json`（增量落盘） |
| `poc2_calibration.py` | 抽取置信度校准：金标三元组逐文档比对 → 0.1 宽分桶 precision + ECE | ① `services.semantic.knowledge.extract`（已合入）；② 对话模型端点（`OA_LLM_BASE_URL`/`OA_LLM_MODEL` 或 config `llm_base_url/llm_model`）；③ 金标集 `services/seeds/golden/power_triples_golden.jsonl`（582 条）。端点未配置时打印提示并以退出码 2 结束 | stdout 校准表；`poc2_calibration.json` |
| `poc3_index_cost.py` | 索引成本标定：chunk_document 分块统计 + bge-m3 嵌入吞吐（批量 32）+ qwen 实测 3 次抽取外推完整 GraphRAG 成本，输出 Lazy vs Full 对比表 | Ollama（缺省 `http://localhost:11434`，可 `OA_OLLAMA_BASE_URL` 覆盖）；Ollama 不可达自动降级为「运行时未达，估算」不阻塞；`--skip-llm` 跳过抽取实测 | stdout 对比表；`poc3_results.json` |

## 用法（仓库根目录）

```bash
# PoC1：四档全跑（100k 受 30 分钟硬顶保护；本批实测约 5 分钟）
python services/devtools/kb-eval/poc1_inference_benchmark.py
python services/devtools/kb-eval/poc1_inference_benchmark.py --quick   # 仅 1k 档
python services/devtools/kb-eval/poc1_inference_benchmark.py --smoke   # 2k 三元组冒烟自检

# PoC2：抽取链落地 + 端点配置后（本次只交付未执行）
python services/devtools/kb-eval/poc2_calibration.py --limit 2         # 冒烟
python services/devtools/kb-eval/poc2_calibration.py --docs d04 d06    # 指定文档前缀

# PoC3：分块 + 嵌入 + LLM 抽取实测 + 外推（本批实测约 3 分钟）
python services/devtools/kb-eval/poc3_index_cost.py
python services/devtools/kb-eval/poc3_index_cost.py --skip-llm         # 跳过 LLM 实测
```

## 数据约定

- 语料：`services/seeds/samples/power/`（18 篇电力停电分析语料，事实边界 = `services/seeds/power_seed.ttl`；`MANIFEST.md` 为目录元数据，poc3 计入语料时自动排除）；
- 金标：`services/seeds/golden/power_triples_golden.jsonl`（582 条抽取金标，evidence_span 逐字来自语料）与 `services/seeds/golden/power_retrieval_golden.jsonl`（30 例检索评估集：local 12 / global 10 / drift 8）；
- token 口径：`len(text)//2` 中文近似（与 `services.semantic.knowledge.chunking.estimate_tokens` 同源），报告中凡估算/外推均已显式标注，与实测分列。

## 结果冻结位置（docs/ 不入 git，本地维护）

- `docs/OntRAG/poc/PoC1-推理基准.md`：推理 SLA 冻结建议 + Fuseki 触发阈值建议
- `docs/OntRAG/poc/PoC2-置信度校准.md`：骨架（方法/金标集已写），校准表待回填
- `docs/OntRAG/poc/PoC3-索引成本.md`：检索默认档裁决建议（默认 LazyGraphRAG）

## U-③a Utopia 基准起步批（2026-10-05，方案依据 docs/OntRAG/Utopia借鉴优化方案.md U-③a）

本批新增三件套（全部零生产代码改动；用户铁律：项目内不使用 mock 数据——真模型一律打本机
vLLM 栈 `127.0.0.1:18001/v1`，无服务自动 skip，ollama marker 同款纪律）：

| 产物 | 作用 |
| --- | --- |
| `scoring.py` | 判分库（平级纯模块，不建包；纯 stdlib）：`roughly()` 全局唯一判等（数值放宽两个量级，约 8.63 亿==862793473.48）、`KnownGapLedger` known_gap 机制（缺口吸收剔除分母、单列计数）、`aggregate` absent 单列口径、`calibration` 0.1 宽分桶 precision+ECE（poc2 同源）、`count_references` resolved/dangling 引用计数、`save_results` *_results.json 落盘。单测 `tests/tools/test_kb_eval_scoring.py`（importlib 按路径加载先例） |
| `u3a_gate.py` | 本批门禁唯一入口（纯 stdlib subprocess，cwd 自定位仓库根，转发 pytest 退出码）：无参=全量 `python -m pytest -q`；`--u3a`=仅 `-m vllm` 真模型场景 |
| `BENCH_LEDGER.md` | 基准轮次台账（日期/commit/模型/两组数字/known_gap 变化），每轮一行追加 |

真模型 e2e 场景：`tests/kb/test_utopia_bench_e2e.py`（`@pytest.mark.integration` + `@pytest.mark.vllm`，
marker 已登记 pyproject）。既有真实语料 3 篇（d00/d03/d06）→ business 入口（`run_pipeline`
五步直调）打真模型 → 结构性断言（管线完成/事实产出≥N/审核队列置信度口径/合并可撤销）+
判分两组数字（结构面 + 置信度×门禁 proxy 校准面）；零逐字内容断言（真模型非确定性）、零虚构语料。
`OA_U3A_RESULTS=<path>` 时落盘判分 JSON（缺省不写盘）。

```bash
# 本批门禁（在仓库根目录）：
python services/devtools/kb-eval/u3a_gate.py --u3a   # 真模型场景（本机 vLLM 不可达自动 skip）
python services/devtools/kb-eval/u3a_gate.py         # 全量套件（全量只由门禁跑，实现者不自行跑全量）

# 判分结果落盘（可选；跑 --u3a 场景时）：
OA_U3A_RESULTS=services/devtools/kb-eval/u3a_results.json python services/devtools/kb-eval/u3a_gate.py --u3a
```

运行注意：本机 vLLM 4B-AWQ 实测单次抽取 ~117s（e2e 内经测试侧耐心装饰器注入 per-call 预算
900s，零生产改动）。已知模型能力缺口：4B 模型在叙事密集 chunk 上复读失控（~40K 字截断 JSON，
2026-10-05 实测 5/22 chunk 命中），命中文档按生产 3 次重试语义失败留痕、由用例显式登记
（known_gap），故全跑约 45-55 分钟属正常。共享本地 PG 与并行 worktree 批次互斥——基准批须
串行（02 §多 Agent 协作纪律）。
