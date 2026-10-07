"""intent v0 金标集生成器（人工规则构造，零 LLM、零随机）：100 条意图×行动类映射。

产出（datasets/golden_v0.jsonl，文件版本化入库，可由本脚本再生校验）：
每行 {"id","query","expected_action","ambiguity_level","notes","expectation"}——
- id：intent-001..intent-100（分四段连续编排，稳定可引）；
- ambiguity_level：clear（明确直射 40）/ ambiguous（歧义近义 30）/
  need_clarification（需澄清 20）/ out_of_scope（越界 10）；
- expectation：map（映射到 expected_action）/ clarify（应触发 ask_user 澄清）/
  reject（应拒或映射最近+低置信）——判分期望与层级一一对应，metrics.py 据此计分；
- notes：金标依据（歧义条给分辨依据；越界条给最近行动类分析），人工可审。

确定性说明（docs/Agent/16 §1「标注集」纪律 + rag build_v0 同款）：
- 全部条目为**人工规则构造的字面清单**（本文件 `_ITEMS`），不用 LLM 造金标——
  金标本身即审计对象，构造规则可逐条复核；
- 行动类词表取自 benchmarks/suites/intent/action_catalog.py（平台常量导入），
  validate() 断言 expected_action ∈ 词表（越界条 expected_action=null）；
- 零随机：重跑本脚本逐字节稳定（sha256 可作金标指纹，随结果 JSON 落档）。

用法（仓库根）：python benchmarks/suites/intent/datasets/build_golden_v0.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]  # datasets/build_golden_v0.py → 仓库根（深 runner 一层）
if __package__ in (None, ""):  # 直跑形态（python benchmarks/suites/intent/datasets/build_golden_v0.py）：
    sys.path.insert(0, str(ROOT))  # 金标词表校验要 import benchmarks 包（run.py 同款 bootstrap）

DATASET_PATH = Path(__file__).resolve().parent / "golden_v0.jsonl"

# 分布契约（docs/Agent/16 §2 intent；本波 ask：40/30/20/10）
EXPECTED_DISTRIBUTION = {"clear": 40, "ambiguous": 30, "need_clarification": 20, "out_of_scope": 10}
_EXPECTATION_BY_LEVEL = {
    "clear": "map",
    "ambiguous": "map",
    "need_clarification": "clarify",
    "out_of_scope": "reject",
}

# ---------------------------------------------------------------- 金标条目（人工规则构造；逐条可审）

_CLEAR: list[tuple[str, str, str]] = [
    # file_read ×4（通读文件）
    ("帮我读一下 README.md 的内容", "file_read", "直白通读请求，动词「读」+单文件全路径"),
    ("打开 config/app.yaml 看看里面写了什么", "file_read", "「打开…看看写了什么」=通读内容"),
    ("把 src/main.py 前 50 行显示出来", "file_read", "行窗口读取=fs_read 的 offset/limit 面"),
    ("我需要查看 docs/plan.md 讲了什么", "file_read", "通读理解请求"),
    # file_write ×3（整文件写）
    ("新建 notes/meeting.txt，内容是「周三例会纪要」", "file_write", "新建文件+整文件内容=整文件写入"),
    ("把这段配置原样保存成 settings.json", "file_write", "整文件落盘（新文件）"),
    ("帮我生成 todo.md，内容用这份模板", "file_write", "新文件整写"),
    # file_edit ×3（精确替换改既有）
    ("把 README 里的版本号 0.9 改成 1.0", "file_edit", "既有文件内精确旧串替换=file_edit 语义"),
    ("app.yaml 中 debug 那行改成 false", "file_edit", "单点修改既有文件"),
    ("修正 main.py 里的拼写错误 recieve，改成 receive", "file_edit", "精确替换"),
    # file_glob ×3（按名找文件）
    ("列出 src 目录下所有的 .py 文件", "file_glob", "按 glob 模式列文件名（非内容检索）"),
    ("项目里有哪些 markdown 文件", "file_glob", "按扩展名匹配文件"),
    ("找出所有文件名带 test 的文件", "file_glob", "按文件名模式匹配"),
    # file_grep ×3（按内容搜）
    ("搜一下哪些文件里出现了 deprecated 这个词", "file_grep", "按内容正则搜索"),
    ("在代码里找一下所有 TODO 标记", "file_grep", "逐行内容检索"),
    ("哪个文件定义了 run_pipeline 函数", "file_grep", "按内容定位定义处"),
    # todo_write ×3（新增待办）
    ("新建一个任务：周五前完成周报", "todo_write", "新增计划项"),
    ("加一条待办：给客户回电话", "todo_write", "新增待办项"),
    ("把「部署验证」加进我的计划清单", "todo_write", "新增计划项"),
    # todo_update ×3（改既有待办）
    ("把待办清单里第 3 条标记为已完成", "todo_update", "更新既有任务状态"),
    ("周报那条任务改成进行中", "todo_update", "更新既有任务状态"),
    ("给「回电话」这条任务加个高优先级", "todo_update", "更新既有任务属性"),
    # todo_read ×2（看板读取）
    ("看看我现在待办清单上都有什么", "todo_read", "读看板"),
    ("我还有哪些没完成的任务", "todo_read", "读看板过滤未完成"),
    # web_fetch ×3（已知 URL 取正文）
    ("抓取 https://fastapi.tiangolo.com/docs/ 这个页面的正文", "web_fetch", "给定 URL 取内容"),
    ("把 https://example.org/report 的内容拉下来", "web_fetch", "给定 URL 取内容"),
    ("读取网页 https://docs.example.com/guide 并给我正文", "web_fetch", "给定 URL 取内容"),
    # web_search ×3（无 URL 找资料）
    ("帮我搜一下 FastAPI 最新版本的发布说明", "web_search", "无 URL 按关键词找资料"),
    ("网上查一下 pydantic v2 迁移指南", "web_search", "无 URL 搜索"),
    ("搜索一下 ontology engineering 入门教程", "web_search", "无 URL 搜索"),
    # run_terminal ×2（沙箱命令）
    ("在沙箱里执行 pytest -q 看看测试结果", "run_terminal", "明确命令执行请求（沙箱面）"),
    ("跑一下 ls -la 列一下当前目录", "run_terminal", "明确 shell 命令"),
    # subagent_spawn ×2（派生委派）
    ("派一个子代理去整理这份 50 页的报告", "subagent_spawn", "委派独立子任务=派生"),
    ("让子任务分头处理这三个目录的文档", "subagent_spawn", "批量派生"),
    # subagent_wait ×1
    ("等子代理跑完，把结果取回来", "subagent_wait", "取回派生结果"),
    # subagent_interrupt ×1
    ("取消正在跑的那个子代理任务", "subagent_interrupt", "取消在途派生"),
    # chat_answer ×2（直接回答）
    ("根据刚才检索到的证据，直接给我解释停电原因", "chat_answer", "基于证据直接回答（不新增工具动作）"),
    ("直接回答我：什么是本体工程", "chat_answer", "直接问答"),
    # spill_get ×2（溢出取回）
    ("把刚才被截断的那篇文章的完整原文取回来", "spill_get", "spill 指针兑换原文（溢出续读面）"),
    ("取回之前溢出存储的网页正文", "spill_get", "spill 兑换"),
]

_AMBIGUOUS: list[tuple[str, str, str]] = [
    # file_read vs file_grep（通读 vs 按内容定位）
    ("看看 requirements.txt 里装没装 redis", "file_grep", "问「有没有某内容」=按内容定位（grep），非通读"),
    ("帮我看下 CHANGELOG 最新的版本段写了什么", "file_read", "要看内容段落=通读窗口（read），非关键词检索"),
    ("config 里数据库地址是多少", "file_grep", "定位具体键值=grep；金标取定位语义（近义对：read）"),
    ("通读一遍 agent.py，给我讲讲结构", "file_read", "「通读+讲解」=read 全文"),
    ("找出所有包含 password 字样的配置", "file_grep", "按内容搜索"),
    # file_glob vs file_grep vs file_read（按名 vs 按内容）
    ("项目里有没有叫 bench 的文件", "file_glob", "按文件名找=glob（内容检索是 grep——近义对题眼）"),
    ("找找哪个文件里提到了 ontology", "file_grep", "按内容找文件=grep"),
    ("看看有哪些 yaml 配置文件", "file_glob", "按模式列文件=glob"),
    ("这几个 yaml 里哪个配了 redis", "file_grep", "按内容甄别=grep"),
    ("把 tests 目录下的文件都列出来", "file_glob", "列文件名=glob"),
    ("检查一下代码里还有没有 print 调试语句", "file_grep", "逐行内容检查=grep"),
    # file_write vs file_edit（整写 vs 改既有）
    ("把结论追加到 report.md 末尾", "file_edit", "改既有文件=edit（write 是整文件覆盖写——近义对题眼）"),
    ("文件里的日期写错了，把 10 月 3 日改成 10 月 8 日", "file_edit", "既有文件单点替换"),
    ("把这份分析结果保存成 summary.md", "file_write", "新文件整写"),
    ("result.txt 内容不对，重新生成一份完整的", "file_write", "整文件重写（overwrite 面）"),
    ("把方案文档里过时的架构描述替换成新版的", "file_edit", "精确替换既有段落"),
    ("把最终结论写进 conclusion.txt", "file_write", "整文件写入（新文件）"),
    # todo_write vs todo_update vs todo_read
    ("帮我记一下明天下午要开会", "todo_write", "新增计划项"),
    ("把开会那条改成下午四点", "todo_update", "改既有项"),
    ("我的任务都做完了吗", "todo_read", "读看板"),
    ("再加两条待办：买服务器、续域名", "todo_write", "批量新增"),
    ("把已完成的那几条任务归档掉", "todo_update", "更新既有项状态"),
    ("看下这周计划还剩几件事", "todo_read", "读看板"),
    # web_search vs web_fetch（找信息 vs 取已知页）
    ("查一下 RAG 会议论文都有哪些经典的", "web_search", "无 URL 找资料=search"),
    ("打开 https://arxiv.org/abs/2005.11401 看下摘要", "web_fetch", "给定 URL 取内容=fetch（搜索近义对题眼）"),
    ("网上有没有 ontology-agent 平台的介绍资料", "web_search", "无 URL 搜索"),
    ("把 https://example.com/spec 这个页面的表格抓下来", "web_fetch", "给定 URL 抓取"),
    # 委派 vs 命令 vs 直答
    (
        "让一个助手分头去把两个目录的文档都总结一遍",
        "subagent_spawn",
        "委派独立子任务=spawn（确定性计算走 terminal——近义对题眼）",
    ),
    ("统计一下代码总行数", "run_terminal", "确定性命令（wc -l）=沙箱执行"),
    # spill_get vs file_read（溢出兑换 vs 普通读）
    ("刚才那篇长文被截断了，把剩余部分给我", "spill_get", "溢出指针兑换（非工作区文件——近义对题眼）"),
]

_NEED_CLARIFICATION: list[tuple[str, str]] = [
    ("帮我改一下那个文件", "目标文件与改动内容均未给出——信息不足应澄清"),
    ("把上周的记录整理一下", "「记录」对象与整理目标不明"),
    ("更新一下配置", "未指明配置项与目标值"),
    ("搜一下那个报错", "搜索范围与关键词均不明"),
    ("帮我写个文档", "文档主题/内容/落点全部缺失"),
    ("把那个东西跑一下", "执行对象与命令不明"),
    ("看看网页", "缺 URL"),
    ("把任务状态改一下", "未指明哪条任务改成什么状态"),
    ("查一下资料", "查询主题缺失"),
    ("读一下文件", "未指明文件路径"),
    ("清理一下没用的东西", "对象与清理动作均不明（且无删除行动类，须澄清意图）"),
    ("把结果存一下", "存哪/存什么未指明"),
    ("再多搜点相关的东西", "补充方向不明"),
    ("帮我盯着子任务", "盯哪个句柄/盯什么未指明"),
    ("把那个链接打开看看", "缺 URL"),
    ("修一下代码里那个 bug", "文件与缺陷描述均缺失"),
    ("把东西都准备好", "完全无可执行信息"),
    ("帮我安排一下", "安排什么/何时全部缺失"),
    ("接着上次的继续", "「上次」无引用上下文，任务不明"),
    ("把这些都处理了", "「这些」指代不明"),
]

_OUT_OF_SCOPE: list[tuple[str, str]] = [
    ("帮我把这个服务部署到生产环境", "平台 17 静态行动类无 deploy；最近亦无——应拒（或极低置信映射 run_terminal）"),
    ("给我发封邮件通知老板", "无 email 行动类——应拒（最近无）"),
    ("帮我订明天下午的会议室", "无日历/预订行动类——应拒"),
    ("把这段话翻译成英文", "无翻译行动类；最近为 chat_answer（基于证据问答≠翻译）——映射须低置信"),
    ("画一张系统架构图", "无绘图行动类；最近为 chat_answer——映射须低置信"),
    ("把 tmp 目录整个删掉", "fs 族无删除行动类（read/write/edit/glob/grep）——应拒"),
    ("帮我付款续费域名", "无支付行动类——应拒"),
    ("监控线上流量异常并告警", "无监控告警行动类——应拒"),
    ("把知识库模型重新训练一遍", "无训练行动类——应拒"),
    ("把这段录音转成文字", "无语音行动类——应拒"),
]


def build_items() -> list[dict[str, Any]]:
    """构造 100 条金标（确定性：字面清单顺序编排，id 四段连续）。"""
    items: list[dict[str, Any]] = []
    seq = 0
    for level, rows in (
        ("clear", _CLEAR),
        ("ambiguous", _AMBIGUOUS),
        ("need_clarification", _NEED_CLARIFICATION),
        ("out_of_scope", _OUT_OF_SCOPE),
    ):
        for row in rows:
            seq += 1
            if level in ("clear", "ambiguous"):
                query, action, note = row  # type: ignore[misc]
                expected: str | None = action
            else:
                query, note = row  # type: ignore[misc]
                expected = "ask_user" if level == "need_clarification" else None
            items.append(
                {
                    "id": f"intent-{seq:03d}",
                    "query": query,
                    "expected_action": expected,
                    "ambiguity_level": level,
                    "notes": note,
                    "expectation": _EXPECTATION_BY_LEVEL[level],
                }
            )
    return items


def validate(items: list[dict[str, Any]], *, valid_actions: set[str]) -> None:
    """金标契约校验（数量/分布/词表/期望一致性/唯一性/id 连续）——漂移即失败。"""
    if len(items) != 100:
        raise ValueError(f"金标须 100 条，得到 {len(items)}")
    dist: dict[str, int] = {}
    ids: set[str] = set()
    for idx, item in enumerate(items, start=1):
        level = item["ambiguity_level"]
        if level not in EXPECTED_DISTRIBUTION:
            raise ValueError(f"{item['id']} 未知 ambiguity_level: {level}")
        dist[level] = dist.get(level, 0) + 1
        if item["expectation"] != _EXPECTATION_BY_LEVEL[level]:
            raise ValueError(f"{item['id']} expectation 与 ambiguity_level 不一致")
        if not item["query"].strip() or not item["notes"].strip():
            raise ValueError(f"{item['id']} query/notes 不得为空")
        if level == "out_of_scope":
            if item["expected_action"] is not None:
                raise ValueError(f"{item['id']} 越界条 expected_action 须为 null")
        else:
            if item["expected_action"] not in valid_actions:
                raise ValueError(f"{item['id']} expected_action {item['expected_action']!r} 不在平台行动类词表")
        if level == "need_clarification" and item["expected_action"] != "ask_user":
            raise ValueError(f"{item['id']} 澄清条 expected_action 须为 ask_user（平台澄清行动类）")
        expected_id = f"intent-{idx:03d}"
        if item["id"] != expected_id:
            raise ValueError(f"第 {idx} 条 id 应为 {expected_id}，得到 {item['id']}")
        if item["id"] in ids:
            raise ValueError(f"id 重复: {item['id']}")
        ids.add(item["id"])
    if dist != EXPECTED_DISTRIBUTION:
        raise ValueError(f"分布漂移：期望 {EXPECTED_DISTRIBUTION}，得到 {dist}")


def dataset_sha256(items: list[dict[str, Any]]) -> str:
    """金标指纹（逐行 JSON 规范化后哈希；随结果 JSON 落档，ORSI 场景集哈希同源思想）。"""
    canonical = "\n".join(json.dumps(it, ensure_ascii=False, sort_keys=True) for it in items)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main() -> int:
    from benchmarks.suites.intent.action_catalog import build_action_catalog

    items = build_items()
    valid = {e.name for e in build_action_catalog()}
    validate(items, valid_actions=valid)
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(it, ensure_ascii=False, sort_keys=True) for it in items]
    DATASET_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[intent] 金标已写入: {DATASET_PATH}（{len(items)} 条；分布 {EXPECTED_DISTRIBUTION}）")
    print(f"[intent] dataset sha256: {dataset_sha256(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
