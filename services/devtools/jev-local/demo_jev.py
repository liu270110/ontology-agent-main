"""
本地版 Jev 演示：用开源的 GLiNER 实现 Jev(TypeSafe AI) 的两类核心能力。

Jev 是闭源 API（不开放权重），无法下载；社区公认的开源替代是 GLiNER。
它能做到 Jev 的"系统一"式判定——不生成自然语言，直接输出带置信度的
结构化结果，毫秒级返回：

  1) 结构化抽取：从自然语言中零样本抽取实体/参数，输出类型化 JSON
  2) 意图/工具路由：判断一句话命中哪个预定义类别（给 Agent 选工具用）

用法:
    python demo_jev.py
首次运行会自动从 hf-mirror.com 下载模型（约 1.1GB，多语言版，支持中文）。
"""

import json
import os
import time

# 国内网络：走 HF 镜像（尊重用户已配置的 HF_ENDPOINT）；禁用 xet 传输（镜像下更稳）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import jieba
import torch
from gliner import GLiNER

MODEL_ID = "urchade/gliner_multi-v2.1"


def zh(text: str) -> str:
    """GLiNER 按空格切词，中文整句会变成一个"词"导致只能预测整句跨度。
    先用 jieba 分词、以空格连接；输出侧再用 replace(" ", "") 还原。"""
    return " ".join(w for w in jieba.cut(text) if w.strip())


def pick_device() -> str:
    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        print(f"[设备] 使用 GPU: {name}")
        return "cuda"
    print("[设备] 未检测到 CUDA，使用 CPU（装 CUDA 版 torch 后自动切 GPU）")
    return "cpu"


def demo_extract(model):
    """能力一：零样本结构化抽取 -> 类型化 JSON（Jev 的看家本领）
    注: GLiNER-Multi 用英文标签效果远好于中文标签，中文文本也能正常抽取。"""
    print("\n=== 能力一：结构化抽取（零样本，无需训练） ===")
    labels = ["person name", "time", "location", "meeting type"]
    text = "张三下周三下午3点在北京和李四开产品评审会"
    t0 = time.perf_counter()
    entities = model.predict_entities(zh(text), labels, threshold=0.4)
    dt = (time.perf_counter() - t0) * 1000

    type_zh = {"person name": "人名", "time": "时间", "location": "地点", "meeting type": "会议类型"}
    result = [
        {
            "type": type_zh.get(e["label"], e["label"]),
            "value": e["text"].replace(" ", ""),
            "confidence": round(e["score"], 3),
        }
        for e in entities
    ]
    print(f"输入: {text}")
    print(f"输出({dt:.0f} ms):")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def demo_route(model):
    """能力二：意图路由 -> 直接给出决策和置信度（给 Agent 选工具）
    分类任务需要给文本加任务前缀，模型才会把整句当作待分类对象。"""
    print("\n=== 能力二：意图/工具路由（Agent 的「系统一」决策） ===")
    tools = {
        "weather query": "天气查询",
        "meeting scheduling": "会议预订",
        "knowledge base search": "知识库检索",
        "code search": "代码搜索",
        "casual chat": "闲聊",
    }
    queries = [
        "帮我看看明天上海要不要带伞",
        "把周五上午和产品团队对一下排期",
        "ontology-agent 这个仓库的 MCP 出口是怎么设计的",
    ]
    for q in queries:
        text = f"Classify the user request into one of the tool categories. Request: {zh(q)}"
        t0 = time.perf_counter()
        hits = model.predict_entities(text, list(tools), threshold=0.25)
        dt = (time.perf_counter() - t0) * 1000
        if hits:
            best = max(hits, key=lambda e: e["score"])
            decision = tools.get(best["label"], best["label"])
            conf = best["score"]
        else:
            decision, conf = "无法判定", 0.0
        print(f"  {q!r} -> 路由到 [{decision}] 置信度 {conf:.2f} ({dt:.0f} ms)")


def main():
    device = pick_device()
    print(f"[加载] {MODEL_ID} ...")
    t0 = time.perf_counter()
    model = GLiNER.from_pretrained(MODEL_ID).to(device).eval()
    print(f"[加载完成] {time.perf_counter() - t0:.1f}s")

    demo_extract(model)
    demo_route(model)

    print("\n提示: 要微调成你自己专属的判定模型，把训练数据放到本目录下的")
    print("      train.json（格式可参考本目录的 train_sample.json），")
    print("      参考 https://github.com/urchade/GLiNER 的 finetune 脚本。")


if __name__ == "__main__":
    main()
