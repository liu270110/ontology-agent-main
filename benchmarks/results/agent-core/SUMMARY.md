
## 2026-10-06T23:34:15+00:00 tag=bench-core-smoke-2 smoke=True commit=eaea7ebb branch=feature/redteam-bench-core

| 场景 | 关键指标 | 断言 | 状态 | 耗时s |
| ---- | ---- | ---- | ---- | ---- |
| session_mutex_rate | session_mutex_rate=1.0 | 2/2 通过 | ok | 0.96 |
| cross_tenant_leak | reject_rate=0.666667 | - | ok | 0.915 |
| memory_cross_contamination | leak_count=0 | - | ok | 0.428 |
| side_effect_duplication | extra_writes=2 | - | ok | 0.017 |
| invalid_retry_count | invalid_retry_count=1 | - | ok | 0.008 |
| recovery_time_s | recovery_time_s_p50=0.0012 | 3/3 通过 | ok | 0.105 |

> 执行 6 场景：ok=6 assert_failed=0 error=0；环境=一次性私库+fakeredis+确定性桩（零真网）

## 2026-10-06T23:45:22+00:00 tag=cli-filter-check smoke=True commit=eaea7ebb branch=feature/redteam-bench-core

| 场景 | 关键指标 | 断言 | 状态 | 耗时s |
| ---- | ---- | ---- | ---- | ---- |
| side_effect_duplication | extra_writes=2 | - | ok | 0.023 |

> 执行 1 场景：ok=1 assert_failed=0 error=0；环境=一次性私库+fakeredis+确定性桩（零真网）

## 2026-10-06T23:45:41+00:00 tag=bench-core-smoke-final smoke=True commit=eaea7ebb branch=feature/redteam-bench-core

| 场景 | 关键指标 | 断言 | 状态 | 耗时s |
| ---- | ---- | ---- | ---- | ---- |
| session_mutex_rate | session_mutex_rate=1.0 | 2/2 通过 | ok | 1.662 |
| cross_tenant_leak | reject_rate=0.666667 | - | ok | 2.064 |
| memory_cross_contamination | leak_count=0 | - | ok | 0.736 |
| side_effect_duplication | extra_writes=2 | - | ok | 0.033 |
| invalid_retry_count | invalid_retry_count=1 | - | ok | 0.012 |
| recovery_time_s | recovery_time_s_p50=0.0009 | 3/3 通过 | ok | 0.116 |

> 执行 6 场景：ok=6 assert_failed=0 error=0；环境=一次性私库+fakeredis+确定性桩（零真网）

## 2026-10-07T00:55:18+00:00 tag=redteam-fix-verified smoke=True commit=28346012 branch=feature/redteam-fix

| 场景 | 关键指标 | 断言 | 状态 | 耗时s |
| ---- | ---- | ---- | ---- | ---- |
| session_mutex_rate | session_mutex_rate=1.0 | 2/2 通过 | ok | 1.191 |
| cross_tenant_leak | reject_rate=1.0 | - | ok | 1.01 |
| memory_cross_contamination | leak_count=0 | - | ok | 0.532 |
| side_effect_duplication | idempotency_key_visible=True | 2/2 通过 | ok | 0.015 |
| invalid_retry_count | invalid_retry_count=1 | - | ok | 0.009 |
| recovery_time_s | recovery_time_s_p50=0.0011 | 3/3 通过 | ok | 0.114 |

> 执行 6 场景：ok=6 assert_failed=0 error=0；环境=一次性私库+fakeredis+确定性桩（零真网）
