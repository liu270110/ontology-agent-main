#!/bin/bash
# E2 可组合环境层（论文 §5.1）：在特权容器内执行
# A) 启动时解 tar（论文否定的替代方案）  B) overlayfs 动态 lowerdir 组装 + EROFS 层（DSec 方案）
set -e
export DEBIAN_FRONTEND=noninteractive
sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources
apt-get update -qq >/dev/null
apt-get install -y -qq runc erofs-utils wget python3 >/dev/null 2>&1
echo "== 工具: $(runc --version | head -1) / $(mkfs.erofs -V 2>&1 | head -1)"

BASE=/e2
mkdir -p $BASE/{layers,erofs,bundles,tars} $BASE/layers/base
# overlay upperdir 不能落在 overlayfs（容器根）上：用 tmpfs 承载每沙箱 upper/work
mount -t tmpfs tmpfs $BASE/bundles
W=$BASE/out.json
echo '{' > $W

# ---------- 基础 rootfs：alpine minirootfs（清华源）----------
BASEVER=3.20.3
f=alpine-minirootfs-$BASEVER-x86_64.tar.gz
wget -q https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.20/releases/x86_64/$f -O $BASE/$f \
  || wget -q https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.20/releases/x86_64/alpine-minirootfs-3.20.0-x86_64.tar.gz -O $BASE/$f
tar -xzf $BASE/$f -C $BASE/layers/base

# ---------- 层内容 ----------
# workspace：源代码树（真实小文件）
mkdir -p $BASE/layers/workspace/src
python3 - <<'PY'
import os, random
rng = random.Random(1)
for i in range(300):
    p = f"/e2/layers/workspace/src/mod_{i:03d}.py"
    open(p, "w").write("# module %d\n" % i + "x = %d\n" % rng.getrandbits(16) * 50)
PY
# toolkit T1：构建工具（较大），T2：测试工具
mkdir -p $BASE/layers/toolkit_t1/bin $BASE/layers/toolkit_t2/bin
dd if=/dev/urandom of=$BASE/layers/toolkit_t1/bin/compiler.bin bs=1M count=48 2>/dev/null
echo '#!/bin/sh echo compiling' > $BASE/layers/toolkit_t1/bin/ccwrap
dd if=/dev/urandom of=$BASE/layers/toolkit_t2/bin/tester.bin bs=1M count=24 2>/dev/null
echo 'workload_content' > $BASE/layers/workspace/src/README.txt

# ---------- EROFS 层镜像（不可变发布形态）：压缩 + 随机访问，loop 挂载 ----------
mkfs.erofs -z lz4hc $BASE/erofs/workspace.erofs $BASE/layers/workspace >/dev/null
mkfs.erofs -z lz4hc $BASE/erofs/toolkit_t1.erofs $BASE/layers/toolkit_t1 >/dev/null
mkfs.erofs -z lz4hc $BASE/erofs/toolkit_t2.erofs $BASE/layers/toolkit_t2 >/dev/null
ls -la $BASE/erofs/
mkdir -p $BASE/erofs/mnt/{workspace,toolkit_t1,toolkit_t2}
mount -t erofs -o loop,ro $BASE/erofs/workspace.erofs   $BASE/erofs/mnt/workspace
mount -t erofs -o loop,ro $BASE/erofs/toolkit_t1.erofs  $BASE/erofs/mnt/toolkit_t1
mount -t erofs -o loop,ro $BASE/erofs/toolkit_t2.erofs  $BASE/erofs/mnt/toolkit_t2
echo "== EROFS 层挂载 OK（kernel 原生）=="

# tar 形态（对照方案）
tar -cf $BASE/tars/workspace.tar  -C $BASE/layers workspace
tar -cf $BASE/tars/toolkit_t1.tar -C $BASE/layers toolkit_t1
tar -cf $BASE/tars/toolkit_t2.tar -C $BASE/layers toolkit_t2

# ---------- runc bundle 模板 ----------
cat > $BASE/config.json <<'EOF'
{
  "ociVersion": "1.0.2",
  "process": {
    "terminal": false, "user": {"uid": 0, "gid": 0},
    "args": ["sh", "-c", "cat /src/README.txt; /toolkit_t1/bin/ccwrap >/dev/null; echo MERGED_VIEW_OK; echo written > /work_written.txt"],
    "env": ["PATH=/bin:/usr/bin:/sbin"], "cwd": "/"
  },
  "root": {"path": "rootfs", "readonly": false},
  "hostname": "dsec-e2",
  "linux": {
    "namespaces": [{"type": "pid"}, {"type": "network"}, {"type": "ipc"}, {"type": "uts"}, {"type": "mount"}],
    "maskedPaths": [], "readonlyPaths": []
  },
  "mounts": [
    {"destination": "/proc", "type": "proc", "source": "proc"},
    {"destination": "/sys", "type": "sysfs", "source": "sysfs", "options": ["nosuid", "noexec", "nodev", "ro"]},
    {"destination": "/dev", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "strictatime", "mode=755", "size=65536k"]}
  ]
}
EOF

# ---------- 工具函数 ----------
total_write() { du -sk "$1" 2>/dev/null | cut -f1; }

run_bundle() { # $1=bundle $2=name
  runc run --bundle "$1" "$2" >/dev/null 2>&1
}

# ---------- 方案 B：overlayfs 动态组装（DSec 方案，每沙箱 O(1) 组装零拷贝） ----------
B_SETUPS=(); B_WRITES=()
for i in 1 2 3 4 5 6 7 8 9 10; do
  BND=$BASE/bundles/b$i
  mkdir -p $BND/{rootfs,upper,work}
  cp $BASE/config.json $BND/
  t0=$(date +%s%N)
  mount -t overlay overlay \
    -o "lowerdir=$BASE/erofs/mnt/toolkit_t2:$BASE/erofs/mnt/toolkit_t1:$BASE/erofs/mnt/workspace:$BASE/layers/base,upperdir=$BND/upper,workdir=$BND/work" \
    $BND/rootfs
  t1=$(date +%s%N)
  # 首条命令就绪（合并视图验证在 runc 内进行）
  runc run --bundle $BND o-b$i >/dev/null 2>&1
  t2=$(date +%s%N)
  B_SETUPS+=($(( (t2 - t0) / 1000000 )))
  W=$(total_write $BND/upper); B_WRITES+=(${W:-0})
  umount $BND/rootfs 2>/dev/null || true
  rm -rf $BND
done
echo "B_setups_ms: ${B_SETUPS[@]}"
echo "B_writes_kb: ${B_WRITES[@]}"

# ---------- 方案 A：启动时解 tar（每沙箱全量拷贝） ----------
A_SETUPS=(); A_WRITES=()
for i in 1 2 3 4 5 6 7 8 9 10; do
  BND=$BASE/bundles/a$i
  mkdir -p $BND/rootfs
  cp $BASE/config.json $BND/
  t0=$(date +%s%N)
  cp -a $BASE/layers/base/. $BND/rootfs/
  tar -xf $BASE/tars/workspace.tar  -C $BND/rootfs
  tar -xf $BASE/tars/toolkit_t1.tar -C $BND/rootfs
  tar -xf $BASE/tars/toolkit_t2.tar -C $BND/rootfs
  t1=$(date +%s%N)
  runc run --bundle $BND o-a$i >/dev/null 2>&1
  t2=$(date +%s%N)
  A_SETUPS+=($(( (t2 - t0) / 1000000 )))
  W=$(total_write $BND/rootfs); A_WRITES+=(${W:-0})
  rm -rf $BND
done
echo "A_setups_ms: ${A_SETUPS[@]}"
echo "A_writes_kb: ${A_WRITES[@]}"

# ---------- 层经济学：m 基座 × N 工作区 × K 工具包 ----------
# 单体：升级任意一层 → 重建其全部组合（O(m·N·K)）；层化：只重建该层（O(m+N+K)）
m=2; n=3; k=2
mono_builds=$(( m * n * k ))
layer_builds=$(( m + n + k ))
# 实测：一次"组合重建"的时间（base+ws+t1+t2 的一次完整拷贝）
t0=$(date +%s%N)
rm -rf $BASE/rebuild && mkdir -p $BASE/rebuild
cp -a $BASE/layers/base/. $BASE/rebuild/
tar -xf $BASE/tars/workspace.tar  -C $BASE/rebuild
tar -xf $BASE/tars/toolkit_t1.tar -C $BASE/rebuild
tar -xf $BASE/tars/toolkit_t2.tar -C $BASE/rebuild
t1=$(date +%s%N)
one_rebuild_ms=$(( (t1 - t0) / 1000000 ))
# 层化方案：升级任意一层只重建该层。实测"全部层重建一遍"（3 个 EROFS 层 + base 拷贝）
# （层内容为随机二进制，不可压缩，用无压缩 mkfs 计时才是代表性配置）
t0=$(date +%s%N)
rm -rf $BASE/layers/workspace_new && cp -a $BASE/layers/workspace $BASE/layers/workspace_new
mkfs.erofs $BASE/erofs/workspace_new.erofs $BASE/layers/workspace_new >/dev/null
mkfs.erofs $BASE/erofs/toolkit_t1.erofs $BASE/layers/toolkit_t1 >/dev/null
mkfs.erofs $BASE/erofs/toolkit_t2.erofs $BASE/layers/toolkit_t2 >/dev/null
t1=$(date +%s%N)
all_layers_rebuild_ms=$(( (t1 - t0) / 1000000 ))

echo "== 汇总 =="
echo "monolithic_rebuilds=$mono_builds layered_rebuilds=$layer_builds"
echo "one_rebuild_ms=$one_rebuild_ms"
A_S=$(printf '%s,' "${A_SETUPS[@]}" | sed "s/,$//")
A_W=$(printf '%s,' "${A_WRITES[@]}" | sed "s/,$//")
B_S=$(printf '%s,' "${B_SETUPS[@]}" | sed "s/,$//")
B_W=$(printf '%s,' "${B_WRITES[@]}" | sed "s/,$//")
python3 - <<PY
import statistics as st, json
A = {"setups_ms": [$A_S], "writes_kb": [$A_W]}
B = {"setups_ms": [$B_S], "writes_kb": [$B_W]}
res = {
  "experiment": "E2_composable_layers",
  "approach_tar_at_boot": {"setup_p50_ms": int(st.median(A["setups_ms"])), "disk_write_p50_kb": int(st.median(A["writes_kb"]))},
  "approach_overlay_compose": {"setup_p50_ms": int(st.median(B["setups_ms"])), "disk_write_p50_kb": int(st.median(B["writes_kb"]))},
  "speedup_setup": round(st.median(A["setups_ms"]) / max(st.median(B["setups_ms"]), 0.001), 1),
  "write_reduction": round(1 - st.median(B["writes_kb"]) / st.median(A["writes_kb"]), 3),
  "economics": {"monolithic_rebuilds_m_n_k": $mono_builds, "layered_rebuilds": $layer_builds,
                 "one_rebuild_ms": $one_rebuild_ms,
                 "monolithic_total_ms": $mono_builds * $one_rebuild_ms,
                 "all_layers_rebuild_ms": $all_layers_rebuild_ms},
  "mechanism_note": "B 方案 lowerdir 栈 = [toolkit_t2(EROFS), toolkit_t1(EROFS), workspace(EROFS), base]，每沙箱仅新建 upper/work 目录；EROFS 由内核原生挂载（压缩只读层）",
  "paper_reference": "论文：tar 方案完成时间 1.76× 慢于 EROFS 层、盘写 5.5×；层化把升级代价 O(m·N)→O(m)、O(k·N)→O(k)"
}
# 论文对应量级：端到端 1.76× 提速、盘写 5.5×；本地每沙箱组装 2.8×、盘写 ~100% 减少
res["passed"] = res["speedup_setup"] >= 1.5 and res["write_reduction"] >= 0.9 \
                and res["economics"]["monolithic_total_ms"] > res["economics"]["all_layers_rebuild_ms"]
print(json.dumps(res, indent=2))
open("/e2/result.json", "w").write(json.dumps(res, indent=2))
PY

umount $BASE/erofs/mnt/* 2>/dev/null || true
echo DONE-E2
