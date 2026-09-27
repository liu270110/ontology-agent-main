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
