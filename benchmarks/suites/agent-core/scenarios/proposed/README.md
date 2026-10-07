# scenarios/proposed/ — 飞轮审核队列（docs/Agent/19 §5 批 F2）

本目录是**飞轮转化环（benchmarks/flywheel/harvest.py）产出的候选场景草稿队列**，
目录即审核队列——**未过审不进正式套件**（宪法 3：候选非成品）。

## 隔离机制（防污染，结构性保证）

- 正式套件 loader（`suites/agent-core/runner.py` `load_scenario_specs`）按**非递归
  glob** 拾取 `scenarios/*.yaml`——本子目录的草稿天然不被加载，不进正式跑面；
- 草稿落盘后立即经 `load_scenario_specs` **同款白名单校验**（schema+断言键+操作符），
  任何不合格 YAML 会在 harvest 管道显式报错（防污染闸）；
- 断言键只引 metrics.py 已登记指标：任务成功口径=`task_outcome_user_reported`
  （2026-10-07 新登记），负例另引 `invalid_retry_count ≤ 2`（内核循环防护界）。

## 草稿形态

- 文件名 `flywheel-{golden|regression}-{短哈希}.yaml`；场景名
  `flywheel:{golden|regression}:{短哈希}`（run_id sha256 前 12 位，不含用户数据）；
- `params.task_prompt` 已脱敏模板化（用户专有名词 UUID/邮箱/IP/绝对路径 →
  `⟪USER_n⟫` 占位，同词同号；保守正则，宁漏替不误伤，漏替由人工终审兜底）；
- `params.user_correction`（负例）=用户纠错文本（同样脱敏后）。

## 过审流转

人工审核通过 → 移出本目录进入 `scenarios/` 正式套件 + 在 `runner.SCENARIOS`
登记对应场景实现（草稿仅为数据文件，执行体另行接线）→ 下一次 release-eval
即带上真实业务任务场景（19 §5 回流环）；拒绝 → 删除本目录内草稿即可，
正式套件零影响。orsi 面：负例草稿由 harvest 顺手产出 quality_regression
注册**建议**（face=O4 / track=shortgap，只建议不注册），登记进当日
`results/flywheel/<date>/harvest.json`。
