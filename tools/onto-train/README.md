# onto-train — 本体判定模型训练工程

依据：[resrch-pj/JEV/03-多专家研讨纪要与技术栈决议.md](../../resrch-pj/JEV/03-多专家研讨纪要与技术栈决议.md)（当前权威）。
隔离环境（Python 3.12 + uv），**全局环境已冻结**。

## 快速开始

```bash
cd tools/onto-train
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
| `src/onto_train/generate_positives.py` | 模板正例（GB/T 附录 D 风格，15 模板） |
| `src/onto_train/generate_negatives.py` | 公理负例（7 类扰动算子，可选 pySHACL 验证） |
| `src/onto_train/smoke_train.py` | 10-step 冒烟（gliner pip 包 Trainer + bf16） |
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
| transformers 4.4x（本工程 venv） | 待 uv sync 完成后回填 |

结论：全参 batch2 可行但偏紧（+桌面开销约 6.2GiB），**下一步按决议转 LoRA r=16（嵌入表冻结）**。
