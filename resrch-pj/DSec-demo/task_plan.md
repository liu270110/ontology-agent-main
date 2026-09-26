# DSec-demo 本地验证任务计划

> 目标：在 resrch-pj/DSec-demo/ 内，用 Docker 把《DeepSeek Elastic Compute (DSec)》论文的核心技术在本地全部验证一遍。
> 资源基线：22 逻辑核 / 31.5 GB 物理内存 / WSL2 内核 5.15.167.4 / Docker Desktop 29.4.3（VM 分到 ~16 GB）。
> 论文规模（160 节点/3 万核）→ 本地按 ~1/1000 缩放：3 台 DinD edge 节点 + 控制面容器。

## 本机能力探测结论（2026-09-26）

| 特性 | 状态 | 影响 |
| --- | --- | --- |
| Docker Hub 直拉 | ❌（size 校验失败） | 全部镜像走 docker.m.daocloud.io 拉取后 retag |
| EROFS 内核支持 | ✅ | E2 可用真 EROFS 挂载做层格式验证（mkfs.erofs+loop 挂载） |
| overlayfs | ✅ | E2 可组合层核心机制 |
| FUSE (/dev/fuse) | ✅ | E3 自研 lazyfs 按需加载可行 |
| /dev/kvm | ✅ | microVM 后端可真机验证（QEMU+KVM；Firecracker 视资源再试） |
| AppArmor | ❌ | E13 用 cap-drop（DAC_OVERRIDE）+ 文件属主等价验证，报告如实说明 |
| DAMON | ❌（5.15 无 sysfs 接口） | E6 只做容器路径（pause+memory.reclaim），DAMON 记录为不支持 |
| cgroup | 待确认 v2 | E6 依赖 memory.reclaim |
| 已有本地镜像 | debian:bookworm-slim, node:22-bookworm-slim | 直接可用 |

## 架构（mini-DSec）

```
SDK(libdsec 式) ──HTTP──▶ apiserver :8000（无每沙箱状态，sandbox_id 编码 edge）
                          ├──▶ iam :8003（多级项目+配额）
                          ├──▶ placement :8001（power-of-k，无持久状态+本地在途视图）
                          ├──▶ watcher :8002（周期采 edge 健康/负载）
                          └──▶ edge :810x（DinD 节点内 agent：本地准入→dockerd→运行时；TTL；快照；chronus=exec 封装）
存储替身：objserver :9000（模拟 3FS：HTTP 对象读 + 字节数记账，E3 用）
```

## 实验清单（编号=验收单元）

- [x] E0 环境/集群骨架：compose 起 4 控制面 + 3 edge(dind)，SDK 生命周期跑通
- [ ] E1 四类后端启动开销：FnCall 预热池 vs 冷容器 vs microVM(QEMU+KVM)
- [ ] E2 可组合环境层：EROFS/overlayfs 动态 lowerdir 组装 vs 单体构建 vs 启动时解 tar；O(m·N)→O(m) 经济学
- [ ] E3 按需镜像加载：元数据本地+数据按需(从 objserver) vs 全量拉取；字节数/耗时/盘写
- [ ] E4 高密度超分：单 edge 节点容器密度爬坡 + 资源占用
- [ ] E5 CPU QoS：BE 负载下 LS 时延膨胀：无保护 vs SCHED_IDLE vs +core scheduling
- [ ] E6 内存：docker pause + cgroup memory.reclaim 回收实测（DAMON 不支持，记录）
- [ ] E7 placement：power-of-k vs 随机 vs 全扫 仿真 + edge 准入拒绝改选集成测试
- [ ] E8 pack_diff：沙箱内搭建环境→增量快照→新沙箱还原 vs 重放重建
- [ ] E9 抢占恢复：命令日志重放，非幂等命令只执行一次
- [ ] E10 网络白名单：按域名/IP+端口 白名单（允许 pypi 禁 npm 形态），默认拒绝
- [ ] E11 IAM：多级项目嵌套、配额继承/越权拒绝、沙箱资源记账
- [ ] E12 突发创建：受限并发批量创建 200+，速率/延迟分布，watcher 负载均衡
- [ ] E13 沙箱内 root 受限：cap 修剪后 root 读不了受保护文件/socket（AppArmor 等价）
- [ ] 最终：docs/VERIFICATION.md 对照表（论文声明→实验→本地结果→差异说明）+ README

## 关键设计决定

- 沙箱基础镜像：debian:bookworm-slim + iptables + curl（自建 dsec-sandbox-base:latest，save/load 进 edge）
- 控制面统一镜像 dsec-control:latest（python:3.12-slim + fastapi/uvicorn/httpx，pip 走清华源）
- edge 镜像 dsec-edge:latest（docker:27-dind + python3 + edge-agent，entrypoint 先 dockerd 后 agent）
- E2 的 runc 直启 bundle 验证 overlay 组装根（docker run 不支持自定义 lowerdir，论文是 30 行 dockerd patch，demo 用 runc 等价复现）
- E3 的"3FS"用 objserver（记录 serve 字节数）+ lazyfs(Python FUSE, 本地 chunk 缓存+元数据本地) 对比全量 rsync
- microVM：QEMU(-enable-kvm) + virtio-pmem/balloon 做内存共享与回收对比；Firecracker 若 GitHub 资产可下则补
- 所有实验结果 JSON 落 results/，报告从 JSON 生成

## 进度日志（append-only）

- 2026-09-26 20:30 环境探测完成，镜像经 daocloud 拉取成功（python:3.12-slim / docker:27-dind / alpine:3.20）
- 2026-09-26 20:50 关键结论补充：
  - docker:27-dind 镜像自带 DOCKER_HOST=tcp://docker:2375，进容器必须覆盖 -e DOCKER_HOST=unix:///var/run/docker.sock
  - DinD 内 dockerd 正常：overlay2 + cgroup v1，内部容器可运行（volume 挂 /var/lib/docker）
  - 宿主 hybrid cgroup：v1 memory 层在（memory.force_empty 可用），v2 无 memory 控制器 → E6 走 v1 force_empty + docker pause
  - SCHED_CORE prctl 返回 EINVAL → 内核无 CONFIG_SCHED_CORE，E5 只做 无保护 vs SCHED_IDLE（论文亦给出该档 ≈+3.4%）
  - DinD 内工具链齐：mkfs.erofs 1.8.2 / runc 1.2.4 / fuse3 ✅
  - microVM 降级方案：QEMU(-enable-kvm)+alpine linux-virt 内核；virtio-pmem 尽力，balloon 经 monitor balloon 命令做回收演示；排在所有集群实验之后
- 2026-09-26 21~23 实验全部跑通，过程中修复的平台 bug（均已固化到代码）：
  - Git Bash 路径转换：`/tmp/...` 参数会被转成 Windows 路径 → MSYS_NO_PATHCONV=1 或包进 sh -c 字符串
  - docker save | docker exec 管道在 Git Bash 不可靠；docker cp 到 DinD 容器成功但文件不可见 → stdin 重定向 `docker exec -i c sh -c "cat > f" < file` 可靠
  - placement 在途视图 TTL 必须略大于心跳周期（5s vs 2s），否则与 watcher 双重记账虚占容量
  - CPU 申请额是软限制（超分语义）：edge 准入与 placement 过滤都不按 CPU 硬卡，只卡内存+容器数
  - watcher/iam/placement 端口要发布到宿主（实验驱动直连）；watcher 失忆靠 edge 心跳 404 重注册自愈
  - pack_diff env_id 必须编码源沙箱（可路由回源 edge）；自定义名只作别名 tag
  - apiserver 出站 httpx 超时 30s 不够 shell 里 apt/编译用 → 600s
  - debian 沙箱镜像切清华源（apt 否则超时）；alpine 沙箱补 python3
  - overlay upperdir 不能落在 overlayfs（DinD 容器根）上 → E2 用 tmpfs 承载 upper/work
  - E2 runc spec 的 namespaces 必须是对象数组
- 2026-09-26 23:15 E0~E13 共 13 项全部 PASS；E14（QEMU+KVM microVM）后台运行中
- 2026-09-26 23:20 VERIFICATION.md / README.md / run_all.sh 完成
