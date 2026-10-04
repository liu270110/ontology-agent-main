# onto-train — 本体判定模型训练工程

依据：[resrch-pj/JEV/03-多专家研讨纪要与技术栈决议.md](../../resrch-pj/JEV/03-多专家研讨纪要与技术栈决议.md)（当前权威）。
隔离环境（Python 3.12 + uv），**全局环境已冻结**。

## 快速开始

```bash
cd services/tools/onto-train
uv sync                    # 自动装 Python 3.12 + 依赖（torch 走 cu128 显式索引）
uv run python src/onto_train/smoke_train.py              # 10-step 冒烟 + 显存实测
uv run python src/onto_train/generate_positives.py       # 模板正例
uv run python src/onto_train/generate_negatives.py --symbolic-check   # 公理负例 + 符号验证
```

## 目录

| 路径 | 说明 |
| --- | --- |
| `configs/labels.json` | 标签注册表：19 实体 + 24 确认关系 + 7 公理算子（领域 IRI 永不入表） |
| `dictionaries/domain_terms.dic` | jieba 固化领域词典（改动=重新生成全部数据+重训） |
| `src/onto_train/tokenizer_utils.py` | 分词一致性唯一入口：jieba.tokenize + char→token 映射（词典缺失即抛错） |
| `src/onto_train/generate_positives.py` | 模板正例（GB/T 附录 D 风格，19 模板，19/19 标签覆盖） |
| `src/onto_train/generate_negatives.py` | 公理负例（7 类扰动算子，`--symbolic-check` 走 pySHACL） |
| `src/onto_train/smoke_train.py` | 10-step 全参冒烟（对照基线） |
| `src/onto_train/smoke_train_lora.py` | 10-step LoRA 冒烟（默认路线） |
| `src/onto_train/llm_rewrite.py` | LLM 改写管线（硬校验拒收；待 ONTO_LLM_* 端点配置） |
| `data/` | 生成的数据集（positives.jsonl / negatives.jsonl） |
| `runs/` | 训练输出（不进 git） |

## 红线（每条对应 03 决议）

1. 一切分词经 `tokenizer_utils`，词典缺失 fail-loud；pin jieba==0.42.1；
2. 训练数据只存 `tokenized_text + ner`（token 跨度），禁止训练端重分词；
3. 标签只用 `configs/labels.json` 的稳定闭集层；本体 changeset 发布须 diff 本表；
4. 负例须过符号验证（pySHACL）才算数——"生成即验证，零标签噪声"；
5. 模型/数据集产物不进 git（Gitee 1MB 红线），登记 MinIO+PG（MLflow，待接）。

## 冒烟基线（2026-09-26，RTX 4060 Laptop 8GB）

| 配置 | 结果 |
| --- | --- |
| transformers 5.16.1（全局） | 训练路径 PASS；bf16 全参 batch2 峰值 5.41 GiB，0.53 s/步 |
| transformers 4.57.6（本工程 venv） | PASS；bf16 全参 batch2 峰值 5.41 GiB，0.45 s/步 |
| **LoRA r=16（venv，默认路线）** | **PASS；峰值 1.60 GiB，可训练参数 0.9%，grad_norm 40~150** |

结论：**LoRA 定为默认路线**（全参 5.41 GiB 偏紧作对照保留）；数据校验 514 条零错误
（2026-09-26 复核：跨度越界 0 / 非法标签 0）。
