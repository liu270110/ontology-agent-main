# MANIFEST — 电力配电网停电分析样例数据集

- 用途：M2.5 批次检索评估（golden QA）与抽取校准（PoC② 金标三元组）的样例语料。
- 事实边界：`seeds/power_seed.ttl`（36 类种子本体）——所有内嵌事实仅使用种子本体的类与关系词汇。
- 语料构成：18 篇 Markdown（故障报告 5 / 调度日志 2 / 操作票 2 / 台账 3 / 工单台账 1 / 停电分析报告 2 / 抢修复电记录 2 / 综述 1），混合文体。
- 金标三元组：`seeds/golden/power_triples_golden.jsonl` 共 **582 条**（逐条带 evidence_span，逐字来自本文档集）；检索评估集：`seeds/golden/power_retrieval_golden.jsonl` 共 30 例（local 12 / global 10 / drift 8）。
- 「登记事实数」= 该篇文档中登记入金标三元组集的事实条数（文档中另有少量背景陈述未入金标，属正常冗余）。

| 文件 | 文体 | 登记事实数 | 涉及本体类 |
| ---- | ---- | ---- | ---- |
| d00_供电概况_城东片区.md | 综述导览 | 0（综述，事实复用各台账） | 全景引用 |
| d01_设备台账_城东片区一次设备.md | 设备台账 | 125 | Substation, Feeder, LineSection, Switch, Transformer, ProtectionDevice, DistributionLine（+ repairCrewAvailable / inSection） |
| d02_台区客户与计量表台账.md | 客户/计量台账 | 160 | Feeder, LineSection, Customer, Meter（+ servesCustomer / inSection） |
| d03_抢修班组与人员名册.md | 名册 | 24 | RepairCrew, CrewMember（+ crewLead） |
| d04_故障报告_0418雷雨滨河线故障.md | 故障报告 | 9 | OutageEvent, OutageConfirmed, IsolateFault, RepairCompleted, PowerRestored（+ affectsFeeder / confirmedAt / restoresOrder / restoredAt） |
| d05_故障报告_0418雷雨城网线柳荫线故障.md | 故障报告 | 17 | 同上（双事件） |
| d06_故障报告_0422翠湖线电缆故障.md | 故障报告 | 9 | 同上 + DispatchRepair |
| d07_故障报告_0502开发区线树障与0508温泉线配变烧损.md | 故障报告（合并篇） | 18 | 同上 + IsolateFault, DispatchRepair |
| d08_故障报告_0526城网线配变低压侧故障.md | 故障报告 | 9 | 同上 |
| d09_调度日志_0418雷雨夜.md | 调度日志 | 4 | StormAlert, DispatchRepair |
| d10_调度日志_0502大风与0516预警.md | 调度日志 | 3 | StormAlert, DispatchRepair |
| d11_操作票_0422翠湖线故障隔离.md | 操作票 | 1 | IsolateFault |
| d12_操作票_0428柳荫线计划检修.md | 操作票 | 9 | OutageEvent, OutageConfirmed, DispatchRepair, RepairCompleted, PowerRestored |
| d13_停电工单台账.md | 工单台账 | 95 | OutageOrder, RepairCrew（+ orderNo / hasStatus / dispatchedTo） |
| d14_停电分析报告_0418雷雨与0422电缆故障.md | 停电分析报告 | 2 | OutageReport |
| d15_停电分析报告_0502大风与0508温泉线.md | 停电分析报告 | 2 | OutageReport |
| d16_抢修完成与复电记录_四月.md | 抢修/复电记录 | 36 | OutageEvent/Confirmed, DispatchRepair, RepairCompleted, PowerRestored（4 事件全链） |
| d17_抢修完成与复电记录_五月.md | 抢修/复电记录 | 59 | 同上（5 事件全链 + 9 工单完成 + 11 派发） |
| **合计** | 18 篇 | **582** | 种子本体 20 类 + 全部 11 条属性（RestorePower 行动类无实例化事实，未入金标） |

## 实体与一致性约定

- 馈线 6 条（城网线/开发区线/滨河线/柳荫线/翠湖线/温泉线），变电站 3 座，线路区段 20 个，一次设备 45 台（开关 20 / 配变 16 / 保护装置 6 / 配电线路 3），台区客户 40 户、计量表 40 只，抢修班组 6 个 18 人，停电工单 24 张，停电确认事件 17 起，风暴预警 3 次，复电事件 17 次，抢修完成 21 次。
- 事件编号 `EV-MMDDNN`、派发 `DA-*`、隔离 `ISO-*`、抢修完成 `RPR-*`、复电 `RST-*`、预警 `SA-*`、报告 `RPT-*`、操作票 `CZ-*`；工单号 `OO-MMDDNN`（符合种子本体 R004 `^OO-[0-9]{6}$`）。
- 时间一律 ISO-8601（`2026-04-18T19:42:00`），与金标三元组字面值逐字一致。
- 互洽性：设备-区段归属以 d01 为准；客户-区段-配变以 d02 为准；工单状态与派往班组以 d13 为准；事件时间以各故障报告/记录篇为准。生成脚本曾对全部 582 条做「evidence_span 逐字存在 + 主客体在证据行内」校验，零失败。
