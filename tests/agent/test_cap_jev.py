# tests/agent/test_cap_jev.py
"""jev_detect 接线测试（docs/Agent/17 §1 批次 A，红队审查 E1「GLiNER 装了没接线」闭环）。

覆盖：
- 引擎判定：伪 GLiNER 桩——标签命中映射/置信度/实体抽取去空格/无命中判拒/前缀噪声标记；
- 依赖可选：缺库=结构化不可用（JevUnavailableError → 绑定 5002），不强制主依赖；
- 超时钳制：判定超时=结构化 5001（不挂起调用方）；
- 开关关零变化：Settings.jev_enabled 默认 False=不注册工具不加载模型（组合根装配面）；
- 对账防线：标签族与平台 17 静态行动类目录同源（import 期漂移即测试失败）；
- 参数纵深防御：缺 text/空 text=3001 结构化拒绝。

零真实模型加载（predictor 注入桩或 _load_gliner 打桩；AAA + 中文命名，test_cap_* 同款）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from services.agent.api.sessions import _build_chat_capability_bindings
from services.agent.business.capabilities.jev import (
    ENTITY_LABELS,
    JEV_DETECT_ACTION_IRI,
    JevEngine,
    JevUnavailableError,
    action_from_label,
    action_label,
    action_names,
    build_jev_binding,
    intent_labels,
    reset_jev_engine_singleton,
)
from services.agent.domain.model.kernel_actions import ExecutionMode, ToolCall
from services.agent.domain.model.kernel_context import TenantContext
from services.platform.config import Settings
from services.platform.errors import ErrorCode

_TENANT = uuid.uuid4()


# ── 构造器 ─────────────────────────────────────────────────────────────────────


class _FakeGliner:
    """伪 GLiNER 桩（predict_entities 协议形；按标签族分流意图/实体两次调用）。"""

    def __init__(
        self,
        *,
        intents: list[dict[str, Any]] | None = None,
        entities: list[dict[str, Any]] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._intents = intents or []
        self._entities = entities or []
        self._delay_s = delay_s
        self.calls: list[tuple[list[str], float]] = []

    def predict_entities(self, text: str, labels: list[str], threshold: float) -> list[dict[str, Any]]:
        self.calls.append((list(labels), threshold))
        if self._delay_s:
            time.sleep(self._delay_s)
        if "file reading" in labels:  # 意图调用（intent_labels 族含 file reading）
            return list(self._intents)
        assert labels == list(ENTITY_LABELS)  # 实体调用（通用实体标签族）
        return list(self._entities)


def _engine(predictor: Any | None, *, timeout_s: float = 10.0) -> JevEngine:
    return JevEngine(model_id="fake/jev-stub", threshold=0.25, timeout_s=timeout_s, predictor=predictor)


def _call(parameters: dict[str, Any]) -> ToolCall:
    return ToolCall(
        action_iri=JEV_DETECT_ACTION_IRI,
        execution_mode=ExecutionMode.READ,
        parameters=parameters,
        param_hash="test-hash",
    )


def _ctx() -> TenantContext:
    return TenantContext(tenant_id=_TENANT, trace_id="trace-jev-test")


@pytest.fixture(autouse=True)
def _stub_zh_tokenize(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """桩路径零真实分词：jieba 为可选依赖（gliner/torch/jieba 同列，锁内外均可缺席），
    缺席时桩路径原样透传——本文件门禁面=引擎判定逻辑（标签映射/去分词空格/噪声标记/
    超时钳制），分词输入不影响桩返回的既定输出；jieba 真分词质量属 jev-local 实测面。
    缺库契约行为由 test_zh_tokenize_缺库_转JevUnavailableError 单测钉死（real_tokenize
    标记豁免本桩）。"""
    if request.node.get_closest_marker("real_tokenize"):
        return
    monkeypatch.setattr("services.agent.business.capabilities.jev.engine._zh_tokenize", lambda t: t)


def _settings(**over: Any) -> Settings:
    return Settings(jwt_secret="x" * 32, **over)


# ── 引擎判定（伪 GLiNER 桩）───────────────────────────────────────────────────


async def test_标签命中_映射回平台行动类与置信度():
    # arrange：桩返回 "file reading" 标签命中（score 0.87）
    engine = _engine(_FakeGliner(intents=[{"label": "file reading", "score": 0.8715, "text": "帮 我 读 一 下"}]))
    # act
    det = await engine.detect("帮我读一下 README.md")
    # assert：映射回平台行动类名 + 置信度 round(3)
    assert det.action == "file_read" and det.confidence == 0.872
    assert det.intents[0]["action"] == "file_read" and det.intents[0]["label"] == "file reading"
    assert engine.threshold == 0.25  # Settings 注入阈值透传（桩收到同值）
    assert engine.loaded and engine.device is None  # 桩形态：已加载但无设备语义


async def test_实体抽取_value去分词空格():
    # arrange：桩返回带分词空格的实体 span（jieba 预分词的输出侧还原契约）
    engine = _engine(
        _FakeGliner(
            intents=[],
            entities=[{"label": "file path", "score": 0.49, "text": "src / main . py"}],
        )
    )
    # act
    det = await engine.detect("读一下 src/main.py")
    # assert：value 已去空格还原原文
    assert det.action == "out_of_scope" and det.confidence == 0.0  # 无意图命中=判拒
    assert det.entities[0]["value"] == "src/main.py" and det.entities[0]["type"] == "file path"


async def test_前缀噪声span_如实标记不遮蔽():
    # arrange：命中 span 落在分类前缀噪声词 "Request"（措辞探针实测的主要退化形态）
    engine = _engine(_FakeGliner(intents=[{"label": "file reading", "score": 0.29, "text": "Request"}]))
    # act
    det = await engine.detect("今天天气如何")
    # assert：照常映射（不静默丢弃）但 raw_spans_noisy=True（审阅可辨，E1 口径诚实）
    assert det.action == "file_read" and det.raw_spans_noisy is True


async def test_空text_值校验拒绝():
    engine = _engine(_FakeGliner())
    with pytest.raises(ValueError, match="非空字符串"):
        await engine.detect_sync("   ")


def test_阈值与超时_构造校验():
    # Arrange / Act / Assert：可变参数边界在构造期收口（D2 纪律）
    with pytest.raises(ValueError):
        JevEngine(model_id="m", threshold=1.5, timeout_s=10.0)
    with pytest.raises(ValueError):
        JevEngine(model_id="m", threshold=0.25, timeout_s=0.0)


# ── 依赖可选与超时钳制（结构化降级）───────────────────────────────────────────


async def test_依赖缺失_引擎不可用_绑定结构化5002(monkeypatch: pytest.MonkeyPatch):
    # arrange：predictor=None 且 _load_gliner 打桩为依赖缺失（importlib 延迟导入失败形态）
    engine = _engine(None)
    monkeypatch.setattr(
        "services.agent.business.capabilities.jev.engine._load_gliner",
        lambda _mid: (_ for _ in ()).throw(JevUnavailableError("GLiNER 依赖未安装")),
    )
    binding = build_jev_binding(engine=engine, settings=_settings(jev_enabled=True))
    # act
    result = await binding.invoke(_call({"text": "任意请求"}), _ctx())
    # assert：结构化 5002 + 指引文案，禁裸异常逃逸
    assert result.ok is False and result.error_code == int(ErrorCode.LLM_UNAVAILABLE)
    assert "依赖" in (result.error_message or "")


def test_真实加载路径_import缺库_转JevUnavailableError(monkeypatch: pytest.MonkeyPatch):
    # arrange：importlib.import_module 对 gliner 抛 ImportError（缺库真形态；其余调用透传）
    import importlib as _importlib

    real_import = _importlib.import_module

    def _fake_import(name: str, *a: Any, **kw: Any) -> Any:
        if name == "gliner":
            raise ImportError("No module named 'gliner'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("services.agent.business.capabilities.jev.engine.importlib.import_module", _fake_import)
    engine = _engine(None)
    # act + assert：ImportError 转结构化 JevUnavailableError（带安装指引）
    with pytest.raises(JevUnavailableError, match="安装指引"):
        engine.warmup()


@pytest.mark.real_tokenize
def test_zh_tokenize_缺库_转JevUnavailableError(monkeypatch: pytest.MonkeyPatch):
    # arrange：importlib.import_module 对 jieba 抛 ImportError（缺库真形态；其余调用透传）
    import importlib as _importlib

    real_import = _importlib.import_module

    def _fake_import(name: str, *a: Any, **kw: Any) -> Any:
        if name == "jieba":
            raise ImportError("No module named 'jieba'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("services.agent.business.capabilities.jev.engine.importlib.import_module", _fake_import)
    # act + assert：缺 jieba 同转结构化 JevUnavailableError（模块 docstring「依赖可选」契约
    # 三件套同列；禁裸 ModuleNotFoundError 逃逸——2026-10-07 全量 pytest 6 例实证修复）
    from services.agent.business.capabilities.jev.engine import _zh_tokenize

    with pytest.raises(JevUnavailableError, match="安装指引"):
        _zh_tokenize("任意请求")


async def test_判定超时_绑定结构化5001():
    # arrange：桩推理 sleep 0.2s，引擎超时钳制 0.05s
    engine = _engine(
        _FakeGliner(intents=[{"label": "file reading", "score": 0.9, "text": "x"}], delay_s=0.2), timeout_s=0.05
    )
    binding = build_jev_binding(engine=engine, settings=_settings(jev_enabled=True))
    # act
    result = await binding.invoke(_call({"text": "读一下文件"}), _ctx())
    # assert：超时=结构化 5001（引擎线程后台自然结束，不挂起调用方）
    assert result.ok is False and result.error_code == int(ErrorCode.LLM_TIMEOUT)


async def test_绑定成功面_action与entities透传_usage零计量():
    engine = _engine(
        _FakeGliner(
            intents=[{"label": "file reading", "score": 0.66, "text": "读一下"}],
            entities=[{"label": "file path", "score": 0.8, "text": "README .md"}],
        )
    )
    binding = build_jev_binding(engine=engine, settings=_settings(jev_enabled=True))
    result = await binding.invoke(_call({"text": "读一下 README.md"}), _ctx())
    assert result.ok is True
    assert result.output["action"] == "file_read"
    assert result.output["entities"][0]["value"] == "README.md"
    assert result.output["model"] == "fake/jev-stub"
    assert result.usage == {"total_tokens": 0}  # 本地推理无 token 计量


async def test_参数纵深防御_缺text空text结构化3001():
    binding = build_jev_binding(engine=_engine(_FakeGliner()), settings=_settings(jev_enabled=True))
    missing = await binding.invoke(_call({}), _ctx())
    blank = await binding.invoke(_call({"text": "  "}), _ctx())
    non_str = await binding.invoke(_call({"text": 123}), _ctx())
    for r in (missing, blank, non_str):
        assert r.ok is False and r.error_code == int(ErrorCode.PARAM_INVALID)


def test_绑定元数据_只读免审批():
    binding = build_jev_binding(engine=_engine(_FakeGliner()), settings=_settings(jev_enabled=True))
    assert binding.meta.name == "jev.detect"
    assert binding.meta.semantic_annotation["action_iri"] == JEV_DETECT_ACTION_IRI
    assert binding.meta.semantic_annotation["channel"] == "tools.bindings"
    assert binding.execution_mode is ExecutionMode.READ  # 只读免审批（B1 基线放行）


# ── 开关关零变化（组合根装配面）───────────────────────────────────────────────


def test_开关默认关_组合根不注册jev不加载模型():
    # arrange：默认 Settings（jev_enabled 缺省 False）
    reset_jev_engine_singleton()
    state = SimpleNamespace(settings=_settings(), chat_orchestrator=SimpleNamespace(subagent_slot=None))
    # act
    names = [b.meta.name for b in _build_chat_capability_bindings(state)]
    # assert：无 jev.detect（零行为变化）；引擎单例未被触发加载
    assert "jev.detect" not in names
    from services.agent.business.capabilities.jev.engine import get_jev_engine

    assert get_jev_engine().loaded is False  # 不注册则模型零加载


def test_开关开_组合根注册jev检测工具():
    state = SimpleNamespace(settings=_settings(jev_enabled=True), chat_orchestrator=SimpleNamespace(subagent_slot=None))
    names = [b.meta.name for b in _build_chat_capability_bindings(state)]
    assert "jev.detect" in names


def test_settings_默认值零行为变化():
    s = Settings(jwt_secret="x" * 32)
    assert s.jev_enabled is False  # 默认关=零行为变化（硬约束）
    assert s.jev_threshold == 0.25
    assert s.jev_timeout_s == 10.0
    assert s.jev_model_id == "urchade/gliner_multi-v2.1"


# ── 对账防线（标签族↔平台行动类目录同源）─────────────────────────────────────


def test_标签族与平台17静态行动类同源():
    # Arrange / Act：平台侧标签族 vs benchmarks 意图目录（IRI 常量同源导入的双向对账）
    from benchmarks.suites.intent.action_catalog import build_action_catalog

    catalog_names = {e.name for e in build_action_catalog()}
    # Assert：集合一致——平台改行动类，此处 import 期即失败（对账防线）
    assert set(action_names()) == catalog_names
    assert len(intent_labels()) == len(catalog_names) == 17
    assert intent_labels() == tuple(sorted(intent_labels()))  # sorted 字节稳定（predict 输入序确定）
    for name in action_names():  # 名↔标签双向映射闭合
        assert action_from_label(action_label(name)) == name


# ── 单例纪律 ─────────────────────────────────────────────────────────────────


def test_进程单例_同settings复用同实例():
    reset_jev_engine_singleton()
    s = _settings(jev_enabled=True)
    from services.agent.business.capabilities.jev.engine import get_jev_engine

    assert get_jev_engine(s) is get_jev_engine(s)
    reset_jev_engine_singleton()  # 测试隔离（清单例）


def test_异步detect与sync一致():
    engine = _engine(_FakeGliner(intents=[{"label": "web search", "score": 0.5, "text": "搜一下"}]))
    det_async = asyncio.run(engine.detect("搜一下 GLiNER"))
    det_sync = engine.detect_sync("搜一下 GLiNER")
    assert det_async.action == det_sync.action == "web_search"
    assert det_async.confidence == det_sync.confidence == 0.5
