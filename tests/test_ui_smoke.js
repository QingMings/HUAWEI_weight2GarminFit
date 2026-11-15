/* 用最小 DOM 桩执行页面脚本，验证 UI 初始化与四个导出按钮无运行时错误。
   运行： node tests/test_ui_smoke.js
*/
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "index.html"), "utf8");
const blocks = [...html.matchAll(/<script(?: id="core")?>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]);
if (blocks.length !== 2) throw new Error("应有两段脚本");

function makeEl(id) {
  const el = {
    id, value: "", checked: true, className: "", textContent: "", innerHTML: "",
    href: "", style: {}, dataset: {}, _h: {}, _list: null,
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener(ev, fn) { this._h[ev] = fn; },
    click() { if (this._h.click) this._h.click(); },
    querySelector() { return this._tbody || (this._tbody = makeEl("tbody")); },
    querySelectorAll() { return []; },
    appendChild() {}, remove() {}, setAttribute() {}, getAttribute() { return null; },
  };
  return el;
}
const els = {};
function get(id) { return els[id] || (els[id] = makeEl(id)); }
const tabs = [makeEl("t1"), makeEl("t2"), makeEl("t3"), makeEl("t4")];
const panes = [makeEl("p1"), makeEl("p2"), makeEl("p3"), makeEl("p4")];
let picks = [];

const document = {
  getElementById: get,
  querySelectorAll(sel) {
    if (sel === ".tab") return tabs;
    if (sel === ".pane") return panes;
    if (sel === ".pick") return picks;
    return [];
  },
  createElement() { return makeEl("a"); },
  body: { appendChild() {} },
};
const downloads = [];
const sandbox = {
  document, console, TextEncoder, Blob, Date, Math, JSON, Object, Array,
  String, Number, RegExp, isNaN, isFinite, parseFloat, parseInt, setTimeout,
  URL: {
    createObjectURL(blob) { downloads.push(blob); return "blob:mock"; },
    revokeObjectURL() {},
  },
  module: undefined,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(blocks[0], sandbox, { filename: "core.js" });
vm.runInContext(blocks[1], sandbox, { filename: "ui.js" });

// 复现页面默认选项（DOM 桩不会读 HTML 里的 selected/checked）
get("height").value = "175";
get("dedupeMode").value = "second";
get("muscleMode").value = "skeletal";
get("subUsers").checked = false;
get("future").checked = true;
get("metabolicAge").checked = false;   // 与 HTML 默认一致：不写 metabolic_age
get("dateFmt").value = "ymd";

// 载入内置示例
get("loadSample")._h.click();
const previewBody = get("preview").querySelector("tbody");
if (!previewBody._list || previewBody._list.length !== 3)
  throw new Error("示例数据没有渲染出 3 行");
picks = previewBody._list.map(() => ({ checked: true }));

// 依次触发所有导出
["csvBtn", "fitBtn", "zipBtn", "monthZipBtn", "backupBtn"].forEach(id => {
  if (!get(id)._h.click) throw new Error(id + " 没有绑定点击事件");
  get(id)._h.click();
});
if (downloads.length !== 5) throw new Error("期望 5 次下载，实际 " + downloads.length);

// 切换日期格式 / 区域 / 手工录入
get("dateFmt").value = "dmy";
get("dateFmt")._h.change();
get("region").value = "global";
get("region")._h.change();
if (get("importLink").href.indexOf("connect.garmin.com") < 0)
  throw new Error("区域链接未切换");

get("mWeight").value = "70.5";
get("mTime").value = "2026-11-15T07:30";
get("mFat").value = "18.3";
get("addManual")._h.click();

// 报告图片 tab：载入示例 → 解析 → 确认加入 → 下载草稿
get("loadReportSample")._h.click();
get("parseReport")._h.click();
if (get("reportMsg").textContent.indexOf("方程校验：通过") < 0)
  throw new Error("报告解析没有通过方程校验：" + get("reportMsg").textContent);
if (get("reportResult").innerHTML.indexOf("data-rkey") < 0)
  throw new Error("报告核对区没有渲染出可编辑字段");
get("addReport")._h.click();
get("downloadDraft")._h.click();
const reportBody = get("preview").querySelector("tbody");
// 累计 5 条，但手工录入与示例最后一条同为 11-15 07:30，按秒去重后预览应为 4 行。
if (get("inputMsg").textContent.indexOf("累计 5 条") < 0)
  throw new Error("报告记录没有加入：" + get("inputMsg").textContent);
if (!reportBody._list || reportBody._list.length !== 4)
  throw new Error("去重后的预览行数应为 4，实际 " + (reportBody._list || []).length);
if (downloads.length !== 6) throw new Error("期望 6 次下载，实际 " + downloads.length);

console.log("UI 冒烟测试通过");
console.log("  初始化 / 示例载入 / 5 个导出 / 设置切换 / 手工录入 / 报告快速录入 均无异常");
console.log("  触发下载次数:", downloads.length);
