"""F2 CLI 子进程通用适配器（docs/Agent/05 §3 F2/§4.4 + 20 篇 §3.2）。

spawn CLI → stdout 逐行归一，两种协议模式（profile.protocol）：
- **jsonl**：stdout 行式 JSON → ``profile.event_map`` 字段路径表达式（``$.type``/``$.data.delta``）
  归一为 GenerationEvent（缺字段/坏行容错跳过不断流）；``agent_settled`` 类终止判据 → finish
  （05 篇 §4.2：pi agent_settled=RUN 终止判据）；
- **text-tail**（功能受限档，注册时明示降级——05 篇 §5.1 决策树叶）：stdout 纯文本尾部提取
  （``text_tail.max_chars``），事件只有 TEXT_MESSAGE。

会话语义（05 篇 §4.4 既有裁决）：F2 CLI 无跨进程会话 → **run-per-turn**（每轮独立 spawn，
进程退出即轮终态）+ **L1 重放**（近窗历史 history 注入提示词模板重放——``{history}``/``
{message}`` 字面替换，缺省模板=时序 ``role: content`` 行 + 本条消息）；feed 能力不声明
（capabilities.feed=false）。

进程纪律：传输层超时=单轮 timeout_ms 钳制（wait_for 不可行于行读循环→逐行 read 超时口径），
超时/取消一律 kill + reap（防僵尸进程）；stderr 并发收集（非零退出结构化报错带 stderr 尾部）；
失败一律结构化 ModelPortError 族（5001/5002），不裸异常逃逸（02 §4.1 ④）。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

from services.agent.business.adapters.base import CHAT_ACTION_IRI, ChatAdapter, ChatTurn, GenerationEvent
from services.agent.business.adapters.profiles import AgentProfile, is_missing, resolve_json_path
from services.agent.domain.model.kernel_context import ExtensionMeta, TenantContext
from services.platform.ports.model_port import ModelTimeoutError, ModelUnavailableError

_IDLE_READ_TIMEOUT_S = 30.0  # 单行读取空闲超时（行间静默上限；整轮上限由内核 wait_for 钳制）
_STDERR_TAIL_CHARS = 400


def build_replay_prompt(turn: ChatTurn, template: str = "") -> str:
    """L1 重放提示词（05 §4.4）：近窗历史（新→旧反转成时序）+ 本条消息。

    profile.transport.prompt_template 提供 ``{history}``/``{message}`` 字面替换（str.replace
    而非 str.format——用户消息可含花括号，禁当格式串解析）；空模板=内置缺省（时序行）。
    """
    history_lines = [f"{role}: {content}" for role, content in reversed(turn.history)]
    history_block = "\n".join(history_lines)
    if template:
        return template.replace("{history}", history_block).replace("{message}", turn.message)
    lines = history_lines + [f"user: {turn.message}"]
    return "\n".join(lines)


class CliJsonlAdapter(ChatAdapter):
    """cli-generic：CLI 子进程 run-per-turn 生成通道（profile 数据文件驱动）。"""

    meta = ExtensionMeta(
        name="chat.cli_generic",
        version="1.0.0",
        semantic_annotation={"action_iri": CHAT_ACTION_IRI},
    )
    adapter_name = "cli-generic"

    def __init__(self, profile: AgentProfile, *, idle_read_timeout_s: float = _IDLE_READ_TIMEOUT_S) -> None:
        self._profile = profile
        self._idle_read_timeout_s = idle_read_timeout_s

    async def stream_chat(
        self, turn: ChatTurn, ctx: TenantContext, *, timeout_ms: int = 30_000
    ) -> AsyncIterator[GenerationEvent]:
        prompt = build_replay_prompt(turn, self._profile.transport.prompt_template)
        argv = self._resolve_argv(prompt)
        env = {**os.environ, **{k: str(v) for k, v in self._profile.transport.env.items()}}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if self._profile.transport.prompt_mode == "stdin" else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._profile.transport.cwd or None,
                env=env,
            )
        except OSError as exc:
            raise ModelUnavailableError(f"CLI spawn 失败（{argv[0] if argv else '空命令'}）: {exc}") from exc
        stderr_task: asyncio.Task[str] | None = None
        try:
            if self._profile.transport.prompt_mode == "stdin" and proc.stdin is not None:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
                await proc.stdin.wait_closed()
            stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))
            if self._profile.protocol == "text-tail":
                async for event in self._iter_text_tail(proc, turn):
                    yield event
            else:
                async for event in self._iter_jsonl(proc, turn):
                    yield event
            code = await proc.wait()
            stderr_text = await stderr_task
            stderr_task = None
            if code != 0:
                raise ModelUnavailableError(
                    f"CLI 退出码 {code}（{self._profile.profile}）: {stderr_text[-_STDERR_TAIL_CHARS:]}"
                )
        except TimeoutError as exc:
            raise ModelTimeoutError(
                f"CLI 输出空闲超时（>{self._idle_read_timeout_s}s，单行读取空闲上限）: {self._profile.profile}"
            ) from exc
        finally:
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()  # 超时/取消路径：收割 stderr 收集任务（防悬挂告警）
            await _terminate(proc)

    # ── argv 解析（{prompt} 占位符 / stdin 模式）──────────────────────────────
    def _resolve_argv(self, prompt: str) -> list[str]:
        cmd = [a for a in self._profile.transport.cmd if a]
        if not cmd:
            raise ModelUnavailableError("cli-generic profile 未配置 transport.cmd（5002，画像不完整）")
        if self._profile.transport.prompt_mode == "arg":
            if "{prompt}" in cmd:
                return [a.replace("{prompt}", prompt) for a in cmd]
            return [*cmd, prompt]  # 无占位符：prompt 追加为末参（run-per-turn 惯例）
        return cmd

    # ── jsonl：stdout 行式 JSON → event_map 归一 ─────────────────────────────
    async def _iter_jsonl(self, proc: asyncio.subprocess.Process, turn: ChatTurn) -> AsyncIterator[GenerationEvent]:
        assert proc.stdout is not None  # noqa: S101 ——spawn 时 PIPE 保证
        finished = False
        usage: dict[str, Any] = {}
        finish_reason = "stop"
        while True:
            raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=self._idle_read_timeout_s)
            if not raw_line:
                break  # EOF=轮终态（run-per-turn：进程退出即终）
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue  # 坏行容错（横幅/告警混入 stdout）
            if not isinstance(payload, dict):
                continue
            kind, delta, rule_usage = self._map_line(payload)
            if kind == "finish" and not finished:
                finished = True
                usage = rule_usage
            elif kind in ("text_delta", "reasoning_delta") and delta:
                yield GenerationEvent(kind=kind, delta=delta)
        # EOF 收尾：finish 规则先至（如 agent_settled）则携带其用量，否则空用量
        yield GenerationEvent(kind="finish", usage=usage, finish_reason=finish_reason)

    def _map_line(self, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """单行 event_map 归一：kind 路径判别 → 规则匹配；缺字段返回空三元组（容错跳过）。"""
        kind = resolve_json_path(payload, self._profile.event_map.kind_path)
        if is_missing(kind) or not isinstance(kind, str):
            return "", "", {}
        for rule in self._profile.event_map.rules:
            if rule.match != kind:
                continue
            if rule.emit == "finish":
                usage = resolve_json_path(payload, rule.usage_path)
                return "finish", "", usage if isinstance(usage, dict) else {}
            delta = resolve_json_path(payload, rule.delta_path)
            return rule.emit, (delta if isinstance(delta, str) else ""), {}
        return "", "", {}

    # ── text-tail：纯文本尾部提取（功能受限档）────────────────────────────────
    async def _iter_text_tail(self, proc: asyncio.subprocess.Process, turn: ChatTurn) -> AsyncIterator[GenerationEvent]:
        assert proc.stdout is not None  # noqa: S101 ——spawn 时 PIPE 保证
        chunks: list[str] = []
        while True:
            raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=self._idle_read_timeout_s)
            if not raw_line:
                break
            chunks.append(raw_line.decode("utf-8", errors="replace"))
        text = "".join(chunks).strip()
        max_chars = self._profile.tail_max_chars
        tail = text[-max_chars:] if max_chars and len(text) > max_chars else text
        if tail:
            yield GenerationEvent(kind="text_delta", delta=tail)
        yield GenerationEvent(kind="finish", usage={}, finish_reason="stop")  # EOF=轮终态（功能受限档零用量面）


async def _drain_stderr(stderr: asyncio.StreamReader | None) -> str:
    """并发收集 stderr（防管道填满死锁）；EOF/None → 空串。"""
    if stderr is None:
        return ""
    try:
        raw = await stderr.read()
    except (OSError, ValueError):  # 进程被杀时句柄可能已失效
        return ""
    return raw.decode("utf-8", errors="replace")


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """超时/取消/异常路径兜底：kill + reap（防僵尸进程；正常退出路径 wait 已回收幂等）。"""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await proc.wait()
    except ProcessLookupError:  # pragma: no cover — 平台竞态兜底
        return
