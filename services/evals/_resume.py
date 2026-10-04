"""evals 断点续跑共享 helper（K6-a，方案依据 docs/Agent/13 §11）。

蓝本：ods-project/hermes-agent-main/batch_runner.py（只读参照）
  - :302-307  单行 JSONL 追加 = write + flush + os.fsync（崩溃不丢已确认行）；
  - :339-347  discarded 墓碑行（resume 扫描时视为已完成，避免每次重启全额重跑）；
  - :542-572  增量 checkpoint + 内容寻址 resume。

本模块把上述机制抽成共享层：内容寻址 key + checkpoint JSONL（写入即落盘，
增量追加而非整体重写）+ 墓碑（resume 默认跳过，`--retry-tombstones` 开关重跑）。
供 services/evals/ 各 harness 逐步收编（首个接入：session_search_schema/runner.py，
K6-b）。输出 JSONL 与 checkpoint 解耦：输出行照旧追加，跳过判定只读 checkpoint。
"""
from __future__ import annotations

import hashlib
import json
import os
import warnings
from datetime import datetime
from pathlib import Path

CHECKPOINT_FILENAME = ".checkpoint.jsonl"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def content_key(task_id, arm, model, prompt) -> str:
    """内容寻址 key：sha256(字段按固定序以 \\x1f 分隔拼接) 的 64 位十六进制。

    - prompt 参与哈希：任务内容（含 prompt 文本）一变 key 即变，
      历史 checkpoint 不会遮挡新输入；
    - 固定拼接序 + 分隔符：防字段跨界碰撞（("t1x","base") 与
      ("t1","xbase") 不共 key），实参传参顺序不影响结果。
    """
    payload = "\x1f".join(str(x) for x in (task_id, arm, model, prompt))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append_line(f, obj) -> None:
    """单行 JSONL 追加：write + flush + os.fsync（蓝本 batch_runner.py:302-307）。

    fsync 返回即该行已在崩溃后可恢复；调用方持有以 "a" 打开的文件句柄。
    """
    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())


class CheckpointStore:
    """每结果集一个 `.checkpoint.jsonl`（与输出文件同目录、点前缀隐藏）。

    - append(record)：写入即 flush + os.fsync；record 需含 key/status，
      缺 ts 自动补；status=="ok" 的记录即时进入 done 判定面；
    - tombstone(key, reason)：记 {key, status:"tombstone", reason, ts}；
    - load()：启动时全量读回 -> (done_keys, tombstones)；尾行截断（进程崩溃
      常见）解析失败即忽略并告警，不影响既有记录；
    - is_blocked(key, retry_tombstones=False)：resume 跳过判定——已完成一律
      跳过；墓碑默认跳过，retry_tombstones=True 时放行重跑。
    """

    FILENAME = CHECKPOINT_FILENAME

    def __init__(self, results_dir):
        self.dir = Path(results_dir)
        self.path = self.dir / self.FILENAME
        self._done: dict[str, str] = {}        # key -> ts（最近一次 ok）
        self._tombstones: dict[str, dict] = {}  # key -> {"reason", "ts"}

    # ── 写入 ────────────────────────────────────────────────────────
    def append(self, record: dict) -> dict:
        rec = dict(record)
        rec.setdefault("ts", _now())
        self._write(rec)
        key, status = rec.get("key"), rec.get("status")
        if key and status == "ok":
            self._done[key] = rec["ts"]
            self._tombstones.pop(key, None)  # 重跑成功覆盖旧墓碑
        return rec

    def tombstone(self, key: str, reason: str) -> dict:
        rec = {"key": key, "status": "tombstone",
               "reason": str(reason), "ts": _now()}
        self._write(rec)
        if key not in self._done:
            self._tombstones[key] = {"reason": rec["reason"], "ts": rec["ts"]}
        return rec

    # ── 读回 ────────────────────────────────────────────────────────
    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> tuple[set[str], dict[str, dict]]:
        """全量读回 -> (done_keys, tombstones)，并刷新内存判定状态。"""
        self._done.clear()
        self._tombstones.clear()
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        warnings.warn(
                            f"[checkpoint] {self.path.name}:{lineno} 行解析"
                            "失败（疑似尾行截断），忽略该行")
                        continue
                    key = rec.get("key")
                    if not key:
                        continue
                    if rec.get("status") == "ok":
                        self._done[key] = rec.get("ts", "")
                        self._tombstones.pop(key, None)
                    elif rec.get("status") == "tombstone":
                        if key not in self._done:  # 后续 ok 行优先于墓碑
                            self._tombstones[key] = {
                                "reason": rec.get("reason", ""),
                                "ts": rec.get("ts", ""),
                            }
        return set(self._done), dict(self._tombstones)

    # ── resume 判定 ────────────────────────────────────────────────
    def is_done(self, key: str) -> bool:
        return key in self._done

    def is_blocked(self, key: str, retry_tombstones: bool = False) -> bool:
        if self.is_done(key):
            return True
        return key in self._tombstones and not retry_tombstones

    # ── 内部 ───────────────────────────────────────────────────────
    def _write(self, rec: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            append_line(f, rec)
