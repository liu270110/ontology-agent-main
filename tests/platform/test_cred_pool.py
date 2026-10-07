# tests/platform/test_cred_pool.py
"""CredentialPool 单测（K5 门 2 只读查询面；方案依据=docs/Agent/13 §10）。

轮换/冷却主语义既有测试锚在 tests/agent/test_model_failover.py，此处只测本批新增的
registered() 按名只读查询：命中/未命中/不扰轮换指针。
"""

from services.platform.llm.cred_pool import CredentialPool


def test_registered_按名命中与未命中():
    # Arrange
    pool = CredentialPool(provider="test", keys=("sk-aaa", "GITHUB_TOKEN"))
    # Act / Assert
    assert pool.registered("GITHUB_TOKEN") is True
    assert pool.registered("MISSING_TOKEN") is False


def test_registered_只读_不推进轮换指针():
    # Arrange
    pool = CredentialPool(provider="test", keys=("k1", "k2"))
    first = pool.acquire()
    assert first == "k1"
    # Act：RR 出队一次后（指针在 k2）做若干只读查询
    assert pool.registered("k2") is True
    assert pool.registered("k3") is False
    # Assert：查询零副作用——下次出队仍按原指针取 k2（未被查询扰动）
    assert pool.acquire() == "k2"
