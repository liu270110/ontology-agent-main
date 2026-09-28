# -*- coding: utf-8 -*-
"""窗口适配性矩阵（30 篇 §7.3 / 03 篇 §2.6）：5 视口 × 3 核心页横向溢出断言。
前置：VITE_ENABLE_MOCK=1 npm run build + npx vite preview --port 4199。
产出：.qa_ix/adaptive/<w>-<theme>-<page>.png；退出码非 0 = 存在横向溢出。
"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://localhost:4199"
OUT = "E:/learn-agnet-project/.qa_ix/adaptive"
VIEWPORTS = [(1440, 900), (1280, 800), (1024, 768), (768, 1024), (375, 667)]
PAGES = [("/login", "login"), ("/kb", "kb"), ("/", "dashboard")]

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
                    failures.append(name)
                    print("OVERFLOW", name)
                else:
                    print("ok", name)
            page.close()
    browser.close()
if failures:
    print(f"FAIL: {len(failures)} viewport/page combos overflow")
    raise SystemExit(1)
print("ALL VIEWPORTS OK (15/15)")
