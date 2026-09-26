"""v1.6 设计稿扩充 QA 截图：按 frame 元素级截图（亮/暗），输出 .qa_v16/"""
import asyncio, pathlib
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / ".qa_v16"
OUT.mkdir(exist_ok=True)

TARGETS = {
    "docs/设计稿/components.html": [
        "p-system", "p-forms", "sec-dataviz", "sec-admin", "sec-ai",
        "sec-overlay", "sec-consumer", "sec-form2",
    ],
    "docs/设计稿/ui-pages.html": [
        "p-onto", "p-explore", "p-dashboard", "p-status", "p-analytics", "p-roles", "p-tasks",
        "p-versions", "p-playground", "p-settings",
    ],
}
async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        for rel, frames in TARGETS.items():
            url = (ROOT / rel).as_uri()
            await page.goto(url, wait_until="networkidle")
            # 截图工艺：吸顶导航不参与元素截图，避免被烘焙进画面
            await page.add_style_tag(content=".board-head{position:static!important}")
            await page.wait_for_timeout(600)
            for dark in (False, True):
                if dark:
                    await page.evaluate("document.documentElement.classList.add('dark')")
                else:
                    await page.evaluate("document.documentElement.classList.remove('dark')")
                await page.wait_for_timeout(250)
                for fid in frames:
                    el = page.locator(f"#{fid}")
                    try:
                        await el.scroll_into_view_if_needed()
                        await page.wait_for_timeout(350)  # 让入场动效播完
                        path = OUT / (f"dark-{fid}.png" if dark else f"{fid}.png")
                        await el.screenshot(path=str(path))
                        print("ok", path.name)
                    except Exception as e:
                        print("FAIL", fid, dark, e)
        await browser.close()

asyncio.run(main())
