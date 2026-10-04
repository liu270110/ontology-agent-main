# services/tools/kb-eval — M2.5 知识库评估工具包

PoC①②③ 的执行与冻结工具（锚点：docs/architecture/01 §7 PoC 出口条件）。全部脚本在**仓库根目录**执行，Python ≥3.11（本仓 3.13），依赖 rdflib/owlrl/pyshacl/httpx/psutil（psutil 可缺省）。

| 脚本 | 作用 | 前置 | 输出 |
| --- | --- | --- | --- |
| `poc1_inference_benchmark.py` | OWL 推理基准：电力模板合成图 1k/5k/10k/100k 三元组，分阶段计时（rdflib 解析 / owlrl 物化 / pySHACL 校验）+ 内存峰值；每档硬超时（10k≤10min、100k≤30min，超时即终止并幂律外推） | 无外部服务 | stdout markdown 矩阵；`poc1_results.json`（增量落盘） |
| `poc2_calibration.py` | 抽取置信度校准：金标三元组逐文档比对 → 0.1 宽分桶 precision + ECE | ① `services.semantic.knowledge.extract`（已合入）；② 对话模型端点（`OA_LLM_BASE_URL`/`OA_LLM_MODEL` 或 config `llm_base_url/llm_model`）；③ 金标集 `services/seeds/golden/power_triples_golden.jsonl`（582 条）。端点未配置时打印提示并以退出码 2 结束 | stdout 校准表；`poc2_calibration.json` |
| `poc3_index_cost.py` | 索引成本标定：chunk_document 分块统计 + bge-m3 嵌入吞吐（批量 32）+ qwen 实测 3 次抽取外推完整 GraphRAG 成本，输出 Lazy vs Full 对比表 | Ollama（缺省 `http://localhost:11434`，可 `OA_OLLAMA_BASE_URL` 覆盖）；Ollama 不可达自动降级为「运行时未达，估算」不阻塞；`--skip-llm` 跳过抽取实测 | stdout 对比表；`poc3_results.json` |

## 用法（仓库根目录）

```bash
# PoC1：四档全跑（100k 受 30 分钟硬顶保护；本批实测约 5 分钟）
python services/tools/kb-eval/poc1_inference_benchmark.py
python services/tools/kb-eval/poc1_inference_benchmark.py --quick   # 仅 1k 档
python services/tools/kb-eval/poc1_inference_benchmark.py --smoke   # 2k 三元组冒烟自检

# PoC2：抽取链落地 + 端点配置后（本次只交付未执行）
python services/tools/kb-eval/poc2_calibration.py --limit 2         # 冒烟
python services/tools/kb-eval/poc2_calibration.py --docs d04 d06    # 指定文档前缀

# PoC3：分块 + 嵌入 + LLM 抽取实测 + 外推（本批实测约 3 分钟）
python services/tools/kb-eval/poc3_index_cost.py
python services/tools/kb-eval/poc3_index_cost.py --skip-llm         # 跳过 LLM 实测
```

## 数据约定

- 语料：`services/seeds/samples/power/`（18 篇电力停电分析语料，事实边界 = `services/seeds/power_seed.ttl`；`MANIFEST.md` 为目录元数据，poc3 计入语料时自动排除）；
- 金标：`services/seeds/golden/power_triples_golden.jsonl`（582 条抽取金标，evidence_span 逐字来自语料）与 `services/seeds/golden/power_retrieval_golden.jsonl`（30 例检索评估集：local 12 / global 10 / drift 8）；
- token 口径：`len(text)//2` 中文近似（与 `services.semantic.knowledge.chunking.estimate_tokens` 同源），报告中凡估算/外推均已显式标注，与实测分列。

## 结果冻结位置（docs/ 不入 git，本地维护）

- `docs/OntRAG/poc/PoC1-推理基准.md`：推理 SLA 冻结建议 + Fuseki 触发阈值建议
- `docs/OntRAG/poc/PoC2-置信度校准.md`：骨架（方法/金标集已写），校准表待回填
- `docs/OntRAG/poc/PoC3-索引成本.md`：检索默认档裁决建议（默认 LazyGraphRAG）
