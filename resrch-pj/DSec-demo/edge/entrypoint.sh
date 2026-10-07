#!/bin/sh
# edge 节点入口：先起内部 dockerd（DSec 的节点运行时），就绪后拉起 edge agent
set -e

# 内部 dockerd 配置须在启动前写入：DNS 用公网解析（内部容器无法用宿主 embedded DNS 127.0.0.11）
mkdir -p /etc/docker /var/log
cat > /etc/docker/daemon.json <<'EOF'
{
  "dns": ["8.8.8.8", "114.114.114.114"],
  "features": { "buildkit": false },
  "live-restore": false
}
EOF

dockerd-entrypoint.sh >/var/log/dockerd.log 2>&1 &

echo "[edge] waiting for inner dockerd..."
i=0
until docker info >/dev/null 2>&1; do
  i=$((i+1))
  if [ $i -gt 180 ]; then
    echo "[edge] dockerd failed to start:"; tail -30 /var/log/dockerd.log
    exit 1
  fi
  sleep 1
done
echo "[edge] inner dockerd ready"

exec python3 /opt/dsec/edge_agent.py
