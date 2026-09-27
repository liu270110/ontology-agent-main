# -*- coding: utf-8 -*-
"""链接邀请功能截图：管理员邀请弹窗（链接模式+已生成列表）/ 登录页 join 提示条。
产物：E:/learn-agnet-project/.qa_ix/invite/
"""
import os
from playwright.sync_api import sync_playwright

BASE = "http://localhost:3275"
OUT = r"E:\learn-agnet-project\.qa_ix\invite"
os.makedirs(OUT, exist_ok=True)


def login(page):
    page.goto(BASE + "/login", wait_until="networkidle")
    page.fill("#email", "admin@example.com")
    page.fill("#password", "password123")
    page.get_by_role("button", name="登 录").click()
    page.wait_for_url(BASE + "/", timeout=10000)


def run(pw, theme):
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.add_init_script(f"window.localStorage.setItem('oa-theme', '{theme}')")
    login(page)

    # 1. 系统管理 → 用户 Tab → 邀请成员弹窗（邮箱模式）
    page.goto(BASE + "/admin", wait_until="networkidle")
    page.wait_for_timeout(800)
    page.click("[data-testid=adm-invite-open]")
    page.wait_for_timeout(500)
    page.screenshot(path=os.path.join(OUT, f"invite-email-{theme}.png"))
    print("saved", f"invite-email-{theme}.png")

    # 2. 切链接模式 → 生成 → 列表
    page.click("[data-testid=adm-invite-mode-link]")
    page.wait_for_timeout(300)
    page.click("[data-testid=adm-invite-link-create]")
    page.wait_for_timeout(800)
    page.screenshot(path=os.path.join(OUT, f"invite-link-{theme}.png"))
    print("saved", f"invite-link-{theme}.png")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)

    # 3. 登录页 join 提示条（登出态直接访问带 token 的 /login）
    page2 = browser.new_page(viewport={"width": 1440, "height": 900})
    page2.add_init_script(f"window.localStorage.setItem('oa-theme', '{theme}')")
    page2.goto(BASE + "/admin", wait_until="networkidle")
    page2.evaluate("localStorage.removeItem('oa-access-token')")
    page2.goto(BASE + "/admin", wait_until="networkidle")
    page2.wait_for_timeout(500)
    # 从邀请列表拿一个真实 token 不可行（跨会话），用假 token 走失效红条 + mock 常规 token 走绿条二选一
    page2.goto(BASE + "/login?join=inv-seed-01", wait_until="networkidle")
    page2.wait_for_timeout(900)
    page2.screenshot(path=os.path.join(OUT, f"join-banner-{theme}.png"))
    print("saved", f"join-banner-{theme}.png")
    browser.close()


with sync_playwright() as pw:
    for t in ("light", "dark"):
        run(pw, t)
print("done")
