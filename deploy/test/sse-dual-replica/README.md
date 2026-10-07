# deploy/test/sse-dual-replica — 批次 B-① 双副本 SSE 压测（脚本与编排）

> 对应：[docs/architecture/评审-2026-09-28-M0-M3阶段验收审核.md](../../../docs/architecture/评审-2026-09-28-M0-M3阶段验收审核.md) §4 批次 B-①「真双进程 SSE 压测（compose 起两网关副本 + 断线重连脚本）」，M3 出口条件（[docs/api/02](../../../docs/api/02-SSE事件流协议.md) §5：双副本下 Last-Event-ID 跨副本断线重连正确、事件不重不漏）。
> 本目录为**可执行资产**：compose override 起两个无状态网关副本（gateway-a :8364 / gateway-b :8365，共享同一 Redis Stream / PG），压测脚本实测 api/02 §4/§5 四个判定。**判定结论须在真实 Docker 环境执行后回填**评审文档批次 B-① 行。

## 文件

| 文件 | 作用 |
| ---- | ---- |
| `docker-compose.sse-dual.yml` | compose override：在 lite 档三存储之上追加两个网关副本（build 本目录 `Dockerfile.gateway`），同网段共享 Redis/PG，端口 8364/8365 错开 |
| `Dockerfile.gateway` | 压测专用网关镜像（python:3.11-slim + requirements.txt + services；仓库暂无正式网关 Dockerfile，本镜像不作生产用途） |
| `test_sse_dual_replica.py` | 压测/验证脚本（仅标准库 + httpx + redis），四个判定各输出 PASS/FAIL，任一失败退出码非零 |

## 一、前置条件

1. **Docker 与 compose 可用**（本仓库镜像加速现状见 `deploy/up.sh` 头注：加速器失效时镜像经华为 SWR 转存；`python:3.11-slim` 如直拉失败可参照 `ensure_image` 手法打回标准名）。
2. **起栈**（仓库根目录；base=lite 档 PG+Redis+MinIO，override 追加两副本）：

   ```bash
   docker compose -f deploy/docker-compose.yml -f deploy/test/sse-dual-replica/docker-compose.sse-dual.yml build
   docker compose -f deploy/docker-compose.yml -f deploy/test/sse-dual-replica/docker-compose.sse-dual.yml up -d
   docker compose -f deploy/docker-compose.yml -f deploy/test/sse-dual-replica/docker-compose.sse-dual.yml ps   # 等 gateway-a/gateway-b healthy
   ```

3. **PG 迁移到 head**（两副本共享业务库；首次执行一次即可，可在宿主机跑，也可借镜像跑）：

   ```bash
   # 宿主机（.env 缺省值即可，仓库根执行）：alembic -c services/alembic.ini upgrade head
   # 或借压测镜像（与 compose 同 env）：
   docker compose -f deploy/docker-compose.yml -f deploy/test/sse-dual-replica/docker-compose.sse-dual.yml \
     run --rm gateway-a alembic -c services/alembic.ini upgrade head
   ```

4. **测试账号与令牌**（api/02 §6：SSE 与 REST 同一 JWT；脚本内自动走登录通道）：
   - 种子管理员（`m1_seed_roles_and_admin` 迁移）：邮箱 `admin@local`，占位口令 `ChangeMe@FirstLogin`。
   - ⚠ **已知缺口（2026-09-28 实勘）**：种子角色 scopes **不含 `session:write`/`session:chat`**，直连环境登录后无法创建会话/收发 SSE（tests 内靠依赖覆盖绕过，真环境会 403+2001）。执行前一次性授权后**重新登录**（scopes 在登录时写入 claims）：

     ```bash
     docker compose -f deploy/docker-compose.yml -f deploy/test/sse-dual-replica/docker-compose.sse-dual.yml \
       exec postgres psql -U onto -d onto -c \
       "UPDATE roles SET scopes = (SELECT array_agg(DISTINCT s) FROM unnest(scopes || ARRAY['session:write','session:chat']::text[]) AS s) WHERE code = 'admin';"
     ```

   - 备选（免授权步）：脚本加 `--jwt-secret dev-only-change-me` 由脚本直签含 `session:write/session:chat` 的访问令牌（HS256，claims 同 `services/platform/security.py build_claims`，身份默认种子 admin/默认租户）。
5. **模型渠道不必配置**：无 OA_LLM_* 时对话流以 `RUN_ERROR(5002)` 收敛——`RUN_STARTED`/`RETRIEVAL_EVIDENCE`/`RUN_ERROR` ≥3 条主干波事件照常经 hub 写入 Redis Stream，四判定只认 seq 连续性与帧契约，不依赖外部 LLM。
6. 本机依赖：Python ≥3.11 + `httpx` + `redis`（pyproject dependencies 已含，无需新增安装）。

## 二、执行

```bash
# 仓库根目录；默认判定③走 XTRIM 等效裁剪（秒级）
python deploy/test/sse-dual-replica/test_sse_dual_replica.py

# 常用参数
#   --gateway-a / --gateway-b   副本基址（默认 http://127.0.0.1:8364 / :8365）
#   --redis-url                 判定③裁剪与清理用（默认 redis://127.0.0.1:6379/0）
#   --email / --password        登录凭据（默认种子 admin）
#   --jwt-secret                直签兜底（见前置 4）
#   --heartbeat-s               预期心跳周期（=OA_SSE_HEARTBEAT_SECONDS，默认 15）
#   --full-window               判定③改走真实 MAXLEN 1000 灌满裁剪（分钟级压测口径）
```

退出码：`0`=四判定全 PASS；`1`=任一 FAIL；`2`=前置失败（副本不可达 / scope 不足——脚本会打印授权 SQL 提示）。

## 三、四个判定与预期输出

| # | 判定（api/02 契约） | 实测方式 | PASS 输出样例 |
| ---- | ---- | ---- | ---- |
| ① | 跨副本订阅（§5：写入与推送分离、网关无状态） | 先订阅 B 副本 `GET /sessions/{id}/events`，再向 A 副本 `POST /sessions/{id}/messages`（Accept: text/event-stream）；断言 B 收到与 A 侧完全相同的 seq 序列与事件名 | `[PASS] ① 跨副本订阅: A 侧 seq=[1, 2, 3] == B 副本实时收到 seq=[1, 2, 3]（同一序列，不重不漏）` |
| ② | 断线重连（§4：Last-Event-ID 头主通道 + `?last_event_id=` 兜底） | A 在线订阅收至断点 → 断开 → 断线期间继续产生事件 → 带 `Last-Event-ID` 重连 B：回放恰为断点之后事件；query 兜底同位点重放结果一致；同一重连连接上继续收到实时事件（无缝衔接） | `[PASS] ② 断线重连: 断点 last_event_id=N：A 断 → B 重连回放 [..] 不重不漏；query 兜底同结果；同连接续收实时 [..]` |
| ③ | 回放窗口（§4：超窗 → 4301 SSE_REPLAY_EXPIRED，HTTP 建议 410） | 默认：Redis `XTRIM` 只留最新一条（网关侧重连走同一 XRANGE 快照缺口判定代码路径，`services/gateway/sse/redis_hub.py subscribe`）→ 带过期 id 重连期望 HTTP 410 + body code 4301；窗内（最新 seq）订阅仍 200 对照。`--full-window`：真实 MAXLEN 1000 灌满裁剪后同断言 | `[PASS] ③ 回放窗口: XTRIM 等效裁剪（快照缺口判定同路径）：last_event_id=N < 窗口 oldest-1=M → HTTP 410 + code 4301；窗内订阅 200` |
| ④ | 心跳保活（§2：15s 注释帧，不计入事件序列） | 对空闲会话订阅 ≥2 个心跳周期，断言收到 `: ping` 注释帧且零事件帧 | `[PASS] ④ 心跳保活: 空闲 30s 收到 2 条注释帧「: ping」（间隔≈15s，不计入事件序列）；事件帧数=0` |

收尾打印 `结论：4/4 PASS`；脚本自动清理本次会话的 `sse:stream:{sid}` / `sse:seq:{sid}` 键（会话与 agent 行留作压测留痕，Stream TTL 10min 自然过期）。

## 四、结果回填

- **主回填**：[docs/architecture/评审-2026-09-28-M0-M3阶段验收审核.md](../../../docs/architecture/评审-2026-09-28-M0-M3阶段验收审核.md) §2「SSE 多副本验证通过」行与 §4 批次 B-① 行——补记执行日期、四判定结果、脚本版本。
- **连带义务**（api/02 §9 已登记待办，压测通过后一并裁决回填）：心跳 15s / 回放窗口 MAXLEN 1000·TTL 10min 两项建议值冻结；「双副本压测执行」待办勾销；若改用非 410 的 HTTP 映射需同步 §7 与 01 篇 §4.3。
- 产物归档建议：脚本 stdout 全文贴入评审文档批次 B-① 条目下（含环境：镜像 tag、compose ps 输出摘要）。

## 五、边界与遗留（只能在真实环境验证的项）

- 本目录交付时**未实跑**（编写环境无完整 Docker 栈拉起条件）：已验证 `python -m py_compile` 与 `docker compose config` 通过；实跑时若 `Dockerfile.gateway` 构建受网络限制，按前置 1 处理镜像。
- 判定③默认为 XTRIM 等效口径（判定逻辑同一代码路径）；**真实 MAXLEN 1000 自然裁剪**须 `--full-window` 在真实环境执行（预计 3~10 分钟，约 350~500 轮无 LLM 对话）。
- 心跳 15s 判定依赖 `OA_SSE_HEARTBEAT_SECONDS` 默认值；若运维改小该值，脚本须以 `--heartbeat-s` 同步。
- 并发压测上限（02 §6：SSE 20 流/用户、429+2005）不在本脚本四判定内；如需加压另起专项。
