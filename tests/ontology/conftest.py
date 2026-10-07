# tests/ontology/conftest.py
"""tests/ontology 共享夹具：ONT-1 一次性 PG 测试库（ont1_pg；经再导出供本目录用例按名请求）。

不触碰既有 11 文件的收集面（夹具按参数名 opt-in，无同名请求即不激活）。
"""

from tests.ontology.ont1_pg import ont1_pg  # noqa: F401  pytest 按名发现（再导出即注册）
