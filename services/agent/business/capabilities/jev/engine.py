"""Jev 判定引擎（GLiNER 懒加载封装；docs/Agent/17 §1 批次 A，jev-local 收编件接线）。

「系统一」式结构化判定——不生成自然语言，直接返回带置信度的类型化结构值：
意图路由（17 平台行动类零样本分类）+ 实体抽取（通用实体标签族），毫秒级返回。

工程纪律：
- **进程内单例**：模型加载一次（get_jev_engine；~1.1GB 权重复载代价不可重复付）；
- **懒加载**：引擎构造不触模型，首次 detect/warmup 才加载（jev_enabled=False 的进程
  零模型加载=零行为变化的加载面）；
- **依赖可选**：gliner/torch/jieba 不在主依赖——importlib 延迟导入，缺库抛
  JevUnavailableError（工具层转结构化错误），**不强制 pyproject 加重依赖**（处置建议
  见 DevResult notes，是否入 dev 组由主会话裁决）；
- **HF 加载**：缓存命中走 snapshot 本地路径直载（local_files_only 全链免 hub 网络检查
  ——2026-10-07 实测：不传该参 transformers tokenizer 装载仍会查 hub，弱网环境挂起）；
  未命中走 hf-mirror 镜像下载（jev-local/demo_jev.py:21 先例照抄）；首跑后即缓存；
- **超时钳制**：单次判定 wait_for(Settings.jev_timeout_s)，超时=结构化超时（不挂起 Run）；
- **推理串行**：GLiNER 推理非线程安全，引擎内 threading.Lock 串行化（并发调用安全）；
- jieba 预分词（GLiNER 按空格切词，中文整句会退化成整句跨度——demo_jev.py:31 zh 先例）。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from services.agent.business.capabilities.jev import labels as jev_labels
from services.platform.config import Settings, get_settings

logger = logging.getLogger(__name__)

# GLiNER 按空格切词，中文需先 jieba 分词（demo_jev.py:31 同款；输出侧 replace 还原）
_CLASSIFY_PREFIX = "Classify the user request into one of the action categories. Request: "


class JevUnavailableError(RuntimeError):
    """GLiNER 运行环境不可用（依赖缺失/模型加载失败）——工具层转结构化错误，不裸逃逸。"""


class _Predictor(Protocol):
    """GLiNER predict_entities 最小协议（tests 注入伪桩；真引擎按此形状调用）。"""

    def predict_entities(self, text: str, labels: list[str], threshold: float) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class JevDetection:
    """单次判定产物（jev_detect 契约；action/confidence 与 intent 金标词表对齐）。"""

    action: str  # 平台行动类名或 out_of_scope（labels.OUT_OF_SCOPE_ACTION）
    confidence: float  # 最佳意图分（0~1，round(3)）
    intents: list[dict[str, Any]]  # 全部意图命中 {label, action, score, span}（降序）
    entities: list[dict[str, Any]]  # 实体命中 {type, value, confidence}（value 已去分词空格）
    latency_ms: float = 0.0  # 引擎侧纯推理耗时（不含排队）
    raw_spans_noisy: bool = field(default=False, repr=False)  # 最佳 span 落在分类前缀噪声词（如实标注）


def _zh_tokenize(text: str) -> str:
    """jieba 分词+空格连接（GLiNER 中文整句跨度退化对策；demo_jev.py zh 先例照抄）。"""
    jieba = importlib.import_module("jieba")
    return " ".join(w for w in jieba.cut(text) if w.strip())


def _load_gliner(model_id: str) -> tuple[Any, str]:
    """懒加载 GLiNER：缓存优先（本地路径直载免 hub），未命中镜像下载。

    importlib 延迟导入 gliner/torch；ImportError → JevUnavailableError（依赖缺失是
    配置态不是代码缺陷——消息带安装指引，调用方结构化透出）。
    返回 (model, device)；device 自适应 CUDA（demo_jev.py pick_device 先例）。
    """
    # HF 镜像环境缺省（jev-local/demo_jev.py:21 先例；platform 层统一出口——能力层禁直读
    # env，07 契约 D1 纪律，tests/agent/test_boundary_discipline.py 门禁）
    from services.platform.hf_env import apply_hf_mirror_defaults

    apply_hf_mirror_defaults()
    try:
        gliner_mod = importlib.import_module("gliner")
    except ImportError as exc:
        raise JevUnavailableError(
            "GLiNER 依赖未安装（gliner/torch/jieba 为可选依赖）。"
            f"安装指引：pip install gliner jieba && pip install torch --index-url "
            f"https://download.pytorch.org/whl/cu128（CUDA 机型）；原始错误：{exc}"
        ) from exc
    local_path: str | None = None
    try:  # 缓存探测：命中=本地路径直载（local_files_only 全链，不触 hub 网络）
        hub = importlib.import_module("huggingface_hub")
        local_path = hub.snapshot_download(model_id, local_files_only=True)
    except Exception:  # noqa: BLE001 ——未缓存/库缺失：走镜像在线装载（首跑一次性下载）
        local_path = None
    try:
        if local_path:
            model = gliner_mod.GLiNER.from_pretrained(local_path, local_files_only=True)
        else:
            model = gliner_mod.GLiNER.from_pretrained(model_id)
    except Exception as exc:
        raise JevUnavailableError(f"GLiNER 模型加载失败（model_id={model_id}）：{exc}") from exc
    torch = importlib.import_module("torch")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    return model.to(device).eval(), device


class JevEngine:
    """GLiNER 判定引擎（懒加载 + 串行推理 + 超时钳制；predictor 注入点供 tests 伪桩）。"""

    def __init__(
        self,
        *,
        model_id: str,
        threshold: float,
        timeout_s: float,
        predictor: _Predictor | None = None,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold 须在 [0,1]，得到 {threshold!r}")
        if timeout_s <= 0:
            raise ValueError(f"timeout_s 须 >0，得到 {timeout_s!r}")
        self._model_id = model_id
        self._threshold = threshold
        self._timeout_s = timeout_s
        self._predictor: _Predictor | None = predictor  # None=真 GLiNER（懒加载）
        self._device: str | None = None
        self._load_seconds: float = 0.0
        self._infer_lock = threading.Lock()  # GLiNER 推理非线程安全（串行化）
        self._load_lock = threading.Lock()

    # ── 配置只读面（bench 结果落档用） ───────────────────────────────────────
    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def device(self) -> str | None:
        """已加载设备（None=未加载）。"""
        return self._device

    @property
    def loaded(self) -> bool:
        return self._predictor is not None

    def load_seconds(self) -> float:
        return self._load_seconds

    # ── 加载 ────────────────────────────────────────────────────────────────
    def warmup(self) -> dict[str, Any]:
        """触发加载（幂等），返回 {model_id, device, load_seconds}（bench 环境指纹落档）。"""
        self._ensure_loaded()
        return {"model_id": self._model_id, "device": self._device, "load_seconds": round(self._load_seconds, 3)}

    def _ensure_loaded(self) -> _Predictor:
        if self._predictor is not None:
            return self._predictor
        with self._load_lock:  # 双检：并发首次调用只加载一次
            if self._predictor is not None:
                return self._predictor
            t0 = time.perf_counter()
            model, device = _load_gliner(self._model_id)
            self._predictor = model
            self._device = device
            self._load_seconds = time.perf_counter() - t0
            logger.info("jev 引擎就绪 model=%s device=%s load=%.1fs", self._model_id, device, self._load_seconds)
            return self._predictor

    # ── 判定 ────────────────────────────────────────────────────────────────
    def detect_sync(self, text: str) -> JevDetection:
        """同步判定（推理在调用线程；bench A2 档经 to_thread 包裹）。"""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text 须为非空字符串")
        predictor = self._ensure_loaded()
        zh = _zh_tokenize(text)
        with self._infer_lock:
            t0 = time.perf_counter()
            raw_intents = predictor.predict_entities(
                f"{_CLASSIFY_PREFIX}{zh}", list(jev_labels.intent_labels()), threshold=self._threshold
            )
            raw_entities = predictor.predict_entities(
                zh, list(jev_labels.ENTITY_LABELS), threshold=self._threshold
            )
            latency_ms = (time.perf_counter() - t0) * 1000
        intents = sorted(
            (
                {
                    "label": hit["label"],
                    "action": jev_labels.action_from_label(hit["label"]),
                    "score": round(float(hit["score"]), 3),
                    "span": str(hit["text"]).replace(" ", ""),
                }
                for hit in raw_intents
            ),
            key=lambda h: -h["score"],
        )
        entities = [
            {
                "type": hit["label"],
                "value": str(hit["text"]).replace(" ", ""),  # 分词空格还原（demo_jev.py:60 先例）
                "confidence": round(float(hit["score"]), 3),
            }
            for hit in raw_entities
        ]
        best = intents[0] if intents else None
        noisy = best is not None and best["span"].lower() in {"request", "classify"}
        return JevDetection(
            action=best["action"] if best and best["action"] else jev_labels.OUT_OF_SCOPE_ACTION,
            confidence=best["score"] if best else 0.0,
            intents=intents,
            entities=entities,
            latency_ms=round(latency_ms, 2),
            raw_spans_noisy=noisy,
        )

    async def detect(self, text: str) -> JevDetection:
        """异步判定：推理入线程池 + wait_for 超时钳制（超时=asyncio.TimeoutError，工具层转结构化）。"""
        return await asyncio.wait_for(asyncio.to_thread(self.detect_sync, text), timeout=self._timeout_s)


# ---------------------------------------------------------------- 进程内单例

_ENGINE: JevEngine | None = None
_ENGINE_LOCK = threading.Lock()


def get_jev_engine(settings: Settings | None = None) -> JevEngine:
    """进程级单例访问（模型加载代价一次性；D2 纪律——可变参数全走 Settings）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            cfg = settings or get_settings()
            _ENGINE = JevEngine(
                model_id=cfg.jev_model_id,
                threshold=cfg.jev_threshold,
                timeout_s=cfg.jev_timeout_s,
            )
        return _ENGINE


def reset_jev_engine_singleton() -> None:
    """测试隔离口（清单例；生产禁用——重复加载代价高）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = None
