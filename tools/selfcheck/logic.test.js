/**
 * 前端纯逻辑回归测试（不需要浏览器）
 *   从 static/index.html 里抽取纯函数，用最小 stub 跑断言。
 *   覆盖的都是“曾经翻过车”的地方：
 *     - groupInfo: 续块/首块题号、跨页续块
 *     - unlinkCont: 取消续块 = 并回首块（不得变成新题目）
 *     - _ts + loadBoxes: 草稿时间戳必须是数字比较（旧字符串不能盖新状态）
 *     - markSaved: 已入库回标（按坐标匹配，避免重复入库）
 *
 * 运行: node tools/selfcheck/logic.test.js      （或 bash tools/selfcheck/run.sh）
 */
const fs = require("fs");
const path = require("path");

const HTML = path.join(__dirname, "..", "crop-tool", "static", "index.html");
const src = fs.readFileSync(HTML, "utf8");
const script = src.match(/<script>([\s\S]*?)<\/script>/g).pop();

function grab(name) {                        // 按大括号配对抽取函数源码
  const start = script.indexOf("function " + name + "(");
  if (start < 0) throw new Error("index.html 里找不到 function " + name);
  let i = script.indexOf("{", start), depth = 0;
  for (let j = i; j < script.length; j++) {
    if (script[j] === "{") depth++;
    else if (script[j] === "}") { depth--; if (!depth) return script.slice(start, j + 1); }
  }
  throw new Error("括号不配对: " + name);
}

// ---------- stubs（与被测函数交互的全局量） ----------
let pages = [], curPage = "P1", DB = {};
const allPageBoxes = () => pages.map(p => ({ pid: p.page, boxes: DB[p.page] })).filter(g => g.boxes && g.boxes.length);
function loadBoxesStub(pid) { return (DB[pid] || []).map(b => Object.assign({}, b)); }
function saveBoxesFor(pid, list) { DB[pid] = list; }
let boxes = [], pushed = 0;
function snap() { return "SNAP"; }
function pushHistory() { pushed++; }
function renderBoxList() { }
function renderCanvas() { }
function toast() { }
function pageNo(pid) { return pid; }
function esc(s) { return String(s == null ? "" : s); }
function boxMissingFigs() { return []; }
function kwList(s) { return String(s || "").split(/[,，、;；\s]+/).filter(Boolean); }
const serverDrafts = {};
const _store = {};
const localStorage = {
  getItem: k => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
  removeItem: k => { delete _store[k]; },
};

// ---------- 抽取被测函数 ----------
// 真 loadBoxes（时间戳测试用）重命名为 loadBoxesReal；其余场景用 stub，避免依赖 localStorage 初值
eval(grab("_ts"));
eval(grab("loadBoxes").replace("function loadBoxes(", "function loadBoxesReal("));
let useRealLoadBoxes = false;
function loadBoxes(pid) { return useRealLoadBoxes ? loadBoxesReal(pid) : loadBoxesStub(pid); }
eval(grab("groupInfo"));          // 用到 allPageBoxes
eval(grab("boxSummary"));
eval(grab("markSaved"));
eval(grab("unlinkCont"));         // 用到 groupInfo/loadBoxes/saveBoxesFor/snap/pushHistory/kwList

// ---------- 断言工具 ----------
let pass = 0, fail = 0;
function ok(cond, msg) {
  if (cond) { pass++; console.log("  ✓ " + msg); }
  else { fail++; console.log("  ✗ " + msg); }
}
function eq(a, b, msg) { ok(JSON.stringify(a) === JSON.stringify(b), msg + "  → " + JSON.stringify(a)); }

// =========================================================
console.log("\n[1] groupInfo: 续块题号 / 跨页续块");
pages = [{ page: "P1" }, { page: "P2" }];
DB = {
  P1: [{ id: "a", group: "a" }, { id: "b", group: "a" }, { id: "c", group: "" }],
  P2: [{ id: "d", group: "a" }, { id: "e", group: "e" }],
};
let gi = groupInfo();
eq(gi.questions, 3, "题数=3（a、c、e）");
eq(gi.qnoOf.a, 1, "首块 a 是第 1 题");
eq(gi.qnoOf.b, 1, "续块 b 沿用首块题号 1（不能是 undefined/0）");
eq(gi.qnoOf.d, 1, "跨页续块 d 也沿用 1");
eq([gi.kOf.b, gi.kOf.d], [1, 2], "续块序号 1、2");
eq(gi.qnoOf.c, 2, "独立题 c 是第 2 题");
eq(gi.qnoOf.e, 3, "另一组首块 e 是第 3 题");

// =========================================================
console.log("\n[2] unlinkCont: 移除续块（不得并入文字、不得变成新题）");
DB = {
  P1: [{ id: "h", group: "h", chapter: "", star: 1, note: "A [图1]", answer: "ansA", analysis: "",
         keywords: "力", figures: [{ n: 1, file: "f1.jpg" }] },
       { id: "c", group: "h", chapter: "三、解答题", star: 3, note: "B [图3]", answer: "",
         analysis: "", keywords: "电,力", figures: [{ n: 3, file: "f3.jpg" }] }],
  P2: [],
};
pages = [{ page: "P1" }];
curPage = "P1"; boxes = loadBoxesStub("P1"); pushed = 0;
unlinkCont("P1", "c");
eq(DB.P1.map(b => b.id), ["h"], "草稿里只剩首块（续块没有变成新题目）");
eq(DB.P1[0].note, "A [图1]", "首块题干没有被续块文字污染（上次报的 bug）");
eq(DB.P1[0].answer, "ansA", "首块答案不被续块改动");
eq(DB.P1[0].keywords, "力", "关键字不取续块的");
eq(DB.P1[0].star, 1, "星级不取续块的");
eq(DB.P1[0].figures.map(f => f.n), [1, 2], "已裁的图块并回首块且不重号");
ok(pushed === 1, "压入撤销栈（可 Ctrl+Z 撤销）");

console.log("\n[3] unlinkCont: 跳页续块 → 只搬图块，不动文字");
DB = {
  P1: [{ id: "h", group: "h", note: "甲", keywords: "", figures: [{ n: 1 }] }],
  P2: [{ id: "c", group: "h", note: "乙 [图5]", keywords: "", figures: [{ n: 5, file: "x.jpg" }] }],
};
pages = [{ page: "P1" }, { page: "P2" }];
curPage = "P1"; boxes = loadBoxesStub("P1");
unlinkCont("P2", "c");
eq(DB.P1[0].note, "甲", "首块（另一页）文字不被改动");
eq(DB.P1[0].figures.map(f => f.n), [1, 2], "图块并回首块（重编号）");
eq(DB.P2.length, 0, "续块页草稿被清掉");

// =========================================================
console.log("\n[4] 草稿时间戳: 旧字符串时间不能盖掉新的数字时间");
useRealLoadBoxes = true;
for (const k of Object.keys(_store)) delete _store[k];      // 清掉前面测试留下的 localStorage
for (const k of Object.keys(serverDrafts)) delete serverDrafts[k];
pages = []; DB = {};
// 本地是较新的毫秒时间戳，服务器是更早的本地时间字符串
localStorage.setItem("boxes_P1", JSON.stringify([{ id: "new", x: 1, y: 1, w: 10, h: 10 }]));
localStorage.setItem("boxes_at_P1", String(Date.now()));
serverDrafts.P1 = { boxes: [{ id: "old", x: 0, y: 0, w: 10, h: 10 }], at: "2020-01-01 00:00:00" };
eq(loadBoxes("P1").map(b => b.id), ["new"], "本地更新 → 用本地（旧服务端草稿不覆盖）");
// 反过来：服务器明确更新（毫秒数字更大）→ 应该采用服务器草稿
localStorage.setItem("boxes_at_P1", String(Date.now() - 60000));
serverDrafts.P1 = { boxes: [{ id: "srv", x: 0, y: 0, w: 10, h: 10 }], at: Date.now() };
eq(loadBoxes("P1").map(b => b.id), ["srv"], "服务端更新 → 用服务端（换设备/清缓存能恢复）");
// 本地为空 → 用服务器
localStorage.removeItem("boxes_P1"); localStorage.removeItem("boxes_at_P1");
eq(loadBoxes("P1").map(b => b.id), ["srv"], "本地为空 → 从服务器草稿恢复");

// =========================================================
console.log("\n[5] markSaved: 允许重复入库（记录编号与次数）");
useRealLoadBoxes = false;
const pend = [{ id: "a", x: 40, y: 200, w: 300, h: 300 }, { id: "b", x: 40, y: 520, w: 300, h: 300 }];
markSaved(pend, [{ code: "MA0001", box: { x: 40, y: 200, w: 300, h: 300 } }]);
eq(pend[0].savedCode, "MA0001", "记录入库编号");
eq(pend[0].savedTimes, 1, "第一次入库");
ok(!pend[1].savedCode, "另一块不受影响");
markSaved(pend, [{ code: "MA0002", box: { x: 40, y: 200, w: 300, h: 300 } }]);
eq(pend[0].savedCode, "MA0002", "重复入库后记录最新编号（不再跳过）");
eq(pend[0].savedTimes, 2, "入库次数累加（允许重复）");

console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
process.exit(fail ? 1 : 0);
