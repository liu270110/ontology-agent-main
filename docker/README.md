# docker/ — GPU 推理资源体

平台主 Compose 编排在 [deploy/](../deploy/)（nginx/backend/五存储，14 篇 §1）；本目录是**独立的 GPU 资源体**（14 篇 §9.1：不并入主编排），v1 单机上与主 Compose 共存，企业化时整体搬去独立 GPU 服务器，只改编排不改代码。

## 内容

| 文件 | 作用 |
| ---- | ---- |
| `docker-compose.gpu.yml` | vLLM（推理体，OpenAI 兼容，端口 8001）+ TEI bge-m3（向量体，端口 8002）；tei-rerank P2 已注释 |

## 启动（开发机）

前置：Docker Desktop 启用 WSL2 后端 + GPU 直通（设置 → Resources → GPU）。

```bash
cd docker
docker compose -f docker-compose.gpu.yml up -d
# 首次会经 hf-mirror 拉取 Qwen3-4B-AWQ（~3GB）与 bge-m3，耐心等 healthcheck 变 healthy
docker compose -f docker-compose.gpu.yml ps
```

起服后切换本地渠道：根目录 `.env` 注释 DeepSeek 三行、放开 vLLM 两行
（`OA_LLM_BASE_URL=http://127.0.0.1:8001/v1`、`OA_LLM_MODEL=local-main`）。

## 变量覆盖（同目录 .env 或环境变量，`docker/.env` 已 gitignore）

| 变量 | 默认 | 说明 |
| ---- | ---- | ---- |
| `VLLM_MODEL` | `Qwen/Qwen3-4B-AWQ` | 生产换 `Qwen/Qwen3-30B-A3B` AWQ/FP8（14 篇 §9.2） |
| `VLLM_IMAGE` | `vllm/vllm-openai:latest` | **生产必须锁固定版本**（14 篇 §6 禁 latest） |
| `TEI_IMAGE` | TEI `cpu-latest` | 生产锁版本 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | 海外环境可覆盖回官方 |
| `HF_TOKEN` | 空 | 仅拉 gated 模型需要 |

## 纪律（14 篇 §9.6）

- **训练与推理永不同时**：跑 LLaMA-Factory QLoRA 前先 `down` 本编排并确认显存排空；`deploy/gpu-mode` 三态切换脚本落地前手动执行；
- vLLM 参数改动（模型/显存/窗口）= 推理体内部优化，上层经 ModelGateway 零感知；
- 验收指标见 14 篇 §9.3 SLO（TTFT P95 < 2s、单流 ≥ 30 tok/s，生产硬件口径）。
