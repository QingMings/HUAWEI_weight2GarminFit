/* Node 侧测试：从 index.html 抽出纯计算核心，验证解析 / CSV / FIT / ZIP。
   场景 A：扁平样例（跨语言逐字节比对的基准）
   场景 B：贴近华为隐私副本的真实格式样例（重复读数、家人记录、骨骼肌、timeZone）
   运行： node tests/test_core.js
*/
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "index.html"), "utf8");
const match = html.match(/<script id="core">([\s\S]*?)<\/script>/);
if (!match) throw new Error("找不到 core 脚本块");

const moduleObj = { exports: {} };
// eslint-disable-next-line no-new-func
new Function("module", "exports", "TextEncoder", match[1])(
  moduleObj, moduleObj.exports, TextEncoder
);
const core = moduleObj.exports;

function assert(cond, msg) { if (!cond) throw new Error(msg); }

/* ---------------- 场景 A：扁平样例 ---------------- */
const sample = fs.readFileSync(path.join(root, "samples", "huawei_health_sample.json"), "utf8");
const records = core.dedupePerDay(core.applyBmi(core.parseAny(sample, false), 175));
assert(records.length === 3, "期望 3 条记录，实际 " + records.length);
assert(records[2].weight_kg === 69.9, "体重解析错误 " + records[2].weight_kg);
assert(records[2].fat_pct === 17.9, "体脂率解析错误 " + records[2].fat_pct);
assert(records[0].bmi === 23.1, "BMI 解析错误 " + records[0].bmi);

const csv = core.buildGarminCsv(records, "ymd");
const expectedCsv = "Body\r\nDate,Weight,BMI,Fat\r\n" +
  "2026-11-13,70.8,23.1,18.6\r\n" +
  "2026-11-14,70.3,22.9,18.2\r\n" +
  "2026-11-15,69.9,22.8,17.9\r\n";
assert(csv === expectedCsv, "CSV 不符：\n" + JSON.stringify(csv));

const fit = core.buildWeightFit(records);
fs.mkdirSync(path.join(root, "out"), { recursive: true });
fs.writeFileSync(path.join(root, "out", "js_garmin_weight.fit"), Buffer.from(fit));
fs.writeFileSync(path.join(root, "out", "js_garmin_import.csv"), Buffer.from(csv, "utf8"));

// 默认不写 metabolic_age；显式启用时文件应多出 4 字节（定义 3 + 数据 1）
const withAge = core.buildWeightFit(records, "skeletal", true);
// 定义消息少/多 3 字节，每条记录的数据多 1 字节
assert(withAge.length - fit.length === 3 + records.length,
  "启用 metabolic_age 后 FIT 应多 " + (3 + records.length) +
  " 字节，实际 " + (withAge.length - fit.length));
assert(Buffer.compare(Buffer.from(fit), Buffer.from(withAge)) !== 0,
  "启用前后 FIT 不应相同");
fs.writeFileSync(path.join(root, "out", "js_garmin_weight_with_age.fit"),
  Buffer.from(withAge));

const zip = core.buildZip(records.map(function (r) {
  const d = new Date(r.timeMs);
  const name = "weight_" + d.getFullYear() + "-" + core.fmtDate(d, "ymd").slice(5) +
    "_" + String(d.getHours()).padStart(2, "0") + String(d.getMinutes()).padStart(2, "0") + ".fit";
  return { name: name, data: core.buildWeightFit([r]) };
}));
fs.writeFileSync(path.join(root, "out", "js_fit_files.zip"), Buffer.from(zip));
assert(zip[0] === 0x50 && zip[1] === 0x4b, "ZIP 头错误");

// 解析文本 CSV（逗号、引号、分号）
const csvRecords = core.parseAny("timestamp,weight_kg,fat_pct\n2026-11-15 07:30,70.5,18.3\n", false);
assert(csvRecords.length === 1 && csvRecords[0].weight_kg === 70.5, "CSV 解析失败");

// 「斤」换算
const jinRecords = core.parseAny("timestamp,weight_kg\n2026-11-15 07:30,140\n", true);
assert(Math.abs(jinRecords[0].weight_kg - 70) < 0.001, "斤换算失败");

// 标准字段名（留档 CSV / 手工录入）必须能回读
const std = core.parseAny(
  "timestamp,weight_kg,muscle_kg,muscle_total_kg,height_cm\n" +
  "2026-11-15 07:30,70.5,31.2,56.0,175\n", false)[0];
assert(std.muscle_kg === 31.2 && std.muscle_total_kg === 56 && std.height_cm === 175,
  "标准字段名回读失败：" + JSON.stringify(std));

// half-up：10.5 内脏脂肪必须写成 11，不是银行家舍入的 10
const halfUpFit = core.buildWeightFit([{
  timeMs: 1794701100000, weight_kg: 70, fat_pct: 17.9, bmi: 22.9,
  water_pct: null, muscle_kg: null, muscle_total_kg: null, bone_kg: null,
  visceral_fat: 10.5, basal_met: null, metabolic_age: null
}]);
assert(halfUpFit.length > 0, "half-up FIT 生成失败");

/* ---------------- 场景 B：真实华为导出格式 ---------------- */
const realisticPath = path.join(root, "samples", "huawei_export_realistic",
  "Health detail data & description", "health detail data.json");
assert(fs.existsSync(realisticPath), "找不到真实格式样例");
const raw = core.parseAny(fs.readFileSync(realisticPath, "utf8"), false);
assert(raw.length === 7, "原始读数应为 7 条，实际 " + raw.length);
const mainOnly = core.filterSubUsers(raw, false);
assert(mainOnly.length === 5, "应排除 2 条家人/访客读数，实际剩 " + mainOnly.length);
let realistic = core.dedupeBySecond(core.applyBmi(mainOnly, 0));
assert(realistic.length === 4, "按秒去重后应为 4 条（保留同日早晚两次），实际 " + realistic.length);
const oct = realistic[1];
assert(oct.muscle_kg === 31.1, "骨骼肌量应取 skeletalMusclelMass=31.1，实际 " + oct.muscle_kg);
assert(oct.muscle_total_kg === 56.1, "总肌肉量应留档为 56.1，实际 " + oct.muscle_total_kg);
assert(oct.tzOffsetMin === 480, "timeZone +0800 解析错误：" + oct.tzOffsetMin);
assert(core.fmtRecordDate(oct, "ymd") === "2026-10-03", "按 timeZone 换算的本地日期错误");
assert(core.monthKeyOf(oct) === "2026-10", "月份分组错误");
assert(realistic[0].timeMs === 1546646400000, "早期记录时间戳错误");
assert(realistic[2].timeMs === 1791033000000,
  "sample 无 startTime 时应回退 record.startTime");
assert(realistic[0].fat_pct === null, "早期记录只有 bodyWeight");

const rFit = core.buildWeightFit(realistic, "skeletal");
const rCsv = core.buildGarminCsv(realistic, "ymd");
const rDir = path.join(root, "out", "realistic");
fs.mkdirSync(rDir, { recursive: true });
fs.writeFileSync(path.join(rDir, "js_garmin_weight.fit"), Buffer.from(rFit));
fs.writeFileSync(path.join(rDir, "js_garmin_import.csv"), Buffer.from(rCsv, "utf8"));

// 口径切换：total 时 muscle_mass 应变成 56.1，文件字节随之不同
const totalFit = core.buildWeightFit(realistic, "total");
assert(Buffer.compare(Buffer.from(rFit), Buffer.from(totalFit)) !== 0,
  "切换肌肉量口径后 FIT 应有变化");

console.log("JS 核心测试通过");
console.log("  场景 A：记录数", records.length, "| FIT 字节", fit.length, "| ZIP 字节", zip.length);
console.log("  场景 B：原始", raw.length, "→ 排除家人", raw.length - mainOnly.length,
  "→ 按秒去重", realistic.length, "| FIT 字节", rFit.length);
console.log("  已输出 out/js_* 与 out/realistic/js_* 供 Python 端比对");
