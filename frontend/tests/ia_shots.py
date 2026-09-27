# -*- coding: utf-8 -*-
"""双区 IA 改造自查截图：主页启动台 / 管理控制台 / 旧路由 redirect（亮/暗）。
前置：dev server 3275（MSW）。产物：E:/learn-agnet-project/.qa_ix/ia/
"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3275"
OUT = r"E:\learn-agnet-project\.qa_ix\ia"
os.makedirs(OUT, exist_ok=True)

PAGES = [("/", "home"), ("/console", "console"), ("/console/approvals", "console-approvals")]


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
    # 旧路由 redirect 证据：/approvals → /console/approvals（查询串透传 /admin?tab=audit）
    page.goto(BASE + "/approvals", wait_until="networkidle")
    page.wait_for_timeout(900)
    assert "/console/approvals" in page.url, f"redirect failed: {page.url}"
    page.screenshot(path=os.path.join(OUT, f"redirect-approvals-{theme}.png"))
    print("saved", f"redirect-approvals-{theme}.png", "url=", page.url)
    page.goto(BASE + "/admin?tab=audit", wait_until="networkidle")
    page.wait_for_timeout(900)
    assert "/console/admin?tab=audit" in page.url, f"redirect failed: {page.url}"
    page.screenshot(path=os.path.join(OUT, f"redirect-admin-audit-{theme}.png"))
    print("saved", f"redirect-admin-audit-{theme}.png", "url=", page.url)
    browser.close()


with sync_playwright() as pw:
    for t in ("light", "dark"):
        run_theme(pw, t)
print("done")
