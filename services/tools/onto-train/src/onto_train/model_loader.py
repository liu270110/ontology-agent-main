"""模型加载统一入口：在线失败自动降级到本地缓存（MLOps 风险 R5 的修复）。

模型权重首次下载后就留在 ~/.cache/huggingface；from_pretrained 默认仍会
联网校验 revision，镜像抖动即整链路失败。这里先在线、失败即强制
HF_HUB_OFFLINE=1 纯缓存加载——训练/推理不再被镜像可用性绑架。
"""

from __future__ import annotations

import os

_MODEL_ID = "urchade/gliner_multi-v2.1"


def load_gliner(model_id: str = _MODEL_ID):
    """加载 GLiNER：先走镜像在线，失败自动转纯本地缓存。"""
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    from gliner import GLiNER

    try:
        return GLiNER.from_pretrained(model_id)
    except Exception:
        os.environ["HF_HUB_OFFLINE"] = "1"
        return GLiNER.from_pretrained(model_id)
