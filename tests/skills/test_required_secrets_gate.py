# tests/skills/test_required_secrets_gate.py
"""K5 G-6 缩减版门 2 登记校验单测（方案依据=docs/Agent/13 §10；上游=deer-flow §10 三门缩减）。

覆盖：register/ingest_scan 逐名查询凭证池，缺失标 unprovisioned（不阻断 listed）、
端口未注入 fail-closed 全记缺失、API 投影透出声明集/缺失集/unprovisioned 布尔。
凭证池适配=CredentialPool.registered（只读查询，tests/platform/test_cred_pool.py 单测）。
门 1 声明解析单测见 tests/skills/test_required_secrets_decl.py。
"""

import uuid

from services.skills.api.schemas.skill import SkillOut
from services.skills.business.scanner import ScannedAsset
from services.skills.business.service import SkillMarketService
from services.skills.domain.model.skill import SkillEntry, SkillOrigin
from services.skills.domain.repo.secrets_query import SecretsQuery


class _FakeSecrets:
    """SecretsQuery 假体（登记名集构造；K5 门 2 查询形状）。"""

    def __init__(self, *names: str) -> None:
        self._names = frozenset(names)
        self._shape: SecretsQuery = self  # 协议形状静态锚点（假体漂移即类型检查报错）

    def registered(self, name: str) -> bool:
        return name in self._names


class _MemRepo:
    """SkillRepository 内存假体（register/ingest 单测最小面）。"""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, SkillEntry] = {}

    async def get(self, skill_id: uuid.UUID) -> SkillEntry | None:
        return self.rows.get(skill_id)

    async def find_by_name_version(self, tenant_id: uuid.UUID, name: str, version: str) -> SkillEntry | None:
        return next(
            (e for e in self.rows.values() if e.tenant_id == tenant_id and e.name == name and e.version == version),
            None,
        )

    async def add(self, entry: SkillEntry) -> None:
        self.rows[entry.id] = entry

    async def save(self, entry: SkillEntry) -> None:
        self.rows[entry.id] = entry

    def repo_rows(self) -> dict[uuid.UUID, SkillEntry]:
        """取数口（单测断言用，避免直捅私有 _repo）。"""
        return self.rows

    async def list(self, tenant_id, *, query, offset, limit):  # noqa: ANN001, ARG002
        rows = [e for e in self.rows.values() if e.tenant_id == tenant_id]
        return rows[offset : offset + limit], len(rows)


async def test_门2_声明凭证全部已登记_缺失快照为空():
    # Arrange：池内已登记 GITHUB_TOKEN
    market = SkillMarketService(repo=_MemRepo(), secrets=_FakeSecrets("GITHUB_TOKEN"))
    # Act
    entry = await market.register(
        tenant_id=uuid.uuid4(),
        name="it-全命中",
        source_uri="u://hit",
        required_secrets=("GITHUB_TOKEN",),
    )
    # Assert：缺失快照空 + 照常直通 listed（K5 门 2 不阻断上架）
    assert entry.required_secrets == ("GITHUB_TOKEN",)
    assert entry.missing_secrets == ()
    assert entry.status.value == "listed"


async def test_门2_缺失凭证标unprovisioned_不阻断listed(caplog):
    # Arrange：池内只有 GITHUB_TOKEN，声明了 DEEPSEEK_API_KEY
    import logging

    market = SkillMarketService(repo=_MemRepo(), secrets=_FakeSecrets("GITHUB_TOKEN"))
    with caplog.at_level(logging.WARNING):
        # Act
        entry = await market.register(
            tenant_id=uuid.uuid4(),
            name="it-半缺失",
            source_uri="u://half",
            required_secrets=("GITHUB_TOKEN", "DEEPSEEK_API_KEY"),
        )
    # Assert：缺失清单精确 + listed 不阻断 + 告警留痕
    assert entry.missing_secrets == ("DEEPSEEK_API_KEY",)
    assert entry.status.value == "listed"
    assert any("unprovisioned" in r.message for r in caplog.records)


async def test_门2_查询端口未注入_fail_closed全记缺失():
    # Arrange：组合根本批不注入（None）——无法确认即记缺失（消费方可感知）
    market = SkillMarketService(repo=_MemRepo())
    # Act
    entry = await market.register(
        tenant_id=uuid.uuid4(),
        name="it-未接线",
        source_uri="u://none",
        required_secrets=("GITHUB_TOKEN", "HF_TOKEN"),
    )
    # Assert
    assert entry.missing_secrets == ("GITHUB_TOKEN", "HF_TOKEN")
    assert entry.status.value == "listed"


async def test_门2_扫描入库_声明与缺失随资产入库():
    # Arrange：repo 来源资产带声明（扫描器投影 → 用例校验 → 聚合）
    repo = _MemRepo()
    market = SkillMarketService(repo=repo, secrets=_FakeSecrets("GITHUB_TOKEN"))
    assets = [
        ScannedAsset(
            name="scanned-gh",
            description="d",
            version="1.0.0",
            source_uri="scanned-gh/SKILL.md",
            body_bytes=64,
            required_secrets=("GITHUB_TOKEN", "GH_TOKEN"),
        ),
        ScannedAsset(name="scanned-plain", description="d", version="1.0.0", source_uri="p/SKILL.md", body_bytes=8),
    ]
    # Act
    result = await market.ingest_scan(tenant_id=uuid.uuid4(), assets=assets)
    # Assert：全量新增；声明资产缺失=GH_TOKEN，无声明资产缺失=空
    assert (result.created, result.skipped) == (2, 0)
    by_name = {e.name: e for e in repo.repo_rows().values()}  # 单测断言经假体取数口
    assert by_name["scanned-gh"].missing_secrets == ("GH_TOKEN",)
    assert by_name["scanned-gh"].origin is SkillOrigin.REPO
    assert by_name["scanned-plain"].missing_secrets == ()


def test_门2_API投影_透出声明缺失与unprovisioned布尔():
    # Arrange：一枚带缺失、一枚全登记
    base = dict(tenant_id=uuid.uuid4(), source_uri="u://x", version="1.0.0")
    marked = SkillEntry(name="it-标记", required_secrets=("A_TOKEN", "B_TOKEN"), missing_secrets=("B_TOKEN",), **base)
    clean = SkillEntry(name="it-干净", required_secrets=("A_TOKEN",), missing_secrets=(), **base)
    # Act
    out_marked, out_clean = SkillOut.from_domain(marked), SkillOut.from_domain(clean)
    # Assert：详情面可感知（unprovisioned+缺失清单）；无缺失=False
    assert out_marked.required_secrets == ["A_TOKEN", "B_TOKEN"]
    assert out_marked.missing_secrets == ["B_TOKEN"]
    assert out_marked.unprovisioned is True
    assert out_clean.required_secrets == ["A_TOKEN"]
    assert out_clean.unprovisioned is False
