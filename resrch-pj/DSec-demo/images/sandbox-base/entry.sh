#!/bin/sh
# 沙箱入口：BE 类沙箱整体置于 SCHED_IDLE（论文 §5.2 CPU QoS 的调度侧），LS 保持默认策略
if [ "$DSEC_QOS" = "BE" ]; then
  if command -v chrt >/dev/null 2>&1; then
    exec chrt -i 0 sleep infinity
  fi
fi
exec sleep infinity
