#!/usr/bin/env python3
"""
错题收集 WebUI 后端
功能: 上传整页照片 -> 网页框选题 -> 裁剪存档 -> 组卷打印

运行:  python 工具/错题裁剪工具/app.py
访问:  http://localhost:8091
"""
import base64
import collections
import io
import json
import zipfile
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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
PAGES_DIR = ROOT / "pages"                           # 整页照片
ITEMS_DIR = ROOT / "items"                           # 裁剪出的错题图
DB_FILE = ROOT / "library.json"

# 科目 -> 英文目录名(界面仍显示中文)。内置科目用固定英文名, 新增科目自动分配 customN
SUBJ_DIRNAME = {"数学": "math", "物理": "physics", "化学": "chemistry",
                "生物": "biology", "英语": "english", "语文": "chinese",
                "政治": "politics", "历史": "history", "地理": "geography",
                "其他": "other", "未分类": "uncategorized"}
DEFAULT_SUBJECTS = list(SUBJ_DIRNAME.keys())[:10]      # 到「其他」为止
SUBJ_FILE = ROOT / "subjects.json"
_SUBJ_CACHE = None


def load_subjects():
    """返回 (科目列表, 科目->目录名 映射)。可在「设置」页增删。"""
    global _SUBJ_CACHE
    if _SUBJ_CACHE is None:
        dirs, lst = dict(SUBJ_DIRNAME), list(DEFAULT_SUBJECTS)
        if SUBJ_FILE.exists():
            try:
                d = json.loads(SUBJ_FILE.read_text("utf-8"))
                if isinstance(d.get("list"), list) and d["list"]:
                    lst = [str(x).strip() for x in d["list"] if str(x).strip()]
                if isinstance(d.get("dirs"), dict):
                    dirs.update({str(k): str(v) for k, v in d["dirs"].items()})
            except Exception:
                pass
        for extra in ("其他", "未分类"):                # 兜底科目始终存在
            if extra not in lst:
                lst.append(extra)
        _SUBJ_CACHE = (lst, dirs)
    return _SUBJ_CACHE[0], dict(_SUBJ_CACHE[1])


def save_subjects(lst, dirs):
    global _SUBJ_CACHE
    _SUBJ_CACHE = None
    SUBJ_FILE.write_text(json.dumps({"list": lst, "dirs": dirs},
                                    ensure_ascii=False, indent=2), "utf-8")


def subj_dirname(subject):
    """科目 -> 目录名(英文)。未知科目自动分配 customN 并持久化。"""
    subject = (subject or "").strip() or "其他"
    lst, dirs = load_subjects()
    if subject in dirs:
        return dirs[subject]
    used = set(dirs.values())
    i = 1
    while f"custom{i}" in used:
        i += 1
    dirs[subject] = f"custom{i}"
    if subject not in lst:
        lst.append(subject)
    save_subjects(lst, dirs)
    return dirs[subject]


TMP_DIR = ROOT / ".tmp"                            # 清理预览临时文件
if getattr(sys, "frozen", False):
    STATIC_DIR = Path(getattr(sys, "_MEIPASS", ROOT)) / "static"
else:
    STATIC_DIR = Path(__file__).resolve().parent / "static"

for d in (PAGES_DIR, ITEMS_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)
FONTS_DIR = ROOT / "fonts"
TYPST_PKG_DIR = ROOT / "typst-packages"          # 本地 Typst 包(mitex)
TYPST_PKG_DIR.mkdir(parents=True, exist_ok=True)
FONTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="错题收集工具")


def load_db():
    if DB_FILE.exists():
        db = json.loads(DB_FILE.read_text(encoding="utf-8"))
    else:
        db = {"items": []}
    # 兼容旧数据: 补全新增字段(编号/答案/解析/关键字/星级/图块)
    dirty = False
    for it in db.get("items", []):
        if "code" not in it:
            it["code"] = next_code(db, it.get("subject") or "未分类")
            dirty = True
        for k, v in (("answer", ""), ("analysis", ""), ("keywords", ""),
                     ("star", 0), ("figures", [])):
            if k not in it:
                it[k] = v
                dirty = True
    if dirty:
        save_db(db)
    return db


# ---------- 数据导入 / 导出 (ZIP 完整包) ----------

@app.get("/api/export")
def export_data():
    """打包全部数据: library.json + items 图片 + 配置, 下载 ZIP。"""
    buf = io.BytesIO()
    db = load_db()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("library.json", json.dumps(db, ensure_ascii=False, indent=2))
        for f, name in ((TPL_PATH, "chapter_templates.json"),
                        (PREFIX_FILE, "code_prefix.json")):
            if f.exists():
                z.write(f, name)
        for f in ITEMS_DIR.rglob("*"):
            if f.is_file():
                z.write(f, str(f.relative_to(ROOT)))
        z.writestr("export_info.json", json.dumps({
            "tool": "HomeWorkCollection", "format": 1,
            "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
            "items": len(db.get("items", [])),
        }, ensure_ascii=False, indent=2))
    buf.seek(0)
    fname = f"homework_export_{time.strftime('%Y%m%d_%H%M')}.zip"
    return Response(buf.read(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/import")
async def import_data(file: UploadFile = File(...)):
    """导入导出的 ZIP 包: 图片解压 + 题目全部重新编号(不覆盖现有数据)。"""
    data = await file.read()
    if len(data) < 50:
        return JSONResponse({"ok": False, "msg": "文件为空"}, status_code=400)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return JSONResponse({"ok": False, "msg": "不是有效的 ZIP 文件"}, status_code=400)
    names = zf.namelist()
    if "library.json" not in names:
        return JSONResponse({"ok": False, "msg": "压缩包里没有 library.json（不是本工具导出的数据包）"},
                            status_code=400)
    try:
        incoming = json.loads(zf.read("library.json")).get("items", [])
    except Exception:
        return JSONResponse({"ok": False, "msg": "library.json 解析失败"}, status_code=400)
    if not incoming:
        return JSONResponse({"ok": False, "msg": "数据包里没有题目"}, status_code=400)

    # 1) 解压图片(同名文件加时间戳, 不覆盖现有)
    img_map, img_n = {}, 0
    for nm in names:
        if not nm.startswith("items/") or nm.endswith("/"):
            continue
        rel = Path(nm)
        target = ROOT / rel
        if target.exists():
            target = target.with_name(f"{target.stem}_{int(time.time() * 1000)}{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(nm))
        img_map[nm] = str(target.relative_to(ROOT))
        img_n += 1

    # 2) 题目入库: 新 id + 重新编号
    db = load_db()
    before = len(db.get("items", []))
    added = 0
    for it in incoming:
        if not isinstance(it, dict):
            continue
        subject = (it.get("subject") or "未分类").strip()
        new = dict(it)
        new["id"] = f"q{int(time.time() * 1000)}{added}"
        new["code"] = next_code(db, subject)                 # 冲突时重新编号
        new["subject"] = subject
        if it.get("image") and it["image"] in img_map:
            new["image"] = img_map[it["image"]]
        figs = []
        for fg in it.get("figures") or []:
            g = dict(fg)
            if g.get("file") in img_map:
                g["file"] = img_map[g["file"]]
            figs.append(g)
        new["figures"] = figs
        new.setdefault("created", time.strftime("%Y-%m-%d %H:%M"))
        for k, v in (("note", ""), ("answer", ""), ("analysis", ""), ("keywords", ""),
                     ("chapter", ""), ("title", ""), ("group", ""), ("star", 0)):
            new.setdefault(k, v)
        db["items"].append(new)
        added += 1
    save_db(db)
    return {"ok": True, "added": added, "images": img_n,
            "total_before": before, "total_after": len(db["items"])}


# ---------- 编号前缀 (按科目, 可在界面修改) ----------
PREFIX_FILE = ROOT / "code_prefix.json"
DEFAULT_PREFIX = {"数学": "MA", "物理": "PH", "化学": "CH", "生物": "BI",
                 "英语": "EN", "语文": "CN", "政治": "ZZ", "历史": "LS",
                 "地理": "DL", "其他": "OT", "未分类": "OT"}


def load_prefix():
    p = dict(DEFAULT_PREFIX)
    if PREFIX_FILE.exists():
        try:
            p.update({k: str(v).strip().upper()[:4]
                      for k, v in json.loads(PREFIX_FILE.read_text("utf-8")).items()})
        except Exception:
            pass
    return p


@app.get("/api/subjects")
def get_subjects():
    lst, dirs = load_subjects()
    return {"ok": True, "subjects": lst, "dirs": dirs, "prefixes": load_prefix()}


@app.put("/api/subjects")
def put_subjects(payload: dict):
    """增删科目。被删科目下的题目归入「其他」(不删除题目和图片)。"""
    new = [str(x).strip() for x in (payload.get("subjects") or []) if str(x).strip()]
    if not new:
        return JSONResponse({"ok": False, "msg": "科目不能为空"}, status_code=400)
    if len(set(new)) != len(new):
        return JSONResponse({"ok": False, "msg": "科目名称重复"}, status_code=400)
    lst0, dirs0 = load_subjects()
    removed = [x for x in lst0 if x not in new]
    dirs, used = {}, set()
    for s in new:                                        # 已存在的科目保留原目录名
        if dirs0.get(s) and dirs0[s] not in used:
            dirs[s] = dirs0[s]; used.add(dirs0[s])
    i = 1
    for s in new:                                        # 新科目分配 customN
        if s not in dirs:
            while f"custom{i}" in used:
                i += 1
            dirs[s] = f"custom{i}"; used.add(f"custom{i}")
    save_subjects(new, dirs)
    # 新科目补编号前缀(默认 OT, 可在设置页改)
    pfx, changed = load_prefix(), False
    for s in new:
        if not pfx.get(s):
            pfx[s] = "OT"; changed = True
    if changed:
        PREFIX_FILE.write_text(json.dumps(pfx, ensure_ascii=False, indent=2), "utf-8")
    # 被删科目的题目 -> 其他
    moved = 0
    if removed:
        with DB_LOCK:
            db = load_db()
            for it in db["items"]:
                if it.get("subject") in removed:
                    it["subject"] = "其他"; moved += 1
            if moved:
                save_db(db)
    return {"ok": True, "subjects": new, "dirs": dirs, "removed": removed, "moved": moved}


@app.get("/api/prefix")
def get_prefix():
    return {"ok": True, "prefix": load_prefix(), "defaults": DEFAULT_PREFIX}


@app.put("/api/prefix")
def put_prefix(payload: dict):
    p = load_prefix()
    for k, v in (payload.get("prefix") or {}).items():
        v = str(v).strip().upper()[:4]
        if v:
            p[k] = v
    PREFIX_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2), "utf-8")
    return {"ok": True, "prefix": p}


def next_code(db, subject):
    """生成编号: 科目前缀 + 流水号(至少 4 位, 超过自动扩位 -> 近乎无限)。
    如 MA0001 … MA9999 → MA10000 → MA100000; 各科目独立计数。"""
    pfx = load_prefix().get(subject, "OT")
    nums = []
    for it in db.get("items", []):
        c = it.get("code") or ""
        if c.startswith(pfx) and c[len(pfx):].isdigit():
            nums.append(int(c[len(pfx):]))
    nxt = max(nums, default=0) + 1
    width = max(4, len(str(nxt)))          # 4 位起步, 超出自动加位
    return f"{pfx}{nxt:0{width}d}"


def save_db(db):
    backup_db()                                   # 每次写库前留一份旧版
    DB_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=2),
                       encoding="utf-8")


def imread_u(path):
    """读图: cv2.imread/np.fromfile 在 Windows 上不支持中文路径, 用 Python IO + imdecode。"""
    try:
        with open(str(path), "rb") as f:               # Python IO 支持任意 Unicode 路径
            data = np.frombuffer(f.read(), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    except Exception as e:
        log_ai("读图失败", f"{path}: {e}")
        return None


def imwrite_u(path, img, ext=None, params=None) -> bool:
    """写图: cv2.imwrite/np.tofile 在 Windows 上不支持中文路径, 用 imencode + Python IO。"""
    ext = ext or (Path(str(path)).suffix or ".jpg")
    try:
        ok, buf = cv2.imencode(ext, img, params or [])
        if not ok:
            return False
        with open(str(path), "wb") as f:
            f.write(buf.tobytes())
        return True
    except Exception as e:
        log_ai("写图失败", f"{path}: {e}")
        return False


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


# ---------- 静态文件: 只开放 pages/ items/ .tmp/(避免 library.json、.ai_config.json 被下载) ----------
FILES_ALLOWED = ("pages/", "items/", ".tmp/")


@app.get("/files/{path:path}")
def serve_file(path: str):
    rel = path.replace("\\", "/").lstrip("/")
    if ".." in rel or not rel.startswith(FILES_ALLOWED):
        return JSONResponse({"ok": False, "msg": "forbidden"}, status_code=403)
    fp = ROOT / rel
    if not fp.is_file():
        log_ai("文件缺失", f"/files/{rel} → {fp}")
        return JSONResponse({"ok": False, "msg": "not found"}, status_code=404)
    return FileResponse(fp)


# ---------- 页面路由 ----------

NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache", "Expires": "0"}


def _index_html():
    """返回首页 HTML, 强制禁用浏览器缓存(否则改动后刷新仍是旧版)。"""
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"),
                        headers=NO_CACHE_HEADERS)


@app.get("/", response_class=HTMLResponse)
def index():
    return _index_html()


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
            "web_url": f"/files/pages/{page_id}_web.jpg"}


@app.get("/api/pages")
def list_pages():
    """整页照片列表(含编号 P1、P2…, 按上传时间正序编号, 最新在前)。"""
    files = [f for f in sorted(PAGES_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
             if f.name.endswith("_web.jpg")]
    out = []
    for i, f in enumerate(files, start=1):          # P1 = 最早上传
        pid = f.name[:-len("_web.jpg")]
        out.append({"page": pid, "no": i,
                    "web_url": f"/files/pages/{f.name}"})
    return list(reversed(out))                      # 列表展示: 最新在前


DB_LOCK = threading.Lock()
BACKUP_DIR = ROOT / "backups"                     # 数据库自动备份
TRASH_DIR = ROOT / ".trash"                       # 删除的文件先移到这里(可恢复)
for d in (BACKUP_DIR, TRASH_DIR):
    d.mkdir(parents=True, exist_ok=True)


def backup_db(keep=30):
    """写库前备份旧版本, 保留最近 keep 份。"""
    if not DB_FILE.exists():
        return
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        shutil.copy2(DB_FILE, BACKUP_DIR / f"library_{ts}.json")
        olds = sorted(BACKUP_DIR.glob("library_*.json"))
        for f in olds[:-keep]:
            f.unlink(missing_ok=True)
    except Exception:
        pass


def to_trash(path):
    """删除文件前移到回收站(保留可恢复)。"""
    try:
        if path.exists():
            dst = TRASH_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{path.name}"
            shutil.move(str(path), str(dst))
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
TPL_PATH = ROOT / "chapter_templates.json"


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


def ai_recognize_one(it, force=False):
    """对单个已保存题目做 AI 识别 + 题内图形裁剪, 写库并返回更新后的 item; 失败返回 None。
    force=False 时遵循 A 策略(已有识别内容则不覆盖, 保护手动输入)。"""
    f = ROOT / it["image"]
    if not f.exists():
        return None
    if not force:
        with DB_LOCK:
            cur = next((x for x in load_db()["items"] if x["id"] == it["id"]), None)
            if cur and (cur.get("note") or "").strip():
                return None
    try:
        text = call_ai_vision(np.array(open_photo(f)))
    except Exception as e:
        log_ai("识别失败", f"{it.get('code')}: {str(e)[:120]}")
        return None
    if not text:
        return None
    issue = False
    figs = []
    raws = re.findall(r"\[图@(\d+),(\d+),(\d+),(\d+)\]", text)
    if raws:
        full = np.array(open_photo(f))
        FH, FW = full.shape[:2]
        for i, (fx, fy, fw, fh) in enumerate(raws, start=1):
            x0 = max(0, int(int(fx) / 1000 * FW)); y0 = max(0, int(int(fy) / 1000 * FH))
            w0 = min(FW - x0, max(20, int(int(fw) / 1000 * FW)))
            h0 = min(FH - y0, max(20, int(int(fh) / 1000 * FH)))
            fig = trim_blank(full[y0:y0 + h0, x0:x0 + w0])
            if fig.shape[0] < 10 or fig.shape[1] < 10:
                issue = True                  # 有图但没裁好 -> 红色标记
                continue
            rel = f"{it['image'][:-4]}_fig{i}.jpg"
            imwrite_u(ROOT / rel, cv2.cvtColor(fig, cv2.COLOR_RGB2BGR))
            figs.append({"n": i, "file": rel, "x": int(fx), "y": int(fy),
                         "w": int(fw), "h": int(fh)})
        cnt = [0]

        def _renum(m):
            cnt[0] += 1
            return f"[图{cnt[0]}]"

        text = re.sub(r"\[图@\d+,\d+,\d+,\d+\]", _renum, text)   # 坐标->图N
    updated = None
    with DB_LOCK:
        db = load_db()
        for x in db["items"]:
            if x["id"] == it["id"]:
                if force or not (x.get("note") or "").strip():
                    x["note"] = text[:4000]
                    if figs:
                        x["figures"] = figs
                    x.pop("fig_issue", None)
                if issue and not (x.get("figures") or []):
                    x["fig_issue"] = True
                updated = dict(x)
                break
        save_db(db)
    return updated


def _auto_ai_bg(saved_items):
    """后台线程池并发 AI 识别(A 策略: 已有内容不覆盖)。"""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(lambda it: ai_recognize_one(it, force=False), saved_items))


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
    if not imwrite_u(fp, cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)):
        return JSONResponse({"ok": False, "msg": "预览图写入失败（检查路径是否含中文/权限）"},
                            status_code=500)
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
        sd = subj_dirname(subject)                  # 英文科目目录名
        subj_dir = ITEMS_DIR / sd
        subj_dir.mkdir(parents=True, exist_ok=True)
        crop_img = img.crop((x, y, x + w, y + h))
        item_id = f"q{int(time.time() * 1000)}{len(saved)}"
        img_name = f"{item_id}.jpg"
        img_path = subj_dir / img_name
        crop_img.save(img_path, "JPEG", quality=95)
        # 图块: box.figures 里的相对坐标(0-1000) -> 裁出图块文件
        figs = []
        for fi, fg in enumerate(b.get("figures") or [], start=1):
            fx, fy, fw, fh = (int(fg.get(k, 0)) for k in ("x", "y", "w", "h"))
            W, H = crop_img.size
            x0 = max(0, int(fx / 1000 * W)); y0 = max(0, int(fy / 1000 * H))
            w0 = min(W - x0, max(20, int(fw / 1000 * W)))
            h0 = min(H - y0, max(20, int(fh / 1000 * H)))
            if x0 >= W or y0 >= H or w0 < 5 or h0 < 5:
                continue                      # 坐标越界/太小: 跳过该图块, 不中断保存
            try:
                fimg = trim_margins(crop_img.crop((x0, y0, x0 + w0, y0 + h0)))
            except Exception:
                continue
            if fimg.size[0] < 3 or fimg.size[1] < 3:
                continue
            fname = f"{item_id}_fig{len(figs) + 1}.jpg"
            try:
                fimg.save(subj_dir / fname, "JPEG", quality=95)
            except Exception:
                continue
            figs.append({"n": len(figs) + 1, "file": f"items/{sd}/{fname}",
                         "x": fx, "y": fy, "w": fw, "h": fh})
        item = {
            "id": item_id,
            "code": next_code(db, subject),
            "image": f"items/{sd}/{img_name}",
            "subject": subject,
            "chapter": chapter,
            "title": title,
            "reason": b.get("reason", ""),
            "note": b.get("note", ""),
            "answer": b.get("answer", ""),
            "analysis": b.get("analysis", ""),
            "keywords": b.get("keywords", ""),
            "star": max(0, min(5, int(b.get("star") or 0))),
            "figures": figs,
            "group": b.get("group", ""),   # 续块分组: 同一组的多块视为一道题
            "source_page": f"pages/{srcs[0].name}",
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
    return {"ok": True, "page": page_id, "web_url": f"/files/pages/{page_id}_web.jpg"}


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
    img = imread_u(f)
    out, done = apply_actions(img, payload.get("actions"))
    if not done:
        return JSONResponse({"ok": False, "msg": "未检测到需要清理的痕迹（红笔或无涂抹区域）"}, status_code=400)
    name = f"clean_{int(time.time() * 1000)}.jpg"
    imwrite_u(TMP_DIR / name, out, params=[cv2.IMWRITE_JPEG_QUALITY, 95])
    return {"ok": True, "url": f"/files/.tmp/{name}"}


@app.post("/api/clean/save")
def clean_save(payload: dict):
    """payload: {image, actions, keep_original} -> 应用并覆盖保存"""
    f = resolve_item_path(payload.get("image", ""))
    if f is None:
        return JSONResponse({"ok": False, "msg": "图片不存在"}, status_code=404)
    img = imread_u(f)
    out, done = apply_actions(img, payload.get("actions"))
    if not done:
        return JSONResponse({"ok": False, "msg": "没有需要保存的清理效果"}, status_code=400)
    backup = ""
    if payload.get("keep_original", True):
        bf = f.with_name(f.stem + "_原版" + f.suffix)
        if not bf.exists():
            shutil.copyfile(f, bf)
            backup = bf.name
    imwrite_u(f, out, params=[cv2.IMWRITE_JPEG_QUALITY, 95])
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
        "temperature": 0,                    # 0 = 尽量确定, 减少随机偏差
    }
    if "deepseek" in (ai_config()["base_url"] or "").lower():
        body["reasoning_effort"] = "none"   # 识别是感知任务, 关思考可提速约 40%
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["key"]})
    try:
        _t0 = time.time()
        with urllib.request.urlopen(req, timeout=120) as rq:
            d = json.loads(rq.read())
        text = d["choices"][0]["message"]["content"]
        log_ai("拆题", cfg["model"], True, (time.time() - _t0) * 1000, f"{len(text)} 字")
    except Exception as e:
        log_ai("拆题", cfg["model"], False, 0, str(e))
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
        to_trash(f)                            # 移入 .trash 而非直接删除
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


CE_ARROWS = [("<=>>", " \\rightleftharpoons "), ("<<=>", " \\leftrightharpoons "),
             ("<=>", " \\rightleftharpoons "), ("<->", " \\leftrightarrow "),
             ("->", " \\rightarrow "), ("<-", " \\leftarrow "),
             ("=>", " \\Rightarrow "), ("<=", " \\Leftarrow "),
             ("↑", " \\uparrow "), ("↓", " \\downarrow ")]


def ce_body(body):
    """处理 \\ce{} 内部: 元素后数字变下标 + 化学箭头。"""
    # 元素符号后紧跟的数字 -> 下标(不碰已有的 _{} ^{} 与括号内数字)
    body = re.sub(r"([A-Z][a-z]?)(\d+)(?![\d}])", r"\1_{\2}", body)
    for a, b in CE_ARROWS:
        body = body.replace(a, b)
    return body


def fix_mitex_compat(s):
    """修补 mitex 不支持/有 bug 的 LaTeX 命令。"""
    s = re.sub(r"\\xrightarrow\[([^\]]*)\]\{([^{}]*)\}", r"\\overset{\2}{\\rightarrow}", s)
    s = re.sub(r"\\xleftarrow\[([^\]]*)\]\{([^{}]*)\}", r"\\overset{\2}{\\leftarrow}", s)
    s = re.sub(r"\\xrightarrow\{([^{}]*)\}", r"\\overset{\1}{\\rightarrow}", s)
    s = re.sub(r"\\xleftarrow\{([^{}]*)\}", r"\\overset{\1}{\\leftarrow}", s)
    return s


def ce_to_latex(s):
    """把 mhchem 化学式宏 \\ce{...} 展开为普通 LaTeX(花括号配对扫描, 支持任意嵌套)。"""
    out, i, n = [], 0, len(s)
    while i < n:
        if s.startswith("\\ce{", i):
            j, depth = i + 4, 1
            while j < n:
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(ce_body(s[i + 4:j]))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return fix_mitex_compat("".join(out))


def latex_var(s):
    """LaTeX 片段 -> 可放入 Typst 字符串 mi(\"...\") 的形式。"""
    s = ce_to_latex(s)
    return s.replace("\\", "\\\\").replace('"', '\\"')


def latex_to_typst(s):
    """把 OCR/AI 输出的公式片段转成 Typst 数学语法, 化学式用正体。"""
    s = re.sub(r"\\ce\{([^{}]*)\}", r"\1", s)   # mhchem \ce{H2O} -> H2O
    s = re.sub(r"\\pu\{([^{}]*)\}", r"\1", s)
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


def ai_proofread(img_rgb, draft):
    """对照原图校对识别结果: 修正错别字/公式/化学式/括号/LaTeX 语法。"""
    _t0 = time.time()
    cfg = ai_config()
    hh, ww = img_rgb.shape[:2]
    maxpx = cfg.get("max_px", 1600) or 1600
    if max(hh, ww) > maxpx:
        sc = maxpx / max(hh, ww)
        img_rgb = cv2.resize(img_rgb, (int(ww * sc), int(hh * sc)),
                             interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR),
                          [cv2.IMWRITE_JPEG_QUALITY, 90])
    b64 = base64.b64encode(buf).decode()
    prompt = ("下面是对这张试卷图片的识别结果。请对照图片逐项核查并修正错误：\n"
              "1) 文字错别字、漏字、多余字；\n"
              "2) 公式/化学式的下标、电荷、系数配平、括号是否配对；\n"
              "3) LaTeX 语法（花括号是否配对、命令拼写）；\n"
              "4) 选项是否缺失、题干与选项是否混杂；\n"
              "5) 与图片不符之处。\n"
              "保持原格式：【题干】标记、选项每行一个(A．B．C．D．)、"
              "公式用 $...$、图形用 [图@x,y,w,h]、不要输出解释。\n"
              "只输出修正后的完整结果。\n\n识别结果：\n" + draft)
    body = {"model": cfg["model"] or "glm-4v-flash",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
            "temperature": 0}
    if "deepseek" in (cfg["base_url"] or "").lower():
        body["reasoning_effort"] = "none"
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["key"]})
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            d = json.loads(r.read())
        fixed = (d["choices"][0]["message"]["content"] or "").strip()
        log_ai("校对", cfg["model"], bool(fixed), (time.time() - _t0) * 1000,
               f"{len(draft)} -> {len(fixed)} 字")
        return fixed
    except Exception as e:
        log_ai("校对", cfg["model"], False, (time.time() - _t0) * 1000, str(e))
        return ""


def call_ai_vision(img_rgb):
    """调用视觉大模型识别图片, 返回 Markdown 文本(公式为 LaTeX)。"""
    _t0 = time.time()
    hh, ww = img_rgb.shape[:2]
    maxpx = ai_config().get("max_px", 1600) or 1600     # 识别清晰度(最长边像素)
    if max(hh, ww) > maxpx:
        s = maxpx / max(hh, ww)
        img_rgb = cv2.resize(img_rgb, (int(ww * s), int(hh * s)),
                             interpolation=cv2.INTER_AREA)
    _, buf = cv2.imencode(".jpg", cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR),
                          [cv2.IMWRITE_JPEG_QUALITY, 90])
    b64 = base64.b64encode(buf).decode()
    prompt = ('你是试卷题目识别工具。识别图片中的题目，注意：\n'
              '1. 忽略图片中所有手写笔迹、批注、涂改痕迹，只识别印刷体题目内容；\n'
              '2. 不要输出题号（如 1. 2. 3.、①②、第1题 等），直接从题目内容开始；\n'
              '3. 忽略与题目无关的内容：页眉页脚、页码、水印、练习册名称、出题人/审题人署名；'
              '但题目自带的提示语、注意事项、说明文字要保留；\n'
              '4. 第一行输出【题干】，后跟题干文字；\n'
              '5. 只输出题干与选项本身。绝对不要输出答案、解析、点评、解题过程、方法总结等任何附加内容'
              '（图片中即使有也全部忽略）：题干括号中的答案如（A）（D C）、选项后的对勾√×、答案行、'
              '答案解析段落一律忽略，无论括号是否闭合；\n'
              '6. 选择题/多选题的选项逐行输出，每行一个：A．选项内容 / B．选项内容 / C．选项内容 / '
              'D．选项内容（用全角句点．）；\n'
              '7. 所有数学公式/化学式用 $...$ LaTeX 语法；\n'
              '8. 题目内嵌图形/示意图用 [图@x,y,w,h] 标记，框必须精确贴合图形本身边界（含图形外框线），'
              '不要包含图形周围的文字、题干或大块空白；坐标是图形相对整图0-1000 比例，'
              '如 [图@620,280,240,180]，没有图形不要加。\n')
    if ai_config().get("font_marks", True):
        prompt += ('9. 标注原题的视觉强调，只用两种标记(不要用其他符号)：\n'
                   '   - 比正文更粗更黑的文字(黑体、加粗的宋体等) -> **文字**\n'
                   '   - 楷体或斜体的文字 -> *文字*\n'
                   '   普通宋体正文不加任何标记；只标确实能分辨的印刷体，'
                   '不确定或手写内容不要标记，宁可漏标不要标错；\n')
    prompt += ('输出前请核对：下标与电荷是否标全、括号是否配对、选项是否齐全，发现错误直接改正。\n'
               '只输出识别结果，不要解释。')
    body = {
        "model": ai_config()["model"] or "glm-4v-flash",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
        "temperature": 0.1,
    }
    if "deepseek" in (ai_config()["base_url"] or "").lower():
        body["reasoning_effort"] = "none"   # 识别是感知任务, 关思考可提速约 40%
    req = urllib.request.Request(
        ai_config()["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + ai_config()["key"]})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
        text = d["choices"][0]["message"]["content"]
        usage = d.get("usage") or {}
        log_ai("识别", ai_config()["model"], True, (time.time() - _t0) * 1000,
               f"{len(text)} 字" + (f" · {usage.get('total_tokens')} tokens" if usage.get("total_tokens") else ""))
        out = clean_ai_text(text)
        if ai_config().get("proofread", False):       # 可选: 额外一轮对照图片校对
            fixed = ai_proofread(img_rgb, out)
            if fixed and len(fixed) >= 20:
                out = clean_ai_text(fixed)
        return out
    except Exception as e:
        log_ai("识别", ai_config()["model"], False, (time.time() - _t0) * 1000, str(e))
        raise


def clean_ai_text(text):
    """AI 识别后处理: 去题干行首题号、删答案与解析、拆分一行多选项、清理残留标记。"""
    # 0. AI 偶尔会附带答案/解析段落 -> 从标记处起到结尾整段丢弃
    text = re.split(r"^\s*【\s*(?:答案|解析|解答|点评|分析|说明|方法总结)\s*】",
                    text, maxsplit=1, flags=re.M)[0].rstrip()
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


def typ_esc(t):
    """转义 Typst 文本中的特殊字符(用于标题/注意事项等自由文本)。"""
    for ch, e2 in (("#", "\\#"), ("$", "\\$"), ("{", "\\{"), ("}", "\\}"),
                   ("[", "\\["), ("]", "\\]"), ("_", "\\_")):
        t = t.replace(ch, e2)
    return t


def render_simple(txt, it, lines):
    """附录页简化渲染: $公式$ -> mitex, [图N] -> 图片, markdown 字体标记。"""
    txt = (txt or "").strip()
    if not txt:
        return
    figmap = {int(f.get("n", 0)): f for f in (it.get("figures") or [])}
    align_mode = None

    def esc1(t):
        for ch, e2 in (("#", "\\#"), ("$", "\\$"), ("{", "\\{"), ("}", "\\}"),
                       ("[", "\\["), ("]", "\\]"), ("_", "\\_")):
            t = t.replace(ch, e2)
        return t

    def md(t):
        out = ""
        for seg in re.split(r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|\*[^*]+?\*|==.+?==|\+\+.+?\+\+)", t):
            if not seg:
                continue
            if seg.startswith("***") and seg.endswith("***") and len(seg) > 6:
                out += "#kb[" + esc1(seg[3:-3]) + "]"
            elif seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
                out += "*" + esc1(seg[2:-2]) + "*"
            elif seg.startswith("==") and seg.endswith("==") and len(seg) > 4:
                out += "#sb[" + esc1(seg[2:-2]) + "]"
            elif seg.startswith("++") and seg.endswith("++") and len(seg) > 4:
                out += "#hbk[" + esc1(seg[2:-2]) + "]"
            elif seg.startswith("*") and seg.endswith("*") and len(seg) > 2:
                out += "_" + esc1(seg[1:-1]) + "_"
            else:
                out += esc1(seg)
        return out

    for ln in txt.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("::: "):
            align_mode = ln[4:].strip() or None
            continue
        if ln == ":::":
            align_mode = None
            continue
        # 图块 -> 占位
        figs = []
        def _fig(m):
            n2 = int(m.group(1))
            w = (m.group(2) or "40%")
            if not w.endswith("%"):
                w += "%"
            f0 = figmap.get(n2)
            fp = ROOT / str(f0.get("file", "")) if f0 else None
            if fp is None or not fp.exists():
                cand = (ROOT / f"{it['image'][:-4]}_fig{n2}.jpg") if it.get("image") else None
                fp = cand if (cand and cand.exists()) else None
            if fp:
                figs.append((str(fp), w))
                return f"@@F{len(figs) - 1}@@"
            return f'#text(fill: rgb("#cc0000"))[图{n2}缺失]'
        ln = re.sub(r"\[图(\d+)(?:\|([\d.]+%?))?\]", _fig, ln)
        ln = ln.replace("（图）", "").replace("(图)", "")
        out = ""
        for seg in re.split(r"(\$[^$]+\$)", ln):
            if seg.startswith("$") and seg.endswith("$") and len(seg) > 2:
                out += '#mi("' + latex_var(seg[1:-1]) + '")'
            else:
                out += md(seg)
        for i, (fp, w) in enumerate(figs):
            out = out.replace(f"@@F{i}@@", f'#image("{fp}", width: {w})')
        if out.strip():
            lines.append(f"#align({align_mode})[{out}]" if align_mode else out + " \\")


@app.get("/api/paper/pdf")
def paper_pdf(ids: str = "", attach: str = "", index: str = "", header: str = "",
              title: str = "", subject_line: str = "", notice: str = "",
              body_size: str = "", leading: str = ""):
    """用 Typst 渲染试卷 PDF。attach: 附答案解析页; index: 1 附编号对照页; header: 页眉文字。
    body_size/leading: 正文字号(pt)与行距(em), 语文卷常用 12pt / 1.5em。"""
    try:
        size_pt = min(16.0, max(9.0, float(body_size))) if body_size else 10.5
    except (TypeError, ValueError):
        size_pt = 10.5
    try:
        lead_em = min(2.5, max(0.85, float(leading))) if leading else 0.95
    except (TypeError, ValueError):
        lead_em = 0.95
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
        '#set page(',
        '  width: 185mm, height: 260mm,',
        '  margin: (top: 2cm, bottom: 2cm, left: 2.2cm, right: 2.2cm),',
        '  footer: context {',
        '    let total = counter(page).final().first()',
        '    align(center)[#text(size: 9pt)[第 #counter(page).display() 页　共 #total 页]]',
        '  },',
        ('  header: align(center)[#text(size: 9pt, fill: rgb("#666666"))[' + header.replace("[", chr(92) + "[").replace("]", chr(92) + "]") + ']\n#v(-0.25em)#line(length: 100%, stroke: 0.4pt + rgb("#cccccc"))],'
         if header.strip() else '  header: none,'),
        ')',
        '#import "@preview/mitex:0.2.4": mi',                        # LaTeX 公式支持
        '#let F_LATIN = "Times New Roman"',                          # 西文/数字: 保留真粗体与真斜体
        '#let F_SONG = (F_LATIN, "SimSun")',                         # 正文: 中文宋体
        '#let F_HEI = (F_LATIN, "SimHei")',                          # 强调/标题: 中文黑体
        '#let F_KAI = (F_LATIN, "KaiTi")',                           # 斜体: 中文楷体
        '#let kb(body) = text(font: F_KAI, stroke: 0.03em, body)',    # 楷体加粗(中文字体无 Bold 变体, 描边合成)
        '#let sb(body) = text(font: F_SONG, stroke: 0.03em, body)',   # 宋体加粗
        '#let hbk(body) = text(font: F_HEI, stroke: 0.03em, body)',   # 黑体加粗
        '#let __unused_hb = 0',    # 黑体加粗(同上)                        # LaTeX 公式支持
        f'#set text(font: F_SONG, size: {size_pt}pt, lang: "zh")',    # 英文 Times 新罗马 / 中文宋体
        f'#set par(justify: true, leading: {lead_em}em, spacing: {lead_em}em)',  # 行距=段距=块距
        '#let BODY = ' + f'{size_pt}pt',
        '#show heading: set text(font: F_HEI, size: 12pt)',           # 大题标题: 小四黑体(西文 Times-Bold)
        '#show heading: set par(leading: 0.7em)',
        '#show heading: set block(spacing: 0.95em)',
        '#show emph: set text(font: F_KAI)',                          # *斜体*: 中文楷体 / 西文 Times-Italic(不写 style, 否则西文退化为正体)
        '#show strong: set text(font: F_HEI, weight: "bold")',        # **粗体**: 中文黑体 / 西文 Times-Bold
        f'#set block(spacing: {lead_em}em)',
        # ---- 卷头(可自定义): 三号标题 / 二号黑体科目 / 五号说明 ----
        f'#align(center)[#text(size: 16pt, font: F_HEI)[{typ_esc(title.strip() or "错题重组试卷")}]]',
        f'#align(center)[#text(size: 22pt, font: F_HEI, weight: "bold")[{typ_esc(subject_line.strip() or (subjects[0] if len(subjects) == 1 else " ".join(subjects)))}]]',
        '#v(0.45cm)',
    ]
    # 注意事项(可自定义, 首行黑体小四, 条目五号)
    notice_text = (notice or "").strip() or "注意事项：\n1．本试卷由错题收集工具生成，请在答题纸上作答；\n2．解答应写出文字说明、证明过程或演算步骤。"
    for i, ln in enumerate(notice_text.split("\n")):
        if not ln.strip():
            continue
        if i == 0:
            lines.append(f'#text(font: F_HEI, size: 12pt)[{typ_esc(ln)}]')
        else:
            lines.append(f'#text(size: BODY)[{typ_esc(ln)}]')
    lines.append('#v(0.45cm)')
    n, prev_g = 0, None
    ordered = {}                      # 题号 -> [该题的块(含续块)]
    for gname, gitems in groups.items():
        if gname and gname not in ("未命名", "未分类", "无"):
            lines.append(f"= {gname}")          # 大题标题: 黑体(空/未命名不显示)
        for it in gitems:
            g = it.get("group") or ""
            if g and g == prev_g:
                lines.append('#text(font: F_KAI, size: BODY)[(续)] \\')
            else:
                n += 1
                lines.append(f"{n}．")               # 题号顶格, 题干接同一行
            ordered.setdefault(n, []).append(it)
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
                para_mode = None     # ::: poem / ::: quote 整段样式
                para_buf = []

                def flush_para():
                    """输出 ::: poem(诗歌: 居中+大行距) / ::: quote(材料: 缩进+中行距) 整段。"""
                    if not para_buf:
                        return
                    flush_opts()
                    body = " \\\n".join(para_buf)
                    if para_mode == "poem":
                        lines.append("#align(center)[#set par(justify: false, "
                                     "leading: 1.7em, spacing: 1.7em)\n" + body + "\n]")
                        lines.append("#v(0.2cm)")
                    else:                    # quote: 阅读材料/引文
                        lines.append("#block(inset: (left: 1.4em, right: 1.4em))"
                                     "[#set par(leading: 1.35em, spacing: 1.35em)\n"
                                     + body + "\n]")
                    para_buf.clear()
                    first_ln = False
                align_mode = None    # ::: center 段落对齐
                figmap = {int(f.get("n", 0)): f for f in (it.get("figures") or [])}

                def esc_one(s):
                    for ch, e2 in (("#", "\\#"), ("$", "\\$"), ("{", "\\{"), ("}", "\\}"),
                                   ("[", "\\["), ("]", "\\]"), ("_", "\\_")):
                        s = s.replace(ch, e2)
                    return s

                def md_inline(s):
                    """字体标记(中文均无 Bold 变体, 加粗用描边合成):
                    *楷体*  **黑体**  ***楷体加粗***  ==宋体加粗==  ++黑体加粗++"""
                    out = ""
                    for seg in re.split(r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|\*[^*]+?\*|==.+?==|\+\+.+?\+\+)", s):
                        if not seg:
                            continue
                        if seg.startswith("***") and seg.endswith("***") and len(seg) > 6:
                            out += "#kb[" + esc_one(seg[3:-3]) + "]"           # 楷体加粗
                        elif seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
                            out += "*" + esc_one(seg[2:-2]) + "*"              # 黑体
                        elif seg.startswith("==") and seg.endswith("==") and len(seg) > 4:
                            out += "#sb[" + esc_one(seg[2:-2]) + "]"           # 宋体加粗
                        elif seg.startswith("++") and seg.endswith("++") and len(seg) > 4:
                            out += "#hbk[" + esc_one(seg[2:-2]) + "]"          # 黑体加粗
                        elif seg.startswith("*") and seg.endswith("*") and len(seg) > 2:
                            out += "_" + esc_one(seg[1:-1]) + "_"              # 楷体
                        else:
                            out += esc_one(seg)
                    return out

                def esc_ln(s):
                    """公式 $..$ 转 Typst, 其余文本: markdown 字体标记 + 转义"""
                    out = ""
                    for seg in re.split(r"(\$[^$]+\$)", s):
                        if seg.startswith("$") and seg.endswith("$") and len(seg) > 2:
                            out += '#mi("' + latex_var(seg[1:-1]) + '")'   # 交给 mitex 渲染
                        else:
                            out += md_inline(seg)
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

                def fig_file(n2):
                    """图块文件: figures 优先, 否则 xxx_figN.jpg"""
                    f0 = figmap.get(n2)
                    if f0:
                        p0 = ROOT / str(f0.get("file", ""))
                        if p0.exists():
                            return p0
                    if not it.get("image"):
                        return None
                    cand = ROOT / f"{it['image'][:-4]}_fig{n2}.jpg"
                    return cand if cand.exists() else None

                def crop_old(fx, fy, fw, fh, idx):
                    """旧格式 [图@x,y,w,h]: 现场裁剪"""
                    full = np.array(open_photo(ROOT / it["image"]))
                    FH, FW = full.shape[:2]
                    x0 = max(0, int(fx / 1000 * FW)); y0 = max(0, int(fy / 1000 * FH))
                    w0 = min(FW - x0, max(20, int(fw / 1000 * FW)))
                    h0 = min(FH - y0, max(20, int(fh / 1000 * FH)))
                    fig = trim_blank(full[y0:y0 + h0, x0:x0 + w0])
                    if fig.shape[0] < 10 or fig.shape[1] < 10:
                        fig = full[y0:y0 + h0, x0:x0 + w0]
                    fp = TMP_DIR / f"fig_{it['id']}_{n}_{idx}.jpg"
                    imwrite_u(fp, cv2.cvtColor(fig, cv2.COLOR_RGB2BGR))
                    return fp

                def scan_figs(s):
                    """提取行内图引用 -> (占位符文本, [{file,w,align}])。
                    支持 [图N] / [图N|60%|left], 兼容旧 [图@x,y,w,h] 与 [图]。"""
                    infos = []

                    def rep_new(m):
                        n2 = int(m.group(1))
                        w = m.group(2) or "40%"
                        if not w.endswith("%"):
                            w += "%"
                        al = m.group(3) or "center"
                        fp = fig_file(n2)
                        if fp:
                            infos.append({"file": str(fp), "w": w, "align": al})
                            return f"@@F{len(infos) - 1}@@"
                        return f'#text(fill: rgb("#cc0000"))[图{n2}未裁好]'

                    s = re.sub(r"\[图(\d+)(?:\|([\d.]+%?))?(?:\|(left|center|right))?\]",
                               rep_new, s)

                    def rep_old(m):
                        fp = crop_old(*(int(v) for v in m.groups()), len(infos))
                        infos.append({"file": str(fp), "w": "40%", "align": "center"})
                        return f"@@F{len(infos) - 1}@@"

                    s = re.sub(r"\[图@(\d+),(\d+),(\d+),(\d+)\]", rep_old, s)
                    if "[图]" in s and it.get("image"):
                        infos.append({"file": str(ROOT / it["image"]),
                                      "w": "45%", "align": "center"})
                        s = s.replace("[图]", f"@@F{len(infos) - 1}@@")
                    s = s.replace("（图）", "").replace("(图)", "")
                    return s, infos

                for ln in txt.split("\n"):
                    ln = ln.strip()
                    if not ln:
                        continue
                    if ln.startswith("::: "):            # ::: center / poem / quote / right
                        mode = ln[4:].strip() or None
                        if mode in ("poem", "quote", "material"):
                            flush_para()
                            para_mode = "poem" if mode == "poem" else "quote"
                        else:
                            align_mode = mode
                        continue
                    if ln == ":::":
                        if para_mode:
                            flush_para()
                            para_mode = None
                        align_mode = None
                        continue
                    om = re.match(r"^([A-D])[．.、)）]\s*(.*)$", ln)
                    if om:                                # 选项行: 收集后 grid 对齐
                        if len(opt_buf) >= 4:
                            flush_opts()
                        opt_buf.append(ln)
                        continue
                    ln2, infos = scan_figs(ln)
                    if para_mode:                        # 整段样式: 收集纯文本行(图仍走通用分支)
                        if not infos:
                            para_buf.append(esc_ln(ln2))
                            continue
                    rest = re.sub(r"@@F\d+@@", "", ln2).strip()
                    if infos and not rest:
                        # 整行只有图: 多图并排 / 单图对齐
                        if len(infos) > 1:
                            cells = "".join(f'[#image("{f0["file"]}", width: {f0["w"]})]'
                                            for f0 in infos)
                            lines.append(f"#grid(columns: {len(infos)}, "
                                         f"column-gutter: 0.6em, row-gutter: 0.5em){cells}")
                        else:
                            f0 = infos[0]
                            lines.append(f'#align({f0["align"]})'
                                         f'[#image("{f0["file"]}", width: {f0["w"]})]')
                        placed = True
                        first_ln = False
                        continue
                    out = esc_ln(ln2)
                    for i, f0 in enumerate(infos):       # 混排: 行内插图
                        out = out.replace(f"@@F{i}@@",
                                          f'#image("{f0["file"]}", width: {f0["w"]})')
                    if out.strip():
                        if align_mode:
                            flush_opts()
                            lines.append(f"#align({align_mode})[{out}]")
                            first_ln = False
                        else:
                            flush_opts()
                            lines.append(out + ("" if first_ln else " \\"))
                            first_ln = False
                flush_para()
                flush_opts()
            else:
                # 未识别出文字: 保留原图(手动添加的纯文字题无图, 跳过)
                if it.get("image"):
                    lines.append(f'#image("{ROOT / it["image"]}", width: 50%)')
            lines.append("#v(0.45cm)")          # 题目之间的间距

    # ---- 文末: 答案 / 解析 附录页 ----
    if attach in ("answer", "analysis", "both"):
        lines.append("#pagebreak()")
        title = {"answer": "参考答案", "analysis": "答案与解析", "both": "参考答案与解析"}[attach]
        lines.append(f'#align(center)[#text(size: 14pt, font: F_HEI)[{title}]]')
        lines.append("#v(0.4cm)")
        has_content = [False]
        for num in sorted(ordered):
            blocks = ordered[num]
            parts = []
            if attach in ("answer", "both"):
                for b in blocks:
                    if (b.get("answer") or "").strip():
                        parts.append(("答案", b["answer"], b))
            if attach in ("analysis", "both"):
                for b in blocks:
                    if (b.get("analysis") or "").strip():
                        parts.append(("解析", b["analysis"], b))
            if not parts:
                continue
            has_content[0] = True
            lines.append(f"**{num}．**")
            for label, content, b in parts:
                lines.append(f'#text(font: F_HEI)[{label}：]')
                render_simple(content, b, lines)
            lines.append("#v(0.3cm)")
        if not has_content[0]:
            lines.append('#text(fill: rgb("#888888"))[本卷题目尚未填写答案或解析；'
                         '可在错题库点题号补充后重新生成。]')
    # ---- 文末: 题号与编号对照页 ----
    if index == "1" or index == "true":
        lines.append("#pagebreak()")
        lines.append('#align(center)[#text(size: 14pt, font: F_HEI)[题目编号对照]]')
        lines.append("#v(0.4cm)")
        for num in sorted(ordered):
            b = ordered[num][0]
            code = b.get("code") or b.get("id") or ""
            chap = b.get("chapter") or "未分类"
            lines.append(f"{num}． {code}　（{chap}）\\")

    typ_path = TMP_DIR / f"paper_{int(time.time() * 1000)}.typ"
    pdf_path = typ_path.with_suffix(".pdf")
    typ_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        typst.compile(typ_path, output=pdf_path,
                      font_paths=[str(FONTS_DIR)], root="/",
                      package_path=str(TYPST_PKG_DIR))
    except Exception as e:
        return JSONResponse({"ok": False,
                            "msg": "PDF生成失败: " + str(e)[:200]}, status_code=500)
    return {"ok": True, "url": f"/files/.tmp/{pdf_path.name}", "count": n}


# ---------- AI 视觉识别 (公式 -> LaTeX, 走大模型 API 不吃本地内存) ----------

AI_CONFIG_FILE = ROOT / ".ai_config.json"


# ---------- 运行日志(AI 调用) ----------
AI_LOG = collections.deque(maxlen=300)


def log_ai(kind, model, ok, ms, msg=""):
    AI_LOG.append({
        "time": time.strftime("%m-%d %H:%M:%S"),
        "kind": kind, "model": model or "-", "ok": bool(ok),
        "ms": int(ms), "msg": str(msg)[:200],
    })


@app.get("/api/logs")
def get_logs(limit: int = 80):
    logs = list(AI_LOG)[-max(1, min(300, limit)):]
    return {"ok": True, "logs": logs[::-1]}


@app.delete("/api/logs")
def clear_logs():
    AI_LOG.clear()
    return {"ok": True}


def ai_config():
    cfg = {"base_url": "", "key": "", "model": "", "proofread": False, "max_px": 1600,
           "font_marks": True}     # font_marks: AI 是否标记原题的加粗/楷体字体差异
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


AI_PRESETS = {
    "deepseek": {"label": "DeepSeek 多模态（deepseek-flash）",
                 "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash"},
}


@app.get("/api/ai/config")
def get_ai_config():
    cfg = ai_config()
    key = cfg["key"] or ""
    return {"ok": True, "base_url": cfg["base_url"], "model": cfg["model"],
            "key_set": bool(key),
            "key_hint": (key[:4] + "****" + key[-4:]) if len(key) > 10 else ("****" if key else ""),
            "max_px": cfg.get("max_px", 1600), "proofread": bool(cfg.get("proofread")),
            "font_marks": bool(cfg.get("font_marks", True)),
            "presets": AI_PRESETS}


@app.post("/api/ai/config")
def set_ai_config(payload: dict):
    cfg = ai_config()
    for k in ("base_url", "key", "model"):
        if k in payload and str(payload[k]).strip():
            cfg[k] = str(payload[k]).strip()
    if "proofread" in payload:
        cfg["proofread"] = bool(payload["proofread"])
    if "font_marks" in payload:
        cfg["font_marks"] = bool(payload["font_marks"])
    if "max_px" in payload:
        try:
            cfg["max_px"] = max(800, min(3000, int(payload["max_px"])))
        except (TypeError, ValueError):
            pass
    AI_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    try:
        os.chmod(AI_CONFIG_FILE, 0o600)
    except Exception:
        pass
    return {"ok": True, "key_set": bool(cfg["key"]), "model": cfg["model"],
            "base_url": cfg["base_url"]}


@app.post("/api/ai/test")
def test_ai_config(payload: dict = None):
    """测试当前配置能否连通(发一个最小文本请求)。"""
    cfg = ai_config()
    if not cfg["key"] or not cfg["base_url"]:
        return JSONResponse({"ok": False, "msg": "尚未配置 API 地址或 Key"}, status_code=400)
    body = {"model": cfg["model"] or "glm-4v-flash",
            "messages": [{"role": "user", "content": "回复 OK"}],
            "max_tokens": 8}
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["key"]})
    try:
        _t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        reply = d["choices"][0]["message"]["content"][:30]
        log_ai("测试连接", cfg["model"], True, (time.time() - _t0) * 1000, reply or "-")
        return {"ok": True, "msg": f"连接成功（模型回复：{reply}）"}
    except Exception as e:
        log_ai("测试连接", cfg["model"], False, 0, str(e))
        return JSONResponse({"ok": False, "msg": "连接失败: " + str(e)[:160]},
                            status_code=502)


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


@app.post("/api/item/{item_id}/ai")
def item_reai(item_id: str, payload: dict = None):
    """对已保存的题目重新 AI 识别(force=True 覆盖识别内容) + 重新裁剪题内图形。"""
    db = load_db()
    it = next((x for x in db["items"] if x["id"] == item_id), None)
    if not it:
        return JSONResponse({"ok": False, "msg": "题目不存在"}, status_code=404)
    if not it.get("image"):
        return JSONResponse({"ok": False, "msg": "该题没有图片（手动添加），无法识别"},
                            status_code=400)
    if not ai_config()["key"]:
        return JSONResponse({"ok": False, "msg": "未配置 AI Key，请到「设置」页配置"},
                            status_code=400)
    if not (ROOT / it["image"]).exists():
        return JSONResponse({"ok": False, "msg": "图片文件不存在"}, status_code=404)
    force = bool((payload or {}).get("force", True))
    upd = ai_recognize_one(it, force=force)
    if upd is None:
        return JSONResponse({"ok": False, "msg": "AI 未返回内容（或已有内容且未强制覆盖）"},
                            status_code=502)
    log_ai("重新识别", f"{upd.get('code')} 图片={it['image']}")
    return {"ok": True, "item": upd}


@app.get("/api/items")
def list_items():
    db = load_db()
    return {"items": list(reversed(db["items"]))}


@app.post("/api/item")
async def create_item(subject: str = Form("未分类"), chapter: str = Form(""),
                      note: str = Form(""), answer: str = Form(""),
                      analysis: str = Form(""), keywords: str = Form(""),
                      title: str = Form(""), star: int = Form(0),
                      file: UploadFile = File(None)):
    """手动新建题目(可不上传图片; 上传的图片作为题目图)。"""
    db = load_db()
    subject = (subject or "未分类").strip()
    item_id = f"q{int(time.time() * 1000)}"
    img_rel = ""
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > 100:
            try:
                im = Image.open(io.BytesIO(data)).convert("RGB")
                if max(im.size) > 2400:
                    im.thumbnail((2400, 2400))
                sd = subj_dirname(subject)
                d = ITEMS_DIR / sd
                d.mkdir(parents=True, exist_ok=True)
                name = f"{item_id}.jpg"
                im.save(d / name, "JPEG", quality=92)
                img_rel = f"items/{sd}/{name}"
            except Exception as e:
                return JSONResponse({"ok": False, "msg": "图片解析失败: " + str(e)[:80]},
                                    status_code=400)
    item = {
        "id": item_id,
        "code": next_code(db, subject),
        "image": img_rel,
        "subject": subject,
        "chapter": chapter.strip(),
        "title": title.strip(),
        "reason": "",
        "note": note,
        "answer": answer,
        "analysis": analysis,
        "keywords": keywords,
        "star": max(0, min(5, int(star or 0))),
        "figures": [],
        "group": "",
        "source_page": "",
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }
    db["items"].append(item)
    save_db(db)
    return {"ok": True, "item": item}


@app.put("/api/item/{item_id}")
def update_item(item_id: str, payload: dict):
    """更新错题信息(标题/章节/错因/备注/科目/答案/解析/关键字/星级/图块)。"""
    db = load_db()
    for it in db["items"]:
        if it["id"] == item_id:
            for k in ("title", "chapter", "reason", "note", "subject",
                      "answer", "analysis", "keywords"):
                if k in payload:
                    it[k] = str(payload[k])[:4000]
            if "star" in payload:
                try:
                    it["star"] = max(0, min(5, int(payload["star"] or 0)))
                except (TypeError, ValueError):
                    it["star"] = 0
            if "figures" in payload:
                it["figures"] = payload["figures"]
            save_db(db)
            return {"ok": True, "item": it}
    return JSONResponse({"ok": False, "msg": "不存在"}, status_code=404)


@app.post("/api/item/{item_id}/upload")
async def upload_figure(item_id: str, file: UploadFile = File(...)):
    """上传图片作为题目的图片附件, 返回编号 n。"""
    db = load_db()
    for it in db["items"]:
        if it["id"] == item_id:
            src = ROOT / it["image"]
            if not src.exists():
                return JSONResponse({"ok": False, "msg": "原图不存在"}, status_code=404)
            data = await file.read()
            if len(data) < 100:
                return JSONResponse({"ok": False, "msg": "文件为空"}, status_code=400)
            figs = it.get("figures") or []
            n = max((f.get("n", 0) for f in figs), default=0) + 1
            suffix = Path(file.filename or "img.jpg").suffix.lower()
            if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
                suffix = ".jpg"
            fname = f"{item_id}_fig{n}{suffix}"
            fdir = src.parent
            (fdir / fname).write_bytes(data)
            figs.append({"n": n, "file": str((fdir / fname).relative_to(ROOT)),
                         "upload": True})
            it["figures"] = figs
            save_db(db)
            return {"ok": True, "n": n, "figures": figs}
    return JSONResponse({"ok": False, "msg": "不存在"}, status_code=404)


@app.delete("/api/item/{item_id}/figure/{n}")
def delete_figure(item_id: str, n: int):
    """删除题目的第 n 个图片附件（同时删文件）。"""
    db = load_db()
    for it in db["items"]:
        if it["id"] == item_id:
            figs, keep = it.get("figures") or [], []
            removed = None
            for f in figs:
                if int(f.get("n", 0)) == n:
                    removed = f
                else:
                    keep.append(f)
            if removed:
                p = ROOT / str(removed.get("file", ""))
                if p.exists():
                    p.unlink(missing_ok=True)
            it["figures"] = keep
            save_db(db)
            return {"ok": True, "figures": keep}
    return JSONResponse({"ok": False, "msg": "不存在"}, status_code=404)


@app.post("/api/item/{item_id}/figure")
def add_figure(item_id: str, payload: dict):
    """给已保存的错题添加图块: {x,y,w,h}(相对题图 0-1000 比例) -> 裁图块并返回编号。"""
    db = load_db()
    for it in db["items"]:
        if it["id"] == item_id:
            src = ROOT / it["image"]
            if not src.exists():
                return JSONResponse({"ok": False, "msg": "原图不存在"}, status_code=404)
            img = open_photo(src)
            W, H = img.size
            fig = payload.get("figure") or payload
            fx, fy, fw, fh = (int(fig.get(k, 0)) for k in ("x", "y", "w", "h"))
            x0 = max(0, int(fx / 1000 * W)); y0 = max(0, int(fy / 1000 * H))
            w0 = min(W - x0, max(20, int(fw / 1000 * W)))
            h0 = min(H - y0, max(20, int(fh / 1000 * H)))
            if w0 < 20 or h0 < 20:
                return JSONResponse({"ok": False, "msg": "框选区域太小"}, status_code=400)
            figs = it.get("figures") or []
            n = max((f.get("n", 0) for f in figs), default=0) + 1
            fname = f"{item_id}_fig{n}.jpg"
            fdir = src.parent
            trim_margins(img.crop((x0, y0, x0 + w0, y0 + h0))).save(
                fdir / fname, "JPEG", quality=95)
            figs.append({"n": n, "file": str((fdir / fname).relative_to(ROOT)),
                         "x": fx, "y": fy, "w": fw, "h": fh})
            it["figures"] = figs
            save_db(db)
            return {"ok": True, "n": n, "figures": figs}
    return JSONResponse({"ok": False, "msg": "不存在"}, status_code=404)


@app.delete("/api/item/{item_id}")
def delete_item(item_id: str):
    db = load_db()
    for it in db["items"]:
        if it["id"] == item_id:
            f = ROOT / it["image"]
            to_trash(f)                        # 移入回收站(可恢复)
            for fg in it.get("figures") or []:  # 图块一并回收
                to_trash(ROOT / str(fg.get("file", "")))
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


app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/{route:path}", response_class=HTMLResponse)
def spa_route(route: str):
    """前端路由回退: /library、/library/数学、/paper、/item/MA0003 等都返回同一个页面。"""
    head = route.split("/")[0]
    if head in ("api", "files", "static", "docs", "openapi.json", "redoc"):
        return JSONResponse({"ok": False, "msg": "not found"}, status_code=404)
    return _index_html()


if __name__ == "__main__":
    import uvicorn
    print("\n错题收集工具已启动: http://localhost:8091\n")
    uvicorn.run(app, host="0.0.0.0", port=8091, log_level="warning")
