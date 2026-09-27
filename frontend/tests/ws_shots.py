# -*- coding: utf-8 -*-
"""S2·工作区面板截图验收（画框23 基线：亮/暗双主题）。
前置：dev server 已在 3275（VITE_ENABLE_MOCK=1）。
产物：E:/learn-agnet-project/.qa_ix/ws/*.png
"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3275"
OUT = r"E:\learn-agnet-project\.qa_ix\ws"
os.makedirs(OUT, exist_ok=True)


def login(page):
    page.goto(BASE + "/login", wait_until="networkidle")
    page.fill("#email", "admin@example.com")
    page.fill("#password", "password123")
    page.get_by_role("button", name="登 录").click()
    page.wait_for_url(BASE + "/", timeout=10000)


def shot(page, name):
    page.wait_for_timeout(450)
    page.screenshot(path=os.path.join(OUT, name), full_page=False)
    print("saved", name)


def run_theme(pw, theme):
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.add_init_script(f"window.localStorage.setItem('oa-theme', '{theme}')")
    login(page)

    page.goto(BASE + "/chat", wait_until="networkidle")
    page.wait_for_selector("text=动力电池标准对比", timeout=10000)
    page.click("text=动力电池标准对比")
    page.wait_for_selector("[data-testid=ctx-panel]", timeout=10000)

    # 1. 文件树（默认页签）
    page.click("[data-testid=right-tab-workspace]")
    page.wait_for_selector("[data-testid=ws-node-artifacts]", timeout=8000)
    page.wait_for_timeout(500)
    shot(page, f"ws-tree-{theme}.png")

    # 2. 文件预览抽屉
    page.click('[data-testid="ws-node-排查报告草稿 v0.1.md"]')
    page.wait_for_selector("[data-testid=ws-preview]", timeout=8000)
    shot(page, f"ws-preview-{theme}.png")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # 3. 终端（执行一条白名单命令再截）
    page.click("[data-testid=ws-tab-term]")
    page.wait_for_selector("[data-testid=ws-term]", timeout=8000)
    page.fill("[data-testid=ws-term-input]", "ls /workspace")
    page.press("[data-testid=ws-term-input]", "Enter")
    page.wait_for_timeout(600)
    shot(page, f"ws-term-{theme}.png")

    # 4. 资源四分组
    page.click("[data-testid=ws-tab-res]")
    page.wait_for_selector("[data-testid=ws-res-res-2481-a1]", timeout=8000)
    shot(page, f"ws-res-{theme}.png")

    browser.close()


with sync_playwright() as pw:
    for t in ("light", "dark"):
        run_theme(pw, t)
print("done")
