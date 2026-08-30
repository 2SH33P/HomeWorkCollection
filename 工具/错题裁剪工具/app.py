#!/usr/bin/env python3
"""
错题收集 WebUI 后端
功能: 上传整页照片 -> 网页框选题 -> 裁剪存档 -> 组卷打印

运行:  python 工具/错题裁剪工具/app.py
访问:  http://localhost:8091
"""
import base64
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps

import cv2
import numpy as np
import typst

# 打包(onefile)后 __file__ 指向临时解压目录, 数据必须放在可执行文件旁
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[2]      # HomeWorkCollection/
PAGES_DIR = ROOT / "页"                             # 整页照片
ITEMS_DIR = ROOT / "错题"                           # 裁剪出的错题图
DB_FILE = ROOT / "错题库.json"
TMP_DIR = ROOT / ".tmp"                            # 清理预览临时文件
if getattr(sys, "frozen", False):
    STATIC_DIR = Path(getattr(sys, "_MEIPASS", ROOT)) / "static"
else:
    STATIC_DIR = Path(__file__).resolve().parent / "static"

for d in (PAGES_DIR, ITEMS_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)
FONTS_DIR = ROOT / "字体"
FONTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="错题收集工具")


def load_db():
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    return {"items": []}


def save_db(db):
    DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2),
                       encoding="utf-8")


def open_photo(path: Path) -> Image.Image:
    """读图并按 EXIF 自动摆正方向。"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def make_thumbnail(src: Path, dst: Path, max_side=2000):
    img = open_photo(src)
    w, h = img.size
    if max(w, h) > max_side:
        s = max_side / max(w, h)
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    img.save(dst, "JPEG", quality=88)


def safe_name(name: str) -> str:
    return "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "未命名"


# ---------- 页面路由 ----------

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ---------- 接口 ----------

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "page.jpg").suffix or ".jpg"
    if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        return JSONResponse({"ok": False, "msg": "不支持的文件类型"}, status_code=400)
    page_id = f"p{int(time.time() * 1000)}"
    src = PAGES_DIR / f"{page_id}{ext.lower()}"
    with src.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    make_thumbnail(src, PAGES_DIR / f"{page_id}_web.jpg")
    return {"ok": True, "page": page_id, "name": file.filename,
            "web_url": f"/files/页/{page_id}_web.jpg"}


@app.get("/api/pages")
def list_pages():
    out = []
    for f in sorted(PAGES_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.name.endswith("_web.jpg"):
            out.append({"page": f.name[:-len("_web.jpg")],
                        "web_url": f"/files/页/{f.name}"})
    return out


DB_LOCK = threading.Lock()
TPL_PATH = ROOT / "错题库_大题模板.json"


@app.get("/api/chapter-tpl")
def get_tpl():
    """按科目配置的大题模板。"""
    d = {}
    if TPL_PATH.exists():
        try:
            d = json.loads(TPL_PATH.read_text("utf-8"))
        except Exception:
            pass
    return {"ok": True, "templates": d}


@app.put("/api/chapter-tpl")
def put_tpl(payload: dict):
    tpls = payload.get("templates", {})
    if not isinstance(tpls, dict):
        return JSONResponse({"ok": False, "msg": "格式错误"}, status_code=400)
    clean = {}
    for k, v in tpls.items():
        items = [str(x).strip() for x in v if str(x).strip()]
        if items:
            clean[str(k).strip()] = items
    TPL_PATH.write_text(json.dumps(clean, ensure_ascii=False, indent=2), "utf-8")
    return {"ok": True, "templates": clean}


def _auto_ai_bg(saved_items):
    """后台线程池并发 AI 识别, 更新备注(公式 LaTeX); 自动裁剪题内图形, 裁不好打 fig_issue 标记。"""
    from concurrent.futures import ThreadPoolExecutor

    def work(it):
        f = ROOT / it["image"]
        if not f.exists():
            return
        try:
            text = call_ai_vision(np.array(open_photo(f)))
        except Exception:
            return
        if not text:
            return
        issue = False
        figs = re.findall(r"\[图@(\d+),(\d+),(\d+),(\d+)\]", text)
        if figs:
            full = np.array(open_photo(f))
            FH, FW = full.shape[:2]
            ok_n = 0
            for fx, fy, fw, fh in figs:
                x0 = max(0, int(int(fx) / 1000 * FW)); y0 = max(0, int(int(fy) / 1000 * FH))
                w0 = min(FW - x0, max(20, int(int(fw) / 1000 * FW)))
                h0 = min(FH - y0, max(20, int(int(fh) / 1000 * FH)))
                fig = trim_blank(full[y0:y0 + h0, x0:x0 + w0])
                if fig.shape[0] >= 10 and fig.shape[1] >= 10:
                    ok_n += 1
                    fp = ROOT / (it["image"][:-4] + f"_fig{ok_n}.jpg")
                    cv2.imwrite(str(fp), cv2.cvtColor(fig, cv2.COLOR_RGB2BGR))
            if ok_n < len(figs):
                issue = True                 # 有图但没裁好 -> 红色标记
        with DB_LOCK:
            db = load_db()
            for x in db["items"]:
                if x["id"] == it["id"]:
                    x["note"] = text[:2000]
                    if issue:
                        x["fig_issue"] = True
                    break
            save_db(db)

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(work, saved_items))


@app.post("/api/crop/preview")
def crop_preview(payload: dict):
    """预览裁剪: 返回 page+box 区域的临时图 URL, 用于保存前手动裁题内图形。"""
    srcs = [s for s in PAGES_DIR.glob(f"{payload.get('page', '')}.*") if "_web" not in s.name]
    if not srcs:
        return JSONResponse({"ok": False, "msg": "页面不存在"}, status_code=404)
    b = payload.get("box") or {}
    r = page_ratio(payload.get("page", "")) or 1.0
    x, y, w, h = (int(b.get(k, 0) * r) for k in ("x", "y", "w", "h"))
    if w < 20 or h < 20:
        return JSONResponse({"ok": False, "msg": "请先框选题目"}, status_code=400)
    img = np.array(open_photo(srcs[0]))
    H, W = img.shape[:2]
    x = max(0, min(x, W - 20)); y = max(0, min(y, H - 20))   # 边界保护, 避免越界 500
    w = min(w, W - x); h = min(h, H - y)
    if w < 20 or h < 20:
        return JSONResponse({"ok": False, "msg": "框选区域超出图片边界，请重新框选"}, status_code=400)
    crop = img[y:y + h, x:x + w]
    if (cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) > 245).mean() > 0.97:
        return JSONResponse({"ok": False, "msg": "框选区域几乎为空白，请重新框选"}, status_code=400)
    fp = TMP_DIR / f"cfig_{int(time.time() * 1000)}.jpg"
    cv2.imwrite(str(fp), cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
    return {"ok": True, "url": f"/files/.tmp/{fp.name}"}


@app.post("/api/crop")
async def crop(payload: dict):
    """payload: {page, boxes:[{subject,chapter,title,reason,note,x,y,w,h}]}
    box 坐标为缩略图(web)坐标, 自动换算到原图。"""
    page = payload.get("page", "")
    boxes = payload.get("boxes", [])
    if not boxes:
        return JSONResponse({"ok": False, "msg": "没有框选任何题目"}, status_code=400)
    srcs = [s for s in PAGES_DIR.glob(f"{page}.*") if "_web" not in s.name]
    if not srcs:
        return JSONResponse({"ok": False, "msg": "页面不存在"}, status_code=404)
    img = open_photo(srcs[0])
    r = page_ratio(page) or 1.0
    db = load_db()
    saved = []
    for b in boxes:
        x, y, w, h = (max(0, int(b.get(k, 0) * r)) for k in ("x", "y", "w", "h"))
        if w < 20 or h < 20:
            continue
        subject = safe_name(b.get("subject") or "未分类")
        chapter = safe_name(b.get("chapter") or "")
        title = safe_name(b.get("title") or "")
        subj_dir = ITEMS_DIR / subject
        subj_dir.mkdir(parents=True, exist_ok=True)
        crop_img = img.crop((x, y, x + w, y + h))
        item_id = f"q{int(time.time() * 1000)}{len(saved)}"
        img_name = f"{item_id}.jpg"
        img_path = subj_dir / img_name
        crop_img.save(img_path, "JPEG", quality=95)
        item = {
            "id": item_id,
            "image": f"错题/{subject}/{img_name}",
            "subject": subject,
            "chapter": chapter,
            "title": title,
            "reason": b.get("reason", ""),
            "note": b.get("note", ""),
            "group": b.get("group", ""),   # 续块分组: 同一组的多块视为一道题
            "source_page": f"页/{srcs[0].name}",
            "created": time.strftime("%Y-%m-%d %H:%M"),
        }
        db["items"].append(item)
        saved.append(item)
    save_db(db)
    if saved:
        threading.Thread(target=_auto_ai_bg, args=(list(saved),), daemon=True).start()
    return {"ok": True, "count": len(saved), "items": saved, "auto_ai": True}


def trim_margins(img, tol=245):
    """裁掉四周白边。"""
    bw = img.convert("L").point(lambda p: 255 if p < tol else 0)
    bbox = bw.getbbox()
    if bbox:
        pad = 6
        l, t, r, b = bbox
        W, H = img.size
        return img.crop((max(0, l - pad), max(0, t - pad),
                         min(W, r + pad), min(H, b + pad)))
    return img


@app.post("/api/merge")
async def merge_pages(payload: dict):
    """payload: {pages: [p1, p2], flip2: bool} 竖拼接两页成一张新页。
    解决一道题跨正反两页: 拍两张, 拼成一张再正常框选。"""
    pages = payload.get("pages") or []
    flip2 = bool(payload.get("flip2"))
    if len(pages) != 2:
        return JSONResponse({"ok": False, "msg": "请选择两页"}, status_code=400)
    imgs = []
    for pid in pages:
        srcs = list(PAGES_DIR.glob(f"{pid}.*"))
        if not srcs:
            return JSONResponse({"ok": False, "msg": f"页面 {pid} 不存在"}, status_code=404)
        imgs.append(open_photo(srcs[0]))
    if flip2:
        imgs[1] = imgs[1].rotate(180, expand=True)
    trimmed = [trim_margins(im) for im in imgs]
    w = max(im.width for im in trimmed)
    gap = 14
    canvas = Image.new("RGB", (w, sum(im.height for im in trimmed) + gap), "white")
    y = 0
    for im in trimmed:
        canvas.paste(im, ((w - im.width) // 2, y))
        y += im.height + gap
    page_id = f"p{int(time.time() * 1000)}"
    canvas.save(PAGES_DIR / f"{page_id}.jpg", "JPEG", quality=92)
    make_thumbnail(PAGES_DIR / f"{page_id}.jpg", PAGES_DIR / f"{page_id}_web.jpg")
    return {"ok": True, "page": page_id, "web_url": f"/files/页/{page_id}_web.jpg"}


# ---------- 去手写 / 去红笔 (轻量本地处理) ----------

def red_pen_mask(img_bgr):
    """检测红/橙/粉笔迹(HSV), 返回修复 mask。"""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 50, 50), (12, 255, 255))
    m2 = cv2.inRange(hsv, (165, 50, 50), (180, 255, 255))
    mask = cv2.bitwise_or(m1, m2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
    return mask


def strokes_to_mask(shape, strokes, radius=24):
    mask = np.zeros(shape[:2], np.uint8)
    for stroke in strokes:
        prev = None
        for pt in stroke:
            x, y = int(pt[0]), int(pt[1])
            if prev:
                cv2.line(mask, prev, (x, y), 255, radius * 2)
            cv2.circle(mask, (x, y), radius, 255, -1)
            prev = (x, y)
    return mask


def apply_actions(img, actions):
    """按顺序应用清理动作: {type: red|mask, strokes?, radius?}。
    返回 (处理后图像, 是否有实际处理)。"""
    done = False
    for a in actions or []:
        t = a.get("type")
        if t == "red":
            mask = red_pen_mask(img)
            if mask.sum() < 500:
                continue
            img = cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)
            done = True
        elif t == "mask":
            strokes = a.get("strokes") or []
            mask = strokes_to_mask(img.shape, strokes, int(a.get("radius", 24)))
            if mask.sum() < 500:
                continue
            img = cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)
            done = True
    return img, done


def resolve_item_path(image_rel):
    f = (ROOT / image_rel).resolve()
    if not str(f).startswith(str(ROOT)) or not f.exists():
        return None
    return f


@app.post("/api/clean/preview")
def clean_preview(payload: dict):
    """payload: {image, actions} -> 返回处理预览 URL"""
    f = resolve_item_path(payload.get("image", ""))
    if f is None:
        return JSONResponse({"ok": False, "msg": "图片不存在"}, status_code=404)
    img = cv2.imread(str(f))
    out, done = apply_actions(img, payload.get("actions"))
    if not done:
        return JSONResponse({"ok": False, "msg": "未检测到需要清理的痕迹（红笔或无涂抹区域）"}, status_code=400)
    name = f"clean_{int(time.time() * 1000)}.jpg"
    cv2.imwrite(str(TMP_DIR / name), out, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return {"ok": True, "url": f"/files/.tmp/{name}"}


@app.post("/api/clean/save")
def clean_save(payload: dict):
    """payload: {image, actions, keep_original} -> 应用并覆盖保存"""
    f = resolve_item_path(payload.get("image", ""))
    if f is None:
        return JSONResponse({"ok": False, "msg": "图片不存在"}, status_code=404)
    img = cv2.imread(str(f))
    out, done = apply_actions(img, payload.get("actions"))
    if not done:
        return JSONResponse({"ok": False, "msg": "没有需要保存的清理效果"}, status_code=400)
    backup = ""
    if payload.get("keep_original", True):
        bf = f.with_name(f.stem + "_原版" + f.suffix)
        if not bf.exists():
            shutil.copyfile(f, bf)
            backup = bf.name
    cv2.imwrite(str(f), out, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return {"ok": True, "image": payload.get("image"), "backup": backup}


# ---------- 自动拆题 (AI 版面分析) ----------

def page_ratio(page):
    """原图与缩略图的宽度比(原图/缩略图), 均按 EXIF 矫正后计算。"""
    srcs = [s for s in PAGES_DIR.glob(f"{page}.*") if "_web" not in s.name]
    if not srcs:
        return None
    orig = open_photo(srcs[0])
    webs = list(PAGES_DIR.glob(f"{page}_web.*"))
    web = open_photo(webs[0]) if webs else orig
    return orig.size[0] / web.size[0]


@app.post("/api/split/ai")
def trim_blank(img, margin=6):
    """自动裁剪图片四周的空白边缘(与背景色接近的连续行列)。返回收边后的图。"""
    if img is None or img.size == 0:
        return img
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    bg = int(np.median([gray[0, 0], gray[0, w - 1], gray[h - 1, 0], gray[h - 1, w - 1]]))
    diff = np.abs(gray.astype(int) - bg)
    row_has = diff.max(axis=1) > 28
    col_has = diff.max(axis=0) > 28
    ys = np.where(row_has)[0]
    xs = np.where(col_has)[0]
    if len(ys) == 0 or len(xs) == 0:
        return img
    y0 = max(0, int(ys[0]) - margin)
    y1 = min(h, int(ys[-1]) + margin + 1)
    x0 = max(0, int(xs[0]) - margin)
    x1 = min(w, int(xs[-1]) + margin + 1)
    return img[y0:y1, x0:x1]


def split_ai(payload: dict):
    """AI 版面分析自动拆题: 视觉模型直接返回每道题边界框(相对坐标 0-1000)。
    不依赖题号识别, 超宽长图/手写题号都能拆。返回缩略图坐标, 与画布一致。"""
    srcs = [s for s in PAGES_DIR.glob(f"{payload.get('page', '')}.*") if "_web" not in s.name]
    if not srcs:
        return JSONResponse({"ok": False, "msg": "页面不存在"}, status_code=404)
    cfg = ai_config()
    if not cfg["key"] or not cfg["base_url"]:
        return JSONResponse({"ok": False,
                            "msg": "未配置 AI Key，无法使用 AI 拆题"}, status_code=400)
    r = page_ratio(payload.get("page", "")) or 1.0
    img = np.array(open_photo(srcs[0]))
    H, W = img.shape[:2]
    if max(H, W) > 1600:                       # 压缩后发 AI, 坐标用相对比例
        s = 1600 / max(H, W)
        img = cv2.resize(img, (int(W * s), int(H * s)),
                         interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR),
                          [cv2.IMWRITE_JPEG_QUALITY, 90])
    b64 = base64.b64encode(buf).decode()
    prompt = ("你是试卷自动拆题工具。把图片看作 1000x1000 的区域，识别出图片中的每一道题，"
              "返回每道题的边界框 JSON 数组。规则：x=框左边缘、y=框上边缘、w=框宽、h=框高，"
              "均为 0-1000 的相对坐标；相邻题目按空白或题号分隔，框不要重叠、不要包含其他题；"
              "title 一律填空字符串。只输出 JSON，不要输出其他任何文字，格式："
              '[{"title": "", "x": 100, "y": 100, "w": 800, "h": 400}]')
    body = {
        "model": cfg["model"] or "glm-4v-flash",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
        "temperature": 0.1,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["key"]})
    try:
        with urllib.request.urlopen(req, timeout=120) as rq:
            d = json.loads(rq.read())
        text = d["choices"][0]["message"]["content"]
    except Exception as e:
        return JSONResponse({"ok": False, "msg": "AI 拆题失败: " + str(e)[:200]},
                            status_code=502)
    m = re.search(r"\[.*\]", text, re.S)     # 提取 JSON 数组
    if not m:
        return JSONResponse({"ok": False,
                            "msg": "AI 未返回有效框: " + text[:120]}, status_code=502)
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return JSONResponse({"ok": False, "msg": "AI 返回格式错误"}, status_code=502)
    webW, webH = W / r, H / r
    boxes = []
    for b in raw:
        try:
            x = float(b.get("x", 0)) / 1000 * webW
            y = float(b.get("y", 0)) / 1000 * webH
            w = float(b.get("w", 0)) / 1000 * webW
            h = float(b.get("h", 0)) / 1000 * webH
        except (TypeError, ValueError):
            continue
        w = min(w, webW - x)                    # 不超边界
        h = min(h, webH - y)
        if w < 30 or h < 30 or x < 0 or y < 0:
            continue
        boxes.append({"title": "", "x": round(x), "y": round(y),
                      "w": round(w), "h": round(h)})
    if not boxes:
        return JSONResponse({"ok": False, "msg": "AI 未识别到题目"}, status_code=502)
    return {"ok": True, "count": len(boxes), "boxes": boxes}


@app.delete("/api/page/{page_id}")
def delete_page(page_id: str):
    """删除已上传的整页照片(原图+缩略图)。"""
    removed = 0
    for f in PAGES_DIR.glob(f"{page_id}*"):   # 含 xxx.png / xxx_web.jpg 等全部
        f.unlink(missing_ok=True)
        removed += 1
    if removed == 0:
        return JSONResponse({"ok": False, "msg": "页面不存在"}, status_code=404)
    return {"ok": True, "removed": removed}


# ---------- 试卷生成 (Typst) ----------


# ---------- Typst 渲染 PDF ----------

MATH_KEYWORDS = {"frac", "sqrt", "sin", "cos", "tan", "log", "ln", "exp", "lim",
                 "sum", "prod", "int", "times", "dot", "plus", "minus", "arrow",
                 "infinity", "mathrm", "text", "left", "right", "mid", "eq", "ne",
                 "approx", "le", "ge", "lt", "gt", "alpha", "beta", "gamma",
                 "delta", "epsilon", "theta", "pi", "sigma", "omega", "phi", "lambda",
                 "mu", "rho", "tau", "cdots", "to", "Delta", "angle", "perp",
                 "parallel", "degrees", "equiv", "dots", "subset", "union",
                 "intersect", "in", "upright"}
_SUB = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def latex_to_typst(s):
    """把 OCR/AI 输出的公式片段转成 Typst 数学语法, 化学式用 mathrm 正体。"""
    s = re.sub(r"[₀-₉]", lambda m: "_" + m.group(0).translate(_SUB), s)
    s = re.sub(r"[⁰-⁹]", lambda m: "^" + m.group(0).translate(_SUP), s)
    s = s.replace("⁻", "^(-)").replace("⁺", "^(+)")
    s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"frac(\1, \2)", s)
    s = re.sub(r"\\sqrt\{([^{}]*)\}", r"sqrt(\1)", s)
    s = s.replace("\\times", " times ").replace("\\cdot", " dot ")
    s = s.replace("\\pm", " plus.minus ").replace("\\rightarrow", " arrow.r ")
    s = s.replace("\\infty", " infinity ")
    s = s.replace("\\le", " lt.eq ").replace("\\ge", " gt.eq ")
    for k, v in (("\\Delta", "Delta"), ("\\alpha", "alpha"), ("\\beta", "beta"),
                 ("\\gamma", "gamma"), ("\\theta", "theta"), ("\\lambda", "lambda"),
                 ("\\mu", "mu"), ("\\pi", "pi"), ("\\sigma", "sigma"),
                 ("\\omega", "omega"), ("\\phi", "phi"), ("\\epsilon", "epsilon"),
                 ("\\angle", "angle"), ("\\perp", "perp"), ("\\parallel", "parallel"),
                 ("\\circ", "degrees"), ("\\dots", " dots "), ("\\ldots", " dots "),
                 ("\\equiv", " equiv "), ("\\approx", " approx "), ("\\neq", " ne "),
                 ("\\geq", " gt.eq "), ("\\leq", " lt.eq "), ("\\in", " in "),
                 ("\\subset", " subset "), ("\\cup", " union "), ("\\cap", " intersect "),
                 ("\\,", " "), ("\\!", " "), ("\\ ", " ")):
        s = s.replace(k, v)
    # 化学/结构式常用命令
    s = re.sub(r"\\dot\{([^{}]*)\}", r"dot(\1)", s)
    for k, v in (("\\leftharpoons", " arrow.l.r "), ("\\rightleftharpoons", " arrow.l.r "),
                 ("\\longleftrightarrow", " arrow.l.r "), ("\\longrightarrow", " arrow.r "),
                 ("\\longleftarrow", " arrow.l "), ("\\implies", " arrow.r.double "),
                 ("\\iff", " arrow.l.r.double "), ("\\quad", " "), ("\\qquad", " "),
                 ("\\operatorname", ""), ("\\text", ""), ("\\underset", ""),
                 ("\\overset", ""), ("\\stackrel", ""), ("\\begin", ""),
                 ("\\end", ""), ("\\dfrac", "frac"), ("\\tfrac", "frac"),
                 ("\\displaystyle", ""), ("\\limits", ""),
                 ("\\left(", "("), ("\\right)", ")"), ("\\left[", "["),
                 ("\\right]", "]"), ("\\left.", ""), ("\\right.", "")):
        s = s.replace(k, v)
    # 兜底: 其余未知 LaTeX 命令去掉反斜杠, 避免 Typst 解析错误
    s = re.sub(r"\\([a-zA-Z]+)", r"\1", s)
    s = s.replace("⇌", " arrow.l.r ").replace("↔", " arrow.l.r ")
    s = s.replace("→", " arrow.r ").replace("←", " arrow.l ")
    s = s.replace("≤", " lt.eq ").replace("≥", " gt.eq ").replace("≠", " ne ")
    s = s.replace("×", " times ").replace("·", " dot ")
    s = re.sub(r"[{}]", "", s)   # 先删花括号, 再包 upright
    # 多字母串/字母数字组合(化学式/元素) -> upright 正体; 单字母是变量保持斜体
    s = re.sub(r"([A-Za-z][A-Za-z0-9]*)",
               lambda m: m.group(1)
               if (len(m.group(1)) == 1 and m.group(1).isalpha())
                  or m.group(1).lower() in MATH_KEYWORDS
               else f'upright("{m.group(1)}")', s)
    return s


def call_ai_vision(img_rgb):
    """调用视觉大模型识别图片, 返回 Markdown 文本(公式为 LaTeX)。"""
    hh, ww = img_rgb.shape[:2]
    if max(hh, ww) > 1600:
        s = 1600 / max(hh, ww)
        img_rgb = cv2.resize(img_rgb, (int(ww * s), int(hh * s)),
                             interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR),
                          [cv2.IMWRITE_JPEG_QUALITY, 90])
    b64 = base64.b64encode(buf).decode()
    prompt = ("你是试卷题目识别工具。识别图片中的题目，注意：\n"
              "1. 忽略图片中所有手写笔迹、批注、涂改痕迹，只识别印刷体题目内容；\n"
              "2. 不要输出题号（如 1. 2. 3.、①②、第1题 等），直接从题目内容开始；\n"
              "3. 忽略与题目无关的内容：专题/章节标题、知识点标签、出题人/审题人署名、页码、页眉页脚、"
              "水印、练习册名称等，只保留题目本身的题干和选项；\n"
              "4. 第一行输出【题干】，后跟题干文字；\n"
              "5. 不要识别/输出答案：题干括号中的答案如（A）（D C）、选项后的对勾√×、答案行等一律忽略，"
              "无论括号是否闭合；\n"
              "6. 选择题/多选题的选项逐行输出，每行一个：A．选项内容 / B．选项内容 / "
              "C．选项内容 / D．选项内容（用全角句点．）；\n"
              "7. 所有数学公式/化学式用 $...$ LaTeX 语法；\n"
              "8. 题目内嵌图形/示意图用 [图@x,y,w,h] 标记，框必须精确贴合图形本身边界"
              "（含图形外框线），不要包含图形周围的文字、题干或大块空白；坐标是图形相对整图"
              "0-1000 比例，如 [图@620,280,240,180]，没有图形不要加。\n"
              "只输出识别结果，不要解释。")
    body = {
        "model": ai_config()["model"] or "glm-4v-flash",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
        "temperature": 0.1,
    }
    req = urllib.request.Request(
        ai_config()["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + ai_config()["key"]})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())
    return clean_ai_text(d["choices"][0]["message"]["content"])


def clean_ai_text(text):
    """AI 识别后处理: 去题干行首题号、删答案、拆分一行多选项、清理残留标记。"""
    # 1. 去题干行首题号(1. 1、① 第1题), 小问(1)(2)保留
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s == "【题干】":
            continue
        lines[i] = re.sub(
            r"^\s*(?:\d+[.、．)）]|第\s*\d+\s*题|[①②③④⑤⑥⑦⑧⑨⑩])\s*",
            "", ln, count=1)
        break
    text = "\n".join(lines)
    # 2. 删答案标注: 括号内的 A-D 组合(含多字母/未闭合), 如（A）（D C）（AC）
    text = re.sub(r"[（(]\s*(?:[A-D]\s*)+[)）]?", "", text)
    text = re.sub(r"[（(]\s*[)）]", "", text)
    # 3. 删答案行
    text = re.sub(r"^\s*答案[:：]?\s*[A-D]\s*$", "", text, flags=re.M)
    # 4. 删选项行尾的对勾/叉号(答案标记)
    text = re.sub(r"[√×✓✗]\s*$", "", text, flags=re.M)
    # 5. 选项句点统一为全角
    text = re.sub(r"^([A-D])[.、)]", r"\1．", text, flags=re.M)
    # 6. 一行多选项拆成每行一个(A．x B．y C．z D．w)
    lines = text.split("\n")
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        parts = re.split(r"(?=[A-D][．.、)）])", s)
        opts = [p for p in parts if re.match(r"^[A-D][．.、)）]", p)]
        if len(opts) > 1:
            head = s
            for p in opts:
                head = head.replace(p, "", 1)
            if head.strip():
                out.append(head.strip())
            out.extend(p.strip() for p in opts)
        else:
            out.append(s)
    return "\n".join(out)


@app.get("/api/paper/pdf")
def paper_pdf(ids: str = ""):
    """用 Typst 渲染试卷 PDF (宋体正文/黑体大题/楷体续块)。"""
    db = load_db()
    wanted = [i for i in ids.split(",") if i]
    order = {iid: n for n, iid in enumerate(wanted)}
    items = sorted((it for it in db["items"] if it["id"] in wanted),
                   key=lambda it: order.get(it["id"], 999))
    if not items:
        return JSONResponse({"ok": False, "msg": "未选中任何题目"}, status_code=400)

    groups = {}
    for it in items:
        groups.setdefault(it.get("chapter") or "", []).append(it)

    subjects = sorted({it.get("subject") or "" for it in items})
    lines = [
        '#set page(width: 185mm, height: 260mm, margin: (top: 2cm, bottom: 2cm, left: 2.2cm, right: 2.2cm), footer: context { align(center)[#counter(page).display()] })',
        '#set text(font: ("Times New Roman", "SimSun"), size: 10.5pt, lang: "zh")',  # 英文 Times 新罗马 / 中文宋体
        '#set par(justify: true, leading: 0.95em, spacing: 0.95em)',  # 行距=段距=块距, 全局统一
        '#show heading: set text(font: "SimHei")',                    # 题型/大题标题: 黑体
        '#show heading: set par(leading: 0.7em)',
        '#show heading: set block(spacing: 0.95em)',
        '#set block(spacing: 0.95em)',
        # ---- 卷头: 三号标题 / 二号黑体科目 / 五号说明 ----
        '#align(center)[#text(size: 16pt, font: "SimHei")[错题重组试卷]]',
        f'#align(center)[#text(size: 22pt, font: "SimHei", weight: "bold")[{subjects[0] if len(subjects) == 1 else " ".join(subjects)}]]',
        f'#align(center)[#text(size: 10.5pt)[共 {len(items)} 题 · {time.strftime("%Y-%m-%d")}]]',
        '#v(0.45cm)',
        '#text(font: "SimHei")[注意事项：]',
        '1．本试卷由错题收集工具生成，请在答题纸上作答；',
        '2．解答应写出文字说明、证明过程或演算步骤。',
        '#v(0.45cm)',
    ]
    n, prev_g = 0, None
    for gname, gitems in groups.items():
        if gname:
            lines.append(f"= {gname}")          # 大题标题: 黑体
        for it in gitems:
            g = it.get("group") or ""
            if g and g == prev_g:
                lines.append('#text(font: "KaiTi", size: 10.5pt)[(续)] \\')
            else:
                n += 1
                lines.append(f"{n}．")               # 题号顶格, 题干接同一行
            prev_g = g
            txt = (it.get("note") or "").strip()
            if txt:
                # 清洗 AI 输出的 Markdown: 去代码块围栏和 # 标题标记, 避免 Typst 误渲染
                txt = re.sub(r"^```[a-z]*\s*$", "", txt, flags=re.M)
                txt = re.sub(r"^#{1,6}\s*", "", txt, flags=re.M)
                txt = txt.replace("【题干】", "")
                placed = False
                fig_tokens = []
                opt_buf = []
                first_ln = True      # 题号后的第一个文本行, 与题号同行(不加换行符)

                def esc_ln(s):
                    """公式 $..$ 转 Typst, 其余文本转义。"""
                    segs = re.split(r"(\$[^$]+\$)", s)
                    out = ""
                    for seg in segs:
                        if seg.startswith("$") and seg.endswith("$") and len(seg) > 2:
                            out += "$" + latex_to_typst(seg[1:-1]) + "$"
                        else:
                            for ch, esc in (("#", "\\#"), ("$", "\\$"), ("{", "\\{"),
                                            ("}", "\\}"), ("[", "\\["), ("]", "\\]"),
                                            ("_", "\\_")):
                                seg = seg.replace(ch, esc)
                            out += seg
                    return out

                def flush_opts():
                    """选项 A．B．C．D． 按内容长度自适应: 短->4列 / 中->2列 / 长->1列。"""
                    nonlocal opt_buf
                    if opt_buf:
                        ops = opt_buf[:4]

                        def est(s):          # 显示宽度估算: 全角=1, 半角=0.55
                            return sum(1.0 if ord(c) > 0x2E7F else 0.55 for c in s)

                        maxw = max(est(o) for o in ops)
                        cols = 4 if maxw <= 9 else (2 if maxw <= 18 else 1)
                        cells = "".join(f"[{esc_ln(o)}]" for o in ops)
                        lines.append(f"#block(inset: (left: 2em))["
                                     f"#grid(columns: {cols}, column-gutter: 1.2em, "
                                     f"row-gutter: 0.95em){cells}]")
                        opt_buf = []

                for ln in txt.split("\n"):
                    ln = ln.strip()
                    if not ln:
                        continue
                    if "[图]" in ln or "[图@" in ln or "（图）" in ln or "(图)" in ln:
                        # 题内图形: 按 [图@x,y,w,h] 精确裁剪小块插入文字流
                        full_img = None
                        def fig_repl(m):
                            nonlocal full_img
                            fx, fy, fw, fh = (int(v) for v in m.groups())
                            idx = len(fig_tokens) + 1
                            saved = ROOT / (it["image"][:-4] + f"_fig{idx}.jpg")
                            if saved.exists():          # 保存时已裁好的图块
                                fp = saved
                            else:                       # 兜底: 现场裁剪
                                if full_img is None:
                                    full_img = np.array(open_photo(ROOT / it["image"]))
                                FH, FW = full_img.shape[:2]
                                x0 = max(0, int(fx / 1000 * FW)); y0 = max(0, int(fy / 1000 * FH))
                                w0 = min(FW - x0, max(20, int(fw / 1000 * FW)))
                                h0 = min(FH - y0, max(20, int(fh / 1000 * FH)))
                                fp = TMP_DIR / f"fig_{it['id']}_{n}_{len(fig_tokens)}.jpg"
                                fig = trim_blank(full_img[y0:y0 + h0, x0:x0 + w0])
                                if fig.shape[0] < 10 or fig.shape[1] < 10:
                                    fig = full_img[y0:y0 + h0, x0:x0 + w0]
                                cv2.imwrite(str(fp), cv2.cvtColor(fig, cv2.COLOR_RGB2BGR))
                            fig_tokens.append(str(fp))
                            return f"@@FIG{len(fig_tokens) - 1}@@"
                        ln = re.sub(r"\[图@(\d+),(\d+),(\d+),(\d+)\]", fig_repl, ln)
                        if "[图]" in ln:
                            ln = ln.replace("[图]", "@@IMG@@")
                        ln = ln.replace("（图）", "").replace("(图)", "")
                        segs = re.split(r"(\$[^$]+\$)", ln)
                        out = ""
                        for seg in segs:
                            if seg.startswith("$") and seg.endswith("$") and len(seg) > 2:
                                out += "$" + latex_to_typst(seg[1:-1]) + "$"
                            else:
                                for ch, esc in (("#", "\\#"), ("$", "\\$"), ("{", "\\{"),
                                                ("}", "\\}"), ("[", "\\["), ("]", "\\]"),
                                                ("_", "\\_")):
                                    seg = seg.replace(ch, esc)
                                out += seg
                        for i, fp in enumerate(fig_tokens):
                            out = out.replace(f"@@FIG{i}@@", f'#image("{fp}", width: 40%)')
                        out = out.replace("@@IMG@@", f'#image("{ROOT / it["image"]}", width: 45%)')
                        if out.strip():
                            flush_opts()
                            lines.append(out + ("" if first_ln else " \\"))
                            first_ln = False
                        placed = True
                        continue
                    om = re.match(r"^([A-D])[．.、)）]\s*(.*)$", ln)
                    if om:                                # 选项行: 收集后 grid 对齐
                        if len(opt_buf) >= 4:
                            flush_opts()
                        opt_buf.append(ln)
                        continue
                    flush_opts()
                    out = esc_ln(ln)
                    if out.strip():
                        lines.append(out + ("" if first_ln else " \\"))
                        first_ln = False
                flush_opts()
            else:
                # 未识别出文字: 保留原图
                lines.append(f'#image("{ROOT / it["image"]}", width: 50%)')

    typ_path = TMP_DIR / f"paper_{int(time.time() * 1000)}.typ"
    pdf_path = typ_path.with_suffix(".pdf")
    typ_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        typst.compile(typ_path, output=pdf_path,
                      font_paths=[str(FONTS_DIR)], root="/")
    except Exception as e:
        return JSONResponse({"ok": False,
                            "msg": "PDF生成失败: " + str(e)[:200]}, status_code=500)
    return {"ok": True, "url": f"/files/.tmp/{pdf_path.name}", "count": n}


# ---------- AI 视觉识别 (公式 -> LaTeX, 走大模型 API 不吃本地内存) ----------

AI_CONFIG_FILE = ROOT / ".ai_config.json"


def ai_config():
    cfg = {"base_url": "", "key": "", "model": ""}
    if AI_CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(AI_CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    for k, env in (("base_url", "AI_BASE_URL"), ("key", "AI_API_KEY"),
                    ("model", "AI_MODEL")):
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    return cfg


@app.get("/api/ai/config")
def get_ai_config():
    cfg = ai_config()
    return {"ok": True, "base_url": cfg["base_url"], "model": cfg["model"],
            "key_set": bool(cfg["key"])}


@app.post("/api/ai/config")
def set_ai_config(payload: dict):
    cfg = ai_config()
    for k in ("base_url", "key", "model"):
        if k in payload:
            cfg[k] = str(payload[k]).strip()
    AI_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return {"ok": True, "key_set": bool(cfg["key"]), "model": cfg["model"]}


@app.post("/api/ocr/ai")
def ocr_ai(payload: dict):
    """payload: {page, box} 或 {image} -> AI 视觉识别, 返回 Markdown(公式为 LaTeX)
    box 为缩略图坐标, 自动换算原图。"""
    cfg = ai_config()
    if not cfg["key"] or not cfg["base_url"]:
        return JSONResponse({"ok": False,
                            "msg": "未配置 AI Key，请在服务器配置 AI_API_KEY"},
                            status_code=400)
    if payload.get("image"):               # 直接识别已保存的错题图
        f = resolve_item_path(payload["image"])
        if f is None:
            return JSONResponse({"ok": False, "msg": "图片不存在"}, status_code=404)
        img = np.array(open_photo(f))
    else:
        srcs = [s for s in PAGES_DIR.glob(f"{payload.get('page', '')}.*") if "_web" not in s.name]
        if not srcs:
            return JSONResponse({"ok": False, "msg": "页面不存在"}, status_code=404)
        b = payload.get("box") or {}
        r = page_ratio(payload.get("page", "")) or 1.0
        x, y, w, h = (int(b.get(k, 0) * r) for k in ("x", "y", "w", "h"))
        if w < 20 or h < 20:
            return JSONResponse({"ok": False, "msg": "请先框选题目"}, status_code=400)
        img = np.array(open_photo(srcs[0]))[y:y + h, x:x + w]
    try:
        text = call_ai_vision(img)
        return {"ok": True, "text": text}
    except Exception as e:
        return JSONResponse({"ok": False, "msg": "AI 识别失败: " + str(e)[:200]},
                            status_code=502)


@app.get("/api/items")
def list_items():
    db = load_db()
    return {"items": list(reversed(db["items"]))}


@app.put("/api/item/{item_id}")
def update_item(item_id: str, payload: dict):
    """更新错题信息(标题/章节/错因/备注/科目)。"""
    db = load_db()
    for it in db["items"]:
        if it["id"] == item_id:
            for k in ("title", "chapter", "reason", "note", "subject"):
                if k in payload:
                    it[k] = str(payload[k])[:2000]
            save_db(db)
            return {"ok": True, "item": it}
    return JSONResponse({"ok": False, "msg": "不存在"}, status_code=404)


@app.delete("/api/item/{item_id}")
def delete_item(item_id: str):
    db = load_db()
    for it in db["items"]:
        if it["id"] == item_id:
            f = ROOT / it["image"]
            if f.exists():
                f.unlink()
            db["items"].remove(it)
            save_db(db)
            return {"ok": True}
    return JSONResponse({"ok": False, "msg": "不存在"}, status_code=404)


@app.get("/api/paper", response_class=HTMLResponse)
def make_paper(ids: str = ""):
    """ids: 逗号分隔, 顺序即试卷顺序。生成可打印的试卷页面。"""
    db = load_db()
    wanted = [i for i in ids.split(",") if i]
    items = [it for it in db["items"] if it["id"] in wanted]
    # 按 ids 中的顺序排列
    order = {iid: n for n, iid in enumerate(wanted)}
    items.sort(key=lambda it: order.get(it["id"], 999))
    cards, n, prev_g = [], 0, None
    for it in items:
        g = it.get("group") or ""
        if g and g == prev_g:                       # 同一续块组: 不重新编号
            no = '<div class="qno cont">(续)</div>'
        else:
            n += 1
            no = f'<div class="qno">{n}.</div>'
        prev_g = g
        cards.append(f'<div class="q">{no}'
                     f'<div class="qimg"><img src="/files/{it["image"]}"></div></div>')
    cards = "".join(cards)
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>错题重组试卷</title>
<style>
  body {{ font-family: "Songti SC","SimSun",serif; margin: 24px; }}
  h1 {{ text-align: center; font-size: 18px; margin: 8px 0 4px; }}
  .meta {{ text-align: center; color:#666; font-size: 12px; margin-bottom: 16px; }}
  .q {{ display: flex; gap: 8px; margin-bottom: 18px; page-break-inside: avoid; }}
  .qno {{ font-size: 15px; font-weight: bold; min-width: 26px; }}
  .qno.cont {{ color: #999; font-weight: normal; font-size: 13px; min-width: 26px; }}
  .qimg img {{ max-width: 620px; border: 1px solid #ddd; }}
  .print {{ position: fixed; top: 12px; right: 12px; padding: 8px 16px;
           font-size: 14px; cursor: pointer; }}
  @media print {{
    .print {{ display: none; }}
    body {{ margin: 0; }}
    .qimg img {{ max-width: 100%; border: none; }}
  }}
  @page {{ size: A4; margin: 15mm; }}
</style></head><body>
<button class="print" onclick="window.print()">🖨 打印 / 存为 PDF</button>
<h1>错题重组试卷</h1>
<div class="meta">共 {n} 题 · 由错题自动编排生成 · (续) 表示同一道题的接续部分</div>
{cards or "<p>未选中任何题目</p>"}
</body></html>"""


# ---------- 静态文件 ----------

class NoCacheStaticFiles(StaticFiles):
    """HTML/JS/CSS 不缓存, 避免手机浏览器拿到旧版界面。"""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.endswith((".html", ".js", ".css")):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


app.mount("/files", StaticFiles(directory=ROOT), name="files")
app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    print("\n错题收集工具已启动: http://localhost:8091\n")
    uvicorn.run(app, host="0.0.0.0", port=8091, log_level="warning")
