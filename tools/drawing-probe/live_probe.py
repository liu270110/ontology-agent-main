"""平台实测驱动:把本地样图 PDF 按客户端身份投喂给 ontology-agent 平台,采集系统真实反应。

实验设计(全部经平台 HTTP 面 /api/v1,令牌用平台 security 模块签发,与测试同款):
  E1  文件通道存在性:对 POST /kb/documents 发 multipart 文件上传 → 预期 422(契约无文件通道)
  E2  硬着头皮投喂:PDF 字节按 UTF-8 有损解码成字符串直传(mime=application/pdf) → 观察受理与流水线终态
  E3  检索验证:对索引结果提问(图号/标题栏/材料) → 观察检索能否给出图纸语义
  E4  对照组:一段人工标注的标题栏文本走同链路 → 区分「文件进不来」与「语义链是否可用」

样图是本地客户资产、永不入库:路径经环境变量 PROBE_PDF_A / PROBE_PDF_B 传入,
未设置时 E1/E2 打印「样图未配置,跳过」并跳过(不报错)。
租户经 PROBE_TENANT_ID 指向既有开发租户(缺省随机 uuid4,见 README)。

输出:逐实验打印证据,末尾汇总 JSON(写入 out/live_probe_result.json)。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.platform.config import get_settings  # noqa: E402
from services.platform.security import build_claims, encode_token  # noqa: E402

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8364/api/v1")
OUT = Path(__file__).parent / "out"
TERMINAL = {"indexed", "failed"}


def sample_pdfs() -> list[Path]:
    """本地样图(客户资产,不入库):路径经环境变量 PROBE_PDF_A / PROBE_PDF_B 传入。"""
    found = []
    for name in ("PROBE_PDF_A", "PROBE_PDF_B"):
        p = os.environ.get(name)
        if p:
            found.append(Path(p))
    return found


def mint_token() -> str:
    """签发探针访问令牌;租户经 PROBE_TENANT_ID 指向既有开发租户,缺省随机 uuid4。"""
    s = get_settings()
    tenant_id = os.environ.get("PROBE_TENANT_ID") or str(uuid.uuid4())
    token_file = OUT / "probe-auth.txt"  # 本地缓存(gitignored),首行记租户防跨租户复用
    if token_file.exists():  # 复用同租户旧令牌,便于跨运行复查同一批文档
        parts = token_file.read_text(encoding="utf-8").split("\n", 1)
        if len(parts) == 2 and parts[0].strip() == tenant_id:
            old = parts[1].strip()
            try:
                with httpx.Client(timeout=10, headers={"Authorization": f"Bearer {old}"}) as t:
                    if t.get(f"{BASE}/kb/documents?limit=1").status_code == 200:
                        return old
            except httpx.HTTPError:
                pass
    claims = build_claims(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        roles=["owner"],
        scopes=["kb:write", "kb:read", "review:read", "review:approve"],
        typ="access",
        ttl_seconds=3600,
    )
    token = encode_token(claims, s.jwt_secret)
    token_file.write_text(f"{tenant_id}\n{token}", encoding="utf-8")
    return token


def poll_document(c: httpx.Client, doc_id: str, timeout_s: int = 240) -> dict:
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        r = c.get(f"{BASE}/kb/documents/{doc_id}")
        last = r.json()
        status = (last.get("data") or {}).get("status") or last.get("status")
        if status in TERMINAL:
            return last
        time.sleep(4)
    return last


def step_of(d: dict) -> list:
    return (d.get("data") or d).get("pipeline") or (d.get("data") or d).get("steps") or []


def unwrap(body: dict) -> dict:
    """兼容两种响应形态:契约信封 {data,...} 与裸 DTO。"""
    return body.get("data") if isinstance(body.get("data"), dict) else body


def main() -> None:
    OUT.mkdir(exist_ok=True)
    token = mint_token()
    c = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {token}"})
    result: dict = {"experiments": []}

    print("== E0 健康检查 ==")
    r = c.get(f"{BASE.rsplit('/api', 1)[0]}/healthz")
    print(f"GET /healthz -> {r.status_code} {r.text[:120]}")

    print("\n== 建库 ==")
    r = c.post(f"{BASE}/kb/collections", json={"name": f"drawing-probe-{int(time.time())}"})
    print(f"POST /kb/collections -> {r.status_code} {r.text[:200]}")
    kb_id = unwrap(r.json())["id"]

    pdfs = sample_pdfs()
    print("\n== E1 文件通道存在性(multipart 直传 PDF 文件) ==")
    if not pdfs:
        print("样图未配置,跳过(设 PROBE_PDF_A / PROBE_PDF_B 后复跑)")
    for pdf in pdfs:
        r = c.post(
            f"{BASE}/kb/documents",
            files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"collection_id": kb_id, "title": pdf.stem},
        )
        print(f"POST /kb/documents (multipart, {pdf.name}) -> {r.status_code} {r.text[:220]}")
        result["experiments"].append({"exp": "E1", "file": pdf.name, "http": r.status_code, "body": r.text[:400]})

    print("\n== E2 PDF 字节有损解码直传(平台唯一可用通道) ==")
    if not pdfs:
        print("样图未配置,跳过(设 PROBE_PDF_A / PROBE_PDF_B 后复跑)")
    for pdf in pdfs:
        mojibake = pdf.read_bytes().decode("utf-8", errors="replace")
        r = c.post(
            f"{BASE}/kb/documents",
            json={
                "collection_id": kb_id,
                "title": pdf.name,
                "mime_type": "application/pdf",
                "content": mojibake,
            },
        )
        print(f"POST /kb/documents (json, {pdf.name}) -> {r.status_code} {r.text[:220]}")
        if r.status_code >= 400:
            result["experiments"].append({"exp": "E2", "file": pdf.name, "http": r.status_code, "body": r.text[:400]})
            continue
        doc_id = unwrap(r.json())["id"]
        r2 = c.post(f"{BASE}/kb/documents/{doc_id}/pipeline/start")
        print(f"POST .../pipeline/start -> {r2.status_code} {r2.text[:160]}")
        final = poll_document(c, doc_id)
        data = unwrap(final)
        print(f"终态: status={data.get('status')} pipeline={data.get('pipeline')}")
        r3 = c.get(f"{BASE}/kb/documents/{doc_id}/chunks?limit=3")
        chunks = unwrap(r3.json()).get("items") or []
        preview = [str(x.get("content", ""))[:160] for x in chunks[:2]]
        print(f"chunks 预览: {preview}")
        result["experiments"].append(
            {
                "exp": "E2",
                "file": pdf.name,
                "doc_id": doc_id,
                "final_status": data.get("status"),
                "chunk_preview": preview,
            }
        )

        print("\n== E3 检索验证(问图纸语义) ==")
        for q in ["这张图纸的图号和标题栏材料是什么", "SAMPLE-0001 图纸的技术要求"]:
            rq = c.post(f"{BASE}/kb/search", json={"query": q, "kb_id": kb_id, "top_k": 3})
            hits = unwrap(rq.json()).get("citations") or []
            print(f"POST /kb/search q={q!r} -> {rq.status_code} citations={len(hits)}")
            for h_ in hits[:2]:
                print(f"   snippet: {str(h_.get('content') or h_.get('quote') or '')[:120]}")
        result["experiments"][-1]["search"] = "见日志"

    print("\n== E4 对照组:标题栏文本走同链路(区分文件通道与语义链) ==")
    synthetic = (
        "# 试验对照文档(非真实图纸,标题栏样式文本)\n\n"
        "图号: SAMPLE-0001  版本: Rev_A  幅面: A3\n"
        "名称: 高压开关柜局放监测装置安装板\n"
        "材料: Q235-B  板厚: 3mm  数量: 2\n"
        "技术要求: 1. 未注公差按 GB/T 1804-m;2. 去毛刺锐边;3. 表面镀锌钝化。\n"
    )
    r = c.post(
        f"{BASE}/kb/documents",
        json={
            "collection_id": kb_id,
            "title": "对照-标题栏文本(实验E4)",
            "mime_type": "text/markdown",
            "content": synthetic,
        },
    )
    print(f"POST /kb/documents (对照文本) -> {r.status_code}")
    if r.status_code < 400:
        doc_id = unwrap(r.json())["id"]
        c.post(f"{BASE}/kb/documents/{doc_id}/pipeline/start")
        final = poll_document(c, doc_id)
        data = unwrap(final)
        print(f"终态: status={data.get('status')} pipeline={data.get('pipeline')}")
        rc = c.get(f"{BASE}/kb/documents/{doc_id}/review/candidates")
        cands = unwrap(rc.json()).get("items") or []
        top3 = [(x.get("fact_type"), str(x.get("quote"))[:60]) for x in cands[:3]]
        print(f"终审候选数: {len(cands)}; 前 3 条: {top3}")
        result["experiments"].append(
            {"exp": "E4", "doc_id": doc_id, "final_status": data.get("status"), "candidates": len(cands)}
        )

    (OUT / "live_probe_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写 {OUT / 'live_probe_result.json'}")


if __name__ == "__main__":
    main()
