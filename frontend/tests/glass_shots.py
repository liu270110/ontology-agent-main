# -*- coding: utf-8 -*-
"""液态玻璃主题切片验收截图：dashboard / chat / kb / admin / settings / login 六页（亮/暗）。
前置：dev server（MSW，勿重启 3275；可用 QA_BASE 指向临时实例，如 http://127.0.0.1:3399）。
产物：E:/learn-agnet-project/.qa_ix/glass/
自查点：玻璃可读性（亮暗文字对比）+ 玻璃质感（透过玻璃可见光斑色彩变化）。
"""
import os
from playwright.sync_api import sync_playwright

BASE = os.environ.get("QA_BASE", "http://localhost:3275")
OUT = r"E:\learn-agnet-project\.qa_ix\glass"
os.makedirs(OUT, exist_ok=True)

# (路径, 产物名)；login 无需鉴权，其余页先登录（管理员可见 /admin）
PAGES = [("/", "dashboard"), ("/chat", "chat"), ("/kb", "kb"),
         ("/admin", "admin"), ("/settings", "settings")]


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
    # login 页：玻璃卡 + 极光底的亮暗验收
    page.goto(BASE + "/login", wait_until="networkidle")
    page.wait_for_timeout(900)
    page.screenshot(path=os.path.join(OUT, f"login-{theme}.png"))
    print("saved", f"login-{theme}.png")
    login(page)
    for path, name in PAGES:
        try:
            page.goto(BASE + path, wait_until="networkidle")
        except Exception:
            page.goto(BASE + path, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        page.screenshot(path=os.path.join(OUT, f"{name}-{theme}.png"), full_page=False)
        print("saved", f"{name}-{theme}.png")
    browser.close()


with sync_playwright() as pw:
    for t in ("light", "dark"):
        run_theme(pw, t)
print("done")
