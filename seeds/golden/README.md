# seeds/golden — 电力停电分析评估金标（检索回归基准，唯一权威）

> 建立 2026-09-28（M2.5 批次）；权威裁决 2026-09-28（kb 域专项审核 §3.1，用户按设计目的裁定）。
> 设计依据：[OntRAG §10 验收标准](../../docs/OntRAG/知识库GraphRAG设计.md)——分层回归集是"检索调参与任何模型/本体/参数变更强制回归的基准"。

## 内容

| 文件 | 内容 | 规模 |
| ---- | ---- | ---- |
| `power_retrieval_golden.jsonl` | 检索金标（local/global/drift 分层；case_id/mode/question/expected_doc_ids/expected_points） | 30 例 |
| `power_triples_golden.jsonl` | 抽取金标三元组（PoC② 置信度校准的标注底座） | 582 条 |
| 语料 | [`seeds/samples/power/`](../samples/power/)（18 篇，MANIFEST 索引） | 18 篇 |

## 工具链

- 加载器：`services/kb/business/retrieval_eval.py`（DEFAULT_GOLDEN_PATH 指向本目录检索金标）；
- 评估/PoC 脚本：[`tools/kb-eval/`](../../tools/kb-eval/)（PoC1 已冻结结果；PoC2 方法冻结、校准表待推理渠道达标后断点续跑回填；PoC3 索引成本）；
- 报告：[docs/OntRAG/poc/](../../docs/OntRAG/poc/) 三份。

## 分工边界（双源裁决）

- **本目录 = 检索评估/抽取校准的唯一回归基准**（变更强制回归挂此）；
- [`seeds/sample_dataset/`](../sample_dataset/) = 抽取流水线冒烟样例 + 种子类锚点对账（manifest anchors 为其独有价值），其 qa/ 不作回归基准。
