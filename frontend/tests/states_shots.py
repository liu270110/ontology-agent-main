# -*- coding: utf-8 -*-
"""S8 状态切片回归截图：KB 文档 / Agent 列表 / 审批中心 三页（亮/暗）。
前置：dev server 3275（MSW）。产物：E:/learn-agnet-project/.qa_ix/states/
"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3275"
OUT = r"E:\learn-agnet-project\.qa_ix\states"
os.makedirs(OUT, exist_ok=True)

PAGES = [("/kb", "kb"), ("/agents", "agents"), ("/approvals", "approvals")]


def login(page):
    page.goto(BASE + "/login", wait_until="networkidle")
    page.fill("#email", "admin@example.com")
    page.fill("#password", "password123")
    page.get_by_role("button", name="登 录").click()
    page.wait_for_url(BASE + "/", timeout=10000)


def run_theme(pw, theme):
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.add_init_script(f"window.localStorage.setItem('oa-theme', '{theme}')")
    login(page)
    for path, name in PAGES:
        page.goto(BASE + path, wait_until="networkidle")
        page.wait_for_timeout(900)
        page.screenshot(path=os.path.join(OUT, f"{name}-{theme}.png"), full_page=False)
        print("saved", f"{name}-{theme}.png")
    browser.close()


with sync_playwright() as pw:
    for t in ("light", "dark"):
        run_theme(pw, t)
print("done")
