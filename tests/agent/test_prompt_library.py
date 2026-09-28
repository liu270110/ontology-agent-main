"""H-1 提示词工程治理批测试：聚合不变式 + 钉死解析器 + builtin 接入 + API 五端点往返。

断言目标（api/01 §5.10 / standards/01 §5.1 版本化红线）：
- 聚合：版本不可变（frozen + append-only）、内容长度上限（32k/8 条×4k/槽位完备性）、
  resolve 缺失版本结构化错误、archive 终态、checksum 同内容稳定/异内容变化；
- 解析器：prompt:{id}@{version|head} 纯解析（违式 3001）、prompt_error_to_gateway 映射；
- builtin：prompt: 钉死引用经注入 resolver 消解为 persona（未接线 fail-closed 5002；
  非 prompt: 前缀行为不变）；
- API（integration，本地 PG 直调端点函数）：POST 201 含 v1 → GET 列表（过滤/分页）→
  GET 详情（版本树）→ PUT 追加新版本（200 带新版本号，v1 不可变）→ DELETE archive 204
  幂等 → resolve_pinned @head/@version/checksum/outbox 事件 → personal 作用域 owner 隔离。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.agent.api.prompts import (
    archive_prompt,
    create_prompt,
    get_prompt,
    list_prompts,
    update_prompt,
)
from services.agent.api.schemas.prompt import (
    FewShotIn,
    PromptCreateIn,
    PromptUpdateIn,
)
from services.agent.business.adapters import BuiltinAdapter, ChatTurn
from services.agent.business.prompts import (
    PromptLibraryService,
    PromptRef,
    ResolvedPrompt,
    format_prompt_ref,
    parse_prompt_ref,
)
from services.agent.business.prompts.resolver import persona_text, prompt_error_to_gateway
from services.agent.data.orm import PromptTemplate as PromptTemplateORM
from services.agent.data.orm import PromptVersion as PromptVersionORM
from services.agent.domain.model.prompt import (
    FewShotExample,
    PromptError,
    PromptScope,
    PromptStatus,
    PromptTemplate,
    VariableDecl,
    compute_checksum,
)
from services.iam.data.orm import Tenant as TenantORM
from services.iam.data.orm import User as UserORM
from services.platform.config import Settings
from services.platform.db.uow import AsyncUnitOfWork
from services.platform.deps import Principal
from services.platform.errors import GatewayError
from services.platform.ports.model_port import ModelUnavailableError
from services.writeback.data.orm import OutboxEventORM

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ══════════════════════════ 一、聚合不变式（纯单测）══════════════════════════

OWNER = uuid.uuid4()


def make_template(**kw: Any) -> PromptTemplate:
    base: dict[str, Any] = {
        "tenant_id": uuid.uuid4(),
        "owner_user_id": OWNER,
        "scope": PromptScope.TENANT,
        "slug": "power-outage-analyst",
        "name": "停电分析人格",
        "system_prompt": "你是电网运维分析专家。",
        "created_by": OWNER,
    }
    base.update(kw)
    return PromptTemplate.create(**base)


def test_聚合_创建即含首版本v1且checksum稳定() -> None:
    """AAA：创建 → 首版本 v1 落链；同内容重算 checksum 一致，改一字节即变化。"""
    tpl = make_template(system_prompt="你是电网运维分析专家。", template=None)
    # Assert
    assert tpl.status is PromptStatus.ACTIVE and len(tpl.versions) == 1
    v1 = tpl.head
    assert v1.version == 1 and v1.created_by == OWNER
    assert v1.checksum == compute_checksum(
        system_prompt="你是电网运维分析专家。", template=None, few_shot=(), variables=()
    )
    assert v1.checksum != compute_checksum(
        system_prompt="你是电网运维分析专家!", template=None, few_shot=(), variables=()
    )


def test_聚合_版本不可变_旧版本只读_追加不改写() -> None:
    """AAA：draft v2 → v1 对象与 checksum 原样（append-only）；frozen 值对象赋值即拒。"""
    tpl = make_template()
    v1_before = tpl.versions[0]
    v1_checksum, v1_text = v1_before.checksum, v1_before.system_prompt
    # Act：追加 v2（部分更新——system_prompt 承接 head，few_shot 替换）
    v2 = tpl.draft_new_version(
        system_prompt="你是电网运维分析专家（二级）。", created_by=OWNER
    )
    # Assert
    assert v2.version == 2 and len(tpl.versions) == 2
    assert tpl.versions[0] is v1_before  # 同一对象未被替换/改写
    assert v1_before.checksum == v1_checksum and v1_before.system_prompt == v1_text
    with pytest.raises(ValidationError):  # frozen 红线：值对象不可赋值
        v2.system_prompt = "篡改"  # type: ignore[misc]


def test_聚合_内容长度上限_32k_8条_槽位完备() -> None:
    """AAA：超限载荷 → PromptError 3001（system_prompt>32k / few_shot>8 / 单条>4k / 槽未声明）。"""
    # Assert（每组 Arrange+Act+Assert 逐条）
    with pytest.raises(PromptError, match="3001"):
        make_template(system_prompt="x" * 32_769)
    with pytest.raises(PromptError, match="3001"):
        make_template(
            system_prompt=None,
            template="输出 {{answer}}",
            variables=[VariableDecl(name="answer")],
            few_shot=[FewShotExample(input="i", output="o")] * 9,
        )
    with pytest.raises(PromptError, match="3001"):
        make_template(
            system_prompt=None,
            template="输出 {{answer}}",
            variables=[VariableDecl(name="answer")],
            few_shot=[FewShotExample(input="i" * 4_097, output="o")],
        )
    with pytest.raises(PromptError, match="3001"):  # 模板槽位必须在 variables 声明
        make_template(system_prompt=None, template="输出 {{undeclared}}")
    with pytest.raises(PromptError, match="3001"):  # system_prompt 与 template 至少一项
        make_template(system_prompt=None, template=None)
    with pytest.raises(PromptError, match="3001"):  # 作用域/形状
        make_template(scope="global")
    with pytest.raises(PromptError, match="3001"):
        make_template(slug="Bad Slug")


def test_聚合_resolve缺失版本_结构化错误含可用范围() -> None:
    """AAA：resolve 不存在版本 → 404 前缀错误，消息含可用版本范围。"""
    tpl = make_template()
    tpl.draft_new_version(system_prompt="v2 内容", created_by=OWNER)
    # Act + Assert
    assert tpl.resolve(1).version == 1 and tpl.resolve(2).version == 2
    with pytest.raises(PromptError) as ei:
        tpl.resolve(99)
    assert str(ei.value).startswith("404") and "1~2" in str(ei.value)


def test_聚合_archive终态_禁止追加版本_重复归档拒绝() -> None:
    """AAA：archive → 状态 archived；追加版本 409；重复 archive 409；运行时引用被拒。"""
    tpl = make_template()
    tpl.archive()
    # Act + Assert
    assert tpl.status is PromptStatus.ARCHIVED
    with pytest.raises(PromptError, match="409"):
        tpl.draft_new_version(system_prompt="归档后变更", created_by=OWNER)
    with pytest.raises(PromptError, match="409"):
        tpl.archive()
    with pytest.raises(PromptError, match="409"):
        tpl.ensure_usable()


def test_聚合_模板槽位静态提取() -> None:
    """AAA：template_slots 提取 {{var}} 槽（确定性、零 LLM）。"""
    tpl = make_template(
        system_prompt=None,
        template="分析 {{region}} 线路，按 {{style}} 输出",
        variables=[VariableDecl(name="region"), VariableDecl(name="style")],
    )
    assert tpl.head.template_slots == ("region", "style")


# ══════════════════════════ 二、钉死解析器（纯单测）═══════════════════════════

def test_解析器_钉死语法_版本与head() -> None:
    """AAA：prompt:{id}@3 → (id, 3)；@head → (id, None)；规范引用往返一致。"""
    tid = uuid.uuid4()
    # Act + Assert
    assert parse_prompt_ref(f"prompt:{tid}@3") == PromptRef(template_id=tid, version=3)
    parsed = parse_prompt_ref(f"prompt:{tid}@head")
    assert parsed.template_id == tid and parsed.version is None
    assert format_prompt_ref(tid, 3) == f"prompt:{tid}@3"


def test_解析器_违式引用_3001结构化错误() -> None:
    """AAA：缺前缀/缺@段/坏 UUID/版本 0 → PromptError 3001。"""
    for bad in ("你是专家", "prompt:no-at-sign", "prompt:not-uuid@1", "prompt:" + str(uuid.uuid4()) + "@0"):
        with pytest.raises(PromptError, match="3001"):
            parse_prompt_ref(bad)


def test_解析器_领域错误到网关映射() -> None:
    """AAA：3001 前缀→400、404→404、409→409（沿 agents 路由同款映射）。"""
    assert prompt_error_to_gateway(PromptError("3001 PARAM_INVALID: x")).status_code == 400
    assert prompt_error_to_gateway(PromptError("404 PROMPT_VERSION_MISSING: x")).status_code == 404
    assert prompt_error_to_gateway(PromptError("409 PROMPT_ARCHIVED: x")).status_code == 409


# ══════════════════════════ 三、builtin 钉死引用接入（纯单测）══════════════════

class RecordingModel:
    """仅 complete_structured 的端口桩：记录 system 入参（回退路径即消费 persona）。"""

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.last_system = kwargs.get("system")  # noqa: B010  桩记录面
        return {"answer": "ok"}


def _turn(system_prompt: str | None) -> ChatTurn:
    return ChatTurn(
        tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        message="线路A为何停电？",
        system_prompt=system_prompt,
    )


def _resolved(text: str) -> ResolvedPrompt:
    tid = uuid.uuid4()
    return ResolvedPrompt(
        template_id=tid,
        slug="s",
        version=1,
        checksum="deadbeef",
        ref=format_prompt_ref(tid, 1),
        system_prompt=text,
        template=None,
        few_shot=(),
        variables=(),
    )


async def test_builtin_钉死引用_经resolver消解为persona() -> None:
    """AAA：turn.system_prompt=prompt: 引用 + 注入 resolver → system 消息=模板人格文本。"""
    captured: list[str] = []

    async def fake_resolver(ref: str) -> ResolvedPrompt:
        captured.append(ref)
        return _resolved("你是停电分析专家（来自模板库 v1）。")

    model = RecordingModel()
    adapter = BuiltinAdapter(model, prompt_resolver=fake_resolver)  # type: ignore[arg-type]
    events = [e async for e in adapter.stream_chat(_turn("prompt:" + str(uuid.uuid4()) + "@1"), _make_ctx())]
    # Assert
    assert captured and captured[0].startswith("prompt:")
    assert model.last_system == "你是停电分析专家（来自模板库 v1）。"
    assert events[-1].kind == "finish"


async def test_builtin_未接线解析器_fail_closed_5002() -> None:
    """AAA：prompt: 引用但 resolver 未装配 → ModelUnavailableError（宁拒不错载）。"""
    adapter = BuiltinAdapter(RecordingModel())
    with pytest.raises(ModelUnavailableError):
        async for _ in adapter.stream_chat(_turn("prompt:" + str(uuid.uuid4()) + "@head"), _make_ctx()):
            pass


async def test_builtin_非引用前缀_行为不变() -> None:
    """AAA：普通人格文本/None → 平台缺省角色约定（现状口径零变化）。"""
    model = RecordingModel()
    adapter = BuiltinAdapter(model)
    async for _ in adapter.stream_chat(_turn("你是运维专家"), _make_ctx()):
        pass
    assert model.last_system is not None and "ontology-agent 平台对话助手" in model.last_system


def _make_ctx() -> Any:
    from services.agent.domain.model.kernel_context import TenantContext

    return TenantContext(tenant_id=uuid.uuid4(), scopes=("session:chat",), trace_id="trace-prompt-test")


# ══════════════════════════ 四、API 五端点往返（integration，本地 PG）══════════


def _principal(tenant_id: uuid.UUID, user_id: uuid.UUID, *extra_scopes: str) -> Principal:
    return Principal(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "roles": ["member"],
            "scopes": ["prompt:read", "prompt:write", *extra_scopes],
            "typ": "access",
            "jti": uuid.uuid4().hex,
        }
    )


@pytest.fixture
async def prompt_env() -> AsyncIterator[tuple[Principal, Principal, AsyncUnitOfWork]]:
    """独立租户+双用户（owner/他者）+UoW；不可达本地 PG 即跳过；结束按 FK 逆序清理。"""
    settings = Settings()
    engine = create_async_engine(settings.pg_dsn)
    try:
        async with engine.connect():
            pass
    except (OSError, SQLAlchemyError):
        await engine.dispose()
        pytest.skip("本地 PG 不可达，跳过 prompt 库集成用例")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        tenant = TenantORM(name="it-提示词租户", slug=f"it-{uuid.uuid4().hex[:12]}")
        db.add(tenant)
        await db.flush()
        owner = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        other = UserORM(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:10]}@it.local", password_hash="it-only")
        db.add_all([owner, other])
        await db.flush()
    uow = AsyncUnitOfWork.from_dsn(settings.pg_dsn)
    yield _principal(tenant.id, owner.id), _principal(tenant.id, other.id), uow
    await uow.aclose()
    async with factory() as db, db.begin():
        for stmt in (
            delete(PromptVersionORM).where(PromptVersionORM.tenant_id == tenant.id),
            delete(PromptTemplateORM).where(PromptTemplateORM.tenant_id == tenant.id),
            delete(OutboxEventORM).where(OutboxEventORM.tenant_id == tenant.id),
            delete(UserORM).where(UserORM.id.in_([owner.id, other.id])),
            delete(TenantORM).where(TenantORM.id == tenant.id),
        ):
            await db.execute(stmt)
    await engine.dispose()


def _create_body(slug: str, **kw: Any) -> PromptCreateIn:
    base: dict[str, Any] = {
        "scope": "tenant",
        "slug": slug,
        "name": "停电分析人格",
        "system_prompt": "你是电网运维分析专家 v1。",
        "template": None,
        "few_shot": [],
        "variables": [],
    }
    base.update(kw)
    return PromptCreateIn(**base)


@pytest.mark.integration
async def test_api_五端点往返_版本化红线(prompt_env) -> None:  # noqa: ANN001
    """AAA：建(201,v1)→列表→详情(版本树)→PUT 追加 v2(200 新版本号,v1 不变)→DELETE 204 幂等。"""
    owner, _, uow = prompt_env
    # Arrange + Act：POST /prompts（201，含首版本）
    created = await create_prompt(
        body=_create_body("power-analyst", few_shot=[FewShotIn(input="问", output="答")]),
        principal=owner,
        uow=uow,
    )
    data = created["data"]
    tid = data["id"]
    assert data["status"] == "active" and len(data["versions"]) == 1
    assert data["versions"][0]["version"] == 1 and data["versions"][0]["checksum"]
    # Act：GET 列表（name 过滤+分页信封）
    listing = await list_prompts(principal=owner, uow=uow, name="停电", offset=0, limit=10)
    assert listing["meta"]["total"] == 1 and listing["data"][0]["id"] == tid
    empty = await list_prompts(principal=owner, uow=uow, name="不存在", offset=0, limit=10)
    assert empty["data"] == [] and empty["meta"]["total"] == 0
    # Act：GET 详情（版本树）
    detail = (await get_prompt(tid, principal=owner, uow=uow))["data"]
    assert detail["head_version"] == 1 and detail["versions"][0]["few_shot"] == [{"input": "问", "output": "答"}]
    v1_checksum = detail["versions"][0]["checksum"]
    # Act：PUT=追加新版本（名字/内容变更都产新版本）
    updated = await update_prompt(
        tid,
        body=PromptUpdateIn(name="停电分析人格·二版", system_prompt="你是电网运维分析专家 v2。"),
        principal=owner,
        uow=uow,
    )
    assert updated["meta"]["version"] == 2  # 200 返回新版本号
    assert updated["data"]["head_version"] == 2 and updated["data"]["version_count"] == 2
    assert updated["data"]["versions"][0]["checksum"] == v1_checksum  # v1 不可变
    assert updated["data"]["versions"][1]["system_prompt"].endswith("v2。")
    # Act：DELETE=archive 软删 204，幂等重复删仍 204；此后列表不含、详情 404、PUT 409
    await archive_prompt(tid, principal=owner, uow=uow)
    await archive_prompt(tid, principal=owner, uow=uow)
    assert (await list_prompts(principal=owner, uow=uow))["meta"]["total"] == 0
    with pytest.raises(GatewayError) as ei404:
        await get_prompt(tid, principal=owner, uow=uow)
    assert ei404.value.status_code == 404
    with pytest.raises(GatewayError) as ei409:
        await update_prompt(tid, body=PromptUpdateIn(system_prompt="复活"), principal=owner, uow=uow)
    assert ei409.value.status_code == 409


@pytest.mark.integration
async def test_api_slug冲突与参数校验(prompt_env) -> None:  # noqa: ANN001
    """AAA：同作用域 slug 重复 → 409；空载荷（双空内容）→ 3001/400。"""
    owner, _, uow = prompt_env
    await create_prompt(body=_create_body("dup-slug"), principal=owner, uow=uow)
    with pytest.raises(GatewayError) as ei:
        await create_prompt(body=_create_body("dup-slug"), principal=owner, uow=uow)
    assert (ei.value.code, ei.value.status_code) == (409, 409)
    with pytest.raises(GatewayError) as ei2:  # 聚合校验：至少一项内容
        await create_prompt(body=_create_body("empty", system_prompt=None), principal=owner, uow=uow)
    assert (ei2.value.code, ei2.value.status_code) == (3001, 400)


@pytest.mark.integration
async def test_api_personal作用域_owner隔离(prompt_env) -> None:  # noqa: ANN001
    """AAA：personal 模板他人列表不可见、详情 404、变更 2002（仅 owner 可写）。"""
    owner, other, uow = prompt_env
    created = await create_prompt(
        body=_create_body("my-persona", scope="personal"), principal=owner, uow=uow
    )
    tid = created["data"]["id"]
    # Assert：他人不可见（列表 0 条；详情 404 不泄露存在性）
    assert (await list_prompts(principal=other, uow=uow, scope="personal"))["meta"]["total"] == 0
    with pytest.raises(GatewayError) as ei404:
        await get_prompt(tid, principal=other, uow=uow)
    assert ei404.value.status_code == 404
    # Assert：他人写入 → 2002/403；owner 自身正常
    with pytest.raises(GatewayError) as ei2002:
        await update_prompt(tid, body=PromptUpdateIn(system_prompt="篡改"), principal=other, uow=uow)
    assert (ei2002.value.code, ei2002.value.status_code) == (2002, 403)
    await archive_prompt(tid, principal=owner, uow=uow)  # owner 自删 204


@pytest.mark.integration
async def test_service_钉死解析_head消解_回执checksum_落事件(prompt_env) -> None:  # noqa: ANN001
    """AAA：resolve_pinned @head→确定版本、@1 带原 checksum；缺失版本 404；归档 409；
    消解落 prompt.ref_resolved outbox 事件（id@version+checksum）。"""
    owner, _, uow = prompt_env
    service = PromptLibraryService(uow)
    created = await service.create(
        tenant_id=owner.tenant_id,
        actor_user_id=owner.user_id,
        scope="tenant",
        slug="pinned-persona",
        name="钉死人格",
        system_prompt="人格 v1。",
    )
    tid = created.id
    await service.update(
        tenant_id=owner.tenant_id,
        actor_user_id=owner.user_id,
        template_id=tid,
        system_prompt="人格 v2。",
    )
    # Act：@head 消解为 v2；@1 钉死旧版本（checksum=落库指纹）
    head = await service.resolve_pinned(tenant_id=owner.tenant_id, ref=f"prompt:{tid}@head")
    v1 = await service.resolve_pinned(tenant_id=owner.tenant_id, ref=f"prompt:{tid}@1")
    # Assert
    assert head.version == 2 and head.system_prompt == "人格 v2。" and head.ref == f"prompt:{tid}@2"
    assert v1.version == 1 and v1.system_prompt == "人格 v1。"
    assert persona_text(head) == "人格 v2。"
    with pytest.raises(GatewayError) as ei404:  # 缺失版本：结构化 404
        await service.resolve_pinned(tenant_id=owner.tenant_id, ref=f"prompt:{tid}@99")
    assert ei404.value.status_code == 404
    with pytest.raises(GatewayError) as ei3001:  # 违式引用：3001/400
        await service.resolve_pinned(tenant_id=owner.tenant_id, ref=f"{tid}@1")
    assert (ei3001.value.code, ei3001.value.status_code) == (3001, 400)
    # Act：归档后引用被拒（409）；outbox 事件按 id@version+checksum 落行
    await service.archive(tenant_id=owner.tenant_id, actor_user_id=owner.user_id, template_id=tid)
    with pytest.raises(GatewayError) as ei409:
        await service.resolve_pinned(tenant_id=owner.tenant_id, ref=f"prompt:{tid}@1")
    assert ei409.value.status_code == 409
    settings = Settings()
    engine = create_async_engine(settings.pg_dsn)
    from sqlalchemy import select

    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        stmt = (
            select(OutboxEventORM)
            .where(
                OutboxEventORM.tenant_id == owner.tenant_id,
                OutboxEventORM.event_type == "prompt.ref_resolved",
            )
            .order_by(OutboxEventORM.created_at)
        )
        events = (await db.execute(stmt)).scalars().all()
        await engine.dispose()
    refs = [e.payload["ref"] for e in events]
    assert refs == [f"prompt:{tid}@2", f"prompt:{tid}@1"]
    assert all(e.payload["checksum"] and e.payload["consumer"] == "api" for e in events)


async def test_组合根接线_builtin携resolver_无租户ctx_fail_closed(monkeypatch):
    """H-1 接线：build_chat_orchestrator 注册的 builtin 携带租户迟绑定解析器；
    无租户上下文时 prompt: 引用 fail-closed 5002（不猜默认租户——多租户红线）。"""
    from services.agent.business.prompts.prompt_service import build_tenant_ctx_prompt_resolver
    from services.platform.errors import GatewayError, tenant_id_ctx

    resolver = build_tenant_ctx_prompt_resolver(session_factory=object())
    try:
        await resolver("prompt:abc@1")
        raised = False
    except GatewayError as exc:
        raised = exc.code == 5002
    assert raised
    # 有租户上下文则进入解析路径（桩 PromptLibraryService 捕获 tenant）
    import services.agent.business.prompts.prompt_service as ps

    captured: dict[str, object] = {}

    class _StubService:
        def __init__(self, uow: object) -> None:
            pass

        async def resolve_pinned(self, *, tenant_id, ref, consumer="api"):
            captured["tenant_id"] = tenant_id
            captured["ref"] = ref
            return object()

    monkeypatch.setattr(ps, "PromptLibraryService", _StubService)
    import services.platform.db.uow as uow_mod

    monkeypatch.setattr(uow_mod, "AsyncUnitOfWork", lambda sf: object())
    tok = tenant_id_ctx.set("11111111-1111-1111-1111-111111111111")
    try:
        await resolver("prompt:abc@2")
    finally:
        tenant_id_ctx.reset(tok)
    assert str(captured["tenant_id"]) == "11111111-1111-1111-1111-111111111111"
    assert captured["ref"] == "prompt:abc@2"
