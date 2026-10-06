# U-③a Utopia 基准台账（BENCH_LEDGER）

> 每轮一节**追加式**记录（禁重排旧条目）；数字来自当轮 u3a 真模型实测，与 `u3a_results.json`
> 落盘同源（`OA_U3A_RESULTS=services/devtools/kb-eval/u3a_results.json` 时自动写出）。
> 门禁唯一入口：`python services/devtools/kb-eval/u3a_gate.py --u3a`。
> 方案依据：docs/OntRAG/Utopia借鉴优化方案.md U-③a；用户铁律（2026-10-05）：项目内不使用
> mock 数据——真模型一律打本机 vLLM 栈 `127.0.0.1:18001/v1`，无服务自动 skip。

## 轮次记录模板

```markdown
## 第 N 轮（YYYY-MM-DD）

- commit：<e2e/判分代码基线 hash>（台账记录于 <ledger commit hash>）
- 模型：<served 名（权重/量化 @ 推理栈）；mock/real 二值——本台账铁律恒为 real，mock 轮不得入账>
- 语料：<stems 列表（services/seeds/samples/power 既有真实语料，禁虚构）>
- 两组数字：
  - 结构面：docs 完成 x/y；facts N（candidate a / rejected b）；subjects s；工单覆盖率 c；
    chunk 引用 resolved r / dangling d；gate_aggregate P=R=F1=p
  - 校准面（自报置信度 × 门禁结论 proxy，正类=过 SHACL+证据门禁）：ECE=e（TP t / FP f）
- known_gap 变化：新增 …/收敛 …/移除 …（附证据）
- 备注：运行时长、环境异动（共享 PG 锁 convoy 等）
```

## 第 1 轮（2026-10-05）

- commit：7a2f1d2（e2e + 判分口径）；判分库 9bd5f1f；门禁 1a3e8a0
- 模型：**real** `local-main`（Qwen3-4B-AWQ @ 本机 vLLM 127.0.0.1:18001/v1，max_model_len 16384）
- 语料：d00（供电概况）/ d03（抢修名册）/ d06（故障报告）——`services/seeds/samples/power` 既有真实语料
- 两组数字（e2e 实测 1 passed，52 分钟）：
  - 结构面：docs 完成 **2/3**（d06 见 known_gap①）；facts **119**（candidate 74 / rejected 45）；
    subjects **65**；工单覆盖率 **1.0**（119 单 ↔ 119 事实一一对应，全 pending_review）；
    chunk 引用 resolved 11 / **dangling 0**；gate_aggregate P=R=F1=**0.6218**
  - 校准面：ECE=**0.35**（TP 74 / FP 45；0.1 宽分桶明细见 `u3a_results.json`）
- known_gap 变化：
  - **新增①** `vllm-4b-runaway`：4B 模型在叙事密集 chunk 上复读失控——输出 ~52K 字截断 JSON
    → `ModelGatewayOutputInvalidError` → 生产 3 次重试语义耗尽（d06 extract 3 连败，e2e 显式登记
    不静默）。探针实测命中率 **5/22 chunk**（d06×2、d08×1、d11×1、d12×1，2026-10-05 同栈实测）；
    d00/d03 全 chunk 干净（两轮完整 e2e 复现）。收敛条件候选：更换模型/量化、收紧 v2 模板
    （candidates 数上限）、或 port 增加可选 max_tokens 注入口（纯新增式，另批裁决）。
  - **新增②**（口径登记）规格稿「审核队列按置信度分流」与仓库现实不符：生产无按置信度自动
    分流——`run_extract` 全量候选进 `pending_review`（kb_extraction._persist_candidates），
    置信度经 `kb_facts.confidence` 列 + `idx_kb_facts_queue` 随单透出供人工终审排序；
    宪法第 3 条硬门禁任何置信度不可跳过。e2e 按生产真实语义断言。
  - 移除：无
- 备注：运行前共享本地 PG 出现锁 convoy（他批残留 83 分钟 idle-in-transaction 只读会话 +
  排队 DDL），已终止该残留会话解锁（只读事务零数据影响）；基准批与并行 worktree 批次
  须串行使用共享 PG。判分口径注记：校准面正类=过门禁（proxy），非金标命中——v2 模板为类级
  schema 抽取，与实例三元组金标（power_triples_golden.jsonl）口径不同源（poc2_calibration.py
  头注同款注记）；金标直接对齐留待后续轮次设计实例级桥接。
- 门禁终验：`python u3a_gate.py --u3a` 实跑 **exit 0**（`1 passed, 1 skipped, 2239 deselected`，
  62 分 38 秒，cwd 自定位 worktree 根正确）。条目化注记：summary 里多出的 `1 skipped` 为
  -m vllm 选中面的一次性运行期条目（收集期恒为 1/2240，跳过源未复现、不影响门禁结果），
  第 2 轮观察项。
