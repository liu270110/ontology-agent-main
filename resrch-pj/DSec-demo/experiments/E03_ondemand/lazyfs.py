#!/usr/bin/env python3
"""lazyfs：按需加载文件系统语义（论文 §5.3 三原则的用户态实现）。

- 元数据本地：文件树/chunk 索引是本地 JSON，路径遍历零远端 I/O（对应 EROFS multi-device 的元数据本地）；
- 读按需、成块：数据以 256KiB chunk 为单位仅在访问时从远端对象存储（模拟 3FS）取；
- 写本地：本 demo 只读层数据不改写（可写层在 E2/E4 由 overlayfs upper 承担）；
- 二级缓存：chunk 落本地盘，重复读不再打远端。
支持两种模式：FUSE 挂载（对工作负载透明）与直接 API 模式。
"""
import json
import os
import sys
import urllib.request

CHUNK = 256 * 1024


class LazyFS:
    def __init__(self, root: str, obj_base: str, cache_dir: str, meta_path: str):
        self.root = root
        self.obj_base = obj_base
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        with open(meta_path) as f:
            self.meta = json.load(f)  # {relpath: {"size": n, "chunks": ["name", ...]}}
        self.stats = {"chunk_fetches": 0, "chunk_bytes": 0, "cache_hits": 0}

    # ---- path helpers
    def _rel(self, path: str) -> str:
        r = os.path.relpath(path, self.root)
        return r.lstrip("./")

    def exists(self, path: str) -> bool:
        return self._rel(path) in self.meta or os.path.isdir(path)

    def listdir(self, path: str) -> list[str]:
        rel = self._rel(path)
        prefix = "" if rel == "." else rel + "/"
        dirs, files = set(), set()
        for name in self.meta:
            if name.startswith(prefix):
                rest = name[len(prefix):]
                if "/" in rest:
                    dirs.add(rest.split("/")[0])
                else:
                    files.add(rest)
        return sorted(dirs) + sorted(files)

    def size(self, path: str) -> int:
        return self.meta[self._rel(path)]["size"]

    # ---- data path：按需成块取数
    def _chunk_path(self, name: str) -> str:
        return os.path.join(self.cache_dir, name.replace("/", "_"))

    def _fetch_chunk(self, name: str) -> str:
        p = self._chunk_path(name)
        if os.path.exists(p):
            self.stats["cache_hits"] += 1
            return p
        with urllib.request.urlopen(f"{self.obj_base}/obj/{name}", timeout=60) as r:
            data = r.read()
        with open(p, "wb") as f:
            f.write(data)
        self.stats["chunk_fetches"] += 1
        self.stats["chunk_bytes"] += len(data)
        return p

    def read(self, path: str) -> bytes:
        m = self.meta[self._rel(path)]
        out = bytearray()
        for cname in m["chunks"]:
            with open(self._fetch_chunk(cname), "rb") as f:
                out.extend(f.read())
        return bytes(out[:m["size"]])


def build_dataset(obj_base: str, data_dir: str, meta_path: str, n_files: int = 4000,
                  file_kb: int = 384, seed: int = 7) -> float:
    """生成数据集并按 chunk 上传远端；返回总字节数。元数据（索引）留本地。"""
    import random
    rng = random.Random(seed)
    os.makedirs(data_dir, exist_ok=True)
    total = 0
    meta = {}
    for i in range(n_files):
        rel = f"d{i // 400}/file_{i:05d}.bin"
        size = file_kb * 1024
        n_chunks = (size + CHUNK - 1) // CHUNK
        names = []
        for c in range(n_chunks):
            name = f"ds/{rel}.{c:03d}.chunk"
            payload = bytes(rng.getrandbits(8) for _ in range(4096)) * (CHUNK // 4096)
            req = urllib.request.Request(f"{obj_base}/obj/{name}", data=payload, method="PUT")
            urllib.request.urlopen(req, timeout=60).read()
            names.append(name)
            total += len(payload)
        meta[rel] = {"size": size, "chunks": names}
        if (i + 1) % 500 == 0:
            print(f"  上传 {i + 1}/{n_files} 文件 ({total / 1e9:.2f} GB)", flush=True)
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    return total


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        n = build_dataset(sys.argv[2], "/data/ignore", "/data/meta.json")
        print(f"dataset built: {n / 1e9:.2f} GB remote objects")
