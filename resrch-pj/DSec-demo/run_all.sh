#!/bin/bash
# DSec-demo 一键验证：起集群 → 集群实验 → 独立实验 → 汇总
# 用法: bash run_all.sh [quick]
#   quick: 跳过 E4 密度与 E12 突发（重型实验），其余照跑
set -e
cd "$(dirname "$0")"

QUICK=${1:-full}

echo "========== 0/5 集群 =========="
bash cluster.sh up

echo "========== 1/5 集群实验 =========="
cd experiments
python E00_smoke/run.py | tail -1
python E01_backend_latency/run.py | tail -6
python E06_memory/run.py | tail -1
python E07_placement/run.py | tail -2
python E09_log_replay/run.py | tail -1
python E10_network/run.py | tail -1
python E11_iam/run.py | tail -1
python E13_root_restricted/run.py | tail -1
if [ "$QUICK" != "quick" ]; then
  python E08_pack_diff/run.py | tail -1
  python E12_burst/run.py | tail -1
  python E04_density/run.py | tail -1
fi

echo "========== 2/5 E3 按需加载（独立容器，依赖 objserver） =========="
MSYS_NO_PATHCONV=1 docker run --rm --privileged --network dsec-demo_default \
  -v "$(pwd -W 2>/dev/null || pwd)/E03_ondemand:/work" \
  -v "$(cd .. && pwd -W 2>/dev/null || pwd)/..//results:/results" \
  -v e3data:/data -e E3_OBJ=http://objserver:9000 -e E3_OUT=/results/E3_ondemand.json \
  python:3.12-slim bash -c 'cd /work && python3 workload.py 2>&1 | tail -3'

echo "========== 3/5 E2 可组合层（独立特权容器） =========="
MSYS_NO_PATHCONV=1 docker run --rm --privileged \
  -v "$(pwd)/E02_composable:/src:ro" \
  -v "$(cd .. && pwd)/results:/results" \
  debian:bookworm-slim bash -c 'bash /src/run_inner.sh >/dev/null 2>&1; cp /e2/result.json /results/E2_composable.json'

echo "========== 4/5 E5 CPU QoS（独立特权容器） =========="
MSYS_NO_PATHCONV=1 docker run --rm --privileged --cpus 10 \
  -v "$(pwd)/E05_cpu_qos:/work" \
  -v "$(cd .. && pwd)/results:/results" \
  -e E5_CPUS=0-7 -e E5_OUT=/results/E5_cpu_qos.json \
  debian:bookworm-slim bash -c '
    sed -i "s|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g" /etc/apt/sources.list.d/debian.sources
    apt-get update -qq >/dev/null && apt-get install -y -qq python3 util-linux >/dev/null 2>&1
    cd /work && python3 run.py 2>&1 | tail -1'

cd ..

echo "========== 5/5 汇总 =========="
python - <<'EOF'
import json, glob
print(f"{'实验':<28}{'判定':<6}关键数字")
for f in sorted(glob.glob("results/*.json")):
    d = json.load(open(f, encoding="utf8"))
    name = d.get("experiment", f.split("/")[-1])
    ok = d.get("passed")
    flag = "PASS" if ok else ("n/a" if ok is None else "FAIL")
    key = ""
    s = d.get("summary") or d.get("results") or {}
    if isinstance(s, dict):
        key = ", ".join(f"{k}={v}" for k, v in list(s.items())[:3])
    print(f"{name:<28}{flag:<6}{key[:70]}")
EOF
echo "完成。详见 docs/VERIFICATION.md"
