# tests/sandbox/test_env_denylist.py
"""K5 门 3 宿主 env 刷洗表单测（方案依据=docs/Agent/13 §10；上游=prime-agent #3345 剥离表）。

覆盖：刷洗表 ≥10 项含敏感族锚点；命中剥离+剥离清单；未命中透传+纯函数不改入参；
spec_from_mapping 反序列化边界 env 透传+剥离告警；docker_backend.create 容器组装点
接线（假 docker client 断言 containers.run 收到的 environment 已剥离）。

K9-c/B-6 追加（方案依据=docs/Agent/13 §15）：后缀通配双层命中——*_API_KEY/*_TOKEN/*
*_SECRET 未列名新凭证变量自动剥离；精确枚举项不回归。
"""

import logging
from pathlib import Path

from docker.errors import NotFound

from services.sandbox.runtime.backend import (
    HOST_ENV_DENYLIST,
    ProvisionSpec,
    sanitize_env,
    spec_from_mapping,
)
from services.sandbox.runtime.docker_backend import DockerBackend


def test_刷洗表_不少于10项且含敏感族锚点():
    # Assert：≥10 项 + GIT/SSH/云/LLM 凭证族锚点（13 §10 点名族）
    assert len(HOST_ENV_DENYLIST) >= 10
    for name in (
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "SSH_AUTH_SOCK",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GIT_HTTP_LOW_SPEED_LIMIT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_SECRET",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        assert name in HOST_ENV_DENYLIST, name


def test_刷洗_命中即剥离并返回剥离清单():
    # Arrange：混排命中与非命中
    env = {
        "PATH": "/usr/bin",
        "GITHUB_TOKEN": "gh.secret",
        "HOME": "/root",
        "AWS_SECRET_ACCESS_KEY": "aws.secret",
    }
    # Act
    clean, stripped = sanitize_env(env)
    # Assert：命中全剥离，非命中原样，剥离清单完整
    assert clean == {"PATH": "/usr/bin", "HOME": "/root"}
    assert "GITHUB_TOKEN" not in clean and "AWS_SECRET_ACCESS_KEY" not in clean
    assert set(stripped) == {"GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"}


def test_刷洗_未命中透传且不改入参():
    # Arrange
    env = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
    # Act
    clean, stripped = sanitize_env(env)
    # Assert：透传 + 纯函数（防御拷贝，入参后续变异不回流）
    assert stripped == ()
    assert clean == {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
    assert clean is not env
    env["PATH"] = "/changed"
    assert clean["PATH"] == "/usr/bin"


def test_刷洗_spec反序列化_env透传并剥离(caplog):
    # Arrange：env 混命中项；daemon 侧反序列化入口
    with caplog.at_level(logging.WARNING, logger="services.sandbox.runtime.backend"):
        # Act
        spec = spec_from_mapping(
            {"instance_id": "i-deny", "env": {"PATH": "/bin", "DEEPSEEK_API_KEY": "sk.secret"}}
        )
    # Assert：剥离后落 spec + 告警留痕
    assert spec.env == {"PATH": "/bin"}
    assert any("DEEPSEEK_API_KEY" in r.getMessage() for r in caplog.records)


def test_刷洗_spec反序列化_无env键_缺省空dict与既有调用零差():
    # Act
    spec = spec_from_mapping({"instance_id": "i-plain"})
    # Assert：既有调用（全仓 3 处调用方均不传 env）行为不变
    assert spec.env == {}


async def test_刷洗_docker容器组装点_直灌前剥离(tmp_path: Path):
    # Arrange：假 docker client 捕获 containers.run 组装参数（不连真 daemon）
    fake = _FakeDockerClient()
    backend = DockerBackend(client=fake, snapshot_dir=tmp_path)
    spec = ProvisionSpec(
        instance_id="i-wire",
        base_image="python:3.12-slim",
        env={"PATH": "/bin", "GITHUB_TOKEN": "gh.secret", "SSH_AUTH_SOCK": "/tmp/agent.sock"},
    )
    # Act
    await backend.create(spec)
    # Assert：容器收到的 environment 已剥离命中项
    assert fake.containers.run_kwargs["environment"] == {"PATH": "/bin"}


async def test_刷洗_docker容器组装点_剥离发生告警(tmp_path: Path, caplog):
    fake = _FakeDockerClient()
    backend = DockerBackend(client=fake, snapshot_dir=tmp_path)
    spec = ProvisionSpec(instance_id="i-warn", base_image="python:3.12-slim", env={"GH_TOKEN": "x"})
    with caplog.at_level(logging.WARNING, logger="services.sandbox.runtime.docker_backend"):
        await backend.create(spec)
    assert any("GH_TOKEN" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- K9-c 后缀通配双层命中


def test_后缀通配_未列名新凭证变量自动剥离():
    # Arrange：三家新厂商凭证（不在精确枚举表内，按命名惯例落标准后缀族）+ 普通变量
    env = {
        "PATH": "/usr/bin",
        "MYAPP_API_KEY": "my.secret",
        "VENDOR_TOKEN": "v.secret",
        "X_SERVICE_SECRET": "x.secret",
    }
    # Act
    clean, stripped = sanitize_env(env)
    # Assert：后缀命中即剥；普通变量透传
    assert clean == {"PATH": "/usr/bin"}
    assert set(stripped) == {"MYAPP_API_KEY", "VENDOR_TOKEN", "X_SERVICE_SECRET"}


def test_精确枚举项不回归_通配未吞精确语义():
    # Assert：精确表里非凭证后缀的项（GIT_ASKPASS/SSH_AUTH_SOCK/GOOGLE_APPLICATION_CREDENTIALS）
    # 与含 TOKEN 字样但不在表内的变量，行为均与 K5 时点一致
    env = {"GIT_ASKPASS": "/path/askpass", "SSH_AUTH_SOCK": "/tmp/agent.sock", "TOKEN_COUNTER": "7"}
    clean, stripped = sanitize_env(env)
    assert set(stripped) == {"GIT_ASKPASS", "SSH_AUTH_SOCK"}  # 精确项照剥
    assert clean == {"TOKEN_COUNTER": "7"}  # TOKEN_COUNTER 不以 _TOKEN 结尾 → 不误剥


def test_后缀通配_反序列化边界同步生效():
    # Act：spec_from_mapping 入口对通配命中项同样剥离
    spec = spec_from_mapping({"instance_id": "i-wild", "env": {"MYAPP_API_KEY": "my.secret", "PATH": "/bin"}})
    # Assert
    assert spec.env == {"PATH": "/bin"}


# ---------------------------------------------------------------- 假 docker client


class _FakeContainers:
    def __init__(self) -> None:
        self.run_kwargs: dict = {}

    def get(self, name: str) -> object:
        raise NotFound("no such container")

    def run(self, image: str, **kwargs: object) -> object:
        self.run_kwargs = dict(kwargs)
        return object()


class _FakeVolumes:
    def create(self, name: str, labels: dict | None = None) -> None: ...
    def get(self, name: str) -> object:
        raise NotFound("no such volume")


class _FakeNetworks:
    def get(self, name: str) -> object:
        return object()  # 网络已存在 → 不触发 create


class _FakeDockerClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()
        self.volumes = _FakeVolumes()
        self.networks = _FakeNetworks()
