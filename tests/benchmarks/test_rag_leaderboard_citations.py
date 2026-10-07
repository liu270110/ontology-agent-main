"""rag 闭源榜单引用表单测（口径=suites/rag/README.md「闭源对比口径」+ docs/Agent/16 §5）。

锁定三件事：
1. public_leaderboards.json 条目 schema（source_url 必填 / caveat 非空 / value 可 null / 日期格式）；
2. 三档分级完整性（每条有 tier，A/B/C 至少各一条，A 档=BEIR 族独占）；
3. orsi_link 不误读 citation_only 件（引用表不进 metrics 流 / 能力注册建议）。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from benchmarks.orsi_link import build_suggestions, load_results

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CITATIONS_PATH = _REPO_ROOT / "benchmarks/suites/rag/leaderboard_citations/public_leaderboards.json"

_REQUIRED_FIELDS = ("system", "vendor", "metric", "dataset", "source_url", "retrieved_at", "caveat", "tier", "status")
_VALID_TIERS = ("A", "B", "C")
_VALID_STATUS = ("cited", "not_available")


def _load_citations() -> dict:
    return json.loads(_CITATIONS_PATH.read_text(encoding="utf-8"))


class TestCitationsSchema:
    """条目 schema：字段必填、URL 可核、日期格式、value 可 null。"""

    def test_文件存在且可解析(self):
        # Arrange / Act / Assert
        payload = _load_citations()
        assert payload["nature"] == "citation_only"
        assert len(payload["citations"]) >= 10  # 引用面成规模，非占位

    def test_每条必填字段齐备且非空(self):
        # Arrange
        payload = _load_citations()
        # Act / Assert
        for i, entry in enumerate(payload["citations"]):
            for field in _REQUIRED_FIELDS:
                assert field in entry, f"citations[{i}] 缺字段 {field}"
                assert entry[field] not in (None, ""), f"citations[{i}].{field} 为空"
            assert entry["tier"] in _VALID_TIERS
            assert entry["status"] in _VALID_STATUS

    def test_source_url_必填且为可核_http_url(self):
        # Arrange
        payload = _load_citations()
        # Act / Assert：source_url 即便 not_available 也指向本次核查过的页面（必填）
        for i, entry in enumerate(payload["citations"]):
            url = entry["source_url"]
            assert url.startswith(("https://", "http://")), f"citations[{i}].source_url 非法: {url}"

    def test_retrieved_at_为_ISO_日期格式且同批次一致(self):
        # Arrange
        payload = _load_citations()
        file_date = payload["retrieved_at"]
        # Act / Assert
        datetime.date.fromisoformat(file_date)  # 非法格式抛 ValueError
        for entry in payload["citations"]:
            assert datetime.date.fromisoformat(entry["retrieved_at"]) == datetime.date.fromisoformat(file_date)

    def test_value_可_null_cited_条目必有数_not_available_必为_null(self):
        # Arrange
        payload = _load_citations()
        # Act / Assert
        for i, entry in enumerate(payload["citations"]):
            value = entry.get("value")
            if entry["status"] == "not_available":
                assert value is None, f"citations[{i}] not_available 却填了 value（涉嫌编造）"
                assert "not_available" in entry["caveat"]
            else:
                assert isinstance(value, (int, float)), f"citations[{i}] cited 却无数值"

    def test_caveat_非空且写明口径原因(self):
        # Arrange
        payload = _load_citations()
        # Act / Assert：cited 条目 caveat 必须写清「口径不可直接比」原因；null 条目写明未取到原因
        for i, entry in enumerate(payload["citations"]):
            assert len(entry["caveat"]) >= 15, f"citations[{i}].caveat 过短"
            if entry["status"] == "cited":
                # A/B 档措辞=「口径不可直接比」；C 档=「纯参考不可比」——语义同族，任一即可
                assert "不可直接比" in entry["caveat"] or "不可比" in entry["caveat"], (
                    f"citations[{i}] cited 条未写口径不可直接比原因"
                )

    def test_自算平均值与逐集引用值一致(self):
        # Arrange：BEIR 条目标 value_computed=true，平均值必须可由逐集引用值复算
        payload = _load_citations()
        # Act / Assert
        for entry in payload["citations"]:
            if not entry.get("value_computed"):
                continue
            per_dataset = entry["per_dataset_values"]
            assert len(per_dataset) == 18
            recomputed = round(sum(per_dataset.values()) / len(per_dataset), 4)
            assert recomputed == entry["value"], f"{entry['system']} 平均值与逐集值复算不符"


class TestTierCompleteness:
    """三档分级完整性：每档有代表条目，A 档=BEIR 族独占（红线锁定）。"""

    def test_三档至少各一条(self):
        # Arrange
        payload = _load_citations()
        tiers = {entry["tier"] for entry in payload["citations"]}
        # Act / Assert
        assert {"A", "B", "C"} <= tiers

    def test_A_档仅_BEIR_族且为_nDCG10(self):
        # Arrange：README 红线——A 档唯一直接可比基准=BEIR nDCG@10
        payload = _load_citations()
        # Act / Assert
        a_entries = [e for e in payload["citations"] if e["tier"] == "A"]
        assert a_entries, "A 档为空（BEIR BM25 基线必须落位）"
        for entry in a_entries:
            assert entry["dataset"].startswith("BEIR"), f"{entry['system']} 冒充 A 档（非 BEIR 基准）"
            assert "nDCG@10" in entry["metric"]

    def test_文件级红线与三档定义落位(self):
        # Arrange
        payload = _load_citations()
        # Act / Assert
        assert set(payload["tier_definitions"]) == {"A", "B", "C"}
        assert "禁止直接并列比较" in payload["red_line"]

    def test_B_档条目为标准检索基准或显式空槽(self):
        # Arrange
        payload = _load_citations()
        # Act / Assert：B 档只允许 MTEB/MIRACL/BEIR 系检索基准
        for entry in (e for e in payload["citations"] if e["tier"] == "B"):
            dataset_or_metric = entry["dataset"] + entry["metric"]
            assert any(k in dataset_or_metric for k in ("MTEB", "MIRACL", "BEIR")), f"{entry['system']} 冒充 B 档"


class TestOrsiLinkIgnoresCitations:
    """orsi_link 不误读 citation_only 件：引用表不进能力注册建议。"""

    def test_直接传引用文件返回空(self):
        # Act
        pairs = load_results(_CITATIONS_PATH)
        # Assert
        assert pairs == []

    def test_直接传引用目录返回空(self):
        # Act
        pairs = load_results(_CITATIONS_PATH.parent)
        # Assert
        assert pairs == []

    def test_混入结果目录时被跳过不产生建议(self):
        # Arrange：临时 results 目录=正常 run 件 + 引用件混放（防人工误拷）
        import tempfile

        run = {
            "suite": "rag",
            "scenario": "naive_bm25",
            "status": "passed",
            "metrics": {"recall_at_k": 1.0, "mrr": 0.975},
        }
        with tempfile.TemporaryDirectory() as tmp:
            results_dir = Path(tmp)
            (results_dir / "run-000000-v0.json").write_text(json.dumps(run), encoding="utf-8")
            (results_dir / _CITATIONS_PATH.name).write_text(
                _CITATIONS_PATH.read_text(encoding="utf-8"), encoding="utf-8"
            )
            # Act（repo_root 用 resolve 后长路径——Windows 临时目录短路径与 load_results 内 resolve 不一致）
            pairs = load_results(results_dir)
            suggestions = build_suggestions(pairs, repo_root=results_dir.resolve())
            # Assert：只有 run 件进建议，引用件零泄漏
            assert [path.name for path, _ in pairs] == ["run-000000-v0.json"]
            assert len(suggestions) == 1
            assert suggestions[0]["proposal"]["name"] == "bench.rag.naive_bm25"
