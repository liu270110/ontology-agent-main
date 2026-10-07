#!/bin/bash
# mini-DSec 集群一键起停：build → compose up → 预载沙箱镜像到各 edge → 健康检查
# 用法: ./cluster.sh up | down | load | status
set -e
cd "$(dirname "$0")"

EDGES="edge-1 edge-2 edge-3"
LOAD_IMAGES="dsec-sandbox-base:latest dsec-sandbox-debian:latest alpine:3.20 debian:bookworm-slim"

build() {
  echo "== 构建镜像 =="
  docker build -t dsec-control:latest ./controlplane
  docker build -t dsec-edge:latest ./edge
  docker build -t dsec-sandbox-base:latest ./images/sandbox-base
  docker build -t dsec-sandbox-debian:latest ./images/sandbox-debian
}

load() {
  echo "== 预载沙箱镜像到各 edge（stdin 重定向传输，绕开 Docker Desktop cp/管道问题）=="
  tmp=$(mktemp -d)
  for img in $LOAD_IMAGES; do
    fname=$(echo "$img" | tr ':/' '__').tar
    docker save -o "$tmp/$fname" "$img"
    for e in $EDGES; do
      if docker exec -i "$e" sh -c "cat > /tmp/load.tar" < "$tmp/$fname" \
         && docker exec "$e" sh -c "docker load -i /tmp/load.tar" >/dev/null 2>&1 \
         && docker exec "$e" rm -f /tmp/load.tar; then
        echo "  $img -> $e"
      else
        echo "  $img -> $e 失败"
      fi
    done
    rm -f "$tmp/$fname"
  done
  rmdir "$tmp"
}

wait_ready() {
  echo "== 等待集群就绪 =="
  for i in $(seq 1 60); do
    if curl -sf localhost:8000/healthz >/dev/null 2>&1; then break; fi
    sleep 2
  done
  for i in $(seq 1 120); do
    ok=1
    for e in $EDGES; do
      port=$([[ $e == edge-1 ]] && echo 8101 || { [[ $e == edge-2 ]] && echo 8102 || echo 8103; })
      curl -sf "localhost:$port/healthz" >/dev/null 2>&1 || ok=0
    done
    [ $ok -eq 1 ] && { echo "集群就绪"; return 0; }
    sleep 2
  done
  echo "集群未在超时内就绪"; return 1
}

case "$1" in
  build) build ;;
  load)  load ;;
  up)
    docker compose up -d
    wait_ready
    load
    echo "== 节点视图 =="
    curl -s localhost:8002/nodes | python -m json.tool | head -30
    ;;
  down) docker compose down -v ;;
  clean)
    for p in 8101 8102 8103; do curl -s -X POST "localhost:$p/admin/stop_all" >/dev/null; done
    docker compose restart iam >/dev/null 2>&1
    echo "已清空全部沙箱并重置 IAM"
    ;;
  status)
    curl -s localhost:8002/nodes | python -c "import json,sys; [print(n['edge_id'], 'healthy' if n['healthy'] else 'DOWN', 'running=%s'%n['running']) for n in json.load(sys.stdin)['nodes']]"
    ;;
  *) echo "usage: $0 up|down|load|status|build"; exit 1 ;;
esac
