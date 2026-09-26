# -*- coding: utf-8 -*-
"""Regenerate the 13 in-image translated DSec figures (final mapping v5).
v5: legend font scaling — Chinese legend text may be scaled down (SCALE dict)
so translated line-marker labels keep the visual weight of the original.
"""
import fitz
from collections import Counter
from PIL import Image, ImageDraw, ImageFont

FONT = "C:/Windows/Fonts/msyh.ttc"
Z = 4
plan = {
 1:(5,(70.9,360.3,524.6,637.8),644.4), 2:(8,(138.9,484.3,456.4,637.9),644.4),
 3:(9,(138.9,85.0,456.4,269.0),275.6), 4:(10,(70.9,85.0,524.4,228.3),252.9),
 5:(10,(138.9,496.6,457.0,654.3),661.0), 6:(11,(138.9,120.6,456.4,275.5),282.1),
 7:(11,(138.3,316.7,456.4,470.3),476.9), 8:(12,(138.9,224.1,456.4,377.3),383.9),
 9:(13,(93.5,85.0,501.4,299.0),305.3), 10:(21,(70.9,85.0,524.4,240.3),246.9),
 11:(21,(138.9,445.0,456.4,625.8),632.4), 12:(22,(70.9,170.2,525.1,319.2),325.8),
 13:(23,(138.9,85.0,456.4,239.9),246.5),
}
M = {
1: {"Cluster-level services":"集群级服务","Per-node runtime":"节点级运行时","Sandbox backends":"沙箱后端",
    "Create / Delete":"创建 / 删除","Unified Python SDK for Sandbox":"统一的沙箱 Python SDK",
    "Training Cluster":"训练集群","Management Request":"管理请求","Data Request":"数据请求",
    "Identity and Access":"身份与访问管理","Management":"",
    "Ingress Proxy":"入口代理","Placement Engine":"放置引擎","Sandbox Scheduler":"沙箱调度器",
    "Worker Monitor":"工作节点监测","Lifecycle Management":"生命周期管理","Session Management":"会话管理",
    "Exec / File / HTTP":"执行 / 文件 / HTTP","Pre-created":"预创建","Containers":"容器",
    "Container":"容器","MicroVM":"微型虚拟机","FullVM":"完整 VM","Proxy":"代理",
    "Isolation: AppArmor + eBPF":"隔离：AppArmor + eBPF","EROFS Images":"EROFS 镜像",
    "OverlayBD Images":"OverlayBD 镜像","QEMU VM":"QEMU 虚拟机"},
2: {"Sandbox count per task":"每任务沙箱数","Container":"容器","microVM":"微型虚拟机"},
3: {"Time (s)":"时间（秒）","setup":"环境准备","tool call":"工具调用","test":"测试",
    "Container CPU":"容器 CPU","Container mem":"容器内存","microVM CPU":"微型虚拟机 CPU","microVM mem":"微型虚拟机内存"},
4: {"Full Image Rebuild":"整镜像重建","Image 1":"镜像 1","Image 2":"镜像 2","Image X":"镜像 X",
    "Toolkit T1":"工具集 T1","Workspace W1":"工作区 W1","Workspace W2":"工作区 W2","Workspace WX":"工作区 WX",
    "Base":"基础镜像","(a) Monolithic images.":"(a) 单体镜像","Runtime Environment":"运行时环境",
    "Only Update Toolkit T1":"仅更新工具集 T1","Workspaces":"工作区","Base Images":"基础镜像",
    "Toolkits":"工具集","Base B1":"基础镜像 B1","(b) Composable environment layers.":"(b) 可组合环境层"},
5: {"Container CPU":"容器 CPU","Container mem":"容器内存","microVM CPU":"微型虚拟机 CPU",
    "microVM mem":"微型虚拟机内存","avg":"平均","peak":"峰值","Used / Requested (%)":"实际用量 / 申请量（%）"},
6: {"Time (h)":"时间（小时）","Sandbox count":"沙箱数量","Container":"容器","microVM":"微型虚拟机"},
7: {"Lifetime (min)":"生命周期（分钟）","p50 (min)":"p50（分钟）","p99 (min)":"p99（分钟）",
    "Container":"容器","microVM":"微型虚拟机"},
8: {"Image fanout (log scale)":"镜像扇出（对数刻度）","Container":"容器","microVM":"微型虚拟机"},
9: {"Composable Layers (§5.1 )":"可组合环境层（§5.1）","CPU QoS  (§5.2 )":"CPU 服务质量（§5.2）",
    "Mem Optimization (§5.2 )":"内存优化（§5.2）","On demand Image Loading (§5.3 )":"按需镜像加载（§5.3）",
    "Container":"容器","microVM":"微型虚拟机","LS: Core Scheduling":"LS：核调度",
    "Multi-device EROFS":"多设备 EROFS","Meta":"元数据","Data Blocks":"数据块","On Demand":"按需",
    "Proactive Reclaim":"主动回收","Guest Memory":"guest 内存",
    "Free Page Reporting":"空闲页上报","Host Memory":"宿主内存",
    "Virtio Balloon":"Virtio 气球设备","Virtio Pmem":"Virtio 持久内存",
    "Bypass Guest":"旁路 guest","Page Cache":"页缓存","IO Request":"I/O 请求",
    "Host Disk Image":"宿主磁盘镜像","EROFS Layer":"EROFS 层","OverlayBD Images":"OverlayBD 镜像",
    "WorkSpace":"工作区","Toolkit":"工具集","Dockerd":"Docker 守护进程",
    "BE: SCHED_IDLE":"BE：空闲调度类"},
10: {"Time (min)":"时间（分钟）","Write IOPS":"写 IOPS",
     "Solid: IOPS   /   Dashed: total":"实线：IOPS / 虚线：累计","Total Write (GB)":"累计写入（GB）",
     "(a) Running containers":"(a) 运行中容器数","(b) Disk writes":"(b) 磁盘写入",
     "Docker (cached)":"Docker（本地缓存）","Docker (cold)":"Docker（冷拉取）","EROFS":"EROFS"},
11: {"CPU (%)":"CPU（%）","Tar":"tar","Time (min)":"时间（分钟）","Disk Write (MB/s)":"磁盘写入（MB/s）"},
12: {"Time (min)":"时间（分钟）","Memory (GB)":"内存（GB）","CPU (%)":"CPU（%）","(a) Memory":"(a) 内存",
     "baseline":"基线","pmem":"持久内存","fpr":"空闲页上报","pmem+fpr":"持久内存+空闲上报"},
13: {"Background CPU load (%)":"后台 CPU 负载（%）","Agent time (s)":"智能体耗时（秒）",
     "baseline":"基线","idle":"仅 SCHED_IDLE","idle + core":"SCHED_IDLE + 核调度","no background load":"无后台负载"},
}
VERT = {3:{"CPU (cores)":"CPU（核）","Memory (GB)":"内存（GB）"},
        6:{"Sandbox count":"沙箱数量"},
        10:{"Write IOPS":"写 IOPS","Total Write (GB)":"累计写入（GB）"},
        11:{"CPU (%)":"CPU（%）","Disk Write (MB/s)":"磁盘写入（MB/s）"},
        12:{"Memory (GB)":"内存（GB）","CPU (%)":"CPU（%）"},
        13:{"Agent time (s)":"智能体耗时（秒）"},
        9:{"WorkSpace":"工作区","Toolkit":"工具集"}}
MERGES = {10:[{"parts":["Running","containers / VM"], "text":"运行中容器数 / VM"}]}
# left-anchored legend / table-row labels (original layout is left-aligned)
LEFT = {(1,"管理请求"),(1,"数据请求"),
        (2,"容器"),(2,"微型虚拟机"),
        (3,"容器 CPU"),(3,"容器内存"),(3,"微型虚拟机 CPU"),(3,"微型虚拟机内存"),
        (5,"容器 CPU"),(5,"容器内存"),(5,"微型虚拟机 CPU"),(5,"微型虚拟机内存"),(5,"平均"),(5,"峰值"),
        (6,"容器"),(6,"微型虚拟机"),
        (7,"容器"),(7,"微型虚拟机"),
        (8,"容器"),(8,"微型虚拟机"),
        (9,"可组合环境层（§5.1）"),(9,"CPU 服务质量（§5.2）"),(9,"内存优化（§5.2）"),(9,"按需镜像加载（§5.3）"),
        (10,"Docker（本地缓存）"),(10,"Docker（冷拉取）"),(10,"EROFS"),
        (11,"tar"),
        (12,"基线"),(12,"持久内存"),(12,"空闲页上报"),(12,"持久内存+空闲上报"),
        (13,"基线"),(13,"仅 SCHED_IDLE"),(13,"SCHED_IDLE + 核调度"),(13,"无后台负载")}
# font scale for line-marker legend labels (Chinese runs wider than Latin)
SCALE = {(13,"基线"):0.75,(13,"仅 SCHED_IDLE"):0.75,(13,"SCHED_IDLE + 核调度"):0.75,(13,"无后台负载"):0.75,
         (10,"Docker（本地缓存）"):0.85,(10,"Docker（冷拉取）"):0.85,(10,"EROFS"):0.85,
         (11,"tar"):0.85,
         (12,"基线"):0.68,(12,"持久内存"):0.68,(12,"空闲页上报"):0.68,(12,"持久内存+空闲上报"):0.68}
CAP = {(7, "p50（分钟）"): 1.10, (7, "p99（分钟）"): 1.10}
SHIFT = {(12, "空闲页上报"): -4.8}

norm = lambda s: s.replace("\xa0", " ")
doc = fitz.open("DeepSeek-Elastic-Compute-DSec-arxiv-2609.22978.pdf")

def font_at(sz):
    return ImageFont.truetype(FONT, max(12, int(round(sz))), index=0)

def col(cint):
    return ((cint >> 16) & 255, (cint >> 8) & 255, cint & 255)

def page_rects(page, clip):
    rs = []
    for dr in page.get_drawings():
        r = dr["rect"]
        if r.width < 8 or r.height < 6:
            continue
        if r.x1 < clip.x0 or r.x0 > clip.x1 or r.y1 < clip.y0 or r.y0 > clip.y1:
            continue
        rs.append(r)
    return rs

def allowed_pdf_w(rects, bx0, by0, bx1, by1, tol=1.5):
    best = None
    for r in rects:
        if r.x0 - tol <= bx0 and r.x1 + tol >= bx1 and r.y0 - tol <= by0 and r.y1 + tol >= by1:
            a = r.width * r.height
            if best is None or a < best.width * best.height:
                best = r
    if best is None:
        return (bx1 - bx0) * 1.25
    return max(best.width - 4, (bx1 - bx0))

def is_pure_cjk(s):
    return all("\u4e00" <= ch <= "\u9fff" for ch in s)

for n in range(1, 14):
    pno, rect, capt = plan[n]
    clip = fitz.Rect(rect[0]-2, rect[1]-2, rect[2]+2, capt-3)
    raw = doc[pno].get_text("rawdict", clip=clip)
    sp = []
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            dirx, diry = line.get("dir", (1, 0))
            for s in line["spans"]:
                txt = "".join(ch["c"] for ch in s["chars"]).strip()
                if txt:
                    sp.append({"t": txt, "x0": s["bbox"][0], "y0": s["bbox"][1],
                               "x1": s["bbox"][2], "y1": s["bbox"][3],
                               "size": s["size"], "color": s["color"],
                               "vertical": abs(diry) > 0.5})
    pix = doc[pno].get_pixmap(matrix=fitz.Matrix(Z, Z), clip=clip, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    d = ImageDraw.Draw(img)
    rects = page_rects(doc[pno], clip)
    mapping = dict(M.get(n, {}))
    vmap = dict(VERT.get(n, {}))
    merged_parts = set(p for m in MERGES.get(n, []) for p in m["parts"])

    def to_px(b):
        return ((b[0]-clip.x0)*Z, (b[1]-clip.y0)*Z, (b[2]-clip.x0)*Z, (b[3]-clip.y0)*Z)

    def sample_bg(bb):
        x0, y0, x1, y1 = bb
        cnt = Counter()
        for yy in range(max(0, int(y0)+1), min(img.height, int(y1))):
            for xx in range(max(0, int(x0)+1), min(img.width, int(x1))):
                cnt[img.getpixel((xx, yy))] += 1
        return cnt.most_common(1)[0][0] if cnt else (255, 255, 255)

    def erase(bb):
        x0, y0, x1, y1 = bb
        d.rectangle([x0-1, y0-1, x1+1, y1+1], fill=sample_bg(bb))

    def draw_center(text, bb, color, size, allow_w_px=None):
        x0, y0, x1, y1 = bb
        f = font_at(size*Z*0.98)
        tw = d.textlength(text, font=f)
        limit = allow_w_px if allow_w_px else (x1-x0)
        while tw > limit and f.size > 12:
            f = ImageFont.truetype(FONT, f.size-1, index=0)
            tw = d.textlength(text, font=f)
        d.text((int((x0+x1)/2), int((y0+y1)/2)), text, font=f, fill=color, anchor="mm")

    def draw_left(text, bb, color, size, scale=1.0, shift_pt=0.0):
        x0, y0, x1, y1 = bb
        f = font_at(size*Z*0.98*scale)
        d.text((int(x0)+2+int(shift_pt*Z), int((y0+y1)/2)), text, font=f, fill=color, anchor="lm")

    def draw_vertical(text, bb, color, size):
        x0, y0, x1, y1 = bb
        if is_pure_cjk(text):
            f = font_at(size*Z*0.98)
            lh = int(f.size * 1.06)
            total = lh * len(text)
            while total > (y1-y0) and f.size > 12:
                f = ImageFont.truetype(FONT, f.size-1, index=0)
                lh = int(f.size * 1.06)
                total = lh * len(text)
            cx = int((x0+x1)/2)
            cy0 = (y0+y1)/2 - total/2
            for i, ch in enumerate(text):
                d.text((cx, int(cy0 + i*lh + lh/2)), ch, font=f, fill=color, anchor="mm")
        else:
            f = font_at(size*Z*0.98)
            tw = d.textlength(text, font=f)
            while tw > (y1-y0) and f.size > 12:
                f = ImageFont.truetype(FONT, f.size-1, index=0)
                tw = d.textlength(text, font=f)
            tmp = Image.new("RGBA", (int(tw)+8, f.size+10), (0, 0, 0, 0))
            ImageDraw.Draw(tmp).text((4, 3), text, font=f, fill=color)
            rot = tmp.rotate(90, expand=True)
            cx, cy = int((x0+x1)/2), int((y0+y1)/2)
            img.paste(rot, (cx-rot.width//2, cy-rot.height//2), rot)

    for s in sp:
        t = norm(s["t"])
        if t in merged_parts:
            continue
        if s["vertical"]:
            if t in vmap and vmap[t]:
                bb = to_px((s["x0"], s["y0"], s["x1"], s["y1"]))
                erase(bb)
                draw_vertical(vmap[t], bb, col(s["color"]), s["size"])
            continue
        if t in mapping:
            bb = to_px((s["x0"], s["y0"], s["x1"], s["y1"]))
            erase(bb)
            if mapping[t]:
                zh = mapping[t]
                if (n, zh) in LEFT:
                    draw_left(zh, bb, col(s["color"]), s["size"],
                              scale=SCALE.get((n, zh), 1.0),
                              shift_pt=SHIFT.get((n, zh), 0.0))
                else:
                    aw = allowed_pdf_w(rects, s["x0"], s["y0"], s["x1"], s["y1"]) * Z
                    cap = CAP.get((n, zh))
                    if cap:
                        aw = min(aw, (s["x1"]-s["x0"]) * cap * Z)
                    draw_center(zh, bb, col(s["color"]), s["size"], allow_w_px=aw)

    for mg in MERGES.get(n, []):
        sel = [s for s in sp if norm(s["t"]) in mg["parts"]]
        if len(sel) == len(mg["parts"]):
            for s in sel:
                erase(to_px((s["x0"], s["y0"], s["x1"], s["y1"])))
            x0 = min(s["x0"] for s in sel); y0 = min(s["y0"] for s in sel)
            x1 = max(s["x1"] for s in sel); y1 = max(s["y1"] for s in sel)
            draw_vertical(mg["text"], to_px((x0, y0, x1, y1)), (37, 37, 37),
                          max(s["size"] for s in sel))

    img.save(f"figures/fig{n:02d}_zh.png")
    print(f"fig{n:02d}_zh.png regenerated")
