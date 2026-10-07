# drawing-probe —— 图纸能力实测靶

图纸能力达标实测的验收靶与回归复跑靶:七个独立小工具覆盖「PDF 载体诊断 → 平台 HTTP 面实测 → 本地后端启动 → 嵌入协议垫片 → golden 评测 → docling 布局引擎对照 → 抽取链路加固实验」,用于验收图纸摄取/检索/终审链路的真实表现,并可在后续回归中复跑对照。

| 脚本 | 作用 |
| ---- | ---- |
| `probe_pdf.py` | PDF 载体诊断(纯标准库,非平台读取链):矢量/光栅、有无文本层、页面尺寸、编码器统计,解释「平台摄取链读不了」的根因分层 |
| `live_probe.py` | 平台 HTTP 面实测探针:E1 文件通道 / E2 字节直传 / E3 检索验证 / E4 标题栏文本对照,逐项打印证据并落盘 `out/live_probe_result.json` |
| `run_backend.py` | 本地后端启动器:支持 `PROBE_PORT`,内置 Windows Proactor→Selector 事件循环修补(psycopg 异步兼容) |
| `embed_shim.py` | Ollama→TEI 嵌入协议垫片(历史用途,见下) |
| `eval_golden.py` | golden 评测:客户资产 golden × 平台终审候选,字段级 P/R/F1 与宏平均(golden 路径经 `GOLDEN_PATH` 注入,永不入库) |
| `docling_probe.py` | docling 布局引擎真机实验:表格 grid 配对/markdown 行序两级提取 × pdfium 文本流与 bbox 空间两路线三向对照,九字段 golden 判分(样图/golden 路径仅经参数与 `GOLDEN_PATH` 传入,永不入库;依赖锁外 docling,独立实验脚本) |
| `extract_lab.py` | 抽取链路加固实验:乱序 PDF 文本 × 受约束抽取空候选根因实测,提示词变体×参数矩阵分组对照(hint/few-shot/no_think/冻结参数;样图 golden 经 `LAB_PDF_A`/`LAB_GOLDEN` 注入,零内置样例值,依赖本机 vLLM) |

## 复跑三步

```bash
# 1) 起后端(独立端口,避开常驻开发实例)
PROBE_PORT=8365 OA_EMBED_PROTOCOL=tei OA_OLLAMA_BASE_URL=http://127.0.0.1:18002 \
  .venv/Scripts/python.exe services/tools/drawing-probe/run_backend.py

# 2) 跑探针:样图为本地客户资产、永不入库,路径经环境变量传入
#    (未设置 PROBE_PDF_A / PROBE_PDF_B 时,E1/E2 打印「样图未配置,跳过」并跳过,不报错)
PROBE_BASE=http://127.0.0.1:8365/api/v1 \
PROBE_PDF_A=<本地样图A路径> PROBE_PDF_B=<本地样图B路径> \
  .venv/Scripts/python.exe services/tools/drawing-probe/live_probe.py

# 3) 判读:E1 应 422(契约无文件通道)、E2 应 415/code3004(载体不受理)、E4 应 indexed 七步全绿
```

## 环境变量

| 变量 | 说明 |
| ---- | ---- |
| `PROBE_BASE` | 探针目标基址,缺省 `http://127.0.0.1:8364/api/v1` |
| `PROBE_PDF_A` / `PROBE_PDF_B` | 本地样图路径(客户资产,不入库);缺省不配置则跳过样图实验 |
| `PROBE_TENANT_ID` | 探针租户。**全链路验证(含 LLM 抽取落库)需指向既有开发租户**(避免 kb_facts 外键悬挂);缺省随机 uuid4,仅够验证受理与索引链路 |
| `GOLDEN_PATH` | `eval_golden.py` 的 golden JSON 路径(客户资产,永不入库,必填) |
| `PROBE_PORT` | `run_backend.py` 监听端口,缺省 8364 |
| `OA_EMBED_PROTOCOL` / `OA_OLLAMA_BASE_URL` | 平台嵌入协议与嵌入服务地址(样例走 TEI) |

## embed_shim.py(历史用途)

平台已内置 `OA_EMBED_PROTOCOL` 双协议(Ollama/TEI)之后,本垫片仅服务无 TEI 直连能力的旧环境:在 11434 端口把 Ollama 嵌入协议(`POST /api/embed`)翻译成 TEI(`POST /embed`)。新环境一律直接配 `OA_EMBED_PROTOCOL=tei`,不再需要垫片。

## 出处

设计依据:docs/Agent/09(本地文档,未入库)。
