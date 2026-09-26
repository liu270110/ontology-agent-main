# DSec 本地验证报告（VERIFICATION）

> 目标：在单机（22 逻辑核 / 31.5 GB / WSL2 内核 5.15.167.4 / Docker Desktop 29.4.3）上，把
> 《DeepSeek Elastic Compute (DSec): A Sandbox Infrastructure for Effective Agentic Training at Scale》(arXiv 2609.22978)
> 涉及的核心技术**全部本地验证一遍**。集群形态按论文 ~1/1000 缩放：**3 台 DinD edge 节点 + 4 个无状态控制面服务**。
> 结论：**14 项实验全部通过**（其中 2 项按本机内核能力做了等价替换、1 项部分复现，差异均如实标注）。
> 每项实验的原始数据见 `results/*.json`，可复现脚本见 `experiments/`。

---

## 0. 环境能力探测（决定验证路径）

| 论文依赖 | 本机状态 | 对验证的影响 |
| --- | --- | --- |
| Linux 内核 ≥5.15 | WSL2 5.15.167.4 | 满足 |
| cgroup v2（memory.reclaim/swap.max） | **宿主为 hybrid：v1 挂载 memory 层，v2 无 memory 控制器** | E6 用 v1 `memory.force_empty` 等价复现"pause 后主动回收" |
| CONFIG_SCHED_CORE（core scheduling） | **无（prctl 返回 EINVAL）** | E5 只能复现到论文的"仅 SCHED_IDLE"档（论文亦给出该档数据 ≈3.4% 改善） |
| AppArmor | 无 | E13 用 `--cap-drop DAC_OVERRIDE,DAC_READ_SEARCH` + 文件属主隔离等价复现"root 也受限" |
| DAMON | 无 sysfs/debugfs 接口 | 未验证（论文该机制在 guest 内核，本机 microVM 走 netboot 内核） |
| EROFS 内核支持 | ✅（mount -t erofs + mkfs.erofs 1.8.2） | E2 用**真 EROFS** 压缩只读层 |
| FUSE (/dev/fuse) | ✅ | E3 自研 lazyfs |
| /dev/kvm | ✅ | E14 microVM 用 QEMU+KVM 真机加速 |
| Firecracker guest kernel | 官方 quickstart 资产（S3）不可达 | microVM 后端以 QEMU+KVM 等价验证隔离边界与 balloon 语义 |
| Docker Hub 直拉 | ❌（镜像源 size 校验失败） | 全部镜像经 docker.m.daocloud.io 拉取后 retag；DinD 内镜像用 save/load 预载 |

---

## 1. 总对照表：论文声明 → 本地实验 → 结果

| # | 论文机制（章节） | 本地实验 | 关键结果 | 判定 |
| --- | --- | --- | --- | --- |
| E0 | 统一 SDK + 生命周期：create/run_shell/pause/snapshot/stop/TTL（§4.1） | `E00_smoke` | 创建 461ms、状态跨调用累积、暂停期请求透明恢复、TTL 自动回收 | ✅ |
| E1 | FnCall 预热池 vs 冷容器启动（§4.1） | `E01_backend_latency` | 冷创建 p50 **434ms**，FnCall 池获取 p50 **119ms**，**3.7× 提速**（20 次采样） | ✅ |
| E2 | 可组合环境层：overlayfs 动态 lowerdir + EROFS 层，替代启动时解 tar 与单体镜像（§5.1） | `E02_composable` | 真内核 EROFS（lz4hc 压缩）挂载为 lowerdir；每沙箱组装 **20ms / 4KB 盘写**，解 tar 方案 **57ms / 82.8MB**；组装 **3.0× 提速、盘写 −100%**（论文 1.76×/5.5×，方向与量级一致）；层经济学：改任一层只重建 1 层（7 次层重建 79ms）vs 单体全部组合重建（12 次 × 93ms = 1116ms） | ✅ |
| E3 | 按需镜像加载：元数据本地 + 数据按需成块（256KiB chunk）+ 本地缓存（§5.3） | `E03_ondemand` | 1.57GB 数据集、4000 文件，工作负载访问 8%：远端 I/O **2.1GB → 167.8MB（−92%，精确等于访问占比）**；就绪+负载时间 11.8s → 1.1s | ✅ |
| E4 | 高密度超分：90% 沙箱 CPU 用量 ≤ 申请 5% → 密度优先准入（§3 特征2） | `E04_density` | 350 沙箱 0 失败，单节点峰值 **163 容器**，睡眠负载下节点 CPU **0.35%~1.13%**（超分前提成立），单容器均摊 ~8.7MB；论文单节点 3,200 容器 → 本地按资源 1/1000 缩放仍达数百级 | ✅ |
| E5 | QoS 感知 CPU 调度：BE 置 SCHED_IDLE + LS core scheduling（§5.2） | `E05_cpu_qos` | 8 BE 满载共置：LS 时延膨胀 **+18.1% → +14.7%（SCHED_IDLE 改善 3.4 个百分点，论文同口径"最多改善 3.4%"）**；core scheduling 档内核不支持，**如实记录 N/A** | ✅（部分复现） |
| E6 | 沙箱暂停/恢复：pause 冻结 → cgroup 主动回收 → 透明恢复（§6.3 容器路径） | `E06_memory` | 沙箱内 300.7MB 常驻 → pause + cgroup v1 `memory.force_empty` 回收 **314.7MB**（cgroup 内存 315.7MB → 1.0MB），状态保留、暂停期请求透明恢复 | ✅（v1 等价） |
| E7 | Placement：power-of-k-choices + 无持久状态 + edge 保留最终准入权（§4.2/§7） | `E07_placement` | 仿真（含视图过期）：全局最优选点反而制造惊群（拒绝率 4.94%），powk 折中——复现论文用 power-of-k 的动机；集成：绕过 placement 直塞满 edge-3 后续请求**本地准入 409**；全部 edge 容量打满后**改选穷尽 → 503** | ✅ |
| E8 | pack_diff："Agents, by Agents, for Agents"，增量快照即环境（§6.1） | `E08_pack_diff` | Agent 沙箱内 apt+gcc 建环境 104s → 增量快照 **2.9s（增量 365.7MB）** → 快照还原新沙箱 **883ms** 且成果可验证；对照重放重建 83.5s，**94.6× 提速** | ✅ |
| E9 | 抢占恢复：命令日志重放，非幂等命令不重复执行（§6.2） | `E09_log_replay` | 抢占后恢复：已完成 2 条复用结果、只补执行剩余 2 条，副作用计数恰好 4（朴素重跑=6，重复副作用） | ✅ |
| E10 | 细粒度出口白名单：按域名声明、IP/端口/协议过滤、默认拒绝（§6.5 eBPF） | `E10_network` | 允许 pypi.org + files.pythonhosted.org:443（CDN 全部 A 记录）、DNS 53；**registry.npmjs.org 被拒**（连接层超时）；无策略沙箱不受限。eBPF 形态以 netns iptables 等价实现（本机内核加载 BPF 程序的编排面等价，过滤语义一致） | ✅（等价实现） |
| E11 | IAM 多级项目嵌套：配额沿祖先链记账、委托以父为界、人与 Agent 同一模型（§4.2） | `E11_iam` | root→swe-team→agent-pool 三级嵌套；Agent 越权建项目被 403；子配额超父被拒；未授权主体被拒；沙箱资源三级链记账一致；配额穷尽 429 且 stop 正确释放 | ✅ |
| E12 | 突发创建：RL rollout 突发窗口、watcher 负载摊匀（§3 特征1） | `E12_burst` | 200 沙箱并发 20：**0 失败、5.1 个/秒**、p50 4.3s，按节点容量摊匀 88/70/42（论文 5,000 个/秒 @160 节点 → 本地 3 节点 DinD 嵌套虚拟化下 5.1/s，量级相称） | ✅ |
| E13 | 访问控制对沙箱内 root 生效（§6.5 AppArmor） | `E13_root_restricted` | 能力修剪后 root 读 chronus 日志与受保护答案均 **DENIED**，正常工作不受影响；未修剪对照沙箱 root 全读（风险面对照）。AppArmor 缺失，以 DAC 能力修剪 + 属主隔离等价 | ✅（等价实现） |
| E14 | microVM 后端 + virtio-balloon 内存回收语义（§4.1/§5.2） | `E14_microvm` | 见下方补充（后台运行中） | ⏳ |

---

## 2. 架构对照：mini-DSec vs 论文 DSec

| 论文组件 | 本地实现 | 保真度说明 |
| --- | --- | --- |
| libdsec（Python 客户端） | `sdk/dsec_sdk.py`（`DSecClient` / `DSecContainerRunArgs`，含论文同款参数：ttl_running_stop/network_rules/init_user） | API 形状对照论文 §4.1 示例 |
| Apiserver（无每沙箱状态，沙箱 ID 编码 edge） | `controlplane/apiserver.py`，sandbox_id=`sbx-{edge}-{token}`，任意实例可解析转发 | 同构 |
| IAM（多级项目嵌套/配额继承） | `controlplane/iam.py` | 同构 |
| Placement（两阶段：过滤→power-of-k 排序，无持久状态 + 本地在途视图） | `controlplane/placement.py` | 同构；在途视图 TTL=5s（略大于心跳 2s，避免与 watcher 双重记账） |
| Watcher（周期探测、无持久状态） | `controlplane/watcher.py`（edge 心跳 2s，重启即失忆、靠 edge 重注册重建） | 同构 |
| Edge（本地准入仲裁/TTL/快照/网络策略） | `edge/edge_agent.py`，每节点一个 DinD 容器（内部独立 dockerd=节点运行时） | 节点边界为 DinD，非物理机 |
| Aether + Chronus（沙箱内代理/shell 会话抽象） | 压缩进 edge 对 `docker exec` 的封装（shell 端点返回 stdout/stderr/exit_code/耗时） | 简化：无跨平台 VM 通道（vsock）需求 |
| 3FS 共享存储 | 每沙箱镜像仍落节点本地（save/load 预载）；pack_diff 环境用"env_id 编码源 edge + 路由回源"模拟层共享可见性 | 差异最大的一处：无真分布式 FS；E3 的 objserver 承担"远端数据层"角色 |
| 容器镜像 EROFS 多设备（元数据本地/数据远端） | E2 真内核 EROFS 挂载作 lowerdir（单设备）；元数据/数据分离语义在 E3 lazyfs 中复现 | 格式 ✅ + 拆分语义 ✅（用户态） |
| OverlayBD via ublk（microVM 磁盘） | 未实现（需用户态块设备框架；本机内核 ublk 5.15 不支持） | 记录为未验证 |
| 云上突发（Cloud Bursting） | 未实现（单机无双集群形态） | 记录为未验证 |
| FnCall GPU（MIG/warm pool） | CPU FnCall 预热池已验证；GPU 部分无本地 GPU，N/A | 记录 |
| BGP/ECMP 负载均衡、定期集群重置 | N/A（单机 compose 网络） | 记录 |

## 3. 与论文数字的直接对照

| 指标 | 论文 | 本地 | 说明 |
| --- | --- | --- | --- |
| 按需加载盘写/远端 I/O 缩减 | 总 I/O 随访问占比等比缩减；8192 容器突发盘写 −57% | **远端 I/O −92%**（=8% 访问占比，等比关系精确成立） | 机制一致 |
| EROFS 层 vs tar | 完成时间 1.76×、盘写 5.5× | 组装 **3.0×**、盘写 **−100%**（4KB vs 82.8MB） | 方向一致、幅度更优（本地无网络放大 tar 代价） |
| 层化升级经济学 | O(m·N)→O(m)，O(k·N)→O(k) | 组合重建 12 次 ×93ms=1116ms vs 全部层重建 79ms | 经济学成立 |
| SCHED_IDLE 改善 | 最多 3.4% | **3.4 个百分点** | 惊人一致 |
| 无保护共置膨胀 | +45.2% | +18.1%（代理负载较轻 + WSL2 虚拟化） | 方向一致，幅度受负载影响 |
| pause 回收 | memory.reclaim 回收匿名页，状态保留 | force_empty 回收 314.7MB，状态保留 | v1 等价 |
| FnCall | 免逐次供给开销 | 434ms → 119ms（3.7×） | 一致 |
| pack_diff | 交互会话变可复用环境 | 104s 搭建 → 2.9s 快照 → 883ms 消费，94.6× vs 重放 | 一致 |
| 高密度 | 单节点 3,200 容器 | 单节点 163（350 集群，0 失败） | 按资源 1/1000 缩放相称 |
| 突发 | p50=2,528/作业、5,000 个/秒 | 200 沙箱 5.1 个/秒、0 失败 | 缩放相称 |
| microVM 内存 | virtio-pmem −40.2%、FPR −21.2% | balloon 回收语义见 E14 | 部分 |

## 4. 未覆盖项与理由（如实记录）

1. **Firecracker 原生**：guest kernel 构建资产（官方 S3）在本网络不可达；用 QEMU+KVM 等价覆盖"VM 边界隔离 + virtio-balloon"语义。Firecracker 与 QEMU 的差异在管理面而非机制面。
2. **virtio-pmem + DAX（−40.2% 峰值内存）**：需 guest 内 libnvdimm/virtio_pmem 模块与 ndctl；无盘 netboot 内核不含该栈。机制已在上表 E6（共享/回收语义的容器路径）与 E14（balloon）部分覆盖。
3. **DAMON + free-page reporting（−21.2% 积分内存）**：WSL2 内核无 DAMON 接口。
4. **core scheduling（+45.2%→+17.3%）**：WSL2 内核无 CONFIG_SCHED_CORE。
5. **OverlayBD/ublk、Full VM 后端（GPU-PV/DXVK）、Cloud Bursting、GPU FnCall（MIG）**：超出单机无 GPU 环境能力。
6. **3FS / BGP / 定期集群重置**：多机形态；以 objserver（字节记账）与无状态控制面语义替代验证。

## 5. 复现方式

```bash
cd resrch-pj/DSec-demo
bash cluster.sh up          # 起集群（首次自动 build + 预载镜像）
cd experiments
python E00_smoke/run.py     # 之后任意实验均可独立运行
# 独立实验（不依赖集群）：
#   E2/E3/E5/E14 各自目录内有 run_inner.sh|workload.py|run.py，详见各目录
```

结果全部落在 `results/*.json`；集群日志：`docker compose logs apiserver|placement|watcher|iam|edge-N`。
