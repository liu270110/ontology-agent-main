"""cli.config 单测（文件为底/环境变量覆盖/令牌落盘 0600/损坏回退；tmp_path 隔离，禁触真实家目录）。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from services.cli.config import config_path, load_config, save_tokens

ENV_KEYS = ("OA_CLI_ENDPOINT", "OA_CLI_TOKEN", "OA_CLI_OUTPUT", "OA_CLI_TIMEOUT", "OA_CLI_CONFIG", "OA_CLI_PASSWORD")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """清空全部 OA_CLI_* 环境变量，防开发机真实环境泄漏进用例。"""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _write_config(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_配置加载_文件为底_各节齐全(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange
    cfg = _write_config(
        tmp_path / "config.json",
        {
            "endpoint": "http://file.test/api/v1",
            "output": "json",
            "timeout": 12,
            "auth": {"access_token": "tok-file", "refresh_token": "ref-file", "email": "dev@x.cn"},
        },
    )
    clean_env.setenv("OA_CLI_CONFIG", str(cfg))
    # Act
    config = load_config()
    # Assert
    assert config.endpoint == "http://file.test/api/v1"
    assert config.output == "json"
    assert config.timeout_seconds == 12.0
    assert config.access_token == "tok-file"
    assert config.refresh_token == "ref-file"
    assert config.email == "dev@x.cn"


def test_配置加载_环境变量覆盖配置文件(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange：文件给一套值，环境变量全量覆盖（CI 场景不落盘）
    cfg = _write_config(
        tmp_path / "config.json",
        {"endpoint": "http://file.test/api/v1", "auth": {"access_token": "tok-file"}},
    )
    clean_env.setenv("OA_CLI_CONFIG", str(cfg))
    clean_env.setenv("OA_CLI_ENDPOINT", "http://env.test/api/v1/")
    clean_env.setenv("OA_CLI_TOKEN", "tok-env")
    clean_env.setenv("OA_CLI_OUTPUT", "json")
    clean_env.setenv("OA_CLI_TIMEOUT", "7.5")
    # Act
    config = load_config()
    # Assert：环境优先，endpoint 尾斜杠归一
    assert config.endpoint == "http://env.test/api/v1"
    assert config.access_token == "tok-env"
    assert config.output == "json"
    assert config.timeout_seconds == 7.5


def test_配置加载_文件缺失或损坏回退默认值(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange：先缺失，后损坏
    missing = tmp_path / "nonexistent.json"
    clean_env.setenv("OA_CLI_CONFIG", str(missing))
    assert load_config().endpoint == "http://127.0.0.1:8000/api/v1"
    broken = tmp_path / "broken.json"
    broken.write_text("{not-json", encoding="utf-8")
    clean_env.setenv("OA_CLI_CONFIG", str(broken))
    # Act
    config = load_config()
    # Assert：不崩溃，逐项回退默认
    assert config.endpoint == "http://127.0.0.1:8000/api/v1"
    assert config.output == "table"
    assert config.timeout_seconds == 30.0
    assert config.access_token is None


def test_配置加载_非法超时与非法输出逐项回退(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange
    cfg = _write_config(tmp_path / "config.json", {"output": "yaml", "timeout": "abc"})
    clean_env.setenv("OA_CLI_CONFIG", str(cfg))
    # Act
    config = load_config()
    # Assert
    assert config.output == "table"
    assert config.timeout_seconds == 30.0


def test_登录令牌落盘_合并保留既有配置且可读回(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange：既有文件带 endpoint 与 output
    cfg = tmp_path / "config.json"
    _write_config(cfg, {"endpoint": "http://keep.test/api/v1", "output": "json"})
    clean_env.setenv("OA_CLI_CONFIG", str(cfg))
    # Act
    saved = save_tokens(access_token="acc-1", refresh_token="ref-1", email="dev@x.cn", expires_in=7200)
    config = load_config()
    # Assert：auth 节并入，既有键保留
    assert saved == cfg
    assert config.access_token == "acc-1"
    assert config.refresh_token == "ref-1"
    assert config.email == "dev@x.cn"
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["endpoint"] == "http://keep.test/api/v1"
    assert on_disk["auth"]["expires_in"] == 7200


def test_凭据文件落盘权限_0600仅POSIX断言(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange / Act
    clean_env.setenv("OA_CLI_CONFIG", str(tmp_path / "config.json"))
    saved = save_tokens(access_token="acc-secret")
    # Assert：Windows 的 chmod 仅支持只读位，权限断言仅 POSIX（例外已在报告注明）
    if os.name != "posix":
        pytest.skip("Windows 平台 chmod 语义受限，0600 断言仅 POSIX")
    mode = stat.S_IMODE(os.stat(saved).st_mode)
    assert mode == 0o600


def test_config_path_显式参数优先于环境变量(clean_env: pytest.MonkeyPatch, tmp_path: Path):
    # Arrange
    explicit = tmp_path / "explicit.json"
    clean_env.setenv("OA_CLI_CONFIG", str(tmp_path / "from-env.json"))
    # Act / Assert
    assert config_path(explicit) == explicit
    assert config_path() == tmp_path / "from-env.json"
    clean_env.delenv("OA_CLI_CONFIG")
    assert config_path() == Path.home() / ".ontology-agent" / "config.json"
