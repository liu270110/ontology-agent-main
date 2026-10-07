# -*- coding: utf-8 -*-
"""S3 知识域截图验收（30 篇 DoD-1：亮/暗双主题，对照画板基线）。
前置：`VITE_ENABLE_MOCK=1 npm run build` + `npx vite preview --port 4199`。
产物：E:/learn-agnet-project/.qa_ix/s3/*.png
"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://localhost:4199"
OUT = r"E:\learn-agnet-project\.qa_ix\s3"
os.makedirs(OUT, exist_ok=True)

LIGHT, DARK = "light", "dark"


def set_theme(page, theme):
    page.evaluate(f"localStorage.setItem('oa-theme', '{theme}')")


def login(page):
    page.goto(BASE + "/login", wait_until="networkidle")
    page.fill("#email", "admin@example.com")
    page.fill("#password", "password123")
    page.get_by_role("button", name="登 录").click()
    page.wait_for_url(BASE + "/", timeout=10000)


def shot(page, name):
    page.wait_for_timeout(400)
    page.screenshot(path=os.path.join(OUT, name), full_page=False)
    print("saved", name)


def run_theme(pw, theme):
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    # 主题在文档加载前写入（about:blank 阶段直接 evaluate localStorage 会被拒）
    page.add_init_script(f"window.localStorage.setItem('oa-theme', '{theme}')")
    login(page)

    # 1. /kb 文档管理主视图（p-kb 基线：统计带+文档表四态）
    page.goto(BASE + "/kb", wait_until="networkidle")
    page.wait_for_selector("text=设备手册.pdf", timeout=10000)
    page.wait_for_timeout(600)
    shot(page, f"kb-main-{theme}.png")

    # 2. IX-KB-01 上传弹窗（拖拽区+队列+解析选项折叠展开）
    page.get_by_role("button", name="上传文档").click()
    page.wait_for_selector("[role=dialog][aria-label=上传文档]", timeout=5000)
    page.get_by_role("button", name="解析选项").click()
    page.wait_for_timeout(300)
    shot(page, f"kb-upload-dialog-{theme}.png")
    page.keyboard.press("Escape")

    # 3. IX-KB-02 分片预览抽屉
    page.get_by_role("button", name="预览 设备手册.pdf").first.click()
    page.wait_for_selector("text=分片预览 · 设备手册.pdf", timeout=8000)
    page.wait_for_timeout(500)
    shot(page, f"kb-chunk-sheet-{theme}.png")
    page.keyboard.press("Escape")

    # 4. /kb/review 审核台主视图（p-review 基线：队列+原文对照+七步条）
    page.goto(BASE + "/kb/review", wait_until="networkidle")
    page.wait_for_selector("text=故障 F-2026-118", timeout=10000)
    page.wait_for_timeout(500)
    shot(page, f"review-main-{theme}.png")

    # 5. IX-REV-03 批量确认弹窗（勾选两条→批量通过）
    page.get_by_label("选择 配变 T-2093").check()
    page.get_by_label("选择 停电事件 E-0901").check()
    page.get_by_test_id("batch-open").click()
    page.wait_for_selector("text=按类型小计", timeout=5000)
    page.wait_for_timeout(300)
    shot(page, f"review-batch-confirm-{theme}.png")
    page.keyboard.press("Escape")

    # 6. /kb/playground 主视图 + 检索后引用悬浮（p-playground 基线）
    page.goto(BASE + "/kb/playground", wait_until="networkidle")
    page.wait_for_selector("[aria-label=检索查询]", timeout=10000)
    shot(page, f"playground-main-{theme}.png")
    page.fill("[aria-label=检索查询]", "单相接地故障的处置流程是什么？")
    page.get_by_test_id("pg-search").click()
    page.wait_for_selector("[data-testid=pg-answer] sup", timeout=10000)
    # 悬停引用角标 → IX-PG-01 悬浮预览
    page.hover("[data-testid=pg-answer] sup")
    page.wait_for_selector("text=点击角标查看分片原文抽屉", timeout=5000)
    shot(page, f"playground-cite-hover-{theme}.png")

    # 7. IX-PG-03 历史抽屉
    page.get_by_role("button", name="历史").click()
    page.wait_for_selector("text=检索历史", timeout=5000)
    page.wait_for_timeout(400)
    shot(page, f"playground-history-{theme}.png")

    browser.close()


with sync_playwright() as pw:
    run_theme(pw, LIGHT)
    run_theme(pw, DARK)
print("done")
