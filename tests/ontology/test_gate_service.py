# tests/ontology/test_gate_service.py
"""服务端硬门禁用例服务测试（计划 2.1 残尾）：submit 门禁不信任客户端自报 + fail-closed 路径。

- 宪法 3（候选非成品 + 硬门禁任何治理档位不可跳过）：客户端伪造 gate_ok=true，
  制品实际违规 → submit_changeset_for_review 服务端实跑门禁拒绝，五态机不推进；
- 合法制品 → 门禁通过且 GateReport/gate_report 留痕携带 gate 版本号；
- fail-closed：解析失败返回结构化违规（不抛网络类异常）、执行超时抛 4204、
  无制品可校验（门禁不可空跑）与 head 制品缺失均拒绝。
"""

from __future__ import annotations

import time
import uuid

import pytest

from services.ontology.business import ontology_gate
from services.ontology.business.changeset_service import submit_changeset_for_review
from services.ontology.business.ontology_gate import GATE_VERSION, run_changeset_gate
from services.ontology.domain.model.ontology import (
    ChangesetStatus,
    DomainError,
    Ontology,
    OntologyVersionRef,
)

PWR = "http://ontology-agent.local/o/t1/power#"
TENANT = uuid.uuid4()
USER = uuid.uuid4()

_LEGAL_TTL = """
@prefix ob2:  <https://ontology-agent.dev/ns/ob2#> .
@prefix pw:   <http://ontology-agent.local/o/t1/power#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
pw:PowerDevice a owl:Class ; rdfs:subClassOf ob2:Object .
pw:Feeder a owl:Class ; rdfs:subClassOf pw:PowerDevice .
"""

_VIOLATING_TTL = """
@prefix ob2:  <https://ontology-agent.dev/ns/ob2#> .
@prefix pw:   <http://ontology-agent.local/o/t1/power#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
pw:Feeder a owl:Class .
pw:InspectionTask owl:someValuesFrom pw:Feeder .
"""

_BROKEN_TTL = "@prefix pw: <http://ontology-agent.local/o/t1/power#> . pw:Feeder a owl:Class"


class FakeOntologyRepository:
    """内存仓储桩（工厂函数构造）：只实现 submit 用例接触的 read_artifact/save 两个方法。"""

    def __init__(self) -> None:
        self.artifacts: dict[str, str] = {}
        self.saved: list[Ontology] = []

    async def save(self, ontology: Ontology) -> None:
        self.saved.append(ontology)

    async def read_artifact(self, artifact_key: str) -> str:
        if artifact_key not in self.artifacts:
            raise FileNotFoundError(artifact_key)
        return self.artifacts[artifact_key]


def _repo() -> FakeOntologyRepository:
    return FakeOntologyRepository()


def _ontology() -> Ontology:
    return Ontology(tenant_id=TENANT, iri_base=PWR, name="电力停电分析本体")


def _changeset(ontology: Ontology, title: str = "候选本体首发"):
    return ontology.open_changeset(title, applicant_id=USER)


# ---------------------------------------------------------------- 宪法 3：客户端自报不参与判定


async def test_submit_客户端伪造gate_ok_服务端实跑门禁拒绝() -> None:
    # Arrange —— 制品实际违规（DL 公理 someValuesFrom 入 rl 档），客户端伪造 gate_ok=true
    repo = _repo()
    ontology = _ontology()
    changeset = _changeset(ontology)
    # Act / Assert —— 服务端实跑门禁拒绝（4204），聚合不推进
    with pytest.raises(DomainError, match="4204"):
        await submit_changeset_for_review(
            repo,
            ontology,
            changeset.id,
            turtle=_VIOLATING_TTL,
            client_gate_ok=True,  # 伪造自报：不得影响判定
            client_gate_report={"gate_ok": True, "lint_ok": True},
        )
    assert changeset.status is ChangesetStatus.DRAFT  # 五态机未推进（draft→in_review 被门禁拦截）
    assert changeset.gate_ok is False  # 服务端结论覆写客户端自报
    assert changeset.gate_report["server_gate"]["conforms"] is False
    assert changeset.gate_report["client_reported"]["gate_ok"] is True  # 自报仅留痕对照（审计）
    assert repo.saved == []  # 拒绝路径不落库（会话回滚语义由调用方承担）


async def test_submit_合法制品_服务端门禁通过留痕版本号() -> None:
    # Arrange
    repo = _repo()
    ontology = _ontology()
    changeset = _changeset(ontology)
    # Act
    report = await submit_changeset_for_review(repo, ontology, changeset.id, turtle=_LEGAL_TTL)
    # Assert —— 门禁通过且报告携带 gate 版本号；五态机推进 in_review，结论随单落库
    assert report.conforms is True
    assert report.gate_version == GATE_VERSION
    assert changeset.status is ChangesetStatus.IN_REVIEW
    assert changeset.gate_ok is True
    assert changeset.gate_report["gate_version"] == GATE_VERSION
    assert changeset.gate_report["server_gate"]["conforms"] is True
    assert repo.saved == [ontology]  # 服务端结论已随单保存


# ---------------------------------------------------------------- fail-closed 路径


async def test_gate_解析失败_fail_closed_返回结构化违规() -> None:
    # Arrange —— 语法非法制品（三元组残缺，无术语可定位）
    # Act
    report = await run_changeset_gate(_BROKEN_TTL)
    # Assert —— 不抛异常：结构化违规（fail-closed），兜底出处指针保证结论可追溯
    assert report.conforms is False
    assert report.lint_ok is False and report.shacl_conforms is False
    violation = report.violations[0]
    assert violation.stage == "parse"
    assert violation.code == "GATE_PARSE_FAILED"
    assert violation.source and violation.path


async def test_submit_解析失败制品_门禁拒绝不推进() -> None:
    # Arrange
    repo = _repo()
    ontology = _ontology()
    changeset = _changeset(ontology)
    # Act / Assert
    with pytest.raises(DomainError, match="4204"):
        await submit_changeset_for_review(repo, ontology, changeset.id, turtle=_BROKEN_TTL)
    assert changeset.status is ChangesetStatus.DRAFT
    assert changeset.gate_ok is False


async def test_gate_执行超时_fail_closed_抛出4204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange —— 门禁超时阈值压至 0.05s，制品装载挂起（to_thread 慢函数）
    monkeypatch.setattr(ontology_gate, "_GATE_TIMEOUT_SECONDS", 0.05)

    def slow_load(content: str) -> None:  # noqa: ARG001 签名对齐 load_turtle
        time.sleep(0.5)

    monkeypatch.setattr(ontology_gate, "load_turtle", slow_load)
    # Act / Assert —— 超时视为门禁失败：向上抛 4204（fail-closed 拒绝）
    with pytest.raises(DomainError, match="4204 GATE_TIMEOUT"):
        await run_changeset_gate(_LEGAL_TTL)


async def test_submit_无turtle且无head_门禁不可空跑拒绝() -> None:
    # Arrange —— 首次提交不携带 turtle 且聚合无 head 版本
    repo = _repo()
    ontology = _ontology()
    changeset = _changeset(ontology)
    # Act / Assert —— 门禁不可空跑（4204 GATE_NO_CONTENT）
    with pytest.raises(DomainError, match="4204 GATE_NO_CONTENT"):
        await submit_changeset_for_review(repo, ontology, changeset.id, turtle=None)
    assert changeset.status is ChangesetStatus.DRAFT


async def test_submit_head制品缺失_门禁拒绝4201() -> None:
    # Arrange —— 重提交场景（turtle=None）但 head 制品三方缺失
    repo = _repo()
    ontology = _ontology()
    ontology.head_version = OntologyVersionRef(
        version="v1", artifact_key="ontologies/missing/v1.ttl", checksum="a" * 64
    )
    changeset = _changeset(ontology, "重提交")
    # Act / Assert —— 制品缺失转 4201 平台码（门禁对象不可得即拒绝）
    with pytest.raises(DomainError, match="4201 GATE_ARTIFACT_MISSING"):
        await submit_changeset_for_review(repo, ontology, changeset.id, turtle=None)


async def test_submit_重提交复用head制品_违规仍被服务端拒绝() -> None:
    # Arrange —— head 制品在库但内容违规：客户端自报 gate_ok=true 依旧不参与判定
    repo = _repo()
    ontology = _ontology()
    ontology.head_version = OntologyVersionRef(version="v1", artifact_key="ontologies/replay/v1.ttl", checksum="b" * 64)
    repo.artifacts["ontologies/replay/v1.ttl"] = _VIOLATING_TTL
    changeset = _changeset(ontology, "重提交")
    # Act / Assert
    with pytest.raises(DomainError, match="4204"):
        await submit_changeset_for_review(repo, ontology, changeset.id, turtle=None, client_gate_ok=True)
    assert changeset.status is ChangesetStatus.DRAFT
    assert changeset.gate_report["server_gate"]["conforms"] is False
