"""SSE 帧解析与主干波 11 事件终端渲染（协议权威=docs/api/02-SSE事件流协议）。

解析取舍：手写增量帧解析器——依赖表无 SSE 客户端库，httpx 只给字节流；帧格式对齐网关
encode_frame（services/gateway/sse/events.py）：``id: N`` + ``event: NAME`` + ``data: JSON 单行``，
空行收帧，心跳为注释帧 ``: ping``（不产帧）。CRLF 容忍、多 data 行按 '\\n' 拼接、
流结束残留未完结帧宽松收口。

渲染规则（主干波 11 事件=api/02 §3 评审修复F；未知事件静默忽略=§7 规则 2）：
- TEXT_MESSAGE_CONTENT 增量即打（不换行、即刷），TEXT_MESSAGE_START 无独立输出（占位）；
- RETRIEVAL_EVIDENCE 折叠为计数行（切片/图谱路径/引用条数），degraded=true 标注「证据链暂缺」；
- TOOL_CALL_ARGS 聚合不逐增量打印，END 输出紧凑摘要；RUN_FINISHED 显示 usage；
- RUN_ERROR 记入 run_error 并写 stderr（app 层据此定退出码）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TextIO

_TRUNC_DEFAULT = 120


def truncate(text: str, limit: int = _TRUNC_DEFAULT) -> str:
    """定长截断（终端行宽保护；--output json 模式不做截断）。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class SseFrame:
    """一帧 SSE 事件（id=会话内单调 seq，即 task_events.seq，供断线重连 Last-Event-ID 定位）。"""

    id: str | None
    event: str
    data: str


def _parse_block(block: str) -> SseFrame | None:
    """解析单帧文本块：收集 id/event/data 字段；':' 开头注释行（心跳）忽略。"""
    event_name = ""
    data_lines: list[str] = []
    frame_id: str | None = None
    for line in block.split("\n"):
        if not line or line.startswith(":"):
            continue
        if line.startswith("id:"):
            frame_id = line[3:].strip()
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        # 其余字段（如 retry:）本协议未用，忽略
    if event_name == "" and not data_lines:
        return None  # 纯注释帧（心跳）或空块
    return SseFrame(id=frame_id, event=event_name or "message", data="\n".join(data_lines))


class SseFrameParser:
    """增量字节流 → SseFrame：feed() 随到随解析，flush() 收口流末残留（宽松）。"""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[SseFrame]:
        self._buffer += chunk.decode("utf-8", errors="replace")
        return self._drain(final=False)

    def flush(self) -> list[SseFrame]:
        """流结束收口：残留缓冲若含事件行则按一帧处理（规范上服务端总以空行收帧）。"""
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[SseFrame]:
        self._buffer = self._buffer.replace("\r\n", "\n").replace("\r", "\n")
        frames: list[SseFrame] = []
        while True:
            boundary = self._buffer.find("\n\n")
            if boundary < 0:
                break
            block = self._buffer[:boundary]
            self._buffer = self._buffer[boundary + 2 :]
            frame = _parse_block(block)
            if frame is not None:
                frames.append(frame)
        if final and self._buffer.strip():
            frame = _parse_block(self._buffer)
            self._buffer = ""
            if frame is not None:
                frames.append(frame)
        return frames


def iter_frames(chunks: Iterable[bytes]) -> list[SseFrame]:
    """便捷入口：字节块序列 → 全部帧（测试与小流量场景用）。"""
    parser = SseFrameParser()
    frames: list[SseFrame] = []
    for chunk in chunks:
        frames.extend(parser.feed(chunk))
    frames.extend(parser.flush())
    return frames


class TrunkEventRenderer:
    """主干波 11 事件 → 终端可读行；未知/扩展波事件静默忽略（api/02 §7 规则 2）。"""

    def __init__(self, sink: TextIO, err: TextIO | None = None) -> None:
        self.sink = sink
        self.err = err if err is not None else sink
        self._tool_names: dict[str, str] = {}
        self._tool_args: dict[str, list[str]] = {}
        self._line_dirty = False  # 文本增量后是否停在半行（决定收尾补换行）
        self.run_error: dict[str, Any] | None = None

    def render(self, frame: SseFrame) -> None:
        """渲染一帧；按事件名分发，未知事件名静默忽略。"""
        payload = self._payload(frame)
        if frame.event == "RUN_STARTED":
            self._line(f"[RUN] 开始 run={payload.get('run_id', '?')} task={payload.get('task_id', '?')}")
        elif frame.event == "TEXT_MESSAGE_START":
            return  # 占位事件：无独立输出，后续 CONTENT 归属同 message_id
        elif frame.event == "TEXT_MESSAGE_CONTENT":
            delta = payload.get("delta", "")
            if delta:
                self.sink.write(str(delta))
                self.sink.flush()
                self._line_dirty = True
        elif frame.event == "TEXT_MESSAGE_END":
            if self._line_dirty:
                self.sink.write("\n")
                self._line_dirty = False
            reason = str(payload.get("finish_reason", "stop"))
            if reason not in ("", "stop"):
                self._line(f"[TEXT] 消息结束 reason={reason}")
        elif frame.event == "TOOL_CALL_START":
            tool_call_id = str(payload.get("tool_call_id", "?"))
            self._tool_names[tool_call_id] = str(payload.get("tool_name", tool_call_id))
            self._line(f"[TOOL] 调用 {self._tool_names[tool_call_id]} …")
        elif frame.event == "TOOL_CALL_ARGS":
            tool_call_id = str(payload.get("tool_call_id", "?"))
            self._tool_args.setdefault(tool_call_id, []).append(str(payload.get("delta", "")))
        elif frame.event == "TOOL_CALL_END":
            tool_call_id = str(payload.get("tool_call_id", "?"))
            args_text = "".join(self._tool_args.get(tool_call_id, []))
            name = self._tool_names.get(tool_call_id, tool_call_id)
            self._line(f"[TOOL] {name} 参数就绪 {truncate(args_text)}")
        elif frame.event == "TOOL_CALL_RESULT":
            self._render_tool_result(payload)
        elif frame.event == "RETRIEVAL_EVIDENCE":
            self._render_evidence(payload)
        elif frame.event == "RUN_FINISHED":
            usage = json.dumps(payload.get("usage", {}), ensure_ascii=False)
            self._line(f"[RUN] 完成 usage={truncate(usage)}")
        elif frame.event == "RUN_ERROR":
            self.run_error = payload
            retryable = bool(payload.get("retryable"))
            hint = "（可重试）" if retryable else ""
            self.err.write(f"[RUN] 失败 code={payload.get('code', '?')} message={payload.get('message', '')}{hint}\n")
        # 其余（STATE_*/MESSAGES_SNAPSHOT/GATE_VERDICT/STEP_* 与未知名）静默忽略

    def finish(self) -> None:
        """流结束收口：文本增量停在半行时补换行，避免终端提示符粘连。"""
        if self._line_dirty:
            self.sink.write("\n")
            self._line_dirty = False

    def _render_tool_result(self, payload: dict[str, Any]) -> None:
        tool_call_id = str(payload.get("tool_call_id", "?"))
        name = self._tool_names.get(tool_call_id, tool_call_id)
        mark = "成功" if payload.get("ok", True) else "失败"
        summary = truncate(str(payload.get("summary", "")))
        self._line(f"[TOOL] {name} {mark}：{summary}（{payload.get('cost_ms', '?')}ms）")

    def _render_evidence(self, payload: dict[str, Any]) -> None:
        """RETRIEVAL_EVIDENCE 折叠渲染：只报计数不展开引用列表；degraded 必须显式标注。"""
        chunks = payload.get("chunks") or []
        paths = payload.get("graph_paths") or []
        citations = payload.get("citations") or []
        self._line(f"[EV] 证据（已折叠）：切片 {len(chunks)}、图谱路径 {len(paths)}、引用 {len(citations)} 条")
        if payload.get("degraded"):
            self._line("[EV] 警告：证据链暂缺（degraded=true，图库/向量库单侧降级）")

    def _line(self, text: str) -> None:
        if self._line_dirty:
            self.sink.write("\n")
            self._line_dirty = False
        self.sink.write(text + "\n")

    @staticmethod
    def _payload(frame: SseFrame) -> dict[str, Any]:
        try:
            loaded = json.loads(frame.data) if frame.data else {}
        except ValueError:
            return {"_raw": frame.data}
        return loaded if isinstance(loaded, dict) else {"_raw": loaded}
