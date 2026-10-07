# tests/agent/test_skills_catalog.py
"""SKILL.md 技能目录装载注入测试（能力通道竖线②，L1 纯提示层，2026-10-05 批）。

纪律：tmp_path 伪技能库 + 真实仓库目录双口径，零网络零第三方解析库；AAA + 中文命名。
覆盖面（批设计点名）：frontmatter 解析（name/description 必填校验，agentskills.io 兼容
受控子集）/坏文件降级跳过留痕/目录式注入文案（确定性渲染）/白名单与排除项/双适配器
系统提示接线（builtin+claude）/组装点开关与降级。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from services.agent.api.sessions import _build_skills_catalog_segment
from services.agent.business.adapters import builtin as builtin_adapter
from services.agent.business.adapters import claude as claude_adapter
from services.agent.business.adapters.base import ChatTurn
from services.agent.business.chat_orchestrator import ChatOrchestrator
from services.agent.business.prompts.skills_catalog import (
    SkillCatalogEntry,
    SkillCatalogError,
    load_skill_catalog,
    parse_skill_frontmatter,
    render_skill_catalog_segment,
)

GOOD_MD = "---\nname: demo-skill\ndescription: 演示技能：用于验证装载链。\n---\n\n# 正文\n步骤……\n"
QUOTED_MD = '---\nname: quoted\ndescription: "Quoted: with colon, comma."\nversion: 1.0.0\n---\nbody\n'
NESTED_MD = (
    "---\nname: nested-ok\ndescription: 嵌套块之上仍可解析。\nmetadata:\n  source: 上游改写（MIT）\n"
    "  nested: true\n---\nbody\n"
)


def _write_skill(root: Path, dir_name: str, content: str) -> None:
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def _make_turn(**kw: object) -> ChatTurn:
    base: dict[str, object] = {
        "tenant_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "message": "线路 A 为何停电？",
    }
    base.update(kw)
    return ChatTurn(**base)  # type: ignore[arg-type]


# ── frontmatter 解析（agentskills.io 兼容受控子集）────────────────────────


def test_解析_标准frontmatter_取name与description():
    assert parse_skill_frontmatter(GOOD_MD) == ("demo-skill", "演示技能：用于验证装载链。")


def test_解析_引号标量剥离_含冒号逗号():
    assert parse_skill_frontmatter(QUOTED_MD) == ("quoted", "Quoted: with colon, comma.")


def test_解析_嵌套metadata块忽略_顶层键仍取到():
    assert parse_skill_frontmatter(NESTED_MD) == ("nested-ok", "嵌套块之上仍可解析。")


def test_解析_缺description_拒绝():
    try:
        parse_skill_frontmatter("---\nname: only-name\n---\nbody")
    except SkillCatalogError as exc:
        assert "description" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺 description 应拒绝")


def test_解析_缺name_拒绝():
    try:
        parse_skill_frontmatter("---\ndescription: 只有描述\n---\nbody")
    except SkillCatalogError as exc:
        assert "name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺 name 应拒绝")


def test_解析_无frontmatter_拒绝():
    try:
        parse_skill_frontmatter("# 纯正文没有 frontmatter\n")
    except SkillCatalogError as exc:
        assert "frontmatter" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("无 frontmatter 应拒绝")


def test_解析_块标量指示行_按缺字段降级拒绝_不产生垃圾条目():
    """ocr 落修回归（2026-10-05）：name: >- / description: |- 不得把 ">-"/"|" 当值注入目录。"""
    text = "---\nname: >-\ndescription: |-\n  折叠正文\n---\nbody\n"
    try:
        parse_skill_frontmatter(text)
    except SkillCatalogError as exc:
        assert "name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("块标量指示行应按缺字段降级拒绝")
    # 装载面同样跳过（不产生 - >-: |- 垃圾条目）
    assert load_skill_catalog(_tmp_skill(text)) == ()


def _tmp_skill(content: str) -> Path:
    import tempfile

    root = Path(tempfile.mkdtemp())
    _write_skill(root, "block-scalar", content)
    return root


def test_解析_frontmatter未闭合_拒绝():
    try:
        parse_skill_frontmatter("---\nname: x\ndescription: y\n")
    except SkillCatalogError:
        pass
    else:  # pragma: no cover
        raise AssertionError("未闭合 frontmatter 应拒绝")


# ── 装载（坏文件降级 / 白名单排除项）────────────────────────────────────


def test_装载_坏文件跳过留痕_其余正常装载(tmp_path: Path):
    _write_skill(tmp_path, "a-good", GOOD_MD)
    _write_skill(tmp_path, "b-broken", "---\nname: broken\n---\n")  # 缺 description
    (tmp_path / "c-not-dir.txt").write_text("not a dir", encoding="utf-8")  # 非目录跳过
    (tmp_path / "d-no-skill-md").mkdir()  # 无 SKILL.md 跳过
    entries = load_skill_catalog(tmp_path)
    assert [e.name for e in entries] == ["demo-skill"]  # frontmatter name（非目录名）


def test_装载_BOM前缀文件正常装载_不误降级(tmp_path: Path):
    """验收批 ocr 落修回归（session 404643c9，bug/low）：BOM（U+FEFF，Cf 类非空白，strip()
    不除）不得把合法 SKILL.md 误判为「frontmatter 起始缺失」而静默剔除。"""
    skill_dir = tmp_path / "bom-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_bytes(
        b"\xef\xbb\xbf" + GOOD_MD.encode("utf-8")
    )  # 真实 UTF-8 BOM 字节（write_text 无 BOM 编码写不出）
    entries = load_skill_catalog(tmp_path)
    assert [e.name for e in entries] == ["demo-skill"]


def test_装载_exclude排除项生效(tmp_path: Path):
    _write_skill(tmp_path, "keep", GOOD_MD)
    _write_skill(tmp_path, "quoted", QUOTED_MD)
    entries = load_skill_catalog(tmp_path, exclude=("quoted",))
    assert [e.name for e in entries] == ["demo-skill"]


def test_装载_include白名单生效(tmp_path: Path):
    _write_skill(tmp_path, "keep", GOOD_MD)
    _write_skill(tmp_path, "quoted", QUOTED_MD)
    entries = load_skill_catalog(tmp_path, include=("quoted",))
    assert [e.name for e in entries] == ["quoted"]


def test_装载_目录不存在_降级空不抛(tmp_path: Path):
    assert load_skill_catalog(tmp_path / "missing") == ()


def test_装载_重名去重_首个生效(tmp_path: Path):
    _write_skill(tmp_path, "a-first", GOOD_MD)
    dup = "---\nname: demo-skill\ndescription: 重复目录名的后来者。\n---\n"
    _write_skill(tmp_path, "z-second", dup)
    entries = load_skill_catalog(tmp_path)
    assert [e.name for e in entries] == ["demo-skill"]
    assert entries[0].description == "演示技能：用于验证装载链。"


# ── 目录式注入渲染（确定性）────────────────────────────────────────────


def test_渲染_按name排序_确定性_同输入字节一致():
    entries = [
        SkillCatalogEntry(name="zeta", description="后者", source="z/SKILL.md"),
        SkillCatalogEntry(name="alpha", description="前者", source="a/SKILL.md"),
    ]
    assert render_skill_catalog_segment(entries) == render_skill_catalog_segment(list(reversed(entries)))
    segment = render_skill_catalog_segment(entries)
    assert segment.splitlines()[0].startswith("【技能目录")
    assert "- alpha: 前者" in segment
    assert segment.index("- alpha") < segment.index("- zeta")  # 稳定排序


def test_渲染_超长description截断_预算保底():
    entry = SkillCatalogEntry(name="long", description="长" * 500, source="x/SKILL.md")
    segment = render_skill_catalog_segment([entry])
    line = segment.splitlines()[1]
    assert len(line) < 200 and line.endswith("…")


def test_渲染_空集返回空串_不注入裸表头():
    assert render_skill_catalog_segment([]) == ""


# ── 适配器接线（builtin + claude 双口；空段字节不变）────────────────────


def test_builtin适配器_系统提示含技能目录段_位于上下文之前():
    turn = _make_turn(skills_catalog="【技能目录】\n- demo: x", context_text="证据段")
    prompt = builtin_adapter._build_system_prompt(turn)
    assert "【技能目录】" in prompt
    assert prompt.index("【技能目录】") < prompt.index("证据段")  # 目录为进程级稳定前缀，B3 段随后


def test_builtin适配器_空目录段_与既有字节一致():
    turn = _make_turn(context_text="CTX")
    assert builtin_adapter._build_system_prompt(turn) == (
        "你是 ontology-agent 平台对话助手：仅依据给定的记忆与知识证据回答，"
        "证据不足时明确说明；引用事实时保持与证据原文一致。\nCTX"
    )


def test_claude适配器_system块含技能目录段():
    turn = _make_turn(skills_catalog="【技能目录】\n- demo: x", context_text="证据段")
    block = claude_adapter._build_system_block(turn)
    assert len(block) == 1
    text = block[0]["text"]
    assert "【技能目录】" in text
    assert text.index("【技能目录】") < text.index("证据段")
    assert block[0]["cache_control"] == {"type": "ephemeral"}


def test_claude适配器_空目录段_与既有字节一致():
    turn = _make_turn(context_text="CTX")
    assert claude_adapter._build_system_block(turn)[0]["text"] == (
        "你是 ontology-agent 平台对话助手：仅依据给定的记忆与知识证据回答，证据不足时明确说明。\nCTX"
    )


def test_ChatTurn_默认空目录段_向后兼容():
    assert _make_turn().skills_catalog == ""


def test_编排器_透传技能目录段到实例():
    orch = ChatOrchestrator(adapters={}, assembler=SimpleNamespace(), skills_catalog="SEG")
    assert orch._skills_catalog == "SEG"


# ── 组装点（sessions 工厂：真实仓库目录 / 开关 / 降级）───────────────────


def test_组装_真实仓库目录_含捆绑技能且排除项生效():
    settings = SimpleNamespace(
        skills_catalog_dir="services/skills", skills_catalog_include="", skills_catalog_exclude=""
    )
    segment = _build_skills_catalog_segment(settings)
    assert segment.startswith("【技能目录")
    assert "wt-batch-close" in segment  # 真实捆绑技能在册
    settings_excl = SimpleNamespace(
        skills_catalog_dir="services/skills",
        skills_catalog_include="",
        skills_catalog_exclude="wt-batch-close,code-review-ocr",
    )
    segment_excl = _build_skills_catalog_segment(settings_excl)
    assert "wt-batch-close" not in segment_excl and "code-review-ocr" not in segment_excl


def test_组装_dir关闭_返回空串():
    settings = SimpleNamespace(skills_catalog_dir=None, skills_catalog_include="", skills_catalog_exclude="")
    assert _build_skills_catalog_segment(settings) == ""


def test_组装_目录不存在_降级空串不抛():
    settings = SimpleNamespace(skills_catalog_dir="no/such/dir", skills_catalog_include="", skills_catalog_exclude="")
    assert _build_skills_catalog_segment(settings) == ""


def test_组装_旧settings无字段_安全缺省关闭():
    settings = SimpleNamespace()  # getattr 兜底：无 skills_catalog_dir=关闭
    assert _build_skills_catalog_segment(settings) == ""
