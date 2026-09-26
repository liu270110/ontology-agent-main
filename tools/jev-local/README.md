# jev-local — 本地版 Jev（"系统一"结构化判定模型）

## 背景

[Jev](https://zhuanlan.zhihu.com) 是 TypeSafe AI（前 OpenAI 研究员 Diogo Almeida）2026 年 9 月发布的
"系统一"判定模型：**不生成自然语言，直接返回带置信度的类型化结构值**，
主打毫秒级、无幻觉的 Agent 流水线决策（选工具、判结果、抽参数），
号称比 LLM 快约 200 倍、便宜约 400 倍（$0.042/1M 输入 token，输出免费）。

**Jev 不开放权重、只提供 API，无法下载。** 社区公认的开源替代是
[GLiNER](https://github.com/urchade/GLiNER)——本地、免费、开放权重，
能覆盖 Jev 的两类核心任务：

1. **结构化抽取**：零样本从文本抽实体/参数 → 类型化 JSON（无需训练）
2. **意图/工具路由**：判断一句话命中哪个预定义类别 → 直接给决策 + 置信度

## 使用

```bash
# 首次运行自动从 hf-mirror 下载模型（urchade/gliner_multi-v2.1，多语言含中文）
python demo_jev.py
```

要求 CUDA 版 PyTorch（本机 RTX 4060 Laptop 8GB）：

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu128
```

## 训练自己领域的判定模型

> ⚠ 2026-09-26 更新：训练工程已独立落地到 [`tools/onto-train/`](../onto-train/README.md)
> （03 决议：**不用 clone 官方仓库**，gliner 0.2.29 pip 包自带 Trainer；LoRA r=16 冒烟
> 峰值 1.60 GiB）。本目录保留零样本演示与 Jev 背景，训练一律去 onto-train。

数据格式沿用 `train_sample.json`（tokenized_text + ner，token 跨度标注；
必须经 onto-train 的 char→token 映射器生成，禁止手工重分词）。

## 文件

| 文件 | 说明 |
| --- | --- |
| `demo_jev.py` | 结构化抽取 + 意图路由演示（CPU/GPU 自适应） |
| `train_sample.json` | 微调数据格式示例（本体领域） |
