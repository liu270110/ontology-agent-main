#!/usr/bin/env bash
# install.sh —— ontology-agent 原生安装器（Linux/macOS，bash≥4）
# 设计权威：docs/ops/04-多平台安装部署与CLI执行设计.md §3.1（八步）+ §3.3（--skip-api 混合开发态）
# Windows 对应 install.ps1（语义逐条对齐，验收单同一张，对齐声明见 deploy/README.md）。
#
# 用法：
#   bash install.sh                                  # 交互三问（配置档落位）
#   bash install.sh --skip-api                       # 混合开发态：只装存储与 CLI，后端自起（§3.3）
#   bash install.sh --profile lite --llm-channel cloud --embed-channel ollama   # 非交互
# 八步：①前置自检 ②uv 自举 ③uv sync --frozen ④存储起底（复用探测）⑤迁移
#       ⑥配置档落位（三问合成 .env；已有则备份 .env.bak-<ts>+diff 提示，绝不覆盖）
#       ⑦oadm doctor 自检门禁 ⑧打印摘要
# 幂等：重复运行=升级语义（§6 验收 3）——备份 .env/迁移前进/卷保留，不重装不丢数据。
#
# 可 source 形态：全部逻辑在 oa_* 函数内、无顶层副作用；测试可 `source install.sh` 后直调
# （tests/cli/test_install_scripts.py）；行尾必须 LF（.gitattributes 锁定）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN_DISK_KB=2097152            # §3.1 磁盘 ≥2G（df -Pk 的 1K 块单位）
PREFLIGHT_PORTS="8000:api 5432:postgres 6379:redis 9000:minio"   # §3.1 端口口径
OA_PY_EXE=""                   # oa_py_ok 选中的解释器（版本回显须与判定同源，ocr 评审 #8）
# set -e 纪律：独立 `[ ] && cmd` 列表条件为假即整条失败会杀脚本——一律用 if 包裹（本文件铁律）

oa_log() { printf '[install] %s\n' "$*"; }
oa_die() { printf '[install] ✗ %s\n' "$*" >&2; exit 1; }

# ── 步骤 1：前置自检（§3.1：python ≥3.11 / 磁盘 ≥2G / 端口占用；失败打印修复指引并退出）──

oa_py_ok() { # 0=存在 ≥3.11 的 python3/python；选中的解释器写入全局 OA_PY_EXE
  local py
  for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
      if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        OA_PY_EXE="$py"
        return 0
      fi
    fi
  done
  return 1
}

oa_py_version() { "$OA_PY_EXE" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'; }

oa_disk_free_kb() { df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $4}'; }

oa_port_busy() { # $1=port → 0=有进程监听（/dev/tcp 探测，bash 内建零依赖）
  if (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then return 0; fi
  return 1
}

oa_container_running() { # $1=piece(api|postgres|redis|minio) → 0=本平台对应容器 healthy 在场
  local out
  out="$(docker ps --filter "name=onto-agent-$1" --format '{{.Status}}' 2>/dev/null || true)"
  printf '%s' "$out" | grep -q 'healthy'
}

oa_port_conflicted() { # $1=port $2=piece → 0=被「非本平台容器」占用（预检红）
  oa_port_busy "$1" && ! oa_container_running "$2"
}

oa_preflight() { # $1=skip_api(0|1)；--skip-api（混合开发态）不查 8000
  oa_log "步骤1 前置自检（python/磁盘/端口）..."
  if ! oa_py_ok; then
    oa_die "Python ≥3.11 未满足：安装 Python 3.11+（python.org / 系统包管理器 / uv python install 3.11）后重跑"
  fi
  oa_log "  python $(oa_py_version) ✓"
  local free
  free="$(oa_disk_free_kb "$ROOT" || true)"
  if [ -z "$free" ] || [ "$free" -lt "$MIN_DISK_KB" ]; then
    oa_die "磁盘可用不足 2G（当前 ${free:-未知}K）：清理空间或将仓库迁移到充足磁盘后重跑"
  fi
  oa_log "  磁盘可用 ${free}K ✓"
  local item port piece
  for item in $PREFLIGHT_PORTS; do
    port="${item%%:*}"; piece="${item##*:}"
    if [ "$1" = "1" ] && [ "$port" = "8000" ]; then continue; fi
    if oa_port_conflicted "$port" "$piece"; then
      oa_die "端口 $port 被外部进程占用（非本平台容器）：停用占用进程，或同步改 deploy/docker-compose*.yml 端口映射与 OA_* 连接配置后重跑"
    fi
    if oa_port_busy "$port"; then
      oa_log "  端口 $port 由本平台 $piece 容器占用（healthy）——幂等重装，让行"
    else
      oa_log "  端口 $port 空闲 ✓"
    fi
  done
}

# ── 步骤 2：uv 自举（§3.1：官方脚本，隔离用户目录，不污染系统）──

oa_bootstrap_uv() {
  if command -v uv >/dev/null 2>&1; then
    oa_log "步骤2 uv 已在场：$(command -v uv)"
    return 0
  fi
  oa_log "步骤2 未检测到 uv，官方脚本自举..."
  # set -e + pipefail 下 curl 失败会静默杀脚本（ocr 评审 #10：与 install.ps1 对齐显式报错）
  if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
    oa_die "uv 自举脚本下载/执行失败：检查网络后重跑，或手动安装 https://docs.astral.sh/uv/getting-started/installation/"
  fi
  if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; fi
  command -v uv >/dev/null 2>&1 || oa_die "uv 自举失败：请按 https://docs.astral.sh/uv/getting-started/installation/ 手动安装后重跑"
  oa_log "  uv 自举完成：$(command -v uv)"
}

# ── 步骤 3：依赖装配（§3.1：uv sync --frozen，lock 忠实）──

oa_sync_deps() {
  oa_log "步骤3 依赖装配：uv sync --frozen..."
  if ! (cd "$ROOT" && uv sync --frozen); then
    oa_die "uv sync 失败：离线/内网环境先配置镜像（export UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple）后重跑"
  fi
}

# ── 步骤 4：存储起底（§3.1：探测既有栈→复用；无则 compose 起底）──

oa_storage_healthy() { # 三件存储容器全 healthy → 0
  oa_container_running postgres && oa_container_running redis && oa_container_running minio
}

oa_ensure_storage() {
  if oa_storage_healthy; then
    oa_log "步骤4 检测到既有 onto-agent 存储栈（healthy）——复用不重建（幂等，§6 验收 3）"
    return 0
  fi
  oa_log "步骤4 起存储栈（PG(pgvector)/Redis/MinIO：deploy/docker-compose.yml + local overlay）..."
  if ! (cd "$ROOT" && docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up -d --remove-orphans); then
    oa_die "存储栈启动失败：核对 docker 可用；国内镜像受限可先 sh deploy/up.sh（华为 SWR 转存）再重跑（两路径幂等）"
  fi
  oa_log "  等待存储 healthcheck 转绿（有界 180s）..."
  local i
  for i in $(seq 1 36); do
    if oa_storage_healthy; then
      oa_log "  存储栈就绪 ✓（PG 5432 / Redis 6379 / MinIO 9000,9001）"
      return 0
    fi
    sleep 5
  done
  oa_die "存储栈未按时转绿：docker ps / docker logs onto-agent-postgres-1 排查后重跑（幂等，不重复造卷）"
}

# ── 步骤 5：迁移（§3.1：alembic upgrade heads；失败打印 PG 连通诊断）──

oa_migrate() {
  oa_log "步骤5 迁移：alembic upgrade heads..."
  if ! (cd "$ROOT" && uv run alembic -c services/alembic.ini upgrade heads); then
    oa_die "迁移失败：多为 PG 连不通——docker ps 看 onto-agent-postgres-1 是否 healthy；核对 .env 的 OA_PG_*（宿主端口 5432）后重跑（upgrade heads 幂等可重入）"
  fi
}

# ── 步骤 6：配置档落位（§3.1 三问合成 .env；已有则备份+diff 提示，绝不覆盖，§4 首配不覆盖）──

oa_env_synth() { # $1=profile(lite|full|full-cad) $2=llm(local|cloud|none) $3=embed(tei|ollama|none) → stdout
  local tmpl="$ROOT/.env.$1"
  if [ ! -f "$tmpl" ]; then
    printf '模板缺失：%s\n' "$tmpl" >&2
    return 1
  fi
  cat "$tmpl"
  printf '\n# ===== 安装器合成段（第 6 步三问结果；追加在文件末尾）=====\n'
  case "$2" in
    local) printf 'OA_LLM_BASE_URL=http://127.0.0.1:18001/v1\nOA_LLM_MODEL=local-main\n' ;;
    cloud) printf 'OA_LLM_BASE_URL=https://api.deepseek.com\nOA_LLM_MODEL=deepseek-chat\n# 【改我】填入云渠道令牌（密钥形制，绝不入库）\n# OA_LLM_API_KEY=\n' ;;
    none) printf '# 模型渠道暂不配置：未设 OA_LLM_BASE_URL=LLM 关闭（后续手编 .env）\n' ;;
  esac
  case "$3" in
    tei) printf 'OA_OLLAMA_BASE_URL=http://127.0.0.1:18002\nOA_EMBED_PROTOCOL=tei\n' ;;
    ollama) printf 'OA_OLLAMA_BASE_URL=http://localhost:11434\nOA_EMBED_PROTOCOL=ollama\n' ;;
    none) printf '# 嵌入暂不配置（不可用自动降级；memory_vector_enabled 默认 false）\n' ;;
  esac
}

oa_redact() { # diff 输出脱敏（ocr 评审 #9：重装路径 diff 会回显用户已填的真实密钥值）
  sed -E 's/^([+-][A-Za-z0-9_]*(KEY|PASSWORD|SECRET|TOKEN|key|password|secret|token)[A-Za-z0-9_]*)=.+/\1=******（已脱敏）/I'
}

oa_env_place() { # $1=root $2=合成内容文件 → 0=已写入新 .env；2=已有 .env（备份后保留原档）
  local ts
  if [ -f "$1/.env" ]; then
    ts="$(date +%Y%m%d%H%M%S)"
    cp "$1/.env" "$1/.env.bak-$ts" || return 1
    printf '[install] 检测到既有 .env：已备份为 .env.bak-%s；原档保留不覆盖（ops/04 §4 首配不覆盖）\n' "$ts"
    printf '[install] 合成档与现有档 diff（密钥值脱敏；自行取舍后手编 .env）：\n'
    diff -u "$1/.env" "$2" | oa_redact || true
    return 2
  fi
  cp "$2" "$1/.env"
  printf '[install] 已写入 %s/.env\n' "$1"
  return 0
}

oa_ask() { # $1=提示 $2=默认 → REPLY_VAL（提示走 stderr，答案走 stdout 管道不受污染）
  local in_=""
  printf '%s（默认 %s）: ' "$1" "$2" >&2
  IFS= read -r in_ || in_="$2"
  REPLY_VAL="${in_:-$2}"
}

oa_validate_choice() { # $1=value $2=label $3...=allowed
  local value="$1" label="$2" a
  shift 2
  for a in "$@"; do
    if [ "$value" = "$a" ]; then return 0; fi
  done
  oa_die "$label 取值非法：$value（允许：$*）"
}

# ── 步骤 7：自检门禁（§3.1：oadm doctor 全绿才过；与安装器同源实现=同一 CLI）──

oa_doctor() {
  oa_log "步骤7 自检：oadm doctor（八项，ops/04 §3.1 第 7 步同源）..."
  if ! (cd "$ROOT" && uv run python -m services.cli.oadm doctor); then
    oa_die "oadm doctor 存在红项：按各项修复指引处理后重跑（幂等）"
  fi
}

# ── 步骤 8：打印摘要（§3.1：访问地址 / 种子账号 / 下一步三条命令）──

oa_summary() { # $1=profile $2=skip_api(0|1)
  cat <<EOF

================ 安装完成（档位：$1）================
访问地址：http://localhost:8000（api 就绪后；OpenAPI 文档 /docs）
种子账号：admin@local（初始占位密码 ChangeMe@FirstLogin——首登后立即改密；值为迁移内公开占位，非凭据）
配置档：$1 → 仓库根 .env（模板=.env.lite/.env.full/.env.full-cad，ops/04 §4）
EOF
  if [ "$2" = "1" ]; then
    printf '混合开发态（--skip-api）：存储与 CLI 已就绪；后端自起：uv run python -m services.main\n'
  else
    printf '起全栈：docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml -f deploy/docker-compose.full.yml --profile full up -d\n'
  fi
  cat <<'EOF'
下一步三条命令：
  bin/oadm doctor                # 安装自检八项（全绿=安装门禁）
  bin/oadm status                # 全件健康矩阵（--output json 供 CI 消费）
  bin/oadm up --profile lite     # 幂等起栈（full/full-cad 同形，§5）
==================================================
EOF
}

# ── 主流程（仅直接执行时运行；source 本文件只获得函数库）──

main() {
  local SKIP_API=0 PROFILE="" LLM_CH="" EMBED_CH="" synth tmp place_rc
  while [ $# -gt 0 ]; do
    case "$1" in
      --skip-api) SKIP_API=1 ;;
      --profile)
        if [ $# -lt 2 ]; then oa_die "--profile 需要取值（lite|full|full-cad）"; fi
        PROFILE="$2"; shift
        ;;
      --llm-channel)
        if [ $# -lt 2 ]; then oa_die "--llm-channel 需要取值（local|cloud|none）"; fi
        LLM_CH="$2"; shift
        ;;
      --embed-channel)
        if [ $# -lt 2 ]; then oa_die "--embed-channel 需要取值（tei|ollama|none）"; fi
        EMBED_CH="$2"; shift
        ;;
      *) oa_die "未知参数：$1（可用：--skip-api --profile <lite|full|full-cad> --llm-channel <local|cloud|none> --embed-channel <tei|ollama|none>）" ;;
    esac
    shift
  done

  cd "$ROOT"
  oa_log "ontology-agent 安装开始（root=$ROOT，skip_api=$SKIP_API）——docs/ops/04 §3.1 八步"

  oa_preflight "$SKIP_API"         # 步骤 1
  oa_bootstrap_uv                  # 步骤 2
  oa_sync_deps                     # 步骤 3
  oa_ensure_storage                # 步骤 4
  oa_migrate                       # 步骤 5

  # 步骤 6：三问（flags 缺省时交互；非交互环境回退安全默认 lite/none/none）
  if [ -z "$PROFILE" ] || [ -z "$LLM_CH" ] || [ -z "$EMBED_CH" ]; then
    if [ -t 0 ]; then
      oa_ask "配置档位 lite|full|full-cad" "lite"
      if [ -z "$PROFILE" ]; then PROFILE="$REPLY_VAL"; fi
      oa_ask "LLM 渠道 local(vllm)|cloud(DeepSeek)|none" "cloud"
      if [ -z "$LLM_CH" ]; then LLM_CH="$REPLY_VAL"; fi
      oa_ask "嵌入渠道 tei|ollama|none" "ollama"
      if [ -z "$EMBED_CH" ]; then EMBED_CH="$REPLY_VAL"; fi
    else
      : "${PROFILE:=lite}"; : "${LLM_CH:=none}"; : "${EMBED_CH:=none}"
    fi
  fi
  oa_validate_choice "$PROFILE" "档位" lite full full-cad
  oa_validate_choice "$LLM_CH" "LLM 渠道" local cloud none
  oa_validate_choice "$EMBED_CH" "嵌入渠道" tei ollama none

  oa_log "步骤6 配置档落位（profile=$PROFILE llm=$LLM_CH embed=$EMBED_CH）..."
  synth="$(oa_env_synth "$PROFILE" "$LLM_CH" "$EMBED_CH")" || oa_die "配置合成失败（模板缺失？）"
  tmp="$(mktemp)"
  printf '%s\n' "$synth" > "$tmp"
  # oa_env_place 返回值：0=新写入；2=已有 .env（备份+diff 提示后保留原档，属正常路径）；1=异常
  place_rc=0
  oa_env_place "$ROOT" "$tmp" || place_rc=$?
  if [ "$place_rc" = "1" ]; then oa_die "配置档落位失败（备份/写入异常）"; fi
  rm -f "$tmp"

  oa_doctor                        # 步骤 7
  oa_summary "$PROFILE" "$SKIP_API"  # 步骤 8
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
