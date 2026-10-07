#!/usr/bin/env bash
# deploy/up.sh — 本平台基础服务一键部署入口（2026-09-27）
#
# 用法（仓库根目录或任意位置）：
#   sh deploy/up.sh          # lite 档三存储：PG(pgvector)+Redis+MinIO（含 bucket 初始化）
#   sh deploy/up.sh --gpu    # 追加 GPU 推理体：vLLM(Qwen3-4B-AWQ)@18001 + TEI(bge-m3)@18002
#
# 做的事（全部幂等，可反复执行）：
#   1. .env 缺失时从 .env.example 引导（提示补密钥）
#   2. 镜像缺失时经华为 SWR 转存拉取后打回标准名（本机 daemon 加速器全废、Hub 直连被墙；
#      海外机器会命中直拉分支，行为一致）
#   3. compose 起 lite 档（base=deploy/docker-compose.yml + overlay=deploy/docker-compose.local.yml）
#   4. 等待三存储 healthcheck 全绿；bucket 由 overlay 的 minio-init 服务幂等创建
#   5. --gpu：确保 vLLM pinned 镜像与 oa-models/ 模型齐备（缺则 ModelScope/hf-mirror 直链下载），
#      docker/ GPU 栈按 base+local 两层 compose 起（端口 18001/18002=本机避让 8001/8002）
#
# 注意：
#   - 端口约定：PG 5432 / Redis 6379 / MinIO 9000,9001 / vLLM 18001 / TEI 18002
#     （本机 8000~8003 被另一套容器栈占用，勿改回；见 docker/docker-compose.gpu.local.yml 头注）
#   - .env 模型渠道切换：本地 vLLM=OA_LLM_BASE_URL=http://127.0.0.1:18001/v1 + OA_LLM_MODEL=local-main
#   - 生产/海外环境把 IMAGE_MIRROR 置空即可走 Docker Hub 官方源

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
IMAGE_MIRROR="${IMAGE_MIRROR:-swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io}"

# ── 1. .env 引导 ──────────────────────────────────────────────
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[env] .env 不存在，已从 .env.example 生成；请编辑补 OA_LLM_API_KEY（云渠道）后重跑"
  exit 1
fi
echo "[env] .env OK"

# ── 2. 镜像保障：缺失时 SWR 转存拉取并打回标准名 ──────────────
ensure_image() { # $1=canonical  $2..=fallback tags(按顺序试)
  local img="$1"; shift
  docker image inspect "$img" >/dev/null 2>&1 && { echo "[img] $img 已就绪"; return 0; }
  for ref in "$@"; do
    echo "[img] 拉取 $ref"
    if docker pull "$ref" 2>/dev/null; then docker tag "$ref" "$img"; echo "[img] $img ✓"; return 0; fi
  done
  if docker pull "$img" 2>/dev/null; then echo "[img] $img ✓(直拉)"; return 0; fi
  echo "[img] ✗ $img 全通道失败"; return 1
}

MINIO_TAG=RELEASE.2024-10-13T13-34-11Z
VLLM_TAG=v0.10.1.1
ensure_image "pgvector/pgvector:pg16"        "$IMAGE_MIRROR/pgvector/pgvector:pg16"
ensure_image "redis:7-alpine"                "$IMAGE_MIRROR/library/redis:7-alpine"
ensure_image "minio/minio:$MINIO_TAG"        "$IMAGE_MIRROR/minio/minio:$MINIO_TAG"
ensure_image "minio/mc:latest"               "$IMAGE_MIRROR/minio/mc:latest"

# ── 3. 起 lite 档 ─────────────────────────────────────────────
echo "[up] compose 起 lite 档 ..."
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml \
  --env-file .env up -d --remove-orphans

# ── 4. 等待三存储健康 ─────────────────────────────────────────
wait_healthy() { # $1=container  $2=秒数
  local c="$1" deadline=$(( $(date +%s) + $2 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    s=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)
    [ "$s" = "healthy" ] && { echo "[ok] $c healthy"; return 0; }
    sleep 5
  done
  echo "[✗] $c 健康等待超时"; docker logs "$c" --tail 20 2>&1 | tail -8; return 1
}
wait_healthy onto-agent-postgres-1 180
wait_healthy onto-agent-redis-1 60
wait_healthy onto-agent-minio-1 60
wait_healthy onto-agent-minio-init-1 60 || true

# ── 5. GPU 推理体（可选） ─────────────────────────────────────
if [ "${1:-}" = "--gpu" ]; then
  ensure_image "vllm/vllm-openai:v$VLLM_TAG" "$IMAGE_MIRROR/vllm/vllm-openai:$VLLM_TAG" || true
  docker tag "vllm/vllm-openai:v$VLLM_TAG" "vllm/vllm-openai:pinned" 2>/dev/null || true

  MODELS="E:/learn-agnet-project/oa-models"   # 与 docker/docker-compose.gpu.local.yml 挂载一致
  qwen_dl() { # ModelScope 直链（阿里 CDN）；断点续传走 curl -C -
    mkdir -p "$MODELS/qwen3-4b-awq"
    for f in config.json generation_config.json merges.txt tokenizer.json tokenizer_config.json vocab.json model.safetensors; do
      [ -s "$MODELS/qwen3-4b-awq/$f" ] || curl -fL --retry 3 -C - -o "$MODELS/qwen3-4b-awq/$f" \
        "https://modelscope.cn/models/Qwen/Qwen3-4B-AWQ/resolve/master/$f"
    done
  }
  bge_dl() { # hf-mirror 经典 LFS 直链
    mkdir -p "$MODELS/bge-m3"
    for f in config.json pytorch_model.bin sentencepiece.bpe.model tokenizer.json tokenizer_config.json special_tokens_map.json; do
      [ -s "$MODELS/bge-m3/$f" ] || curl -fL --retry 3 -C - -o "$MODELS/bge-m3/$f" \
        "https://hf-mirror.com/BAAI/bge-m3/resolve/main/$f"
    done
  }
  [ -s "$MODELS/qwen3-4b-awq/model.safetensors" ] && [ -s "$MODELS/qwen3-4b-awq/config.json" ] \
    && echo "[model] qwen3-4b-awq 已就绪" || { echo "[model] 下载 Qwen3-4B-AWQ (~2.7G) ..."; qwen_dl; }
  [ -s "$MODELS/bge-m3/pytorch_model.bin" ] && [ -s "$MODELS/bge-m3/config.json" ] \
    && echo "[model] bge-m3 已就绪" || { echo "[model] 下载 bge-m3 (~2.3G) ..."; bge_dl; }

  echo "[up] compose 起 GPU 栈（vllm 首次加载权重约 3~6 分钟）..."
  docker compose -f docker/docker-compose.gpu.yml -f docker/docker-compose.gpu.local.yml up -d
  wait_healthy oa-tei-embed 420
  wait_healthy oa-vllm 900 || echo "[!] vllm 未按时转绿：docker logs oa-vllm 查看加载进度后重试"
fi

# ── 6. 汇总 ──────────────────────────────────────────────────
echo
echo "=========== 就绪状态 ==========="
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'onto-agent|oa-' || true
echo " PG: localhost:5432  Redis: localhost:6379  MinIO: localhost:9000(Console 9001)"
[ "${1:-}" = "--gpu" ] && echo " vLLM: http://127.0.0.1:18001/v1 (model=local-main)  TEI: http://127.0.0.1:18002"
echo " 一键停止：docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml down"
echo "================================"
