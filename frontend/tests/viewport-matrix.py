# -*- coding: utf-8 -*-
"""S8 视口矩阵 CI 脚本：5 视口 × 亮/暗 × 3 核心页（22 篇 §4 视口测试矩阵）。
前置：VITE_ENABLE_MOCK=1 npm run build + npx vite preview --port 4199。
产出：.qa_ix/viewport/<viewport>-<theme>-<page>.png；布局破损检测（横向溢出）断言。
"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://localhost:4199"
OUT = "E:/learn-agnet-project/.qa_ix/viewport"
VIEWPORTS = [(1440, 900), (1280, 800), (1024, 768), (768, 1024), (375, 667)]
PAGES = [("/login", "login"), ("/kb", "kb"), ("/chat", "chat")]

os.makedirs(OUT, exist_ok=True)
failures = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for w, h in VIEWPORTS:
        for theme in ("light", "dark"):
            page = browser.new_page(viewport={"width": w, "height": h})
            page.add_init_script(f"window.localStorage.setItem('oa-theme','{theme}')")
            for path, tag in PAGES:
                page.goto(BASE + path, wait_until="networkidle")
                page.wait_for_timeout(600)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
                )
                name = f"{w}-{theme}-{tag}.png"
                page.screenshot(path=os.path.join(OUT, name))
                if overflow:
                    failures.append(f"{name}: horizontal overflow")
                print("shot", name, "overflow" if overflow else "ok")
            page.close()
    browser.close()
if failures:
    print("LAYOUT FAILURES:")
    [print(" ", f) for f in failures]
else:
    print("ALL VIEWPORTS OK")
