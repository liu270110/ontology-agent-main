# 电力停电分析样例数据集（抽取流水线冒烟样例）

> 版本 0.3 | 日期 2026-09-28 | 上游：[阶段验收审核 §4 批次 B-③](../../docs/architecture/评审-2026-09-28-M0-M3阶段验收审核.md)、[OntRAG §2/§10](../../docs/OntRAG/知识库GraphRAG设计.md)、种子本体 [../power_seed.ttl](../power_seed.ttl)
>
> **角色定位（2026-09-28 双源裁决，用户按设计目的裁定）**：本目录 = **抽取流水线冒烟样例 + 种子类锚点对账资产**（manifest 的 seed_class_anchors 为独有价值）。**检索评估基准（OntRAG §10 分层回归集）= [`seeds/golden/`](../golden/)**（30 条检索金标 + 582 金标三元组 + 18 篇语料，已接线 `retrieval_eval` / `services/devtools/kb-eval`）——两套分工互不替代；`qa/golden-qa.jsonl` 保留为样例配套问答，**不作为检索调参回归基准使用**。

## 1. 内容清单

| 路径 | 内容 | 抽取类型（OntRAG §2.3） |
| ---- | ---- | ---- |
| [docs/outage-ticket-OO-260928.md](./docs/outage-ticket-OO-260928.md) | 停电工单（完整处置时序） | 时序流程型 |
| [docs/feeder-ledger-F102.md](./docs/feeder-ledger-F102.md) | F102 馈线台账（设备/客户实例） | 数据型 |
| [docs/outage-handling-manual-extract.md](./docs/outage-handling-manual-extract.md) | 停电处置规程节选（术语/规则/约束） | 文本知识型 |
| [manifest.json](./manifest.json) | 文档清单 + 种子类锚点 + sha256 | 机器可读 |
| [qa/golden-qa.jsonl](./qa/golden-qa.jsonl) | 样例配套 QA 30 条（local/global/drift 各 10；非回归基准） | 演示问答 |

三种抽取类型各覆盖一份，对齐研究 03 篇 §3.1 分类型抽取策略——一份样例集即可端到端验证四类提示词模板中的三类（多模态型随后续批次补）。

## 2. 与种子本体的对齐

- 工单编号遵守种子规则 `pw:R004` 的 SHACL 格式（`^OO-[0-9]{6}$`，样例 `OO-260928`）；
- 工单时序覆盖种子规则链素材：停电确认（`OutageConfirmed`）→ 派发抢修（`DispatchRepair`，对应 R003）→ 故障隔离（`IsolateFault`，对应 R005 风暴条款在手册条款 3）→ 复电（`PowerRestored`）→ 抢修完成（`RepairCompleted`）；
- 每份文档的 `manifest.docs[].seed_class_anchors` 声明其内容可抽取到的种子类 IRI，抽取结果以此对账（候选实例的类必须落在锚点集内）——**本目录的核心用途**。

## 3. 消费方式

1. **抽取验收（主用途）**：三份文档经 kb 上传 → 七步流水线 → 候选实例 → 终审，候选类的锚点对账用 manifest；
2. **检索评估 → 走 [`seeds/golden/`](../golden/)**（裁决后唯一基准；工具 `services/devtools/kb-eval/`、加载器 `retrieval_eval.py`）；
3. **测试素材**：`tests/kb` 的内联样例文本可逐步切换为本目录制品（消除测试数据与验收数据双源）。

## 4. 边界与声明

- **全部内容为合成样例**，不含任何真实单位/客户/人员数据；
- 检索回归基准在 seeds/golden 维护（2026-09-28 裁决）；本目录 qa/ 不再做"标定冻结"承诺；
- 多模态型（接线图/现场照片）样例随 M5 批次补入本目录。
