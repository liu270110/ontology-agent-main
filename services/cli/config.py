"""CLI 配置与凭据自管层（OA_CLI_* 环境变量 + ~/.ontology-agent/config.json）。

职责边界：cli 为独立进程形态，禁 import services/（架构锚点 §2 分层纪律），配置体系与
后端 Settings 完全分立。

取舍（2026-09-28 实施裁决，报告挂账）：
- docs/cli/CLI设计.md §4 规划 ONTO_* / ~/.onto/config.yaml / keyring；当期任务裁决走
  OA_CLI_* + ~/.ontology-agent/config.json 单文件（标准库 json 读写，免 yaml/keyring 新依赖）；
- 登录令牌并入同一配置文件 auth 节，落盘权限 0600（POSIX）；Windows 的 chmod 仅支持只读位，
  权限尽力而为（不作为访问控制手段，安全验收口径：令牌不进进程参数与日志）。

优先级：命令行参数 > 环境变量 > 配置文件 > 内置默认（CI 场景走环境变量，不落盘）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ENDPOINT = "http://127.0.0.1:8000/api/v1"
DEFAULT_OUTPUT = "table"
DEFAULT_TIMEOUT_SECONDS = 30.0
VALID_OUTPUTS = ("table", "json")

ENV_ENDPOINT = "OA_CLI_ENDPOINT"
ENV_TOKEN = "OA_CLI_TOKEN"
ENV_OUTPUT = "OA_CLI_OUTPUT"
ENV_TIMEOUT = "OA_CLI_TIMEOUT"
ENV_CONFIG = "OA_CLI_CONFIG"  # 配置文件路径覆盖（测试/CI 隔离）
ENV_PASSWORD = "OA_CLI_PASSWORD"  # 登录密码（CI 非交互；交互场景走 getpass）


@dataclass
class CliConfig:
    """CLI 运行配置（终端形态最小集：端点/输出/超时 + 登录令牌）。"""

    endpoint: str = DEFAULT_ENDPOINT
    output: str = DEFAULT_OUTPUT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    access_token: str | None = None
    refresh_token: str | None = None
    email: str | None = None


def config_path(path_override: str | Path | None = None) -> Path:
    """配置文件路径：显式参数 > OA_CLI_CONFIG > ~/.ontology-agent/config.json。"""
    if path_override is not None:
        return Path(path_override)
    env = os.environ.get(ENV_CONFIG, "").strip()
    if env:
        return Path(env)
    return Path.home() / ".ontology-agent" / "config.json"


def _read_json_file(path: Path) -> dict[str, Any]:
    """读 JSON 文件；缺失/损坏一律回退空表（CLI 不因配置文件损坏而崩溃）。"""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _first_str(*candidates: str | None) -> str | None:
    """取第一个非空字符串（空串视同未设置）。"""
    for item in candidates:
        if item is not None and item.strip():
            return item.strip()
    return None


def load_config(path_override: str | Path | None = None) -> CliConfig:
    """加载配置：文件为底、环境变量覆盖；非法值逐项回退默认，不抛异常。"""
    raw = _read_json_file(config_path(path_override))
    auth = raw.get("auth")
    auth_map = auth if isinstance(auth, dict) else {}

    try:
        timeout = float(raw.get("timeout", DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    output = str(raw.get("output", DEFAULT_OUTPUT))
    if output not in VALID_OUTPUTS:
        output = DEFAULT_OUTPUT

    endpoint = str(raw.get("endpoint", DEFAULT_ENDPOINT)).rstrip("/") or DEFAULT_ENDPOINT
    config = CliConfig(
        endpoint=endpoint,
        output=output,
        timeout_seconds=timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS,
        access_token=_first_str(auth_map.get("access_token")),
        refresh_token=_first_str(auth_map.get("refresh_token")),
        email=_first_str(auth_map.get("email") if isinstance(auth_map.get("email"), str) else None),
    )

    env_endpoint = _first_str(os.environ.get(ENV_ENDPOINT))
    if env_endpoint:
        config.endpoint = env_endpoint.rstrip("/")
    env_token = _first_str(os.environ.get(ENV_TOKEN))
    if env_token:
        config.access_token = env_token
    env_output = _first_str(os.environ.get(ENV_OUTPUT))
    if env_output in VALID_OUTPUTS:
        config.output = env_output
    try:
        env_timeout = float(os.environ[ENV_TIMEOUT])
    except (KeyError, TypeError, ValueError):
        env_timeout = 0.0
    if env_timeout > 0:
        config.timeout_seconds = env_timeout
    return config


def save_tokens(
    *,
    access_token: str,
    refresh_token: str | None = None,
    email: str | None = None,
    expires_in: int | None = None,
    path_override: str | Path | None = None,
) -> Path:
    """登录令牌落盘：合并进既有配置（保留 endpoint 等项），文件权限 0600（POSIX）。"""
    path = config_path(path_override)
    raw = _read_json_file(path)
    auth = raw.get("auth")
    auth_map = dict(auth) if isinstance(auth, dict) else {}
    auth_map.update(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "email": email,
            "expires_in": expires_in,
        }
    )
    raw["auth"] = auth_map
    _write_private_json(path, raw)
    return path


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """0600 私有写：os.open 预置权限 + chmod 兜底；Windows 权限尽力而为（异常不视为错误）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows chmod 语义受限，仅只读位
        pass
