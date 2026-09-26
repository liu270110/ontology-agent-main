# DSec-demo — DSec 论文技术本地验证场

以单机 Docker 复现《[DeepSeek Elastic Compute (DSec): A Sandbox Infrastructure for Effective Agentic Training at Scale](https://arxiv.org/abs/2609.22978)》的全部可落地核心机制。论文 160 节点/3 万核的生产集群，按 ~1/1000 缩放为 **3 台 DinD edge 节点 + 4 个无状态控制面服务**，跑在 22 逻辑核 / 16GB VM 上。

**验证结论：14 项实验全部通过**（含 3 项按本机内核能力做的等价复现，差异逐项标注）。
完整对照表见 **[docs/VERIFICATION.md](docs/VERIFICATION.md)**，原始数据在 `results/*.json`。

## 架构

```
SDK(libdsec 式, sdk/dsec_sdk.py)
   │ HTTP
   ▼
apiserver :8000 ──无每沙箱状态，sandbox_id 编码 edge──┐
   │                                                  │
   ├─▶ iam :8003        多级项目嵌套/配额链/人与 Agent 同一模型
   ├─▶ placement :8001  两阶段选点：过滤→power-of-k，本地在途视图
   ├─▶ watcher :8002    周期心跳/无持久状态（重启失忆→edge 重注册自愈）
   └─▶ edge-{1,2,3}     DinD 节点：本地准入 409、TTL、pack_diff 快照、
        (docker:dind)   iptables 出口白名单、SCHED_IDLE QoS、FnCall 预热池
objserver :9000        模拟 3FS：对象读 + 字节记账（E3 用）
```

## 快速开始

```bash
# 前置：Docker Desktop 运行中；国内网络需 daocloud 镜像源（已在本机 daemon.json 配置）
bash cluster.sh up        # build 镜像 → 起控制面+3 edge → 预载沙箱镜像 → 健康检查

cd experiments
python E00_smoke/run.py   # 生命周期烟雾测试（应 PASS）
python E01_backend_latency/run.py
python E04_density/run.py  # E12 / E06 / E07 / E08 / E10 / E11 / E13 同理
```

独立实验（不依赖集群，直接跑特权容器）：

| 实验 | 命令 |
| --- | --- |
| E2 可组合层（真 EROFS + overlayfs + runc） | 见 `experiments/E02_composable/` 内 docker run 一行 |
| E3 按需加载（lazyfs chunk 缓存 vs 全量拉取） | `experiments/E03_ondemand/`（依赖 compose 里的 objserver） |
| E5 CPU QoS（SCHED_IDLE 时延膨胀） | `experiments/E05_cpu_qos/` |
| E14 microVM（QEMU+KVM + balloon） | `experiments/E14_microvm/` |

## 实验清单（对应论文机制）

| 实验 | 论文章节 | 一句话结果 |
| --- | --- | --- |
| E0 生命周期 | §4.1 | create 461ms / 透明恢复 / TTL 回收全通 |
| E1 后端延迟 | §4.1 | FnCall 池 119ms vs 冷容器 434ms（3.7×） |
| E2 可组合层 | §5.1 | EROFS 层 + overlayfs 动态组装：3.0× 提速、盘写 −100%、层升级 O(m·N)→O(m) |
| E3 按需加载 | §5.3 | 元数据本地+chunk 按需：远端 I/O −92%（=8% 访问占比） |
| E4 高密度超分 | §3 | 350 沙箱 0 失败，单节点峰值 163，睡眠 CPU≈0 |
| E5 CPU QoS | §5.2 | SCHED_IDLE 改善 3.4pt（论文 3.4%）；core scheduling 内核缺失 N/A |
| E6 暂停回收 | §6.3 | pause+force_empty 回收 314.7MB，状态保留+透明恢复 |
| E7 Placement | §4.2/§7 | powk 仿真 + edge 409 改选 + 穷尽 503 |
| E8 pack_diff | §6.1 | 快照 2.9s → 还原 883ms（重放重建的 94.6×） |
| E9 日志重放 | §6.2 | 非幂等命令恢复后不重复执行 |
| E10 出口白名单 | §6.5 | 允 pypi 拒 npm，默认拒绝 |
| E11 IAM 嵌套 | §4.2 | 三级项目、配额链记账、越权 403、穷尽 429 |
| E12 突发创建 | §3 | 200 沙箱 5.1/s、按容量摊匀 |
| E13 root 受限 | §6.5 | DAC 能力修剪等价 AppArmor"root 也受限" |
| E14 microVM | §4.1/§5.2 | QEMU+KVM 无盘启动 + balloon 回收（Firecracker N/A 如实记录） |

## 目录

```
controlplane/   apiserver / placement / watcher / iam（一个镜像四种角色）
edge/           edge_agent.py + dind 入口
sdk/            libdsec 式客户端
images/         dsec-sandbox-base(alpine+iptables+chrt) / dsec-sandbox-debian
experiments/    E00~E14 各实验自包含可复现
results/        全部实验原始 JSON
docs/           VERIFICATION.md（论文声明→实验→结果 对照表）
cluster.sh      up / down / load / clean / status / build
```

## 与论文的已知差异（详见 VERIFICATION.md §4）

- **3FS → objserver + 节点本地镜像**：无真分布式文件系统；E3 用 objserver 字节记账复现"远端数据层"，pack_diff 用 env_id 编码源 edge 模拟层共享可见性。
- **cgroup v2 memory.reclaim → v1 force_empty**：宿主 hybrid cgroup。
- **core scheduling / DAMON / AppArmor / Firecracker / virtio-pmem / OverlayBD / 云突发**：本机内核或资产不可达，如实记录 N/A 或以等价机制复现。
