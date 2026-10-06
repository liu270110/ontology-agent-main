# benchmarks/ — 基准对比体系（docs/Agent/16-基准对比体系设计）

> 用户裁决（2026-10-06）：多维对比数据是项目可信度的根基。单独文件夹、每次优化后重跑、
> 数据作为参考指标、机制挂 ORSI 作参考依据、测试可被（自研+接入的）agent 扩充。
> 问题输入源 = docs/评审/红队攻击性审查-2026-10-06.md。

## 目录形态（16 篇 §1）

```
benchmarks/
  README.md                  # 本文件：运行指南 + 指标口径字典
  run.py                     # 统一入口：--suite/--tag/--smoke/--scenario/--list
  orsi_link.py               # suite 结果 → ORSI capability 注册建议（本波只建议不注册）
  suites/
    agent-core/              # A/B/C/H 域：agent 核心能力（本波落地）
      scenarios/*.yaml       # 场景=数据文件（schema 校验防作弊断言；可经集市审核链扩充）
      runner.py              # 执行器：起环境→跑场景→收指标
      metrics.py             # 指标计算（口径唯一事实源，六指标纯函数）
    rag/                     # F 域（第二波）：检索六维对比 harness
    intent/                  # D/E 域（第二波）：意图识别+JEV+ontology 约束 A/B
    ontology-scale/          # G 域（第二波）：重型本体规模梯度
  results/                   # <suite>/<date>/*.json + SUMMARY.md（机器写，人审，追加式）
```

## 运行指南

前置：本地 PG 可达（`OA_PG_*` 缺省 `localhost:5432/onto`，账号须 CREATEDB——基准建
**一次性私库** `oa_wt_test_<hex>`，用毕 DROP，不触碰共享库 schema）；Python ≥3.11 且
仓库依赖已装（httpx/fakeredis/pyyaml 随 dev extra）。**零真网**：模型面全确定性桩，
无需 LLM/vLLM 在线。

```bash
# 冒烟（全链走通为准，场景内缩参）——门禁①
python benchmarks/run.py --suite agent-core --smoke

# 全量（完整参数）+ 版本标签（进 manifest 与 SUMMARY，供对比曲线对齐）
python benchmarks/run.py --suite agent-core --tag v0.1.0

# 单场景 / 场景清单
python benchmarks/run.py --suite agent-core --scenario recovery_time_s
python benchmarks/run.py --suite agent-core --list

# ORSI 注册建议（读 results，输出建议 JSON；不自动注册）
python benchmarks/orsi_link.py --results benchmarks/results/agent-core/<date> --out orsi-suggestions.json
```

产物：`results/<suite>/<date>/<HHMMSS>-<scenario>.json`（指标+样本+断言+环境指纹）、
同目录 `manifest.json`（运行清单）、`results/<suite>/SUMMARY.md`（**追加式**，不覆盖
历史——每次优化后的参考指标曲线）。退出码 0=全链执行成功（**断言失败≠执行失败**，
断言结果是数据）；1=场景执行 error 或环境失败。

环境口径（全场景一致）：一次性私库 + fakeredis 进程内替身 + httpx.ASGITransport 进程内
app（不起端口）+ 限流常量基准期上调（测量互斥/越权/恢复语义，快速连发被 429 截断会污染
指标）+ 模型面确定性桩。

## 指标口径字典（agent-core 第一波六项，口径冻结于 suites/agent-core/metrics.py）

| 指标 | 定义（口径） | 采集点 | 应然值 | 红队映射 | 场景要点 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| session_mutex_rate | 同 session 并发消息一轮，受理数≤1 即互斥成立；rate=成立轮/总轮 | 非SSE `POST /sessions/{sid}/messages` 响应状态码 ×N 轮 | 1.0 | A1 | 轮间 `/tasks/{id}/cancel` 收敛；拒绝形态按状态码分布留样（4102 预检 vs 5xxx 兜底冲突） |
| cross_tenant_leak.reject_rate | 端点矩阵攻击中被拒（非 2xx）笔数/总笔数 | 同租户 B+跨租户 C 的 token 打 owner 的 messages/inbox/rewind/trajectory/sessions 六路矩阵 | 1.0（应全拒） | A2 | inbox 趁活跃 Run 先打；rewind 破坏性放最后；owner 自读为对照探针防「资源不存在型假拒绝」 |
| memory_cross_contamination.leak_count | 互查探针中读到他人独特征标记的次数 | 双/三用户各写 L2 独特征事实→列表互读+指名 user_id 越权读+检索面互查 | 0 | A3 | 标记=uuid 片段入正文；越权读应 403/2002 |
| side_effect_duplication.extra_writes | 强制失败→run 级重试后 EXTERNAL_WRITE 净重复次数（总写−轮数） | 假写工具 invoke 计数（编排层直调：写成功→后继步强制失败→携 validated 锚点重试） | 0（每轮写次数应 1） | C2 | 复刻 worker 重试语义（新 run_id+resumed_validated）；内核对账 EXTERNAL_WRITE 恒不跳——本指标量化该形态真实重复数 |
| invalid_retry_count | 恒失败工具被无效重复调用次数=调用数−1 | 伪模型注入同签名 N 步恒失败计划，假工具 invoke 计数 | 有界（≤循环防护阈值+1）且以 5008 放弃 | C1 | 10 步注入证「收敛于循环防护（A-1）而非步数预算截断」；防护失效信号=5005/unbounded |
| recovery_time_s | cancel 中断发起→任务状态完全收敛的墙钟时延（p50/p95/max） | 在途工具运行中 asyncio cancel；收敛=在途工具中止+无 chat-run 僵尸任务；恢复=收敛后同编排器新一轮完成 | 收敛率/恢复率=1.0，时延有界 | A5/C 族 | kill -9 真实进程屠杀形态登记后续批次（第一波=进程内 cancel 口径） |

断言机制：场景 YAML 的 `asserts` 只允许对上表指标键断言（白名单校验，防作弊断言），
操作符受控（`>= <= == > <`）；断言失败在结果 JSON 标 `assert_failed`，**不影响退出码**
（基准的职责是测量与留档，裁决在人）。

## 场景数据文件（agent 可扩充通道）

`scenarios/*.yaml` schema：`version`（恒 1）+ `scenario`（runner 注册名）+ `description`
+ `params`（场景参数，与 runner.DEFAULT_PARAMS 浅合并；`--smoke` 注入缩参）+ `asserts`
（白名单断言）。schema 校验失败或断言键越权 → 拒绝加载（16 篇 §3「runner 对场景 schema
校验（防作弊断言）」）。新增场景=新 YAML + runner 注册实现，走集市 skills/tools 同款审核
链上架。

## ORSI 挂钩（16 篇 §3）

`orsi_link.py` 读 results JSON → 产出注册建议（face/status=candidate/evidence_uri=results
路径/capability_fingerprint=场景集哈希）。**场景集变=指纹变=能力需重评**——ORSI「参考
依据」的失效联动面。本波只产出建议 JSON 不自动注册；指纹为基准侧独立计算，正式注册由
services/rsi 业务层重算。
