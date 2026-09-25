#!/usr/bin/env python3
"""后端逻辑回归测试（不需要起服务、不碰真实数据）

覆盖“曾经翻过车 / 容易再翻车”的点：
  - fig_size_args: 图片默认高度、指定宽/高、过高过宽都不超版心
  - /api/crop 图块: 题框内坐标 + 整页坐标(全局裁图) + 续块并题
  - /api/item/{id}/figure/crop: from_page(从整页原图裁)
  - paper_pdf: 真 Typst 编译（公式/图注/续块编号）
  - auto_plan: 指定题目每份必含、随机题跨份不重复
  - rename_global: 科目 / 大题(标签) / 关键字 / 编号前缀
  - vault: 新建 / 切换 / 合并（源仓库保留）

运行: .venv/bin/python tools/selfcheck/backend.test.py   （或 bash tools/selfcheck/run.sh）
所有测试都在临时仓库目录里做，绝不写用户数据。
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "crop-tool"))

import app as m  # noqa: E402

PASS, FAIL = 0, 0
TMPROOT = Path(tempfile.mkdtemp(prefix="selfcheck-"))
VAULT = TMPROOT / "vault"


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  \u2713 " + msg)
    else:
        FAIL += 1
        print("  \u2717 " + msg)


def eq(a, b, msg):
    ok(a == b, f"{msg}  → {a!r}")


def make_page(name="P1", w=600, h=900, blocks=()):
    """造一张整页图并在指定位置画黑色方块(当图形)。"""
    from PIL import Image
    img = Image.new("RGB", (w, h), (250, 250, 250))
    for (x0, y0, x1, y1) in blocks:
        for x in range(x0, x1):
            for y in range(y0, y1):
                img.putpixel((x, y), (20, 20, 20))
    d = m.PAGES_DIR
    d.mkdir(parents=True, exist_ok=True)
    img.save(d / f"{name}.jpg")


def dark_ratio(p):
    from PIL import Image
    im = Image.open(p).convert("L")
    px = list(im.getdata())
    return sum(1 for v in px if v < 200) / max(1, len(px))


# ---------------------------------------------------------------- 初始化
m.BASE_DIR = TMPROOT
m.VAULT_FILE = TMPROOT / "vaults.json"
m._bind_data_paths(VAULT)          # 数据全部落到临时目录

print("\n[1] fig_size_args: 图片尺寸/不超版心")
make_page("PX", 600, 900, [(100, 100, 400, 200)])          # 宽图 3:1
wide = str(m.PAGES_DIR / "PX.jpg")
src = m.ROOT / "items" / "t"; src.mkdir(parents=True, exist_ok=True)
from PIL import Image  # noqa: E402
Image.new("RGB", (400, 100)).save(src / "wide.jpg")
Image.new("RGB", (100, 400)).save(src / "tall.jpg")
a = m.fig_size_args(str(src / "wide.jpg"), "", 24)          # 默认高度 24% → 太宽 → width:100%
ok(a.startswith("width:"), "默认高度下过宽的图退回宽度限制：" + a)
eq(m.fig_size_args(str(src / "wide.jpg"), "60%", 24), "width: 60%", "指定宽度百分比原样输出")
h = m.fig_size_args(str(src / "tall.jpg"), "50%", 24)
ok(h.startswith("width: ") and float(h.split()[1][:-2]) <= 14.1, "竖图指定宽度过大 → 改按高度定尺寸：" + h)
ok("%" not in m.fig_size_args(str(src / "tall.jpg"), "", 24).split()[1],
   "默认高度算出的宽度用 cm（便于精确控制）")

print("\n[2] /api/crop：题框内坐标 + 整页坐标（全局裁图）")
make_page("P1", 600, 900, [(120, 300, 220, 380), (400, 600, 520, 700)])
r = asyncio.run(m.crop({"page": "P1", "boxes": [{
    "subject": "数学", "chapter": "一、选择题", "note": "题干 [图1] 图在框外 [图2]",
    "x": 40, "y": 200, "w": 300, "h": 300,
    "figures": [{"n": 1, "x": 250, "y": 330, "w": 340, "h": 270},      # 相对题图
                {"n": 2, "px": 660, "py": 660, "pw": 200, "ph": 110}]}]}))
eq(r["count"], 1, "保存 1 道题")
it = r["items"][0]
ok(len(it["figures"]) == 2, "两个图块都保存（框内 + 框外）")
ok("px" in it["figures"][1], "整页坐标的图块保留 px 标记")
for f in it["figures"]:
    fp = m.ROOT / f["file"]
    ok(fp.exists() and dark_ratio(fp) > 0.2, f"图块 [图{f['n']}] 内容非空白（确实裁到了方块）")

print("\n[3] /api/crop：续块并题（文字按块拼接 + 图号重排）")
r2 = asyncio.run(m.crop({"page": "P1", "boxes": [
    {"subject": "数学", "chapter": "三、解答题", "note": "第一块 [图1] 求 $x^2$ 的值", "group": "gx",
     "x": 40, "y": 200, "w": 300, "h": 300, "figures": [{"n": 1, "x": 250, "y": 330, "w": 340, "h": 270}]},
    {"subject": "数学", "chapter": "", "note": "A．选项一 [图1] B．选项二", "group": "gx",
     "x": 40, "y": 520, "w": 300, "h": 300, "figures": [{"n": 1, "px": 660, "py": 660, "pw": 200, "ph": 110}]},
] }))
eq(r2["count"], 1, "续块没有单独入库（只 1 条新题）")
eq(r2["merged"], 1, "合并了 1 个续块")
merged = r2["items"][0]
ok("选项一" in merged["note"], "续块的文字（选项）并入了同一道题：" + merged["note"].replace("\n", " | "))
ok("[图1]" in merged["note"] and "[图2]" in merged["note"], "图号重排不撞号")
nums = [f["n"] for f in merged["figures"]]
eq(sorted(nums), sorted(set(nums)), "图块编号不重复：" + str(nums))
ok(all((m.ROOT / f["file"]).exists() for f in merged["figures"]), "两个图块文件都落盘")

print("\n[4] 预览窗裁图 from_page（从整页原图裁）")
d = m.crop_item_figure(merged["id"], {"x": 620, "y": 620, "w": 260, "h": 150, "from_page": 1})
ok(isinstance(d, dict) and d.get("ok"), "从整页原图裁成功")
ok((m.ROOT / d["figures"][-1]["file"]).exists(), "图块文件落盘")

print("\n[5] paper_pdf：真 Typst 编译（续块显示 (续) / 图注 / 公式）")
res = m.paper_pdf(ids=merged["id"], attach="both", fig_height="24", title="自检卷")
ok(isinstance(res, dict) and res.get("ok"), "PDF 生成成功")
typ = sorted(m.TMP_DIR.glob("paper_*.typ"))[-1]
txt = typ.read_text(encoding="utf-8")
ok(typ.with_suffix(".pdf").exists() and typ.with_suffix(".pdf").stat().st_size > 2000, "PDF 文件非空")
ok("#mi(" in txt, "公式走 mitex 渲染")
ok("#image(" in txt, "图块按 [图N] 插入")

print("\n[6] auto_plan：指定题目每份必含 + 随机题不重复")
items = []
for i in range(1, 13):
    items.append({"id": f"a{i}", "code": f"MA{i:04d}", "chapter": "三、解答题",
                  "star": (i % 5) + 1, "keywords": "", "subject": "数学", "title": ""})
groups = [{"chapter": "三、解答题", "count": 4, "stars": {}}]
papers, diag = m.auto_plan(items, groups, subject="数学", papers=3, fixed_ids=["a1"])
ok(diag.get("ok"), "组卷成功：" + diag.get("msg", ""))
ok(all("a1" in [x["id"] for x in p] for p in papers), "指定题目 a1 出现在每一份里")
rest = [x["id"] for p in papers for x in p if x["id"] != "a1"]
eq(len(rest), len(set(rest)), "随机题跨份不重复（共 %d 道）" % len(rest))

print("\n[7] rename_global：四类改名")
r = m.rename_global({"kind": "chapter", "old": "三、解答题", "new": "三、解答题（改）"})
ok(isinstance(r, dict) and r.get("changed", 0) > 0, "大题(标签)改名：" + r["msg"])
r = m.rename_global({"kind": "keyword", "old": "无此关键字", "new": "x"})
ok(isinstance(r, dict) and r.get("changed") == 0, "关键字改名无匹配时 changed=0（不误伤）")
r = m.rename_global({"kind": "prefix", "subject": "数学", "new": "MATH"})
ok(isinstance(r, dict) and r.get("ok"), "编号前缀改名：" + r["msg"])
db = m.load_db()
ok(all(x["code"].startswith("MATH") for x in db["items"] if x.get("subject") == "数学"),
   "该科目所有题重新编号为 MATHxxxx")
r = m.rename_global({"kind": "subject", "old": "数学", "new": "数学A"})
ok(isinstance(r, dict) and r.get("ok"), "科目改名：" + r["msg"])
ok(any(x.get("subject") == "数学A" for x in m.load_db()["items"]), "题目科目已同步")

print("\n[8] vault：新建 / 切换 / 合并（源仓库保留）")
cur = m.get_vault()
ok(len(cur["vaults"]) >= 1, "仓库列表可读（当前：%s）" % cur["current"])
created = m.create_vault({"name": "临时仓库B"})
ok(isinstance(created, dict) and created.get("ok"), "新建仓库并切换")
m.switch_vault({"name": m.DEFAULT_VAULT_NAME})        # 切回默认(临时)仓库
m._bind_data_paths(VAULT)
Path(VAULT, "library.json").write_text(json.dumps({"items": [
    {"id": "z1", "code": "ZZ0001", "subject": "其他", "chapter": "一、选择题",
     "note": "合并来的题", "image": "", "figures": [], "keywords": "", "star": 1}]},
    ensure_ascii=False), encoding="utf-8")
before = len(m.load_db()["items"])
r = m.merge_vault({"from": "临时仓库B"})
after = len(m.load_db()["items"])
msg = r.get("msg") if isinstance(r, dict) else str(r)
ok(isinstance(r, dict) and r.get("ok"), "合并仓库：" + msg)
ok(r.get("added") == 0, "空仓库合并进来不新增题目")
ok((Path(VAULT).parent / "vaults" / "临时仓库B" / "library.json").exists(), "源仓库保留")


print("\n[9] AI 提示词必须是严格模式（禁止凭记忆补出图片里没有的内容）")
P = m.AI_PROMPT_STRICT
ok("不算识别结果" in P, "提示词明确说明：记得的真题内容不算识别结果")
ok("没有选项就绝对不要输出" in P, "提示词禁止输出图片里没有的选项行")
ok("截断处结束" in P, "提示词要求截断处结束、不补全")
ok("逐字找到" in P, "提示词要求输出前自查每行都能在图片里逐字找到")
ok(m.call_ai_vision.__doc__ is not None and "识别" in m.call_ai_vision.__doc__, "识别函数存在且用该提示词")

print("\n[10] 允许重复入库 + 小框不丢（用户要求：不限制框）")
before = len(m.load_db()["items"])
box = {"subject": "数学", "chapter": "一、选择题", "note": "重复入库测试",
       "x": 40, "y": 200, "w": 300, "h": 300}
r1 = asyncio.run(m.crop({"page": "P1", "boxes": [dict(box)]}))
r2 = asyncio.run(m.crop({"page": "P1", "boxes": [dict(box)]}))
eq(len(m.load_db()["items"]) - before, 2, "同一道题保存两次 = 入库两条（允许重复）")
codes = [it["code"] for it in (r1["items"] + r2["items"])]
eq(len(set(codes)), 2, "两条编号不同：" + str(codes))
tiny = {"subject": "数学", "chapter": "", "note": "小框", "x": 100, "y": 100, "w": 12, "h": 12}
rt = asyncio.run(m.crop({"page": "P1", "boxes": [tiny]}))
eq(rt["count"], 1, "很小的框也能保存（不再被 20px 门槛丢掉）")

print("\n[11] 不再自动插图标签 + 保存时自动识别(含续块)")
for _c, _want in [("题干 [图1] 继续", "题干 继续"),
                  ("题干【图1】继续", "题干继续"),
                  ("题干（图1）继续", "题干继续"),
                  ("题干(图 1)继续", "题干继续"),
                  ("题干 [图片] 继续", "题干 继续"),
                  ("题干 ![示意图](x.png) 继续", "题干 继续"),
                  ("题干 [图1|60%|left] 继续", "题干 继续")]:
    eq(m.strip_fig_marks(_c), _want, "AI 结果里的图标签会被清掉：" + _c)
ok("绝对不要输出任何形式的图块" in m.AI_PROMPT_STRICT, "识别提示词点名禁止所有形式的图标签")
_pf = __import__("inspect").getsource(m.ai_proofread)
ok("[图N] 及其位置保持不变" not in _pf and "不要新增任何图块标记" in _pf,
   "校对提示词不再教模型 [图N] 格式")
ok("插入 [图1]" not in m.AI_PROMPT_STRICT, "提示词里没有“插入 [图1]”这种指令")
# 用假识别函数跑保存流程：空文字的块(含续块)必须被自动识别并合并
(Path(m.BASE_DIR) / ".ai_config.json").write_text(
    '{"base_url":"http://x","model":"fake","key":"fake-key"}', encoding="utf-8")
real_cv = m.call_ai_vision
m.call_ai_vision = lambda img: "自动识别文字 [图1]"
try:
    rr = asyncio.run(m.crop({"page": "P1", "boxes": [
        {"subject": "数学", "chapter": "三、解答题", "note": "", "group": "gz",
         "x": 40, "y": 200, "w": 300, "h": 300, "figures": []},
        {"subject": "数学", "chapter": "", "note": "", "group": "gz",
         "x": 40, "y": 520, "w": 300, "h": 300, "figures": []},
    ]}))
finally:
    m.call_ai_vision = real_cv
item = rr["items"][0]
ok("自动识别文字" in (item.get("note") or ""), "保存时对空文字块自动识别：" + (item.get("note") or "").replace("\n", " | "))
eq((item.get("note") or "").count("自动识别文字"), 2, "首块与续块各识别了一次并合并成一道题")
ok("[图1]" not in (item.get("note") or ""), "自动识别结果里的 [图N] 已被清掉")
eq(rr["merged"], 1, "续块合并计数正确")

print("\n[12] 重复录入不得并进老题目（批次隔离）")
before = len(m.load_db()["items"])
same = {"subject": "数学", "chapter": "三、解答题", "note": "题干", "group": "gdup",
        "x": 40, "y": 200, "w": 300, "h": 300, "figures": []}
cont = {"subject": "数学", "chapter": "", "note": "选项", "group": "gdup",
        "x": 40, "y": 520, "w": 300, "h": 300, "figures": []}
r1 = asyncio.run(m.crop({"page": "P1", "batch": "BATCH-1", "boxes": [dict(same), dict(cont)]}))
r2 = asyncio.run(m.crop({"page": "P1", "batch": "BATCH-2", "boxes": [dict(same), dict(cont)]}))
eq(r2["count"], 1, "第二次保存仍然新建了一条题目")
eq(r2["merged"], 1, "续块并进的是本次新建的首块")
eq(len(m.load_db()["items"]) - before, 2, "两次保存 = 两条题目（不是并进同一条）")
ok("选项" in (r2["items"][0].get("note") or ""), "第二条里带上续块文字")
ok((r1["items"][0].get("note") or "").count("选项") == 1, "第一条没有被第二次保存重复追加")
# 同一批次跨请求(跨页): 续块仍然并入同批次首块
r3 = asyncio.run(m.crop({"page": "P1", "batch": "BATCH-3", "boxes": [dict(same)]}))
r4 = asyncio.run(m.crop({"page": "P1", "batch": "BATCH-3", "boxes": [dict(cont)]}))
eq(r4["count"], 0, "同批次第二页的续块不再新建题目")
eq(r4["merged"], 1, "同批次跨请求仍能并入首块")

print("\n[13] 续块没文字也没裁图 -> 整块丢弃（不生成 [图N]）")
# 模拟“只有图没有文字”的块：模型会返回 '【题干】' 这类噪声（用假识别函数，避免真实调用）
(Path(m.BASE_DIR) / ".ai_config.json").write_text(
    '{"base_url":"http://x","model":"fake","key":"fake-key"}', encoding="utf-8")
real_cv2 = m.call_ai_vision
m.call_ai_vision = lambda img: "【题干】"
try:
    before = len(m.load_db()["items"])
    rd = asyncio.run(m.crop({"page": "P1", "batch": "BATCH-D", "boxes": [
        {"subject": "数学", "chapter": "三、解答题", "note": "只有题干", "group": "gdrop",
         "x": 40, "y": 200, "w": 300, "h": 300, "figures": []},
        {"subject": "数学", "chapter": "", "note": "", "group": "gdrop",
         "x": 40, "y": 520, "w": 300, "h": 300, "figures": []},
    ]}))
finally:
    m.call_ai_vision = real_cv2
eq(rd["count"], 1, "只入库 1 条题目")
eq(rd.get("discarded"), 1, "空续块（只有噪声文字）被计入丢弃")
item = rd["items"][0]
eq((item.get("note") or "").strip(), "只有题干", "首块题干没被塞进 [图N] 或噪声文字")
eq([f["n"] for f in item["figures"]], [], "没有为丢掉的续块生成图块")
eq(len(m.load_db()["items"]) - before, 1, "库里只多了一条")
eq(m.real_text("【题干】"), "", "real_text: 纯噪声 = 没文字")
eq(m.real_text("[图1]  "), "", "real_text: 只有图标签 = 没文字")
ok(m.real_text("A．选项内容") != "", "real_text: 真文字能识别出来")

print("\n[14] 框选页上传的图片能正常入库（复制而非裁剪）")
from PIL import Image as _Img
(Path(m.UPLOADS_DIR)).mkdir(parents=True, exist_ok=True)
up = Path(m.UPLOADS_DIR) / "up_selftest.png"
_Img.new("RGB", (120, 80), (10, 120, 200)).save(up)
ru = asyncio.run(m.crop({"page": "P1", "batch": "BATCH-UP", "boxes": [
    {"subject": "数学", "chapter": "一、选择题", "note": "带上传图 [图1]", "group": "",
     "x": 40, "y": 200, "w": 300, "h": 300,
     "figures": [{"n": 1, "file": "uploads/" + up.name, "upload": True}]},
]}))
eq(ru["count"], 1, "入库 1 条")
uit = ru["items"][0]
fig_files = [f["file"] for f in uit["figures"]]
eq(len(fig_files), 1, "上传的图成为该题的图块")
ok(fig_files and fig_files[0].startswith("items/"), "图块复制到了 items/ 下：" + str(fig_files))
ok((m.ROOT / fig_files[0]).exists(), "图块文件存在")
ok(uit["figures"][0].get("upload") is True, "保留 upload 标记")
ok(not up.exists(), "临时上传文件已删除")

print("\n[15] Windows 路径安全：给 Typst 的路径不许有反斜杠")
eq(m.posix(r"C:\Users\me\HomeWorkCollection\items\math\a.jpg"),
   "C:/Users/me/HomeWorkCollection/items/math/a.jpg", "posix: 反斜杠 -> 正斜杠")
eq(m.typ_img(r'C:\a\x".jpg'), 'C:/a/x\\".jpg', "typ_img: 正斜杠 + 转义双引号")
import inspect as _ins
_src = _ins.getsource(m.paper_pdf) + _ins.getsource(m.render_simple)
eq(_src.count("#image("), _src.count("typ_img("), "每个 #image( 都走 typ_img()（防漏改）")
# 抓一次真实编译参数, 确认没有反斜杠
_w = asyncio.run(m.crop({"page": "P1", "batch": "BATCH-W", "boxes": [
    {"subject": "数学", "chapter": "一、选择题", "note": "Windows 路径测试",
     "x": 40, "y": 200, "w": 300, "h": 300, "figures": []}]}))
_wid = _w["items"][0]["id"]
_real_compile = m.typst.compile
_cap = {}
def _fake(inp, output=None, **kw):
    _cap.update({"in": inp, "out": output, **kw})
m.typst.compile = _fake
try:
    _r = m.paper_pdf(ids=_wid, fig_height="24")
finally:
    m.typst.compile = _real_compile
ok(isinstance(_r, dict) and _r.get("ok"), "抓参数时 PDF 流程正常返回")
for _k in ("in", "out"):
    ok("\\" not in str(_cap.get(_k, "")), f"编译参数 {_k} 无反斜杠：{_cap.get(_k)}")
ok(all("\\" not in p for p in (_cap.get("font_paths") or [])), "font_paths 无反斜杠")
ok("\\" not in str(_cap.get("package_path") or ""), "package_path 无反斜杠")

# ---------------------------------------------------------------- 收尾
shutil.rmtree(TMPROOT, ignore_errors=True)
print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
