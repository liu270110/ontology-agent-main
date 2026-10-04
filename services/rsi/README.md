# rsi（平台自进化，模块 14）

M5+ 启动（权威=architecture/09）。

## 阶段 A 骨架（2026-09-27 M5-2 交付，权威=architecture/09 §9 阶段 A 收缩）

- 双轨触发注册面（`triggers.py`：经验轨=事件 / 指标轨=周期 + kill switch 红线 6）；
- 五类白名单枚举与机械校验（`whitelist.py`：类型外/目标禁区拒绝 + 安全审计，09 §3 铁律）；
- 三级门禁链骨架（`gates.py`：0 级硬门槛真跑，①沙箱回放/②金标回归/③灰度对比 = M5+ 演练位）；
- 候选状态机（`proposal.py`：09 §7 六态，代码校验承载同语义，PG CHECK 随 DDL）；
- **apply 恒拒绝**（`service.py`：唯一"生效"入口恒抛 `RsiApplyForbiddenError` 并留审计，
  登记「M5+ 启用」——本批无自改能力红线）；
- 全动作审计（`audit.py`：09 §6 红线 7；内存/日志汇，PG audit_logs 承接随组合根注入）。
- 欠账：`rsi_proposals`/评估运行表 DDL（database/01，09 §10 登记）；LLM 归因/起草、
  审核工单、端点与 CLI 随阶段 B（09 §10）。

## G0 缺口轨（2026-09-29 B9 批交付，权威=architecture/09 §13.3/§13.4）

- 进化面注册表（`surfaces.py`：§13.2 封闭八面 O1~O8 + 四元组元数据逐字承载；
  **新增面=代码变更=人工审批**，运行期不可扩面）；
- G0 缺口轨核心（`gap.py`：四型缺口事件/场景指纹 v1（意图向量+实体类型集+失败模式的
  规范化拼接）/`GapStore`（InMemory + JsonlGapStore 两文件追加写，PG rsi_proposals 表
  随改表流程批次）/`GapCollector` 滑窗聚类（默认 30 天/5 次）达标开单——**全确定性零 LLM**，
  开单经 `RsiService.submit`（draft、surface=O1、evidence 簇证据），同（租户, 指纹）
  未闭合工单去重；
- 信号汇（`sinks.py`，因依赖 writeback 台账域模型**不进包根命名空间**）：
  `LedgerFailureSink`（台账 FAILED 行→execution_failure，--live 真实源）+
  `UnboundActionSink`（dispatcher resolve-miss→unbound_action，经
  `ActionDispatcher(gap_sink=…)` 可选注入，默认 None 零侵入）；
  unmapped_intent/manual_fallback 仅定义采集接口留内核线上报（不接假信号）；
- 运行入口 `services/tools/orsi/run_g0.py`（`--demo` 合成事件 / `--live` PG 台账，Markdown 报告；
  首跑实录=docs/rsi/G0-首次运行-2026-09-29.md，本地文档）；
- `TriggerTrack` 增 `GAP` 枚举位（三轨，09 §13.4）；触发注册面对 GAP 显式拒绝
  （缺口检测器=聚合巡检非注册制）；候选生命周期迁移仍必经 `Proposal.transition`。

## G1 起草（2026-09-29 B10 批交付，权威=architecture/09 §13.3 G1 + §13.2 分界铁律）

- 起草引擎 `drafter.py`（因依赖 ontology.core.tbox/platform.ports **不进包根命名空间**）：
  三级降路径 **L1 组合既有工具**（确定性零 LLM：种子行动类 ∪ ConnectorRegistry 注入 IRIs
  中按同命名空间组合为计划模板，落 **O5**）→ **L2 市场能力包检索**（PluginMarketService
  list_market/get_detail 结构子集 Protocol，语义标注精确 > 关键词全命中；**plugin 侧只读
  不装**——只产候选包引用，安装走既有审核；落 **O1**）→ **L3 LLM 起草**（本链唯一 agent
  环节：ModelPort.complete_structured 受约束生成 + **确定性校验**——action_iri 必落种子
  行动类集/execution_mode 枚举合法/description 非空，越界即拒、重试预算硬上限 1+2 次，
  耗尽=该工单本轮失败；落 **O1**）；
- `DraftArtifact`（path/surface/content/action_iri/execution_mode/confidence/rationale）：
  产物写回 `envelope["draft_artifact"]`（JSONB 整体重赋值），**候选状态保持 draft**——
  迁移唯一入口仍为 RsiService/Proposal.transition（G2/G3 本批不做不 stub）；
- 种子行动类装载 `load_seed_actions`（services/seeds/power_seed.ttl 的 ob2:Action 子类闭包，
  复用 ontology.core.tbox 装载器，只读）；
- 运行入口 `services/tools/orsi/run_g1.py`（`--demo` 合成两簇演示三级命中/降级 / `--live` OA_
  配置骨架；Markdown 报告含逐级尝试留痕；首跑实录=docs/rsi/G1-首次运行-2026-09-29.md，
  本地文档；store 口径：G0=.orsi-g0、G1=.orsi-g1，同一 GapStore JSONL 格式）。
