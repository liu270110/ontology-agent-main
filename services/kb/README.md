# kb（知识库 GraphRAG）

api(collections/documents/pipeline/search) + business(七步流水线，断点续跑) + retrieval(chunking/embed BM25+pgvector/hybrid) + data(6 表含 kb_facts 权威表)。

## 解析引擎（OA_KB_PARSER，2026-10-08 组合实验批）

`OA_KB_PARSER`（Settings.kb_parser，`^pdfium|docling|plumber$`，缺省 `pdfium`）选文件通道
preprocess 的文本引擎（business/parsers.py）：

- `pdfium`（缺省）：pypdfium2 文本层直抽，零模型确定性，主依赖；顺带产出 run 级带坐标
  片段（text_runs，标题栏空间级兜底输入）；
- `docling`：懒加载可选引擎（不进主依赖；一次性模型下载建议
  `HF_ENDPOINT=https://hf-mirror.com` 镜像）——版面模型+阅读顺序+内建 OCR（RapidOCR）+
  表格结构，`export_to_markdown()` 阅读序全文写 `meta.content`（chunk/embed/extract 链零
  感知）；`drawing_ir` 记 engine/page_count/pages 每页图幅 pt；无带坐标片段（空间级对空
  列表 no-op，标题栏投影走文本级）；缺库降级 pdfium（meta.degraded=["parser"] +
  parser_requested 留痕）；
- `plumber`：pdfplumber 兜底，降级路径同上。

两级流水线（图纸文档，OA_KB_PARSER=docling）：preprocess 产出 docling 阅读序文本 →
标题栏九字段投影跑在该文本上（文本级「标签: 值」行直配，阅读序对投影正则显著更友好，
命中写 meta.titleblock）→ chunk 步将投影字段作额外语义块追加 → extract（active
kb_extract@v4：编号目录制 + 标题栏结构化线索区，投影产物经 titleblock_fields 注入）。
真机依据（services/devtools/drawing-probe/extract_lab.py，两图宏 F1，golden 字段级口径）：
v4×docling=0.434，v4×pdfium=0.000，v2/v3 纯提示词×docling=0.000——引擎轴与线索区缺一不可。
