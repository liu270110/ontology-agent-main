#!/usr/bin/env python3
"""objserver：模拟 3FS 的对象读出路径（E3 用）。

- PUT /obj/{name}    上传对象（数据集分块/整文件均可）
- GET /obj/{name}    下载对象，按 served 字节数记账（对照"全量拉取"的盘写/网络量）
- GET /metrics       各对象 served_bytes / served_count
- GET /healthz
纯标准库；数据落 /data（compose 卷 objdata）。
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("OBJ_DATA", "/data")
LOCK = threading.Lock()
STATS: dict[str, dict] = {}


def stat_of(name: str) -> dict:
    return STATS.setdefault(name, {"served_bytes": 0, "served_count": 0, "put_bytes": 0})


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/healthz":
            return self._j(200, {"ok": True})
        if self.path == "/metrics":
            with LOCK:
                return self._j(200, {"stats": STATS})
        name = self.path.removeprefix("/obj/").split("?")[0]
        path = os.path.join(DATA, os.path.basename(name))
        if not os.path.exists(path):
            return self._j(404, {"error": "no such object"})
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        sent = 0
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)
                sent += len(chunk)
        with LOCK:
            s = stat_of(name)
            s["served_bytes"] += sent
            s["served_count"] += 1

    def do_PUT(self):
        name = self.path.removeprefix("/obj/").split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        path = os.path.join(DATA, os.path.basename(name))
        got = 0
        with open(path, "wb") as f:
            while got < n:
                chunk = self.rfile.read(min(1 << 20, n - got))
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        with LOCK:
            stat_of(name)["put_bytes"] += got
        self._j(200, {"ok": True, "bytes": got})


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 9000), H).serve_forever()
