# -*- coding: utf-8 -*-
"""分词一致性工具（03 决议红线：训练/推理两端禁止重分词）。

GLiNER 按空格切词并只在词粒度上预测跨度；中文没有空格，必须：
  1. 用 jieba 分词后以空格连接喂给模型（推理侧 zh() 与训练侧 tokenized_text 同源）；
  2. 数据集存储 tokenized_text，训练端绝不重新分词；
  3. LLM 合成/模板生成的字符跨度必须经 char_span_to_token_span 映射成词跨度再入库。

本模块是唯一允许调用 jieba 的地方；词典由 dictionaries/domain_terms.dic 固化，
pin jieba==0.42.1，升级词典须重新生成全部数据集。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import jieba

_DICT_PATH = Path(__file__).resolve().parents[2] / "dictionaries" / "domain_terms.dic"

_loaded = False


def load_domain_dict(dict_path: Path | None = None) -> int:
    """加载固化领域词典（幂等）。词典缺失直接抛错——分词一致性红线不允许静默降级。"""
    global _loaded
    path = dict_path or _DICT_PATH
    if not path.exists():
        raise FileNotFoundError(f"领域词典缺失：{path}（分词一致性红线，禁止无词典分词）")
    added = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        word = parts[0]
        freq = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        tag = parts[2] if len(parts) > 2 else None
        if word not in jieba.dt.FREQ:  # 只统计新增，保证幂等
            jieba.add_word(word, freq=freq, tag=tag)
            added += 1
    _loaded = True
    return added


@lru_cache(maxsize=100_000)
def _tokenize_cached(text: str) -> tuple[tuple[str, int, int], ...]:
    return tuple(jieba.tokenize(text))


def tokenize_with_spans(text: str) -> list[tuple[str, int, int]]:
    """分词并保留字符偏移。加载词典后必须先于一切分词调用。"""
    if not _loaded:
        load_domain_dict()
    # (word, start, end)；去掉纯空白 token，但保留其余字符偏移原样
    return [(w, s, e) for w, s, e in _tokenize_cached(text) if w.strip()]


def to_gliner_text(text: str) -> str:
    """中文原文 -> GLiNER 输入（空格连接）。"""
    return " ".join(w for w, _, _ in tokenize_with_spans(text))


def tokenized_text(text: str) -> list[str]:
    """训练格式用：token 列表（保证与空格连接后逐词一致）。"""
    return [w for w, _, _ in tokenize_with_spans(text)]


def char_span_to_token_span(text: str, char_start: int, char_end: int) -> tuple[int, int] | None:
    """字符跨度 -> 词跨度 [i, j]（闭区间）。无重叠时返回 None。"""
    tokens = tokenize_with_spans(text)
    hits = [i for i, (_, s, e) in enumerate(tokens) if s < char_end and e > char_start]
    if not hits:
        return None
    return hits[0], hits[-1]


def token_span_to_surface(text: str, i: int, j: int) -> str:
    """词跨度 -> 原文表面值（去掉分词空格，还原连续文本）。"""
    tokens = tokenize_with_spans(text)
    chars: list[str] = []
    for _, s, e in tokens[i : j + 1]:
        chars.append(text[s:e])
    return "".join(chars)
