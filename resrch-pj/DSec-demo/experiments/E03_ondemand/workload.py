#!/usr/bin/env python3
"""E3 工作负载驱动：全量拉取 vs 按需加载（元数据本地 + chunk 级按需 + 本地缓存）。

论文观察：运行时只访问镜像的 4.2%~13.3%；按需加载把总 I/O 缩到实际访问比例，而不仅是挪时间。
工作负载：随机访问 8% 的文件并计算校验和（模拟 Agent 会话中真实触达的文件子集）。
指标：远端 serve 字节数（objserver /metrics 计账）、完成时间、本地盘写。
"""
import hashlib
import json
import os
import random
import shutil
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lazyfs import LazyFS, CHUNK  # noqa: E402

OBJ = os.environ.get("E3_OBJ", "http://objserver:9000")
META = "/data/meta.json"
CACHE = "/data/chunk_cache"
PULL = "/data/pulled"
N_ACCESS = 320  # 4000 文件 × 8%

out = {"experiment": "E3_ondemand_loading"}


def server_served() -> dict:
    with urllib.request.urlopen(f"{OBJ}/metrics", timeout=10) as r:
        return json.load(r)["stats"]


def workload_read(fs_reader, files: list[str]) -> float:
    t0 = time.time()
    for rel in files:
        data = fs_reader(rel)
        hashlib.sha256(data).hexdigest()
    return time.time() - t0


# ---- 准备数据集（若远端还没有）
meta_exists = os.path.exists(META)
if not meta_exists:
    from lazyfs import build_dataset
    print("生成并上传数据集（一次性）...", flush=True)
    build_dataset(OBJ, "/data/ignore", META)
with open(META) as f:
    meta = json.load(f)
out["dataset"] = {"files": len(meta), "total_gb": round(sum(m["size"] for m in meta.values()) / 1e9, 2),
                  "chunk_kb": CHUNK // 1024}

rng = random.Random(42)
files = rng.sample(sorted(meta.keys()), N_ACCESS)

# ---- (1) 全量拉取基线：把整个"镜像"拉到本地再跑工作负载（Docker eager pull 的对应物）
if os.path.exists(PULL):
    shutil.rmtree(PULL)
served_before = {k: v["served_bytes"] for k, v in server_served().items()}
t0 = time.time()
os.makedirs(PULL)
for rel, m in meta.items():
    dst = os.path.join(PULL, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    buf = bytearray()
    for cname in m["chunks"]:
        with urllib.request.urlopen(f"{OBJ}/obj/{cname}", timeout=120) as r:
            buf.extend(r.read())
    with open(dst, "wb") as f:
        f.write(bytes(buf[:m["size"]]))
full_pull_t = time.time() - t0
served_after = {k: v["served_bytes"] for k, v in server_served().items()}
full_pull_bytes = sum(served_after.get(k, 0) - served_before.get(k, 0) for k in served_after)
full_pull_disk = int(os.popen(f"du -sk {PULL}").read().split()[0]) * 1024
out["full_pull"] = {"wall_s": round(full_pull_t, 1), "remote_bytes": full_pull_bytes,
                    "remote_gb": round(full_pull_bytes / 1e9, 2), "local_disk_write_bytes": full_pull_disk}

# 工作负载在已全量拉取的本地数据上运行
work_files = [os.path.join(PULL, rel) for rel in files]
t0 = time.time()
for p in work_files:
    hashlib.sha256(open(p, "rb").read()).hexdigest()
out["full_pull_workload_s"] = round(time.time() - t0, 3)

# ---- (2) 按需加载：元数据本地，数据按 chunk 访问时取，本地缓存
if os.path.exists(CACHE):
    shutil.rmtree(CACHE)
served_before = {k: v["served_bytes"] for k, v in server_served().items()}
lz = LazyFS("/lazyfs", OBJ, CACHE, META)
t0 = time.time()
workload_read(lambda rel: lz.read(os.path.join("/lazyfs", rel)), files)
lazy_t = time.time() - t0
served_after = {k: v["served_bytes"] for k, v in server_served().items()}
lazy_bytes = sum(served_after.get(k, 0) - served_before.get(k, 0) for k in served_after)
lazy_disk = int(os.popen(f"du -sk {CACHE}").read().split()[0]) * 1024
out["lazy"] = {"wall_s": round(lazy_t, 3), "remote_bytes": lazy_bytes, "remote_mb": round(lazy_bytes / 1e6, 1),
               "local_disk_write_bytes": lazy_disk, "chunks_fetched": lz.stats["chunk_fetches"],
               "files_accessed": len(files)}
out["lazyfs_stats"] = lz.stats

# ---- 结论指标
out["summary"] = {
    "accessed_fraction": round(len(files) / len(meta), 3),
    "remote_io_ratio": round(lazy_bytes / max(full_pull_bytes, 1), 4),
    "remote_io_reduction_pct": round((1 - lazy_bytes / max(full_pull_bytes, 1)) * 100, 1),
    "paper_reference": "论文：运行时数据访问占比 4.2%~13.3%；8192 容器突发按需加载盘写 −57%、完成时间 1.71× 提速",
}
out["passed"] = (lazy_bytes < full_pull_bytes * 0.2
                 and out["lazy"]["wall_s"] < out["full_pull"]["wall_s"] / 5)
with open(os.environ.get("E3_OUT", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "..", "..", "results", "E3_ondemand.json")), "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(json.dumps(out, indent=2, ensure_ascii=False))
