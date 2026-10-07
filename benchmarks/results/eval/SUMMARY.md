# eval release 曲线（SUMMARY.md，机器写人审；追加式不覆盖历史）


## 2026-10-07T08:17:59.800193+00:00 release-eval tag=v0.2.0-m4.7-first-curve

| suite | 健康 | run tag | run 完成时间 | 核心指标 |
| ---- | ---- | ---- | ---- | ---- |
| agent-core | ok | redteam-fix-verified | 2026-10-07T00:55:18+00:00 | session_mutex_rate.session_mutex_rate=1, cross_tenant_leak.reject_rate=1, memory_cross_contamination.leak_count=0, side_effect_duplication.idempotency_key_visible=true, invalid_retry_count.invalid_retry_count=1, recovery_time_s.recovery_time_s_p50=0.0011 |
| rag | ok | v0 | 2026-10-07T07:50:06 | ours_kb.recall_at_k=0.975, ours_kb.mrr=0.975, ours_kb.faithfulness=1, ours_kb.latency_p50_ms=209.916 |
| intent | ok | v0 | 2026-10-07T10:22:39 | a0.intent_accuracy=0.9, a1.intent_accuracy=0.671429, gain.intent_accuracy=-0.228571 |
| ontology-scale | ok | onto-scale-v0 | 2026-10-07T02:46:46+00:00 | scale_1e2.validate_clean_p95_ms=277, scale_1e3.validate_clean_p95_ms=4176, scale_1e4.validate_clean_p95_ms=38688 |

> market_reference：citation_only（红线：本仓 benchmarks/results/rag 的 v0 电力语料实测数字（recall@5/MRR 等）与下列榜单数字禁止直接并列比较——数据集不同…；全量指针见 dashboard.json）

## 2026-10-07T08:17:59.800193+00:00 release=v0.2.0-m4.7-first-curve version-diff

| suite | 状态 | prev → curr | 可比 | 回归指标 |
| ---- | ---- | ---- | ---- | ---- |
| agent-core | ok | bench-core-smoke-final → redteam-fix-verified | 是 | recovery_time_s.recovery_time_s_p50 |
| rag | no_baseline | - | - | - |
| intent | no_baseline | - | - | - |
| ontology-scale | no_baseline | - | - | - |

### agent-core 逐指标（bench-core-smoke-final → redteam-fix-verified）

| metric | prev | curr | delta | trend |
| ---- | ---- | ---- | ---- | ---- |
| cross_tenant_leak.reject_rate | 0.666667 | 1 | 0.333333 | ↑ |
| invalid_retry_count.invalid_retry_count | 1 | 1 | 0 | → |
| memory_cross_contamination.leak_count | 0 | 0 | 0 | → |
| recovery_time_s.recovery_time_s_p50 | 0.0009 | 0.0011 | 0.0002 | ↑ |
| session_mutex_rate.session_mutex_rate | 1 | 1 | 0 | → |
| side_effect_duplication.extra_writes | 2 | 2 | 0 | → |
| side_effect_duplication.idempotency_key_visible | - | true | - | n/a |

> 指标取自各 suite results/ 已落盘 run JSON（只聚合不重算）；trend=↑升 ↓降 →平；regressions=按方向表判定的变差指标（neutral 指标不作回归判定）
