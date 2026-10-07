# 01 · Jev 调研与学习笔记（训练/微调核心资料）

> 2026-09-26。原始事实来源见文末链接；论文条目为按 Jev 技术脉络整理的必读清单。

## 1. Jev 是什么（事实层）

- 2026-09-15 由 TypeSafe AI（创始人 Diogo Almeida，ChatGPT/InstructGPT 共同作者）发布。
- 定位"系统一（System One）"模型：不生成自然语言，接收一段**状态** + 一组**预定义选项**
  （单次最多 255 个、每个选项带声明类型），70ms 返回**带校准概率的判定**。
- 商业形态：纯 API，$0.042/M 输入 token，输出免费；64k token 上下文。
- 软件工程价值：Agent 100 次模型调用中约 90 次是"工具选择/结果判断/路由"这类简单判定，
  交给 Jev 后 LLM 只保留 10 次复杂规划与生成。
- **未开源权重、未公开架构、无论文**。backbone 是否 Transformer 均未官宣。

## 2. 训练方法：RLCD（机制层）

**RLCD = Reinforcement Learning for Calibrated Decisions（面向校准决策的强化学习）**，
TypeSafe 自创名称，未发论文。公开材料+社区推断的技术画像：

| 维度 | RLHF | RLCD |
| --- | --- | --- |
| 优化目标 | 人类偏好（哪个回答更好） | 概率真实性（说 0.9 就要真有 90% 对） |
| 训练信号 | 人类投票 → 奖励模型 | 对比正负例 + 概率回归 |
| 失败模式 | 讨好/过自信（幻觉温床） | ——目标就是消灭过自信 |
| 下游用途 | 对话/创作 | 直接当执行阈值（≥阈值自动执行，<阈值转人工） |

社区推断的技术构成（供复现参考）：
1. **监督骨架**：在"状态+选项→正确选项"格式的分类数据上训练（非自回归，单次前向）。
2. **对比负例**：大量"表面相似但错误"的选项做 hard negative（这是校准的关键）。
3. **概率校准**：训练后处理（温度缩放/保形预测类方法）让置信度可用作业务阈值。
4. **拒答机制**：所有选项分数都低时输出"无法判定"（Jev API 的 noul 类语义）。

## 3. 学术脉络：必读论文清单（学习一遍的结果）

按"要复现一个 Jev / 设计我们的本体判定模型"需要读的顺序：

1. **Guo et al. 2017, On Calibration of Modern Neural Networks** (ICML)
   ——校准问题的开山作，温度缩放出自这里。理解"accuracy≠confidence"。
   arXiv:1706.04599
2. **Kadavath et al. 2022, Language Models (Mostly) Know What They Know** (Anthropic)
   ——LLM 自知与校准，Jev 宣传里"消灭幻觉"的学术根据。
   arXiv:2207.05221
3. **Angelopoulos & Bates 2021, A Gentle Introduction to Conformal Prediction**
   ——比"校准概率"更强的承诺：分布无关的覆盖率保证。我们校准层的首选方案。
   arXiv:2107.07511
4. **Zaratiana et al. 2023, GLiNER: Generalist NER via Open-Type Span-Based Extraction** (NeurIPS)
   ——单次前向零样本抽取。开源"系统一"的事实骨架，也是我们本体模型的地基（已跑通）。
   arXiv:2311.08526
5. **Tunstall et al. 2022, SetFit: Efficient Few-Shot Learning Without Prompts**
   ——8 条样本起训的分类微调。路由头最省钱的微调路径。arXiv:2209.11055
6. **Zhou et al. 2023, UniversalNER** / **Wang et al. 2023, InstructUIE**
   ——大模型蒸馏到小抽取器的数据合成路线（Jev 模式：LLM 离线造数据）。
   arXiv:2302.12874 / arXiv:2308.03279
7. **He et al. 2023, DeBERTa-v3** ——GLiNER 同款骨干，理解 disentangled attention。
8. 选读：**Geifman & El-Yaniv, Selective Classification**（拒答理论）、
   **Gu et al. 2018, Non-Autoregressive Neural MT**（非自回归的历史语境）。

## 4. 开源生态（2026-09 时点）

| 模型 | 参数 | 说明 | 对我们的价值 |
| --- | --- | --- | --- |
| **Laya** | 421M | Convai Innovations 出品，开放权重，研究轨迹早于 Jev，可跑浏览器 | 本地"系统一"首选替代，可对比 GLiNER 路线 |
| **Von** | 395M | ModernBERT 双向架构 | 参考其"双向编码器做判定"的思路 |
| Kev / Laya-MLX / CUA-S1-FORMS | — | Jev 发布 2 天内的克隆潮 | 生态风向标 |
| **GLiNER 系**（GLiNER/GLiClass/GLiREL） | 0.1~0.6B | 零样本 NER/分类/关系抽取全家桶 | ★ 本体模型骨干（已落地 tools/jev-local/） |

社区共识：Jev 的"非自回归+typed 输出"本质上是**把 NER 式 span 分类器的接口产品化**，
GLiNER 系就是学术原型，Laya 是更贴近 Jev API 形态的开源复刻。

## 5. 对本项目要点的启示（承接 02 提案）

1. Jev 解决"决策"，LLM 解决"推理/创作"——**都不是我们本体模型要解决的**。
2. Jev 的可复用资产是**接口范式**（typed value + 校准概率 + 拒答）和**校准目标**
   （RLCD 精神），不是模型本身。
3. 我们的判定接口天然 typed：MCP 出口每个 tool 已有 `_meta.x-ontology`
   （action_iri/object_class/guard_rules/risk_level），与 Jev 的"声明类型选项"同构。

## 6. 原始来源

- 腾讯云/腾讯新闻：《Jev 爆火：不生成文字，70 毫秒返回判断，输入 $0.042/M》
- 小宇宙播客：0064《Jev：闭嘴的 AI 更快更便宜》（RLCD 名称出处）
- 知乎专栏：《Jev 模型：又快又便宜，消灭幻觉，轻松打败顶流 LLM》
- [Latent Space: Here are 6 clones of Jev](https://www.latent.space/p/ainews-here-are-6-clones-of-jev-in)
- [Jev vs Laya: Hosted API or Open Weights? (2026 Guide) — Hugging Face blog](https://huggingface.co/blog/sora-2/jev-vs-laya-hosted-api-or-open-weights-2026-guide)
- [Jev vs Laya: Live Demo（421M 参数跑在浏览器 tab 里）— Medium](https://medium.com/@visrow/jev-vs-laya-live-demo-i-ran-a-421-million-parameter-ai-decision-model-inside-a-browser-tab-no-84b86bed1f10)
- [Top 7 Open-Source Jev Alternatives — DataCamp](https://www.datacamp.com/blog/top-open-source-jev-alternatives)
- [Best Open Source Jev Alternatives — Pinggy](https://pinggy.io/blog/best_open_source_jev_alternatives_self_hosted_decision_models)
- [Jev Alternatives: 7 Ways to Make a Typed Decision — opentweet.io](https://opentweet.io)
