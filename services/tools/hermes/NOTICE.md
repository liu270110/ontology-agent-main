# NOTICE — services/tools/hermes 中收编自上游的工具

## 来源

- **来源仓**：[hermes-agent-main](https://github.com/NousResearch/hermes-agent)（Nous Research，**MIT License**）
- **上游本地快照**：`ods-project/hermes-agent-main`（主仓内只读）
- **上游路径**：`tools/<file>.py`
- **收录日期**：2026-10-04
- **收编方式**：adapt（收编标准：零第三方依赖、纯函数/独立脚本、不 import hermes 运行时；每文件头加一行 `# 来源：hermes-agent-main tools/<file>.py（MIT）`，fuzzy_match.py 因 shebang 置于第 2 行）

## 许可

上游 LICENSE（仓库根）原文关键行：

```
MIT License

Copyright (c) 2025 Nous Research
```

## 收录清单（6 个）与逐文件适配说明

| 文件 | 上游路径 | 适配说明 |
| ---- | ---- | ---- |
| ansi_strip.py | `tools/ansi_strip.py` | 原样收编（仅加来源头）；stdlib `re`，ANSI/控制字符/Unicode TAG 清洗 |
| binary_extensions.py | `tools/binary_extensions.py` | 原样收编（仅加来源头）；无 import |
| fuzzy_match.py | `tools/fuzzy_match.py` | LLM 编辑容错替换策略链；删除文件尾 PLUGIN-COMPAT typing 再导出块（List/Tuple，仅服务 hermes 外部插件，本仓无消费方）；`zip` 补 `strict=`（一处 `strict=True` 有前置等长校验，一处 `strict=False` 保持上游截断语义）；E741 变量 `l` → `line` |
| threat_patterns.py | `tools/threat_patterns.py` | 提示注入/提示件/外传检测正则库；超长正则行改相邻字面量拼接（正则语义不变）；正则数据内的 hermes 路径模式（`~/.hermes/.env` 等）按上游原样保留——它们是检测规则数据，非运行时依赖 |
| schema_sanitizer.py | `tools/schema_sanitizer.py` | 原样收编（仅加来源头 + ruff 自动修）；工具 JSON schema 规范化 |
| osv_check.py | `tools/osv_check.py` | MCP 扩展包 OSV 恶意软件预检；**两处 hermes 运行时依赖已改写**：① `_disk_cache_path()` 由 `hermes_constants.get_hermes_home()` 改为 `OSV_CHECK_CACHE_DIR` 环境变量（未设置则仅用进程内缓存，fail-open 语义不变）；② `_save_disk_cache()` 由 `utils.atomic_write_text` 改为 stdlib tempfile + `os.replace` 原子写；UA `hermes-agent-osv-check/1.0` → `ontology-agent-osv-check/1.0` |

## 本批验证过但排除的工具及原因

| 工具 | 排除原因（import 集违规） |
| ---- | ---- |
| url_safety.py | `from hermes_constants import get_hermes_home_override`、`from utils import is_truthy_value` |
| env_probe.py | `from hermes_cli._subprocess_compat import windows_hide_flags` |
| working_diff.py | `from hermes_cli._subprocess_compat import ...` |
| arg_coercion.py | `from tools.registry import registry`（与 hermes 工具注册表耦合） |
| web_result_cache.py | `from utils import atomic_json_write` |
| tool_output_limits.py | `from hermes_constants import hermes_home_key` |
| patch_parser.py | `tools.file_operations_common.PatchResult`（TYPE_CHECKING 引用，且本批 6 席已满） |
| read_extract.py | 依赖可选第三方包 `anydoc`，不满足零第三方依赖 |

## 门禁记录（2026-10-04）

- import 集验证：`rg "^\s*(import|from)\s+" *.py` 过滤 `hermes|state|approval|bot|tools.|utils` → 零匹配
- `python -m ruff check services/skills services/tools/hermes` → All checks passed
- `py_compile` → 6 文件全过
