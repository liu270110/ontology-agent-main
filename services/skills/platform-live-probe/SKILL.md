---
name: platform-live-probe
description: 平台 HTTP 面实测复跑技能。验收/回归 ontology-agent 后端(kb 摄取链、七步流水线、检索)时使用——按环境口径起后端、跑探针、按 E1/E2/E4 判读、PG 直查落库状态。配套工具 services/tools/drawing-probe/。
---

# platform-live-probe:平台实测复跑

平台改动的完成标准不是"测试绿",而是**真机探针复跑通过**。本技能定义复跑口径。

## 三步复跑

1. **起后端**(本地验收环境口径;8364 常被并行会话占用,**一律用独立端口**):

   ```
   PROBE_PORT=8365 OA_EMBED_PROTOCOL=tei OA_OLLAMA_BASE_URL=http://127.0.0.1:18002 \
     .venv/Scripts/python.exe services/tools/drawing-probe/run_backend.py
   ```

   - 本地 vLLM 渠道**不需要** OA_LLM_API_KEY(组合根空 key 装配+EMPTY 占位,2026-10-04 已修);
   - 启动器内置 Windows Proactor 循环修补(psycopg 异步必须 Selector 循环),勿绕过启动器直跑 uvicorn;
   - GPU 栈(docker ps 核对 vllm/tei 容器 healthy)未起时先 `sh deploy/up.sh` + GPU override。

2. **跑探针**(指向自己的端口;样图路径经环境变量传入,属客户资产不入库):

   ```
   PROBE_BASE=http://127.0.0.1:8365/api/v1 \
   PROBE_PDF_A="<本地样图A>" PROBE_PDF_B="<本地样图B>" \
   PROBE_TENANT_ID="<既有开发租户,LLM 抽取落库需要>" \
     .venv/Scripts/python.exe services/tools/drawing-probe/live_probe.py
   ```

3. **判读**(期望值,任何偏离=待解释):

   | 实验 | 期望 | 含义 |
   | --- | --- | --- |
   | E1 multipart 直传 | 422(3001) | 文件通道不存在(M3 前契约如此) |
   | E2 字节直传文本通道 | **415(3004)** | 二进制拒收业务错误;出现 500=摄取防御回归 |
   | E4 对照文本全链路 | **indexed,7/7 步** | 语义链健康 |

## 落库核验(不信 API 自报)

```
docker exec onto-agent-postgres-1 psql -U onto -d onto -c \
  "select step,status from kb_pipeline_step where document_id='<E4的doc_id>' order by finished_at;"
docker exec onto-agent-postgres-1 psql -U onto -d onto -c \
  "select seq, embedding is null as no_vec from document_chunks where document_id='<同上>';"
```

七步全 done + `no_vec=f`(向量已落 pgvector)才是全绿;embed 步 failed 而 API 仍报 indexed=嵌入软降级,查后端进程是否带了 OA_EMBED_PROTOCOL 环境(端口被占时最容易起新实例丢 env——netstat 找 PID、核命令行再杀,别盲重启)。

## 边界

- 探针令牌探针自管(out/ 下,已被 .gitignore 挡,永不入库);
- 本技能覆盖验收靶口径;更早历史诊断(协议垫片 embed_shim.py)仅在无 TEI 直连的旧环境使用。
