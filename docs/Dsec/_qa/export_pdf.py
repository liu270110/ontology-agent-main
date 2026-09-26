# -*- coding: utf-8 -*-
"""Export the two DSec markdown documents to PDF via headless Chromium."""
import pathlib, markdown
from playwright.sync_api import sync_playwright

BASE = pathlib.Path(__file__).resolve().parent.parent  # docs/Dsec

CSS = """
@page { size: A4; margin: 16mm 13mm 18mm 13mm; }
* { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: 'Microsoft YaHei','微软雅黑',sans-serif; font-size: 10.5pt;
       line-height: 1.7; color: #222; margin: 0; }
h1 { font-size: 17pt; line-height: 1.45; margin: 0 0 10pt; }
h2 { font-size: 14pt; margin: 16pt 0 7pt; padding-bottom: 3pt;
     border-bottom: 1.5px solid #888; page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 12pt 0 5pt; page-break-after: avoid; }
p { margin: 5pt 0; text-align: justify; }
img { max-width: 100%; height: auto; display: block; margin: 7pt auto;
      border: 0.6pt solid #ccc; page-break-inside: avoid; }
table { border-collapse: collapse; width: 100%; margin: 7pt 0;
        font-size: 8.8pt; page-break-inside: avoid; }
th, td { border: 0.6pt solid #999; padding: 3pt 6pt; text-align: left; }
th { background: #f0f0f0; }
code { font-family: Consolas, monospace; font-size: 8.8pt; background: #f4f4f4;
       padding: 0 3px; }
pre { background: #f6f6f6; border: 0.6pt solid #ddd; padding: 8pt;
      page-break-inside: avoid; overflow-x: hidden; }
pre code { font-size: 8.3pt; background: none; padding: 0; white-space: pre-wrap; }
blockquote { border-left: 3px solid #9db8c8; background: #f5f8fa;
             margin: 7pt 0; padding: 5pt 12pt; color: #333; }
blockquote p { margin: 3pt 0; }
li { margin: 2pt 0; }
hr { border: none; border-top: 0.8pt solid #bbb; margin: 12pt 0; }
"""

def md_to_html(md_path: pathlib.Path) -> str:
    text = md_path.read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<style>{CSS}</style></head><body>{body}</body></html>")

jobs = [
    (BASE / "DSec-论文中文翻译.md", BASE / "DSec-论文中文翻译.pdf"),
    (BASE / "DSec-技术报告.md",     BASE / "DSec-技术报告.pdf"),
]

with sync_playwright() as p:
    try:
        browser = p.chromium.launch()
    except Exception:
        browser = p.chromium.launch(channel="msedge")
    page = browser.new_page()
    for md, pdf in jobs:
        html_path = pdf.with_suffix(".print.html")
        html_path.write_text(md_to_html(md), encoding="utf-8")
        page.goto(html_path.as_uri(), wait_until="load")
        page.wait_for_timeout(2500)  # let all base64 images decode
        page.pdf(path=str(pdf), format="A4",
                 margin={"top": "16mm", "bottom": "18mm", "left": "13mm", "right": "13mm"},
                 print_background=True)
        print(f"exported {pdf.name}")
    browser.close()

import fitz
for _, pdf in jobs:
    d = fitz.open(str(pdf))
    print(f"{pdf.name}: {d.page_count} pages, {pdf.stat().st_size/1048576:.2f} MB")
    d.close()
