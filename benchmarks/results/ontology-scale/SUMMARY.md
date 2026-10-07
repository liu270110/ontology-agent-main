
## 2026-10-07T02:00:31+00:00 tag=onto-scale-v0 smoke=True commit=c940b994 branch=feature/bench-ontoscale

| 场景 | 关键指标 | 断言 | 状态 | 耗时s |
| ---- | ---- | ---- | ---- | ---- |
| scale_1e2 | validate_clean_p95_ms=299.0 | 5/5 通过 | ok | 3.793 |
| scale_1e3 | validate_clean_p95_ms=3892.0 | 4/5 通过 | assert_failed | 38.069 |
| scale_1e4 | validate_clean_p95_ms=34845.0 | 4/5 通过 | assert_failed | 406.652 |

> 执行 3 场景：ok=1 assert_failed=2 error=0 partial=0；环境=确定性合成（种子固定，零网络依赖；vLLM@18001 仅 token 计数）

## 2026-10-07T02:12:33+00:00 tag=onto-scale-v0 smoke=True commit=c940b994 branch=feature/bench-ontoscale

| 场景 | 关键指标 | 断言 | 状态 | 耗时s |
| ---- | ---- | ---- | ---- | ---- |
| scale_1e2 | validate_clean_p95_ms=321.0 | 5/5 通过 | ok | 4.097 |
| scale_1e3 | validate_clean_p95_ms=3903.0 | 5/5 通过 | ok | 42.982 |
| scale_1e4 | validate_clean_p95_ms=51752.0 | 5/5 通过 | ok | 502.014 |

> 执行 3 场景：ok=3 assert_failed=0 error=0 partial=0；环境=确定性合成（种子固定，零网络依赖；vLLM@18001 仅 token 计数）

## 2026-10-07T02:46:46+00:00 tag=onto-scale-v0 smoke=True commit=c940b994 branch=feature/bench-ontoscale

| 场景 | 关键指标 | 断言 | 状态 | 耗时s |
| ---- | ---- | ---- | ---- | ---- |
| scale_1e2 | validate_clean_p95_ms=277.0 | 5/5 通过 | ok | 3.842 |
| scale_1e3 | validate_clean_p95_ms=4176.0 | 5/5 通过 | ok | 37.923 |
| scale_1e4 | validate_clean_p95_ms=38688.0 | 5/5 通过 | ok | 437.4 |

> 执行 3 场景：ok=3 assert_failed=0 error=0 partial=0；环境=确定性合成（种子固定，零网络依赖；vLLM@18001 仅 token 计数）
