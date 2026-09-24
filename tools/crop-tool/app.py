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
import random
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
    return {"ok": True, "subjects": new, "dirs": dirs, "removed": removed, "moved": moved,
            "prefixes": load_prefix()}


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


def _kw_list(s):
    return [x for x in re.split(r"[,，、;；\s]+", s or "") if x]


def _tpl_all():
    d = dict(DEFAULT_TPL)
    if TPL_PATH.exists():
        try:
            d.update(json.loads(TPL_PATH.read_text("utf-8")))
        except Exception:
            pass
    return d


@app.post("/api/rename")
def rename_global(payload: dict = None):
    """一键全局改名: kind = subject(科目) | chapter(大题归类/标签) | keyword(关键字) | prefix(编号前缀)。
    改名后所有题目、大题模板、编号引用一次性全部更新(写库前会自动备份 library.json)。"""
    p = payload or {}
    kind = (p.get("kind") or "").strip()
    old = str(p.get("old") or "").strip()
    new = str(p.get("new") or "").strip()
    subj = str(p.get("subject") or "").strip()
    if kind not in ("subject", "chapter", "keyword", "prefix"):
        return JSONResponse({"ok": False, "msg": "未知的改名类型"}, status_code=400)
    if not new:
        return JSONResponse({"ok": False, "msg": "请输入新名称"}, status_code=400)
    if kind == "prefix":                     # 改编号前缀 -> 该科目所有题重新编号
        if not subj:
            return JSONResponse({"ok": False, "msg": "请选择科目"}, status_code=400)
        newp = re.sub(r"[^0-9A-Za-z]", "", new).upper()[:4]
        if not newp:
            return JSONResponse({"ok": False, "msg": "前缀只能含字母 / 数字"}, status_code=400)
        pfx = load_prefix()
        oldp = pfx.get(subj, "OT")
        clash = next((s for s, v in pfx.items() if s != subj and v == newp), None)
        if clash:
            return JSONResponse({"ok": False, "msg": f"前缀 {newp} 已被科目「{clash}」占用"},
                                status_code=400)
        pfx[subj] = newp
        PREFIX_FILE.write_text(json.dumps(pfx, ensure_ascii=False, indent=2), "utf-8")
        n = 0
        if oldp != newp:
            with DB_LOCK:
                db = load_db()
                for it in db["items"]:
                    c = it.get("code") or ""
                    if (it.get("subject") or "未分类") == subj and c.startswith(oldp):
                        it["code"] = newp + c[len(oldp):]
                        n += 1
                save_db(db)
        return {"ok": True, "changed": n,
                "msg": f"「{subj}」编号前缀 {oldp} → {newp}"
                       + (f"，{n} 道题已重新编号" if n else "")}
    if not old:
        return JSONResponse({"ok": False, "msg": "请选择原名"}, status_code=400)
    if old == new:
        return JSONResponse({"ok": False, "msg": "新旧名称相同"}, status_code=400)
    n = 0
    if kind == "subject":                     # 改科目名: 题目/目录映射/前缀/模板全同步
        lst, dirs = load_subjects()
        if old not in lst:
            return JSONResponse({"ok": False, "msg": f"科目「{old}」不存在"}, status_code=400)
        if new in lst:
            return JSONResponse({"ok": False, "msg": f"科目「{new}」已存在"}, status_code=400)
        if old in dirs:
            dirs[new] = dirs.pop(old)         # 英文目录名不变 -> 图片不用搬
        lst = [new if s == old else s for s in lst]
        save_subjects(lst, dirs)
        pfx = load_prefix()
        pfx[new] = pfx.pop(old, pfx.get(new, "OT"))
        PREFIX_FILE.write_text(json.dumps(pfx, ensure_ascii=False, indent=2), "utf-8")
        tpls = _tpl_all()
        if old in tpls:
            tpls[new] = tpls.pop(old)
            TPL_PATH.write_text(json.dumps(tpls, ensure_ascii=False, indent=2), "utf-8")
        with DB_LOCK:
            db = load_db()
            for it in db["items"]:
                if (it.get("subject") or "未分类") == old:
                    it["subject"] = new
                    n += 1
            save_db(db)
        return {"ok": True, "changed": n, "msg": f"科目「{old}」→「{new}」，{n} 道题已更新"}
    with DB_LOCK:
        db = load_db()
        if kind == "chapter":                 # 改大题归类(标签): 题目 + 大题模板
            for it in db["items"]:
                if (it.get("chapter") or "") == old and \
                        (not subj or (it.get("subject") or "未分类") == subj):
                    it["chapter"] = new
                    n += 1
            save_db(db)
            tpls, changed_t = _tpl_all(), 0
            for k in list(tpls):
                if subj and k != subj:
                    continue
                if old in tpls[k]:
                    tpls[k] = [new if x == old else x for x in tpls[k]]
                    changed_t += 1
            if changed_t:
                TPL_PATH.write_text(json.dumps(tpls, ensure_ascii=False, indent=2), "utf-8")
            return {"ok": True, "changed": n,
                    "msg": f"大题「{old}」→「{new}」，{n} 道题、{changed_t} 个模板已更新"}
        for it in db["items"]:                # 改关键字: 逗号分隔的令牌逐个替换
            ks = _kw_list(it.get("keywords") or "")
            if old in ks:
                it["keywords"] = ",".join(new if x == old else x for x in ks)
                n += 1
        save_db(db)
    return {"ok": True, "changed": n, "msg": f"关键字「{old}」→「{new}」，{n} 道题已更新"}


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
        log_ai("读图失败", "-", False, 0, f"{path}: {e}")
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
        log_ai("写图失败", "-", False, 0, f"{path}: {e}")
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
        log_ai("文件缺失", "-", False, 0, f"/files/{rel} → {fp}")
        return JSONResponse({"ok": False, "msg": "not found"}, status_code=404)
    # 图片/文件可能被同名覆盖(如重裁图块、重新取景), 必须每次校验, 否则浏览器一直显示旧图
    return FileResponse(fp, headers={"Cache-Control": "no-cache, must-revalidate",
                                     "Pragma": "no-cache"})


# ---------- 页面路由 ----------

NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache", "Expires": "0"}


def _index_html():
    """返回首页 HTML(强制禁用缓存, 改动后刷新即生效)。"""
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
# 各科默认大题模板(首次运行/未自定义时使用; 可在「组卷」页修改)
DEFAULT_TPL = {
    "语文": ["一、实用类文本阅读", "二、现代文阅读", "三、文言文阅读", "四、古诗文阅读",
             "五、语言文字运用", "六、作文"],
    "数学": ["一、单项选择题", "二、多选题", "三、填空题", "四、解答题"],
    "物理": ["一、单项选择题", "二、多选题", "三、实验题", "四、计算题"],
    "化学": ["一、单项选择题", "二、简答题"],
    "生物": ["一、单项选择题", "二、简答题"],
    "英语": ["A篇", "B篇", "C篇", "D篇", "七选五", "完形填空", "语法填空", "作文"],
}


@app.get("/api/chapter-tpl")
def get_tpl():
    """按科目配置的大题模板。"""
    d = dict(DEFAULT_TPL)
    if TPL_PATH.exists():
        try:                                   # 用户改过的以文件为准(按科目覆盖)
            d.update(json.loads(TPL_PATH.read_text("utf-8")))
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
        log_ai("识别失败", "-", False, 0, f"{it.get('code')}: {str(e)[:120]}")
        return None
    if not text:
        return None
    text = strip_fig_marks(text)          # 图形一律人工处理, 这里只保留文字
    updated = None
    with DB_LOCK:
        db = load_db()
        for x in db["items"]:
            if x["id"] == it["id"]:
                if force or not (x.get("note") or "").strip():
                    x["note"] = text[:4000]
                updated = dict(x)
                break
        save_db(db)
    return updated


def strip_fig_marks(text):
    """清掉 AI 输出里的图块标记([图N]、[图@x,y,w,h]) —— 已不再让 AI 自动插图标签，
    图块一律由用户自己裁图/上传后引用，避免正文里凭空出现 [图1]。"""
    text = text or ""
    text = re.sub(r"\[?\s*图@[^\]\n]*\]?", " ", text)          # 旧式坐标标记
    text = re.sub(r"\[图\d+(?:\|[^\]]*)?\]", "", text)            # [图1] / [图1|60%]
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


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
    if w < 6 or h < 6:
        return JSONResponse({"ok": False, "msg": "请先框选题目"}, status_code=400)
    img = np.array(open_photo(srcs[0]))
    H, W = img.shape[:2]
    x = max(0, min(x, W - 6)); y = max(0, min(y, H - 6))   # 边界保护, 避免越界 500
    w = min(w, W - x); h = min(h, H - y)
    if w < 6 or h < 6:
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
    saved, merged = [], 0

    def _remap_refs(text, mapping):
        """文本里的 [图N|…] 按 mapping 重编号(mapping 里没有的保持不变)。"""
        return re.sub(r"\[图(\d+)([^\]]*)\]",
                      lambda m: f"[图{mapping.get(int(m.group(1)), int(m.group(1)))}{m.group(2)}]",
                      text or "")

    _ai_ready = bool(ai_config().get("key"))

    def _auto_note(box_img, note):
        """保存入库时自动识别文字(仅当该块文字为空; 不覆盖手填/已识别内容)。失败就返回原文。"""
        if (note or "").strip() or not _ai_ready:
            return note or ""
        try:
            t = strip_fig_marks(clean_ai_text(call_ai_vision(np.array(box_img))))
            return t or (note or "")
        except Exception:
            return note or ""

    def _crop_fig(box_img, fg):
        """按 fg 的坐标裁出图块图。fg 带 px/py/pw/ph 时坐标相对**整页原图**(全页裁图),
        否则相对题目图。返回 (PIL 图, 坐标字段) 或 None。"""
        full = fg.get("px") is not None or fg.get("pw") is not None
        keys = ("px", "py", "pw", "ph") if full else ("x", "y", "w", "h")
        try:
            fx, fy, fw, fh = (int(fg.get(k, 0) or 0) for k in keys)
        except (TypeError, ValueError):
            return None
        base = img if full else box_img
        W, H = base.size
        x0 = max(0, int(fx / 1000 * W)); y0 = max(0, int(fy / 1000 * H))
        w0 = min(W - x0, max(6, int(fw / 1000 * W)))
        h0 = min(H - y0, max(6, int(fh / 1000 * H)))
        if x0 >= W or y0 >= H or w0 < 3 or h0 < 3:
            return None
        try:
            im2 = trim_margins(base.crop((x0, y0, x0 + w0, y0 + h0)))
        except Exception:
            return None
        if im2.size[0] < 3 or im2.size[1] < 3:
            return None
        return im2, {keys[0]: fx, keys[1]: fy, keys[2]: fw, keys[3]: fh}

    for b in boxes:
        x, y, w, h = (max(0, int(b.get(k, 0) * r)) for k in ("x", "y", "w", "h"))
        if w < 6 or h < 6:
            continue
        subject = safe_name(b.get("subject") or "未分类")
        chapter = safe_name(b.get("chapter") or "")
        title = safe_name(b.get("title") or "")
        sd = subj_dirname(subject)                  # 英文科目目录名
        subj_dir = ITEMS_DIR / sd
        subj_dir.mkdir(parents=True, exist_ok=True)
        crop_img = img.crop((x, y, x + w, y + h))
        # ---- 续块: 并入同一组的首块(不单独成题) ----
        gid = str(b.get("group") or "").strip()
        head = next((q for q in db["items"]
                     if gid and (q.get("group") or "") == gid
                     and (q.get("subject") or "") == subject), None) if gid else None
        if head is not None:
            hdir = (ROOT / str(head.get("image") or "")).parent
            hfigs = head.setdefault("figures", [])
            nxt = [max([0] + [int(f.get("n", 0) or 0) for f in hfigs])]

            def take():
                nxt[0] += 1
                return nxt[0]

            # 续块 = 同一道题的另一块(可能是文字块, 也可能是图块): 文字按块拼接, 图块接在后面
            mapping = {}                            # 续块图号 -> 并题后的新图号
            for fg in (b.get("figures") or []):
                got = _crop_fig(crop_img, fg)
                if not got:
                    continue
                fimg, coord = got
                n_new = take()
                fname = f"{head['id']}_fig{n_new}.jpg"
                try:
                    fimg.save(hdir / fname, "JPEG", quality=95)
                except Exception:
                    nxt[0] -= 1
                    continue
                mapping[int(fg.get("n", 0) or 0)] = n_new
                rec = {"n": n_new, "file": str((hdir / fname).relative_to(ROOT)),
                       "t": int(time.time() * 1000)}
                rec.update(coord)
                hfigs.append(rec)
            # 文字里引用了但还没裁的图号也分配新号, 避免与首块撞号
            refs = str(b.get("note") or "") + " " + str(b.get("answer") or "") \
                   + " " + str(b.get("analysis") or "")
            for _m in {int(v) for v in re.findall(r"\[图(\d+)", refs)}:
                if _m not in mapping:
                    mapping[_m] = take()
            cnote = _remap_refs(_auto_note(crop_img, b.get("note")), mapping).strip()
            if cnote:
                head["note"] = ((head.get("note") or "").rstrip() + "\n" + cnote).strip()
            else:                                   # 这块没文字 -> 整块图作为 [图N] 并入
                nn = take()
                fname = f"{head['id']}_fig{nn}.jpg"
                crop_img.save(hdir / fname, "JPEG", quality=95)
                hfigs.append({"n": nn, "file": str((hdir / fname).relative_to(ROOT)),
                              "t": int(time.time() * 1000)})
                head["note"] = ((head.get("note") or "").rstrip() + f"\n[图{nn}]").strip()
            for k in ("answer", "analysis"):
                add = _remap_refs(b.get(k) or "", mapping).strip()
                if add:
                    old = (head.get(k) or "").strip()
                    head[k] = (old + "\n" + add) if old else add
            head["keywords"] = ",".join(dict.fromkeys(
                _kw_list(head.get("keywords")) + _kw_list(b.get("keywords"))))
            head["star"] = max(int(head.get("star") or 0),
                                max(0, min(5, int(b.get("star") or 0))))
            if not (head.get("chapter") or "").strip():
                head["chapter"] = chapter
            merged += 1
            continue
        item_id = f"q{int(time.time() * 1000)}{len(saved)}"
        img_name = f"{item_id}.jpg"
        img_path = subj_dir / img_name
        crop_img.save(img_path, "JPEG", quality=95)
        # 图块: box.figures 里的相对坐标(0-1000) -> 裁出图块文件(支持整页坐标)
        figs = []
        for fg in (b.get("figures") or []):
            got = _crop_fig(crop_img, fg)
            if not got:
                continue                      # 坐标越界/太小: 跳过该图块, 不中断保存
            fimg, coord = got
            n_new = len(figs) + 1
            fname = f"{item_id}_fig{n_new}.jpg"
            try:
                fimg.save(subj_dir / fname, "JPEG", quality=95)
            except Exception:
                continue
            rec = {"n": n_new, "file": f"items/{sd}/{fname}",
                   "t": int(time.time() * 1000)}
            rec.update(coord)
            figs.append(rec)
        item = {
            "id": item_id,
            "code": next_code(db, subject),
            "image": f"items/{sd}/{img_name}",
            "subject": subject,
            "chapter": chapter,
            "title": title,
            "reason": b.get("reason", ""),
            "note": _auto_note(crop_img, b.get("note")),    # 空着的话保存时自动识别
            "answer": b.get("answer", ""),
            "analysis": b.get("analysis", ""),
            "keywords": b.get("keywords", ""),
            "star": max(0, min(5, int(b.get("star") or 0))),
            "figures": figs,
            "group": gid,                 # 续块分组: 同一组的多块合并为一道题
            "source_page": f"pages/{srcs[0].name}",
            "box": {k: int(b.get(k, 0)) for k in ("x", "y", "w", "h")},   # 取景框(缩略图坐标)
            "created": time.strftime("%Y-%m-%d %H:%M"),
        }
        db["items"].append(item)
        saved.append(item)
    save_db(db)
    if saved:
        threading.Thread(target=_auto_ai_bg, args=(list(saved),), daemon=True).start()
    return {"ok": True, "count": len(saved), "items": saved, "auto_ai": True,
            "merged": merged}


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
              "1) 文字错别字、漏字、多余字（只能改图片里能看到的内容）；\n"
              "2) 公式/化学式的下标、电荷、系数配平、括号是否配对；\n"
              "3) LaTeX 语法（花括号是否配对、命令拼写）；\n"
              "4) **图片里看不到的内容必须删掉**：尤其不要凭记忆补出选项、答案、图注；\n"
              "   图片里没有选项就不要添 A．B．C．D．；题目被截断就在截断处结束；\n"
              "5) 与图片不符之处。\n"
              "保持原格式：【题干】标记、公式用 $...$、图块标记 [图N] 及其位置保持不变"
              "（不要新增、删除或改号）、不要输出解释。\n"
              "只输出修正后的完整结果（不得比原文多出图片里看不到的内容）。\n\n识别结果：\n" + draft)
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


AI_PROMPT_STRICT = (
    "你是试卷题目识别工具。只做一件事：把图片里**真实可见**的印刷文字逐字抄出来。\n"
    "硬性规则（违反即为错误）：\n"
    "1. 只输出图片中确实出现的文字，一个字都不许补、不许改、不许续写；\n"
    "2. 你可能会认出这是某道真题、并想起它的选项或答案——那些**不算识别结果**，一律不许输出；\n"
    "3. 图片里没有选项就绝对不要输出 A．B．C．D．这类选项行；图片里没有的图注、说明、答案、解析一律不输出；\n"
    "4. 题目在图片边缘被截断时，就在截断处结束，不要补全；\n"
    "5. 忽略手写笔迹、批注、涂改痕迹、页眉页脚、页码、水印、练习册名称、出题人/审题人署名；\n"
    "6. 不要输出题号（如 1. 2. 3.、①②、第1题），直接从题目内容开始；"
    "但英语完形填空/语法填空每小题的编号（如 41. 61.）属于题目内容，保留；\n"
    "7. 第一行输出【题干】，后跟题干文字；\n"
    "8. 不要输出 [图1]、[图2]、[图@…] 这类图块标记，也不要描述图形；图形一律由用户自己裁图后引用；\n"
    "9. 所有数学公式/化学式用 $...$ LaTeX。\n"
    "输出前自查：你要输出的每一行，都能在图片里逐字找到吗？找不到就删掉。\n"
    "只输出识别结果，不要解释。"
)


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
    prompt = AI_PROMPT_STRICT
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
    text = re.split(r"^\s*[【\[]\s*(?:参考)?\s*(?:答案|解析|解答|点评|分析|说明|方法总结|译文)"
                    r"(?:与解析)?\s*[】\]]",
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
    # 2-6. 逐行清洗(表格行原样保留, 避免误删单元格内容)
    out = []
    for ln in text.split("\n"):
        st = ln.strip()
        if st.startswith("|"):                      # 表格行不参与答案/选项清洗
            out.append(st)
            continue
        if not st:
            continue
        # 删答案标注: 括号内的 A-G 组合(含多字母/未闭合), 如（A）（D C）（AC）
        st = re.sub(r"[（(]\s*(?:[A-G]\s*)+[)）]?", "", st)
        st = re.sub(r"[（(]\s*[)）]", "", st)
        if re.fullmatch(r"答案[:：]?\s*[A-G]", st):   # 纯答案行
            continue
        st = re.sub(r"[√×✓✗]\s*$", "", st)          # 选项行尾的对勾/叉号
        st = re.sub(r"^([A-G])[.、)]", r"\1．", st)   # 选项句点统一全角
        parts = re.split(r"(?=[A-G][．.、)）])", st)   # 一行多选项 -> 每行一个
        opts = [x for x in parts if re.match(r"^[A-G][．.、)）]", x)]
        if len(opts) > 1:
            head = st
            for x in opts:
                head = head.replace(x, "", 1)
            head = head.strip()
            if re.fullmatch(r"\d{1,2}[.、．)）]", head):
                opts[0] = head + " " + opts[0]        # 完形填空/语法填空: 小题号跟着第一个选项
            elif head:
                out.append(head)
            out.extend(x.strip() for x in opts)
        else:
            out.append(st)
    return "\n".join(out)


def md_table_typst(rows, esc_cell):
    """Markdown 管道表 -> Typst 三线表(居中, 自动列数)。
    rows: 以 | 开头的连续行; esc_cell: 单元格转义函数(支持 $公式$ / **粗体** / 上标)。
    第二行若是 |---|:--:| 这类分隔行, 则其上一行视为表头, 并按分隔行决定各列对齐。"""
    if not rows:
        return ""
    body, aligns, header_rows = [], [], 0
    for r in rows:
        c = r.strip()
        c = c[1:] if c.startswith("|") else c
        c = c[:-1] if c.endswith("|") else c
        cells = [x.strip() for x in c.split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", x) for x in cells):
            aligns = [("center" if x.startswith(":") and x.endswith(":") else
                       "right" if x.endswith(":") else
                       "left" if x.startswith(":") else "center") for x in cells]
            header_rows = len(body)
            continue
        body.append(cells)
    if not body:
        return ""
    ncol = max(len(r) for r in body)
    body = [r + [""] * (ncol - len(r)) for r in body]
    al = ", ".join((aligns[i] if i < len(aligns) else "center") + " + horizon"
                   for i in range(ncol))
    out = ["#align(center)[#table(",
           "  columns: " + str(ncol) + ",",
           "  stroke: none,",
           "  inset: (x: 6pt, y: 3pt),",
           "  align: (" + al + "),",
           "  table.hline(stroke: 1pt),"]
    for ri, r in enumerate(body):
        out.append("  " + ", ".join("[" + esc_cell(x) + "]" for x in r) + ",")
        if header_rows and ri == header_rows - 1:
            out.append("  table.hline(stroke: 0.5pt),")
    out.append("  table.hline(stroke: 1pt),")
    out.append(")]")
    return "\n".join(out)


def typ_esc(t):
    """转义 Typst 文本中的特殊字符(用于标题/注意事项等自由文本)。"""
    for ch, e2 in (("#", "\\#"), ("$", "\\$"), ("{", "\\{"), ("}", "\\}"),
                   ("[", "\\["), ("]", "\\]"), ("_", "\\_")):
        t = t.replace(ch, e2)
    return t


MAX_W_CM = 14.1          # 版心宽度 = 185mm - 左右页边距 2.2cm×2
TEXT_H_CM = 22.0         # 版心高度 = 260mm - 上下页边距 2cm×2


def fig_size_args(fp, spec, h_pct=24.0):
    """算图块的 Typst 尺寸参数。spec: 空 / '60%'(宽) / '8cm'(宽) / '24%h'(高) / '6cmh'(高)。
    默认按版心高度的 h_pct% 定高; 任何写法都保证不超版心宽/高(过长或过高的图自动换一种定尺寸方式)。"""
    spec = (spec or "").strip()
    try:
        with Image.open(fp) as im0:
            pw, ph = im0.size
    except Exception:
        pw, ph = 4, 3
    ratio = pw / max(1, ph)
    h_cm = TEXT_H_CM * min(90.0, max(3.0, h_pct)) / 100.0
    if spec:
        try:
            if spec[-1] in "hH":                   # 按高度: 24%h / 6cmh
                v = spec[:-1].strip()
                if v.endswith("cm"):
                    h_cm = min(TEXT_H_CM, max(0.5, float(v[:-2])))
                else:
                    h_cm = TEXT_H_CM * min(100.0, max(2.0, float(v.rstrip("%")))) / 100.0
            else:                                  # 按宽度: 60% / 8cm
                is_cm = spec.endswith("cm")
                if is_cm:
                    w_cm = min(MAX_W_CM, max(0.5, float(spec[:-2])))
                else:
                    pctv = min(100.0, max(5.0, float(spec.rstrip("%"))))
                    w_cm = MAX_W_CM * pctv / 100.0
                if w_cm * ph / max(1, pw) <= TEXT_H_CM:
                    return f"width: {w_cm:.2f}cm" if is_cm else f"width: {pctv:g}%"
                h_cm = TEXT_H_CM                  # 太高会顶出版心 -> 改按高度
        except ValueError:
            pass
    w_cm = h_cm * ratio                            # 按高度定尺寸, 宽度按比例算
    return "width: 100%" if w_cm > MAX_W_CM else f"width: {w_cm:.2f}cm"


def render_simple(txt, it, lines, fig_h_pct=24.0):
    """附录页简化渲染: $公式$ -> mitex, [图N] -> 图片, [图注:文字] -> 图下注释, markdown 字体标记。"""
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
        for seg in re.split(r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|\*[^*]+?\*|==.+?==|\+\+.+?\+\+|\^[^\^\s]{1,6}\^)", t):
            if not seg:
                continue
            if seg.startswith("^") and seg.endswith("^") and len(seg) > 2:
                out += "#super[" + esc1(seg[1:-1]) + "]"
            elif seg.startswith("***") and seg.endswith("***") and len(seg) > 6:
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

    tbl_buf2 = []
    for ln in txt.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("|"):                        # 附录页表格
            tbl_buf2.append(ln)
            continue
        if tbl_buf2:
            t2 = md_table_typst(tbl_buf2, md)
            tbl_buf2.clear()
            if t2:
                lines.append(t2)
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
            spec = m.group(2) or ""
            f0 = figmap.get(n2)
            fp = ROOT / str(f0.get("file", "")) if f0 else None
            if fp is None or not fp.exists():
                cand = (ROOT / f"{it['image'][:-4]}_fig{n2}.jpg") if it.get("image") else None
                fp = cand if (cand and cand.exists()) else None
            if fp:
                figs.append((str(fp), fig_size_args(fp, spec, fig_h_pct)))
                return f"@@F{len(figs) - 1}@@"
            return f'#text(fill: rgb("#cc0000"))[图{n2}缺失]'
        cap = None
        cm = re.search(r"\[图注[:：]\s*([^\]]*)\]", ln)
        if cm:
            cap = md(cm.group(1).strip())
            ln = ln.replace(cm.group(0), "").strip()
            if not ln:
                lines.append(f'#align(center)[#text(size: 0.88em)[{cap}]]')
                continue
        ln = re.sub(r"\[图(\d+)(?:\|([\d.]+%?[hH]?))?\]", _fig, ln)
        ln = ln.replace("（图）", "").replace("(图)", "")
        out = ""
        for seg in re.split(r"(\$[^$]+\$)", ln):
            if seg.startswith("$") and seg.endswith("$") and len(seg) > 2:
                out += '#mi("' + latex_var(seg[1:-1]) + '")'
            else:
                out += md(seg)
        for i, (fp, sz) in enumerate(figs):
            out = out.replace(f"@@F{i}@@", f'#image("{fp}", {sz})')
        if out.strip():
            lines.append(f"#align({align_mode})[{out}]" if align_mode else out + " \\")
        if cap:
            lines.append(f'#align(center)[#text(size: 0.88em)[{cap}]]')


# ---------- 自动组卷 ----------
AUTO_LAST_FILE = ROOT / "auto_last.json"
DRAFT_FILE = ROOT / "drafts.json"      # 框选草稿(框的位置 + 已填内容), 防止浏览器数据丢失


def load_drafts():
    try:
        return json.loads(DRAFT_FILE.read_text("utf-8")) or {}
    except Exception:
        return {}


def put_draft_file(d):
    DRAFT_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")


def load_auto_last():
    try:
        return list(json.loads(AUTO_LAST_FILE.read_text("utf-8")) or [])
    except Exception:
        return []


def save_auto_last(ids):
    try:
        AUTO_LAST_FILE.write_text(json.dumps(list(ids)[:5000]), "utf-8")
    except Exception:
        pass


def _star(it):
    try:
        return max(0, min(5, int(it.get("star") or 0)))
    except (TypeError, ValueError):
        return 0


def _kwset(it):
    raw = (it.get("keywords") or "").replace("，", ",").replace("、", ",")
    return {k.strip().lower() for k in raw.split(",") if k.strip()}


def _bounds(spec, star):
    """返回某星级的 (最少, 最多); 最多为 None 表示不限。spec: {"2": [2,4], "3": [3,null]}"""
    b = (spec or {}).get(str(star))
    if b is None:
        b = (spec or {}).get(star)
    if b is None:
        return 0, None
    if isinstance(b, (int, float)):
        return int(b), None
    lo = int(b[0] or 0) if len(b) > 0 else 0
    hi = b[1] if len(b) > 1 else None
    hi = None if hi in (None, "") else int(hi)
    return max(0, lo), hi


def auto_pool(items, groups, subject="", exclude_ids=None):
    """按大题分组筛出候选题目(排除分节块 §、可选排除指定 id)。"""
    ex = set(exclude_ids or [])
    pool = {}
    for g in groups:
        ch = g.get("chapter") or ""
        pool[ch] = [it for it in items
                    if (it.get("chapter") or "") == ch
                    and (not subject or (it.get("subject") or "") == subject)
                    and it.get("id") not in ex
                    and not (it.get("title") or "").strip().startswith("§")]
    return pool


TILT_W = {                       # 难度倾向 -> 各星级权重(用于补足阶段的挑选偏好)
    "": {0: 1, 1: 1, 2: 1, 3: 1, 4: 1, 5: 1},
    "easy": {0: 3, 1: 4, 2: 3, 3: 2, 4: 1, 5: 0.5},
    "mid": {0: 1, 1: 1, 2: 2, 3: 3, 4: 2, 5: 1},
    "hard": {0: 0.5, 1: 0.5, 2: 1, 3: 2, 4: 3, 5: 4},
}


def _alloc_star_counts(group, avail, P, tilt=""):
    """把"某大题各星级可用数"分摊到 P 份卷子: 返回 P×6 的数量矩阵, 或 (None, 原因)。
    约束: 每份总量 = count, 各星级 lo ≤ n ≤ hi(hi=None 不限), 且各份合计不超过可用数。"""
    C = int(group.get("count") or 0)
    if C <= 0:
        return [[0] * 6 for _ in range(P)], None
    spec = group.get("stars") or {}
    lo, hi = {}, {}
    for st in range(6):
        lo[st], hi[st] = _bounds(spec, st)
    base = sum(lo.values())
    if base > C:
        return None, f"各星级「最少」之和 {base} 道已超过题量 {C} 道"
    n = [[lo[st] for st in range(6)] for _ in range(P)]
    R = {st: avail.get(st, 0) - P * lo[st] for st in range(6)}   # 各星级还能再分几道
    for st, v in R.items():
        if v < 0:
            return None, (f"{st} 星题目不足：{P} 份共需至少 {P * lo[st]} 道，"
                          f"库里只有 {avail.get(st, 0)} 道")
    r = [C - base] * P                                          # 每份还差几道
    total = sum(r)
    while total > 0:
        pi = max(range(P), key=lambda i: r[i])                  # 先补缺口最大的那份
        if r[pi] <= 0:
            break
        cand = [st for st in range(6) if R[st] > 0 and (hi[st] is None or n[pi][st] < hi[st])]
        if not cand:
            return None, (f"题目不足：受星级「最多」限制，每份最多只能排 "
                          f"{C - r[pi]} 道 / 需要 {C} 道")
        w = TILT_W.get(tilt or "", TILT_W[""])
        # 难度倾向优先(决定整体偏易/偏难), 同权重内再看剩余量与已取数(保持份与份之间均衡)
        st = max(cand, key=lambda x: (w.get(x, 1), R[x], -n[pi][x]))
        n[pi][st] += 1
        R[st] -= 1
        r[pi] -= 1
        total -= 1
    return n, None


def auto_plan(items, groups, subject="", papers=1, unique_keywords=False,
              exclude_ids=None, seed=None, max_probe=50, tilt="", fixed_ids=None):
    """自动组卷: 先算全局配额(每份各星级拿几道), 再发牌。
    fixed_ids: 每份卷子都必须包含的题目(指定题目)，其余题随机抽。
    返回 (卷子列表, 诊断); 卷子内按大题顺序、大题内按星级升序。"""
    rnd = random.Random(seed)
    fixed_ids = [str(x) for x in (fixed_ids or [])]
    byid = {x["id"]: x for x in items}
    fx = [byid[i] for i in fixed_ids if i in byid]          # 指定题目(每份必含)
    fx_by_ch = collections.defaultdict(list)
    for x in fx:
        fx_by_ch[x.get("chapter") or ""].append(x)
    ex = set(exclude_ids or []) | {x["id"] for x in fx}    # 指定题不再参加随机
    base = auto_pool(items, groups, subject, ex)

    def eff(g):                                              # 该大题还需随机抽几道
        return max(0, int(g.get("count") or 0) - len(fx_by_ch.get(g.get("chapter") or "", [])))

    diag = {"max_papers": 0, "per_group": [], "msg": ""}
    for g in groups:
        ch = g.get("chapter") or ""
        diag["per_group"].append({"chapter": ch, "available": len(base.get(ch) or []),
                                  "count": int(g.get("count") or 0),
                                  "fixed": len(fx_by_ch.get(ch, []))})
    avail = {ch: collections.Counter(_star(x) for x in v) for ch, v in base.items()}
    live = [g for g in groups if int(g.get("count") or 0) > 0]
    if not live:
        return [], {**diag, "ok": False, "msg": "请先填写各大题的题量"}
    rlive = [g for g in live if eff(g) > 0]                 # 需要随机抽题的大题
    ups = [len(base.get(g.get("chapter") or "", [])) // eff(g) for g in rlive]
    upper = min(min(ups) if ups else max_probe, max_probe)
    for P in range(1, upper + 1):
        bad = None
        for g in rlive:
            _, err = _alloc_star_counts({**g, "count": eff(g)},
                                        avail.get(g.get("chapter") or "", collections.Counter()),
                                        P, tilt)
            if err:
                bad = err
                break
        if bad:
            diag["msg"] = f"再出第 {P} 份时不够：{bad}"
            break
        diag["max_papers"] = P
    if diag["max_papers"] == upper and diag["max_papers"] > 0:
        for g in rlive:                      # 已到总量上限: 再试一份, 报告卡在哪
            _, err2 = _alloc_star_counts({**g, "count": eff(g)},
                                         avail.get(g.get("chapter") or "",
                                                     collections.Counter()),
                                         diag["max_papers"] + 1, tilt)
            if err2:
                diag["msg"] = f"再出第 {diag['max_papers'] + 1} 份时不够：{err2}"
                break
    if diag["max_papers"] == 0:
        return [], {**diag, "ok": False, "msg": diag["msg"] or "题目不足，无法组卷"}
    want = max(1, int(papers or 1))
    if want > diag["max_papers"]:
        return [], {**diag, "ok": False,
                    "msg": f"按当前约束最多只能出 {diag['max_papers']} 份（{diag['msg']}）"}
    # 发牌: 每个大题各星级洗牌后, 按配额分给各份
    for attempt in range(4):                # 关键字去重可能因发牌顺序失败 -> 重试几次
        papers_items, kw_used, fail = [[x for x in fx] for _ in range(want)], [set() for _ in range(want)], None
        for g in live:
            ch = g.get("chapter") or ""
            n, err = _alloc_star_counts({**g, "count": eff(g)},
                                        avail.get(ch, collections.Counter()), want, tilt)
            if err:
                fail = err
                break
            buckets = {}
            for x in base.get(ch, []):
                buckets.setdefault(_star(x), []).append(x)
            for v in buckets.values():
                rnd.shuffle(v)
            for pi in range(want):
                for st in range(6):
                    for _ in range(n[pi][st]):
                        got = None
                        while buckets.get(st):
                            x = buckets[st].pop()
                            if unique_keywords and (_kwset(x) & kw_used[pi]):
                                continue
                            got = x
                            break
                        if got is None:
                            fail = (f"「{ch}」{st} 星题目不够分配"
                                    + ("（关键字去重要求下，可关闭该选项）" if unique_keywords else ""))
                            break
                        papers_items[pi].append(got)
                        kw_used[pi] |= _kwset(got)
                    if fail:
                        break
                if fail:
                    break
            if fail:
                break
        if not fail:
            break
    else:
        return [], {**diag, "ok": False, "msg": fail or "题目分配失败"}
    order = {(g.get("chapter") or ""): i for i, g in enumerate(groups)}
    fx_ord = {x["id"]: i for i, x in enumerate(fx)}
    out = []
    for pi in range(want):
        # 指定题目排在该大题最前(按添加顺序), 随机题按星级升序跟在后面
        seg = sorted(papers_items[pi],
                     key=lambda x: (order.get(x.get("chapter") or "", 99),
                                    0 if x["id"] in fx_ord else 1,
                                    fx_ord.get(x["id"], _star(x))))
        out.append(seg)
    return out, {**diag, "ok": True,
                 "msg": f"已生成 {want} 份（当前题库与约束最多可出 {diag['max_papers']} 份）"}


@app.post("/api/auto/plan")
def auto_plan_api(payload: dict = None):
    """自动组卷预检: 不生成, 只报告最多可出几份与每个大题的可用题量。"""
    payload = payload or {}
    groups = payload.get("groups") or []
    if not groups:
        return JSONResponse({"ok": False, "msg": "请先设置各大题的题量与星级限制"}, status_code=400)
    ex = set(load_auto_last()) if payload.get("avoid_last") else set()
    _, diag = auto_plan(load_db()["items"], groups, payload.get("subject") or "",
                        papers=1, exclude_ids=ex, seed=payload.get("seed"),
                        tilt=payload.get("tilt") or "", fixed_ids=payload.get("fixed"))
    mx = diag.get("max_papers", 0)
    msg = (f"最多可出 {mx} 份（{diag.get('msg', '')}）" if diag.get("ok") else diag.get("msg", ""))
    return {"ok": bool(diag.get("ok")), "max_papers": mx,
            "per_group": diag.get("per_group", []), "msg": msg}


@app.post("/api/auto/build")
def auto_build_api(payload: dict = None):
    """自动组卷: 生成 N 份互不重复的卷子, 返回每份的题目列表(卷内已按星级升序)。"""
    payload = payload or {}
    groups = payload.get("groups") or []
    if not groups:
        return JSONResponse({"ok": False, "msg": "请先设置各大题的题量与星级限制"}, status_code=400)
    ex = set(load_auto_last()) if payload.get("avoid_last") else set()
    papers, diag = auto_plan(load_db()["items"], groups, payload.get("subject") or "",
                             papers=int(payload.get("papers") or 1),
                             unique_keywords=bool(payload.get("unique_keywords")),
                             exclude_ids=ex, seed=payload.get("seed"),
                             tilt=payload.get("tilt") or "", fixed_ids=payload.get("fixed"))
    if not diag.get("ok"):
        return JSONResponse({"ok": False, "msg": diag.get("msg", "组卷失败"),
                             "max_papers": diag.get("max_papers", 0),
                             "built": diag.get("built", 0)}, status_code=400)
    save_auto_last([x["id"] for p in papers for x in p])
    out = [{"index": i,
            "items": [{"id": x["id"], "code": x.get("code"), "star": _star(x),
                       "chapter": x.get("chapter") or ""} for x in p]}
           for i, p in enumerate(papers, start=1)]
    log_ai("自动组卷", "-", True, 0, f"{payload.get('subject', '')} {len(papers)} 份")
    return {"ok": True, "papers": out, "msg": diag.get("msg", "")}


@app.post("/api/auto/export")
def auto_export(payload: dict = None):
    """把多份卷子渲染为 PDF 并打包 ZIP。payload: {papers:[{ids:[...], title:...}], 卷头参数…}"""
    payload = payload or {}
    papers = payload.get("papers") or []
    if not papers:
        return JSONResponse({"ok": False, "msg": "没有可导出的卷子"}, status_code=400)
    import zipfile
    zpath = TMP_DIR / f"papers_{int(time.time() * 1000)}.zip"
    made = 0
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for i, pp in enumerate(papers, start=1):
                ids = pp.get("ids") or []
                if not ids:
                    continue
                d = paper_pdf(ids=",".join(ids), attach=payload.get("attach") or "",
                              index=payload.get("index") or "", header=payload.get("header") or "",
                              title=pp.get("title") or payload.get("title") or "",
                              subject_line=payload.get("subject_line") or "",
                              notice=payload.get("notice") or "",
                              body_size=payload.get("body_size") or "",
                              leading=payload.get("leading") or "",
                              subtitle=payload.get("subtitle") or "",
                              first_indent=payload.get("first_indent") or "",
                              fig_height=payload.get("fig_height") or "")
                if isinstance(d, dict) and d.get("ok"):
                    f = ROOT / str(d["url"]).lstrip("/").replace("files/", "", 1)
                    if f.exists():
                        z.write(f, arcname=f"paper-{i:02d}.pdf")
                        made += 1
    except Exception as e:
        return JSONResponse({"ok": False, "msg": "导出失败: " + str(e)[:150]}, status_code=500)
    if not made:
        return JSONResponse({"ok": False, "msg": "没有生成任何 PDF"}, status_code=500)
    return {"ok": True, "url": f"/files/.tmp/{zpath.name}", "count": made}


@app.get("/api/paper/pdf")
def paper_pdf(ids: str = "", attach: str = "", index: str = "", header: str = "",
              title: str = "", subject_line: str = "", notice: str = "",
              body_size: str = "", leading: str = "", subtitle: str = "",
              first_indent: str = "", fig_height: str = ""):
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
    try:                                   # 段落首行缩进(语文卷常用 2 字符)
        indent_em = min(4.0, max(0.0, float(first_indent))) if first_indent else 0.0
    except (TypeError, ValueError):
        indent_em = 0.0
    try:                                   # 图片默认高度(占版心高度百分比)
        fig_h_pct = min(90.0, max(6.0, float(fig_height))) if fig_height else 24.0
    except (TypeError, ValueError):
        fig_h_pct = 24.0
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
        f'#set par(justify: true, leading: {lead_em}em, spacing: {lead_em}em, first-line-indent: 0em)',
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
        (f'#align(center)[#text(size: BODY, font: F_SONG)[{typ_esc(subtitle.strip())}]]'
         if subtitle.strip() else '#none'),
        '#v(0.45cm)',
    ]
    # 注意事项(可自定义, 首行黑体小四, 条目五号)
    notice_text = (notice or "").strip() or "注意事项：\n1．本试卷由错题收集工具生成，请在答题纸上作答；\n2．解答应写出文字说明、证明过程或演算步骤。"
    for i, ln in enumerate(notice_text.split("\n")):
        if not ln.strip():
            continue
        if i == 0:
            lines.append(f'#text(font: F_HEI, size: 12pt)[{typ_esc(ln)}] \\')
        else:
            lines.append(f'#text(size: BODY)[{typ_esc(ln)}] \\')
    lines.append('#v(0.45cm)')
    flat = [it for _g, _items in groups.items() for it in _items]
    grp_first = {}                    # 续块组 -> 卷面上该组第一块(用于判断「续」块)
    for _it in flat:
        _g0 = _it.get("group") or ""
        if _g0 and _g0 not in grp_first:
            grp_first[_g0] = _it["id"]
    n = 0
    ordered = {}                      # 题号 -> [该题的块(含续块)]
    for gname, gitems in groups.items():
        if gname and gname not in ("未命名", "未分类", "无"):
            lines.append(f"= {gname}")          # 大题标题: 黑体(空/未命名不显示)
        for it in gitems:
            g = it.get("group") or ""
            t0 = (it.get("title") or "").strip()
            if t0.startswith("§"):
                # § 前缀 = 卷面分节块(大题标题/阅读材料), 黑体整行, 不参与编号
                lines.append(f'#text(font: F_HEI, size: BODY, weight: "bold")'
                             f'[{typ_esc(t0[1:].strip())}] \\')
            elif g and grp_first.get(g) != it["id"]:
                # 同一续块组的后续块: 不重新编号(不要求与首块相邻, 跨页/中间插了别的题也能正确识别)
                lines.append('#text(font: F_KAI, size: BODY)[(续)] \\')
            else:
                n += 1
                lines.append(f"{n}．")               # 题号顶格, 题干接同一行
            ordered.setdefault(n, []).append(it)
            txt = (it.get("note") or "").strip()
            if txt:
                # 清洗 AI 输出的 Markdown: 去代码块围栏和 # 标题标记, 避免 Typst 误渲染
                txt = re.sub(r"^```[a-z]*\s*$", "", txt, flags=re.M)
                txt = re.sub(r"^#{1,6}\s*", "", txt, flags=re.M)
                txt = txt.replace("【题干】", "")
                placed = False
                fig_tokens = []
                opt_buf = []
                FLUSH_PREFIX = ("阅读下面", "材料一", "材料二", "材料三", "材料四",
                                "材料五", "[注]", "（一）", "（二）", "（三）", "（四）", "（五）")

                def keep_flush(t):
                    """原卷规则: 材料标签/题号/选项/注释/指示语/大题标题 不参与首行缩进。
                    注意: "2025 年…" 这种以数字开头的正文段仍要缩进, 故题号须带 ．、)） 才算。"""
                    t = (t or "").strip().lstrip("*=+")     # 先剥掉 **粗体** ==加粗== 等标记
                    if not t:
                        return False
                    if t[0] in "（(":                       # （1） （一） 之类小问/层级标号
                        j = 1
                        while j < len(t) and (t[j].isdigit() or t[j] in "一二三四五六七八九十"):
                            j += 1
                        if j > 1 and j < len(t) and t[j] in "）)":
                            return True
                    else:
                        j = 0
                        while j < len(t) and t[j].isdigit():
                            j += 1
                        if 0 < j <= 2 and j < len(t) and t[j] in "．.、)）":     # 1． 17、
                            return True
                        if len(t) > 1 and t[0] in "ABCD" and t[1] in "．.、)）":  # 选项行
                            return True
                        if len(t) > 1 and t[0] in "一二三四五六七八九十" and t[1] == "、":
                            return True
                    return t.startswith(FLUSH_PREFIX)

                first_ln = True      # 题号后的第一个文本行, 与题号同行(不加换行符)
                para_mode = None     # ::: poem / ::: quote 整段样式
                para_buf = []
                tbl_buf = []         # 连续收集的 | 表格行

                def flush_para():
                    """输出 ::: poem(诗歌: 居中+大行距) / ::: quote(材料: 缩进+中行距) 整段。"""
                    if not para_buf:
                        return
                    flush_opts()
                    body = " \\\n".join(para_buf)
                    if para_mode == "poem":
                        lines.append("#align(center)[#set par(justify: false, first-line-indent: 0em, "
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
                    for seg in re.split(r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|\*[^*]+?\*|==.+?==|\+\+.+?\+\+|\^[^\^\s]{1,6}\^)", s):
                        if not seg:
                            continue
                        if seg.startswith("^") and seg.endswith("^") and len(seg) > 2:
                            out += "#super[" + esc_one(seg[1:-1]) + "]"        # 上标角标(注释序号①/[注])
                        elif seg.startswith("***") and seg.endswith("***") and len(seg) > 6:
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
                    """提取行内图引用 -> (占位符文本, [{file,size,align}])。
                    支持 [图N] / [图N|60%] / [图N|24%h] / [图N|60%|left]，兼容旧 [图@x,y,w,h] 与 [图]。"""
                    infos = []

                    def rep_new(m):
                        n2 = int(m.group(1))
                        spec = m.group(2) or ""
                        al = m.group(3) or "center"
                        fp = fig_file(n2)
                        if fp:
                            infos.append({"file": str(fp),
                                          "size": fig_size_args(fp, spec, fig_h_pct),
                                          "align": al})
                            return f"@@F{len(infos) - 1}@@"
                        return f'#text(fill: rgb("#cc0000"))[图{n2}未裁好]'

                    s = re.sub(r"\[图(\d+)(?:\|([\d.]+%?[hH]?))?(?:\|(left|center|right))?\]",
                               rep_new, s)

                    def rep_old(m):
                        fp = crop_old(*(int(v) for v in m.groups()), len(infos))
                        infos.append({"file": str(fp),
                                      "size": fig_size_args(fp, "", fig_h_pct),
                                      "align": "center"})
                        return f"@@F{len(infos) - 1}@@"

                    s = re.sub(r"\[图@(\d+),(\d+),(\d+),(\d+)\]", rep_old, s)
                    if "[图]" in s and it.get("image"):
                        _ip = ROOT / it["image"]
                        infos.append({"file": str(_ip),
                                      "size": fig_size_args(_ip, "45%", fig_h_pct),
                                      "align": "center"})
                        s = s.replace("[图]", f"@@F{len(infos) - 1}@@")
                    s = s.replace("（图）", "").replace("(图)", "")
                    return s, infos

                raw_lines = txt.split("\n")
                skip_next = False
                for li, raw_ln in enumerate(raw_lines):
                    ln = raw_ln.strip()
                    if not ln:
                        continue
                    if skip_next:
                        skip_next = False
                        continue
                    capm = re.match(r"^\[图注[:：]\s*(.*?)\s*\]$", ln)
                    if capm:                             # 独立图注行(前面没图): 居中排在这里
                        flush_opts()
                        lines.append(f'#align(center)[#text(size: 0.88 * BODY)'
                                     f'[{esc_ln(capm.group(1))}]]')
                        first_ln = False
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
                    om = re.match(r"^(\d{1,2}[.、．)）]\s*)?([A-G])[．.、)）]\s*(.*)$", ln)
                    if om:                                # 选项行: 收集后 grid 对齐
                        if len(opt_buf) >= 4:
                            flush_opts()
                        opt_buf.append((om.group(1) or "") + om.group(2) + "．" + om.group(3))
                        continue
                    if ln.strip().startswith("|"):       # 表格行: 连续收集后整体输出
                        flush_opts()
                        tbl_buf.append(ln.strip())
                        continue
                    if tbl_buf:
                        t = md_table_typst(tbl_buf, esc_ln)
                        tbl_buf.clear()
                        if t:
                            lines.append(t)
                            lines.append("#v(0.15cm)")
                    inline_cap = None
                    cm3 = re.search(r"\[图注[:：]\s*([^\]]*)\]", ln)
                    if cm3:
                        inline_cap = cm3.group(1).strip()
                        ln = ln.replace(cm3.group(0), "").strip()
                    ln2, infos = scan_figs(ln)
                    if para_mode:                        # 整段样式: 收集纯文本行(图仍走通用分支)
                        if not infos:
                            para_buf.append(esc_ln(ln2))
                            continue
                    rest = re.sub(r"@@F\d+@@", "", ln2).strip()
                    if infos and not rest:
                        # 整行只有图: 多图并排 / 单图对齐; 下一行是 [图注:文字] 时排在图正下方
                        cap = None
                        if li + 1 < len(raw_lines):
                            m2 = re.match(r"^\[图注[:：]\s*(.*?)\s*\]$", raw_lines[li + 1].strip())
                            if m2:
                                cap = esc_ln(m2.group(1))
                                skip_next = True
                        cap_in = (f" \\ #v(-0.12cm) #text(size: 0.88 * BODY)[{cap}]"
                                  if cap else "")
                        if len(infos) > 1:
                            cells = "".join(f'[#image("{f0["file"]}", {f0["size"]})]'
                                            for f0 in infos)
                            lines.append(f"#grid(columns: {len(infos)}, "
                                         f"column-gutter: 0.6em, row-gutter: 0.5em){cells}")
                            if cap:
                                lines.append("#align(center)[#v(-0.25cm)"
                                             f"#text(size: 0.88 * BODY)[{cap}]]")
                        else:
                            f0 = infos[0]
                            lines.append(f'#align({f0["align"]})'
                                         f'[#image("{f0["file"]}", {f0["size"]}){cap_in}]')
                        placed = True
                        first_ln = False
                        continue
                    out = esc_ln(ln2)
                    for i, f0 in enumerate(infos):       # 混排: 行内插图
                        out = out.replace(f"@@F{i}@@",
                                          f'#image("{f0["file"]}", {f0["size"]})')
                    if out.strip():
                        if align_mode:
                            flush_opts()
                            lines.append(f"#align({align_mode})[{out}]")
                            first_ln = False
                        else:
                            flush_opts()
                            if indent_em > 0:
                                # 段落化(空行分隔), 并对散文段落显式加 2 字符首行缩进
                                if not keep_flush(ln2):
                                    out = f"#h({indent_em}em)" + out
                                lines.append(out + chr(10))
                            else:
                                lines.append(out + " \\")
                            first_ln = False
                        if inline_cap:                   # 行内图注: 紧跟本行下方居中
                            lines.append(f'#align(center)[#text(size: 0.88 * BODY)'
                                         f'[{esc_ln(inline_cap)}]]')
                flush_para()
                if tbl_buf:                          # 收尾: 题末仍是表格
                    t = md_table_typst(tbl_buf, esc_ln)
                    tbl_buf.clear()
                    if t:
                        lines.append(t)
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
                render_simple(content, b, lines, fig_h_pct)
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
    return {"ok": True, "url": f"/files/.tmp/{pdf_path.name}", "count": len(items)}


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
        if w < 6 or h < 6:
            return JSONResponse({"ok": False, "msg": "请先框选题目"}, status_code=400)
        img = np.array(open_photo(srcs[0]))[y:y + h, x:x + w]
    try:
        text = clean_ai_text(call_ai_vision(img))
    except Exception as e:
        return JSONResponse({"ok": False, "msg": "AI 识别失败: " + str(e)[:200]},
                            status_code=502)
    text = strip_fig_marks(text)                # 图形由用户手动裁剪, AI 只给文字
    return {"ok": True, "text": text}


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
    log_ai("重新识别", "-", True, 0, f"{upd.get('code')} 图片={it['image']}")
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
    # 手动添加若带了图片且题干为空 -> 后台 AI 识别(与框选保存一致)
    auto = bool(img_rel) and not (note or "").strip()
    if auto:
        threading.Thread(target=_auto_ai_bg, args=([item],), daemon=True).start()
    return {"ok": True, "item": item, "auto_ai": auto}


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
async def upload_figure(item_id: str, file: UploadFile = File(...), n: str = Form("")):
    """上传图片作为题目的图片附件。n 非空时把图片绑定到该已有编号(覆盖同编号旧图)。"""
    db = load_db()
    for it in db["items"]:
        if it["id"] == item_id:
            src = ROOT / it["image"]
            if not src.exists():
                return JSONResponse({"ok": False, "msg": "原图不存在"}, status_code=404)
            data = await file.read()
            if len(data) < 100:
                return JSONResponse({"ok": False, "msg": "文件为空"}, status_code=400)
            figs = list(it.get("figures") or [])
            try:
                want = int(str(n).strip())
            except (TypeError, ValueError):
                want = 0
            if want > 0:                              # 绑定到已有标签 [图N]
                old = next((f for f in figs if int(f.get("n", 0)) == want), None)
                if old:
                    p0 = ROOT / str(old.get("file", ""))
                    if p0.exists():
                        p0.unlink(missing_ok=True)
                    figs.remove(old)
                num = want
            else:
                num = max((f.get("n", 0) for f in figs), default=0) + 1
            suffix = Path(file.filename or "img.jpg").suffix.lower()
            if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
                suffix = ".jpg"
            fname = f"{item_id}_fig{num}{suffix}"
            fdir = src.parent
            (fdir / fname).write_bytes(data)
            figs.append({"n": num, "file": str((fdir / fname).relative_to(ROOT)),
                         "upload": True})
            figs.sort(key=lambda f: int(f.get("n", 0)))
            it["figures"] = figs
            save_db(db)
            return {"ok": True, "n": num, "figures": figs}
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
    """给已保存的错题添加图块: {x,y,w,h}(相对题图 0-1000 比例) -> 裁图块并返回编号。
    可选 {n: 3} 把裁好的图绑定到已有标签 [图3] 上(覆盖同编号旧图)。"""
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
            w0 = min(W - x0, max(6, int(fw / 1000 * W)))
            h0 = min(H - y0, max(6, int(fh / 1000 * H)))
            if w0 < 6 or h0 < 6:
                return JSONResponse({"ok": False, "msg": "框选区域太小"}, status_code=400)
            figs = list(it.get("figures") or [])
            try:
                want = int(payload.get("n") or 0)
            except (TypeError, ValueError):
                want = 0
            if want > 0:
                old = next((f for f in figs if int(f.get("n", 0)) == want), None)
                if old:
                    p0 = ROOT / str(old.get("file", ""))
                    if p0.exists():
                        p0.unlink(missing_ok=True)
                    figs.remove(old)
                n = want
            else:
                n = max((f.get("n", 0) for f in figs), default=0) + 1
            fname = f"{item_id}_fig{n}.jpg"
            fdir = src.parent
            trim_margins(img.crop((x0, y0, x0 + w0, y0 + h0))).save(
                fdir / fname, "JPEG", quality=95)
            figs.append({"n": n, "file": str((fdir / fname).relative_to(ROOT)),
                         "x": fx, "y": fy, "w": fw, "h": fh})
            figs.sort(key=lambda f: int(f.get("n", 0)))
            it["figures"] = figs
            save_db(db)
            return {"ok": True, "n": n, "figures": figs}
    return JSONResponse({"ok": False, "msg": "不存在"}, status_code=404)


@app.patch("/api/item/{item_id}/figure/{n}")
def rebind_figure(item_id: str, n: int, payload: dict = None):
    """把已有图片改绑到另一个编号: {n: 新编号}。会同步改写题干/答案/解析里的 [图N] 引用。"""
    try:
        m = int((payload or {}).get("n") or 0)
    except (TypeError, ValueError):
        m = 0
    if m <= 0:
        return JSONResponse({"ok": False, "msg": "新编号无效"}, status_code=400)
    if m == n:
        return JSONResponse({"ok": False, "msg": "编号未变化"}, status_code=400)
    db = load_db()
    for it in db["items"]:
        if it["id"] == item_id:
            figs = list(it.get("figures") or [])
            f0 = next((f for f in figs if int(f.get("n", 0)) == n), None)
            if not f0:
                return JSONResponse({"ok": False, "msg": f"[图{n}] 不存在"}, status_code=404)
            if any(int(f.get("n", 0)) == m for f in figs):
                return JSONResponse({"ok": False, "msg": f"[图{m}] 已有图片，请先删除或改用上传覆盖"},
                                    status_code=400)
            old_p = ROOT / str(f0.get("file", ""))
            if old_p.exists():                             # 文件名同步带上新编号, 便于排查
                new_p = old_p.with_name(f"{item_id}_fig{m}{old_p.suffix}")
                try:
                    old_p.rename(new_p)
                    f0["file"] = str(new_p.relative_to(ROOT))
                except OSError:
                    pass
            f0["n"] = m
            figs.sort(key=lambda f: int(f.get("n", 0)))
            it["figures"] = figs
            for k in ("note", "answer", "analysis"):      # 两步换号, 避免新旧编号互相覆盖
                t = it.get(k) or ""
                if t:
                    t = re.sub(rf"\[图{n}(\D|$)", f"[图@@{m}@@\\1", t)
                    t = t.replace(f"[图@@{m}@@", f"[图{m}")
                    it[k] = t
            save_db(db)
            return {"ok": True, "figures": figs, "note": it.get("note", ""),
                    "answer": it.get("answer", ""), "analysis": it.get("analysis", "")}
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


@app.post("/api/items/batch")
def batch_items(payload: dict = None):
    """批量修改或删除题目(整批只写库一次, 便于「撤销」整体回退)。
    payload: {ids:[...], set:{chapter?,subject?,star?}, keywords_add?, delete?}"""
    payload = payload or {}
    ids = {str(x) for x in (payload.get("ids") or [])}
    if not ids:
        return JSONResponse({"ok": False, "msg": "没有选中题目"}, status_code=400)
    sets = payload.get("set") or {}
    kw_add = (payload.get("keywords_add") or "").strip()
    do_del = bool(payload.get("delete"))
    upd = dele = 0
    with DB_LOCK:
        db = load_db()
        keep = []
        for it in db["items"]:
            if it.get("id") not in ids:
                keep.append(it)
                continue
            if do_del:
                if it.get("image"):
                    to_trash(ROOT / it["image"])
                for fg in it.get("figures") or []:
                    to_trash(ROOT / str(fg.get("file", "")))
                dele += 1
                continue
            if "chapter" in sets:
                it["chapter"] = str(sets["chapter"] or "").strip()
            if "subject" in sets and str(sets["subject"]).strip():
                it["subject"] = str(sets["subject"]).strip()
            if "star" in sets:
                try:
                    it["star"] = max(0, min(5, int(sets["star"])))
                except (TypeError, ValueError):
                    pass
            if kw_add:
                cur = [k.strip() for k in (it.get("keywords") or "").replace("，", ",").split(",") if k.strip()]
                for k in kw_add.replace("，", ",").split(","):
                    k = k.strip()
                    if k and k not in cur:
                        cur.append(k)
                it["keywords"] = ",".join(cur)
            keep.append(it)
            upd += 1
        db["items"] = keep
        if upd or dele:
            save_db(db)
    log_ai("批量" + ("删除" if do_del else "修改"), "-", True, 0, f"{upd or dele} 题")
    return {"ok": True, "updated": upd, "deleted": dele}


@app.post("/api/undo")
def undo_last_write():
    """撤销上一次写库: 用 backups 里最新的一份覆盖当前库。
    覆盖前先把当前状态另存为 undo_point_*, 所以撤销本身也能再撤回来。"""
    # 注意: backup_db 用 copy2 会保留源文件 mtime, 故不能按 mtime 排序;
    # 备份名 library_YYYYmmdd_HHMMSS.json 里的时间戳才是可靠的先后顺序
    bks = sorted(BACKUP_DIR.glob("library_*.json"), key=lambda q: q.name)
    if not bks:
        return JSONResponse({"ok": False, "msg": "没有可回退的备份"}, status_code=400)
    last = bks[-1]
    with DB_LOCK:
        try:
            if DB_FILE.exists():
                shutil.copy2(DB_FILE, BACKUP_DIR / f"undo_point_{time.strftime('%Y%m%d_%H%M%S')}.json")
            shutil.copy2(last, DB_FILE)
        except Exception as e:
            return JSONResponse({"ok": False, "msg": "回退失败: " + str(e)[:120]}, status_code=500)
        db = load_db()
        pts = sorted(BACKUP_DIR.glob("undo_point_*.json"), key=lambda q: q.name)
        for q in pts[:-10]:
            q.unlink(missing_ok=True)
    log_ai("撤销", "-", True, 0, last.name)
    return {"ok": True, "msg": f"已回退到上一次修改前的状态（备份 {last.name}）",
            "count": len(db.get("items", []))}


@app.post("/api/item/{item_id}/figure/crop")
def crop_item_figure(item_id: str, payload: dict = None):
    """在题目图上按 0-1000 相对坐标当场裁出图块并存为附件(不再拖到渲染时裁)。
    返回 {n, figures}: n 为该图块编号(用于在正文里写 [图N])。
    payload: {x, y, w, h, replace?}  replace=某图块编号时覆盖它(用于手动微调)"""
    payload = payload or {}
    with DB_LOCK:
        db = load_db()
        it = next((x for x in db["items"] if x["id"] == item_id), None)
        if it is None or not it.get("image"):
            return JSONResponse({"ok": False, "msg": "题目不存在或无图片"}, status_code=404)
        src = ROOT / str(((it.get("source_page") or "") if payload.get("from_page") else it["image"]) or "")
        if not src.exists():
            return JSONResponse({"ok": False,
                                 "msg": "来源图片不存在（这道题没有记录整页原图）" if payload.get("from_page")
                                        else "图片文件不存在"}, status_code=404)
        img = np.array(open_photo(src))
        FH, FW = img.shape[:2]
        try:
            fx, fy = int(payload.get("x", 0)), int(payload.get("y", 0))
            fw, fh = int(payload.get("w", 0)), int(payload.get("h", 0))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "msg": "坐标无效"}, status_code=400)
        x0 = max(0, int(fx / 1000 * FW))
        y0 = max(0, int(fy / 1000 * FH))
        w0 = min(FW - x0, max(4, int(fw / 1000 * FW)))
        h0 = min(FH - y0, max(4, int(fh / 1000 * FH)))
        if x0 >= FW or y0 >= FH or w0 < 4 or h0 < 4:
            return JSONResponse({"ok": False, "msg": "框选区域超出图片范围"}, status_code=400)
        fig = trim_blank(img[y0:y0 + h0, x0:x0 + w0])
        if fig.shape[0] < 8 or fig.shape[1] < 8:
            fig = img[y0:y0 + h0, x0:x0 + w0]
        figs = list(it.get("figures") or [])
        try:
            rep_n = int(payload.get("replace") or 0)
        except (TypeError, ValueError):
            rep_n = 0
        old = next((f for f in figs if int(f.get("n", 0) or 0) == rep_n), None) if rep_n else None
        if old:                                     # 覆盖已有图块(手动微调)
            n = rep_n
            rel = old.get("file") or f"{it['image'][:-4]}_fig{n}.jpg"
            if not imwrite_u(ROOT / rel, cv2.cvtColor(fig, cv2.COLOR_RGB2BGR)):
                return JSONResponse({"ok": False, "msg": "图块写入失败"}, status_code=500)
            old.update({"n": n, "file": rel, "x": fx, "y": fy, "w": fw, "h": fh,
                        "t": int(time.time() * 1000)})
        else:                                       # 新增图块(bind>0 时绑定到指定编号 [图N])
            try:
                bind_n = int(payload.get("bind") or 0)
            except (TypeError, ValueError):
                bind_n = 0
            if bind_n > 0:
                ob = next((f for f in figs if int(f.get("n", 0) or 0) == bind_n), None)
                if ob:
                    p0 = ROOT / str(ob.get("file", ""))
                    if p0.exists():
                        p0.unlink(missing_ok=True)
                    figs.remove(ob)
                n = bind_n
            else:
                n = max([int(f.get("n", 0) or 0) for f in figs] + [0]) + 1
            rel = f"{it['image'][:-4]}_fig{n}.jpg"
            if not imwrite_u(ROOT / rel, cv2.cvtColor(fig, cv2.COLOR_RGB2BGR)):
                return JSONResponse({"ok": False, "msg": "图块写入失败"}, status_code=500)
            figs.append({"n": n, "file": rel, "x": fx, "y": fy, "w": fw, "h": fh,
                         "t": int(time.time() * 1000)})
        it["figures"] = figs
        save_db(db)
    log_ai("裁图块", "-", True, 0, f"{item_id} 第{n}块" + ("(覆盖)" if old else ""))
    return {"ok": True, "n": n, "figures": figs, "replaced": bool(old)}


@app.post("/api/item/{item_id}/recrop")
def recrop_item(item_id: str, payload: dict = None):
    """用"原图取景框"(缩略图坐标)重新裁剪题目图, 覆盖 it.image。
    用于核对/修正"裁剪结果与实际框选位置不一致"的题。payload: {x,y,w,h}"""
    payload = payload or {}
    with DB_LOCK:
        db = load_db()
        it = next((x for x in db["items"] if x["id"] == item_id), None)
        if it is None:
            return JSONResponse({"ok": False, "msg": "题目不存在"}, status_code=404)
        src_rel = it.get("source_page") or ""
        pid = Path(src_rel).stem if src_rel else ""
        srcs = [s for s in PAGES_DIR.glob(f"{pid}.*") if "_web" not in s.name] if pid else []
        if not srcs:
            return JSONResponse({"ok": False,
                                "msg": "找不到该题对应的整页照片，无法重新取景"}, status_code=404)
        r = page_ratio(pid) or 1.0
        img = open_photo(srcs[0])
        W, H = img.size
        try:
            x = int(payload.get("x", 0) * r); y = int(payload.get("y", 0) * r)
            w = int(payload.get("w", 0) * r); h = int(payload.get("h", 0) * r)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "msg": "坐标无效"}, status_code=400)
        x = max(0, min(x, W - 6)); y = max(0, min(y, H - 6))
        w = min(w, W - x); h = min(h, H - y)
        if w < 6 or h < 6:
            return JSONResponse({"ok": False, "msg": "框太小"}, status_code=400)
        sd = subj_dirname(it.get("subject") or "其他")
        d = ITEMS_DIR / sd
        d.mkdir(parents=True, exist_ok=True)
        name = f"{item_id}_recrop{int(time.time())}.jpg"
        img.crop((x, y, x + w, y + h)).save(d / name, "JPEG", quality=95)
        old_img = ROOT / it["image"] if it.get("image") else None
        it["image"] = f"items/{sd}/{name}"
        it["box"] = {k: int(payload.get(k, 0)) for k in ("x", "y", "w", "h")}
        it.pop("fig_issue", None)
        save_db(db)
    if old_img and old_img.exists():
        to_trash(old_img)                       # 旧裁剪图移入回收站(可恢复)
    log_ai("重新取景", "-", True, 0, f"{it['code']} box={it['box']}")
    return {"ok": True, "image": it["image"], "box": it["box"],
            "figures_kept": len(it.get("figures") or [])}


@app.get("/api/drafts")
def get_drafts():
    """所有页的框选草稿(每页最近一次)."""
    return {"ok": True, "drafts": load_drafts()}


@app.put("/api/draft/{page_id}")
def put_draft(page_id: str, payload: dict = None):
    """保存某页的框选草稿(框位置 + 已填写的题干/答案/大题等)。"""
    payload = payload or {}
    boxes = payload.get("boxes")
    if not isinstance(boxes, list):
        return JSONResponse({"ok": False, "msg": "boxes 必须是数组"}, status_code=400)
    with DB_LOCK:
        d = load_drafts()
        at = int(time.time() * 1000)          # 毫秒时间戳: 前端按数字比较, 避免时区/格式不一致导致旧草稿盖新草稿
        d[page_id] = {"boxes": boxes, "at": at,
                      "at_str": time.strftime("%Y-%m-%d %H:%M:%S"), "count": len(boxes)}
        put_draft_file(d)
    return {"ok": True, "page": page_id, "count": len(boxes), "at": at,
            "at_str": d[page_id]["at_str"]}


@app.delete("/api/draft/{page_id}")
def delete_draft(page_id: str):
    """清掉某页草稿(已保存到错题库后调用)。"""
    with DB_LOCK:
        d = load_drafts()
        d.pop(page_id, None)
        put_draft_file(d)
    return {"ok": True}


@app.get("/api/paper", response_class=HTMLResponse)
def make_paper(ids: str = ""):
    """ids: 逗号分隔, 顺序即试卷顺序。生成可打印的试卷页面。"""
    db = load_db()
    wanted = [i for i in ids.split(",") if i]
    items = [it for it in db["items"] if it["id"] in wanted]
    # 按 ids 中的顺序排列
    order = {iid: n for n, iid in enumerate(wanted)}
    items.sort(key=lambda it: order.get(it["id"], 999))
    grp_first = {}                                  # 续块组 -> 首块 id(卷面顺序)
    for it in items:
        g0 = it.get("group") or ""
        if g0 and g0 not in grp_first:
            grp_first[g0] = it["id"]
    cards, n = [], 0
    for it in items:
        g = it.get("group") or ""
        if g and grp_first.get(g) != it["id"]:     # 同一续块组: 不重新编号
            no = '<div class="qno cont">(续)</div>'
        else:
            n += 1
            no = f'<div class="qno">{n}.</div>'
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


# ---------- 数据仓库(类似 Obsidian 的仓库): 切换 / 新建 / 合并 ----------
# 编译成 exe 后, 数据(图片/题库/配置)就存在 exe 所在目录; 可再建多个仓库子目录切换。
BASE_DIR = ROOT                     # 程序所在目录: 仓库注册表、字体、Typst 包都放这里
VAULT_FILE = BASE_DIR / "vaults.json"
VAULTS_DIR = "vaults"
DEFAULT_VAULT_NAME = "默认仓库"


def _bind_data_paths(root):
    """把全部数据路径指向某个仓库目录(切换仓库时调用)。字体/Typst 包仍用程序目录。"""
    global ROOT, PAGES_DIR, ITEMS_DIR, DB_FILE, TMP_DIR, PREFIX_FILE, TPL_PATH, \
        AUTO_LAST_FILE, DRAFT_FILE, BACKUP_DIR, TRASH_DIR, SUBJ_FILE, _SUBJ_CACHE
    ROOT = Path(root)
    PAGES_DIR = ROOT / "pages"
    ITEMS_DIR = ROOT / "items"
    DB_FILE = ROOT / "library.json"
    TMP_DIR = ROOT / ".tmp"
    PREFIX_FILE = ROOT / "code_prefix.json"
    TPL_PATH = ROOT / "chapter_templates.json"
    AUTO_LAST_FILE = ROOT / "auto_last.json"
    DRAFT_FILE = ROOT / "drafts.json"
    BACKUP_DIR = ROOT / "backups"
    TRASH_DIR = ROOT / ".trash"
    SUBJ_FILE = ROOT / "subjects.json"
    _SUBJ_CACHE = None
    for d in (PAGES_DIR, ITEMS_DIR, TMP_DIR, BACKUP_DIR, TRASH_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _vaults():
    """返回 (当前仓库名, {仓库名: 相对程序目录的路径})。"""
    if VAULT_FILE.exists():
        try:
            d = json.loads(VAULT_FILE.read_text("utf-8"))
            lst = {str(k): str(v) for k, v in (d.get("list") or {}).items() if str(k).strip()}
            if lst:
                cur = str(d.get("current") or "")
                return (cur if cur in lst else next(iter(lst))), lst
        except Exception:
            pass
    return DEFAULT_VAULT_NAME, {DEFAULT_VAULT_NAME: "."}


def _save_vaults(cur, lst):
    VAULT_FILE.write_text(json.dumps({"current": cur, "list": lst},
                                     ensure_ascii=False, indent=2), "utf-8")


def _vault_root(name, lst=None):
    _, lst = (None, lst) if lst else _vaults()
    rel = lst.get(name)
    return (BASE_DIR / rel).resolve() if rel else None


def _discover_vaults():
    """把 vaults/ 目录下已有的子目录登记为仓库。"""
    cur, lst = _vaults()
    vdir = BASE_DIR / VAULTS_DIR
    changed = False
    if vdir.exists():
        for d in sorted(vdir.iterdir()):
            if d.is_dir() and d.name not in lst:
                lst[d.name] = f"{VAULTS_DIR}/{d.name}"
                changed = True
    if changed:
        _save_vaults(cur, lst)
    return cur, lst


@app.get("/api/vault")
def get_vault():
    """列出全部仓库(数据目录)与当前仓库、数据存放路径。"""
    cur, lst = _discover_vaults()
    out = []
    for name, rel in lst.items():
        r = (BASE_DIR / rel).resolve()
        cnt = 0
        try:
            if (r / "library.json").exists():
                cnt = len(json.loads((r / "library.json").read_text("utf-8")).get("items", []))
        except Exception:
            pass
        out.append({"name": name, "path": str(r), "items": cnt, "current": name == cur})
    return {"ok": True, "current": cur, "base": str(BASE_DIR),
            "path": str(_vault_root(cur, lst) or BASE_DIR), "vaults": out}


@app.post("/api/vault/switch")
def switch_vault(payload: dict = None):
    """切换当前仓库(数据目录)。"""
    name = str((payload or {}).get("name") or "").strip()
    cur, lst = _discover_vaults()
    if name not in lst:
        return JSONResponse({"ok": False, "msg": "仓库不存在"}, status_code=404)
    _bind_data_paths(_vault_root(name, lst))
    _save_vaults(name, lst)
    log_ai("切换仓库", "-", True, 0, name)
    return {"ok": True, "current": name, "path": str(_vault_root(name, lst))}


@app.post("/api/vault/create")
def create_vault(payload: dict = None):
    """新建仓库(空题库), 并切换过去。"""
    name = safe_name(str((payload or {}).get("name") or "").strip())
    if not name:
        return JSONResponse({"ok": False, "msg": "请输入仓库名"}, status_code=400)
    cur, lst = _vaults()
    if name in lst or (BASE_DIR / VAULTS_DIR / name).exists():
        return JSONResponse({"ok": False, "msg": "同名仓库已存在"}, status_code=400)
    d = BASE_DIR / VAULTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "library.json").write_text(json.dumps({"items": []}, ensure_ascii=False), "utf-8")
    lst[name] = f"{VAULTS_DIR}/{name}"
    _bind_data_paths(d)
    _save_vaults(name, lst)
    return {"ok": True, "current": name, "path": str(d)}


@app.post("/api/vault/rename")
def rename_vault(payload: dict = None):
    """给当前仓库改名(目录也会改名)。"""
    name = safe_name(str((payload or {}).get("new") or "").strip())
    cur, lst = _vaults()
    if not name or name == cur:
        return JSONResponse({"ok": False, "msg": "新名称无效"}, status_code=400)
    if name in lst:
        return JSONResponse({"ok": False, "msg": "同名仓库已存在"}, status_code=400)
    if lst.get(cur) == ".":
        return JSONResponse({"ok": False, "msg": "默认仓库就是程序目录，不能改名"}, status_code=400)
    src, dst = (BASE_DIR / lst[cur]).resolve(), BASE_DIR / VAULTS_DIR / name
    try:
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
    except OSError as e:
        return JSONResponse({"ok": False, "msg": f"改名失败: {e}"}, status_code=500)
    lst[name] = f"{VAULTS_DIR}/{name}"
    lst.pop(cur, None)
    _bind_data_paths(dst)
    _save_vaults(name, lst)
    return {"ok": True, "current": name, "path": str(dst)}


@app.post("/api/vault/merge")
def merge_vault(payload: dict = None):
    """把另一个仓库的题目合并进当前仓库(源仓库保留不动): 复制图片、重新编号、不覆盖现有题。"""
    name = str((payload or {}).get("from") or "").strip()
    cur, lst = _vaults()
    if name not in lst:
        return JSONResponse({"ok": False, "msg": "源仓库不存在"}, status_code=404)
    if name == cur:
        return JSONResponse({"ok": False, "msg": "不能合并到自身"}, status_code=400)
    src_root = _vault_root(name, lst)
    if not (src_root / "library.json").exists():
        return JSONResponse({"ok": False, "msg": "源仓库没有 library.json"}, status_code=400)
    try:
        sdb = json.loads((src_root / "library.json").read_text("utf-8"))
    except Exception as e:
        return JSONResponse({"ok": False, "msg": f"读取源仓库失败: {e}"}, status_code=500)
    added, missing = 0, 0
    with DB_LOCK:
        db = load_db()
        for i, it0 in enumerate(sdb.get("items", [])):
            it = dict(it0)
            subject = it.get("subject") or "未分类"
            sd = subj_dirname(subject)
            ddir = ITEMS_DIR / sd
            ddir.mkdir(parents=True, exist_ok=True)
            new_id = f"m{int(time.time() * 1000)}{i}"
            rel = str(it.get("image") or "")
            if rel and (src_root / rel).exists():
                suffix = Path(rel).suffix or ".jpg"
                new_rel = f"items/{sd}/{new_id}{suffix}"
                shutil.copy2(src_root / rel, ROOT / new_rel)
                it["image"] = new_rel
            else:
                it["image"] = ""
                missing += 1
            figs = []
            for f0 in (it.get("figures") or []):
                frel = str(f0.get("file") or "")
                if frel and (src_root / frel).exists():
                    suffix = Path(frel).suffix or ".jpg"
                    new_frel = f"items/{sd}/{new_id}_fig{f0.get('n', 0)}{suffix}"
                    shutil.copy2(src_root / frel, ROOT / new_frel)
                    nf = dict(f0)
                    nf["file"] = new_frel
                    figs.append(nf)
            it["figures"] = figs
            it["id"] = new_id
            it["code"] = next_code(db, subject)
            it["merged_from"] = name
            db["items"].append(it)
            added += 1
        if added:
            save_db(db)
    log_ai("仓库合并", "-", True, 0, f"{name} -> {cur}: {added} 题")
    return {"ok": True, "added": added, "missing": missing,
            "msg": f"已从「{name}」合并 {added} 道题（源仓库保留不动）"
                   + (f"，{missing} 道缺图片" if missing else "")}


_init_vault_cur, _init_vault_lst = _discover_vaults()
_bind_data_paths(_vault_root(_init_vault_cur, _init_vault_lst) or BASE_DIR)


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


def _bind_dual_stack(port=8091):
    """自己建一个双栈 socket(IPv6 + v4-mapped IPv4)。
    uvicorn 在 host="::" 时会强制 IPV6_V6ONLY=1, 导致 IPv4 访问不通, 故手工建 socket 交给它。"""
    import socket
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)   # 关键: 同时接受 IPv4
    except OSError:
        pass
    sock.bind(("::", port))
    sock.listen(2048)
    return sock


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("HOST", "")          # 需要只监听 IPv4 时设 HOST=0.0.0.0
    print("\n错题收集工具已启动：")
    print("  本机     http://localhost:8091")
    try:                                       # 局域网地址(手机同 WiFi 用这个)
        import socket as _sk
        _s = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        print(f"  手机/局域网 http://{_s.getsockname()[0]}:8091")
        _s.close()
    except OSError:
        pass
    try:                                       # 公网 IPv6 地址(需服务端与客户端都有 IPv6)
        _v6 = [a for a in _sk.getaddrinfo(_sk.gethostname(), None, _sk.AF_INET6)
               if not a[4][0].startswith("fe80")]
        if _v6:
            print(f"  公网 IPv6   http://[{_v6[0][4][0]}]:8091")
    except OSError:
        pass
    print()
    if host:
        uvicorn.run(app, host=host, port=8091, log_level="warning")
    else:
        try:
            srv = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
            srv.run(sockets=[_bind_dual_stack(8091)])
        except OSError as e:
            print(f"双栈监听失败({e})，回退到 IPv4")
            uvicorn.run(app, host="0.0.0.0", port=8091, log_level="warning")
