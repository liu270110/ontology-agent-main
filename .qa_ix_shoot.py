"""交互态画板 QA 截图：8 文件 × 全部 frame × 亮/暗，输出 .qa_ix/<file>/"""
import asyncio, pathlib, re
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / ".qa_ix"
OUT.mkdir(exist_ok=True)

FILES = sorted((ROOT / "docs/设计稿/interactions").glob("ix-*.html"))

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        await page.emulate_media(reduced_motion="reduce")  # 入场动效禁用，防中间帧
        for f in FILES:
            html = f.read_text(encoding="utf-8")
            frames = re.findall(r'<section class="frame" id="([^"]+)"', html)
            outdir = OUT / f.stem
            outdir.mkdir(exist_ok=True)
            url = f.as_uri()
            await page.goto(url, wait_until="networkidle")
            await page.add_style_tag(content=".board-head{position:static!important}")
            await page.wait_for_timeout(500)
            for dark in (False, True):
                await page.evaluate(
                    "document.documentElement.classList.toggle('dark', %s)" % ("true" if dark else "false")
                )
                await page.wait_for_timeout(250)
                for fid in frames:
                    el = page.locator(f"#{fid}")
                    try:
                        await el.scroll_into_view_if_needed()
                        await page.wait_for_timeout(250)
                        name = (f"dark-{fid}.png" if dark else f"{fid}.png")
                        await el.screenshot(path=str(outdir / name))
                    except Exception as e:
                        print("FAIL", f.stem, fid, dark, e)
            n = len(frames)
            print(f"done {f.stem}: {n} frames × 2")
        await browser.close()

asyncio.run(main())
