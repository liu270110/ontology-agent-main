"""PDF 图纸载体诊断探针(纯标准库,非平台读取链,仅工程诊断用)。

目的:在不安装任何解析库的前提下,客观刻画两份图纸 PDF 的载体形态——
矢量/光栅、有无文本层、页面尺寸——用于解释「平台摄取链读不了」的根因分层。
判定依据是 PDF 内部对象统计,不做任何语义解读。
"""

from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

TOKEN_IMAGE = re.compile(rb"/Subtype\s*/Image")
TOKEN_FONT = re.compile(rb"/Type\s*/Font|/BaseFont")
TOKEN_DCT = re.compile(rb"/DCTDecode")
TOKEN_CCITT = re.compile(rb"/CCITTFaxDecode")
TOKEN_JBIG2 = re.compile(rb"/JBIG2Decode")
TOKEN_JPX = re.compile(rb"/JPXDecode")
TOKEN_PAGE = re.compile(rb"/Type\s*/Page[^s]")
TOKEN_MEDIA = re.compile(rb"/MediaBox\s*\[([^\]]+)\]")
TEXT_OPS = re.compile(rb"\bTj\b|\bTJ\b")
PATH_OPS = re.compile(rb"\bl\b|\bm\b|\bc\b|\bre\b")
STREAM = re.compile(rb"stream\r?\n")


def scan(path: Path) -> dict:
    raw = path.read_bytes()
    report: dict = {
        "file": path.name,
        "size_bytes": len(raw),
        "pdf_version": raw[:8].decode("ascii", "replace").strip(),
        "page_objects": len(TOKEN_PAGE.findall(raw)),
        "image_xobjects": len(TOKEN_IMAGE.findall(raw)),
        "font_objects": len(TOKEN_FONT.findall(raw)),
        "codec_dct_jpeg": len(TOKEN_DCT.findall(raw)),
        "codec_ccitt_g4": len(TOKEN_CCITT.findall(raw)),
        "codec_jbig2": len(TOKEN_JBIG2.findall(raw)),
        "codec_jpx": len(TOKEN_JPX.findall(raw)),
        "mediabox": None,
        "text_ops_in_streams": 0,
        "path_ops_in_streams": 0,
        "streams_scanned": 0,
        "readable_text_sample": [],
    }
    if m := TOKEN_MEDIA.search(raw):
        nums = re.findall(rb"[-0-9.]+", m.group(1))
        if len(nums) >= 4:
            w = float(nums[2]) - float(nums[0])
            h = float(nums[3]) - float(nums[1])
            report["mediabox_pt"] = [round(w, 1), round(h, 1)]
            report["mediabox_mm"] = [round(w * 25.4 / 72, 1), round(h * 25.4 / 72, 1)]

    for sm in STREAM.finditer(raw):
        start = sm.end()
        end = raw.find(b"endstream", start)
        if end < 0:
            continue
        payload = raw[start:end]
        try:
            data = zlib.decompress(payload)
        except Exception:
            continue  # 未压缩/加密/图片流跳过
        report["streams_scanned"] += 1
        report["text_ops_in_streams"] += len(TEXT_OPS.findall(data))
        report["path_ops_in_streams"] += len(PATH_OPS.findall(data))
        if len(report["readable_text_sample"]) < 400:
            for tm in re.finditer(rb"\(((?:[^()\\]|\\.)*)\)\s*Tj", data):
                s = tm.group(1)
                if 2 <= len(s) <= 80 and sum(32 <= b < 127 for b in s) >= len(s) * 0.7:
                    report["readable_text_sample"].append(s.decode("latin-1"))
                    if len(report["readable_text_sample"]) >= 400:
                        break
    return report


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        r = scan(Path(arg))
        print("=" * 60)
        for k, v in r.items():
            if k == "readable_text_sample":
                sample = v[:40]
                print(f"{k}: {len(v)} 段, 前 40 段示例: {sample}")
            else:
                print(f"{k}: {v}")
