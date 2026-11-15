/* 报告图片 / OCR 文本解析的跨语言一致性测试。
   与 tests/fixtures/report_expected.json（Python 侧同一基准）逐字段比对，
   并验证 JS 生成的 FIT 与 Python CLI 产物逐字节一致。
   运行： node tests/test_report.js
*/
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "web", "index.html"), "utf8");
const match = html.match(/<script id="core">([\s\S]*?)<\/script>/);
if (!match) throw new Error("找不到 core 脚本块");
const moduleObj = { exports: {} };
new Function("module", "exports", "TextEncoder", match[1])(
  moduleObj, moduleObj.exports, TextEncoder
);
const core = moduleObj.exports;
const expected = JSON.parse(
  fs.readFileSync(path.join(root, "tests", "fixtures", "report_expected.json"), "utf8")
);
function assert(cond, msg) { if (!cond) throw new Error(msg); }
function near(a, b, tol, msg) {
  assert(a !== null && a !== undefined && b !== null && b !== undefined &&
         Math.abs(a - b) <= tol, msg + " 期望 " + b + "，实际 " + a);
}

/* ---- 场景 1：人工核对后的键值文本，方程必须全部自洽 ---- */
const text = fs.readFileSync(path.join(root, expected.source), "utf8");
const result = core.reportParseText(text);
assert(result.equationOk === expected.equation_ok, "方程校验结论不符");
assert(result.timestampText === expected.timestamp_text,
  "测量时间不符：" + result.timestampText);
near(result.inferredHeightCm, expected.inferred_height_cm, 0.05, "反推身高");
near(result.raw.weight_kg, expected.weight_kg, 0.001, "体重");
Object.keys(expected.resolved).forEach(key => {
  near(result.resolved[key], expected.resolved[key], 0.005, "字段 " + key);
});
const bad = result.checks.filter(c => c.status !== "ok");
assert(bad.length === 0, "存在未通过校验的字段：" + bad.map(c => c.label).join(","));
assert(result.warnings.length === 0, "不应有警告：" + result.warnings.join("|"));

/* ---- 场景 2：转成记录后，CSV / FIT 与 Python 逐字节一致 ---- */
const record = core.reportToRecord(result);
near(record.weight_kg, 87.3, 0.001, "记录体重");
near(record.muscle_kg, 33.8, 0.001, "记录骨骼肌量");
const csv = core.buildGarminCsv([record], "ymd");
assert(csv === expected.csv, "CSV 不符：\n" + JSON.stringify(csv));
const fit = Buffer.from(core.buildWeightFit([record]));
// 源记录里身体年龄 = 34，但默认不写入 FIT（Garmin 显示回归）
const withAge = Buffer.from(core.buildWeightFit([record], "skeletal", true));
assert(record.metabolic_age === 34, "报告里的身体年龄应为 34");
assert(withAge.length - fit.length === 4,
  "启用 metabolic_age 后 FIT 应多 4 字节，实际 " + (withAge.length - fit.length));
const outDir = path.join(root, "out", "report");
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, "js_garmin_weight.fit"), fit);
const pyFit = path.join(outDir, "garmin_weight.fit");
if (fs.existsSync(pyFit)) {
  assert(Buffer.compare(fit, fs.readFileSync(pyFit)) === 0,
    "FIT 与 Python 产物不一致（先跑 python -m sync_tool report ...）");
  console.log("  FIT 与 Python CLI 产物逐字节一致：" + fit.length + " 字节");
} else {
  console.log("  跳过 FIT 跨语言比对（尚未生成 " + pyFit + "）");
}

/* ---- 场景 3：原始 OCR 文本必须拒绝「假闭合」 ---- */
const ocrText = fs.readFileSync(path.join(root, expected.ocr_source), "utf8");
const ocrResult = core.reportParseText(ocrText);
assert(ocrResult.equationOk === expected.ocr_equation_ok,
  "原始 OCR 不应宣称方程通过");
assert(ocrResult.warnings.length > 0, "原始 OCR 应给出精度不足警告");
assert(ocrResult.checks.some(c => c.status !== "ok") === expected.ocr_needs_review,
  "原始 OCR 应标出待复核字段");

/* ---- 场景 4：网页里的人工修正能覆盖解析值 ---- */
const edited = core.reportToRecord(result, { weight_kg: 88.0, bone_kg: 3.3 });
assert(edited.weight_kg === 88.0, "体重修正未生效");
assert(edited.bone_kg === 3.3, "骨盐量修正未生效");
const draft = core.reportDraftText(result);
assert(draft.indexOf("体重 = 87.3") >= 0, "草稿文本缺少体重");

/* ---- 场景 5：草稿回流闭环，网页下载的草稿粘回去仍能通过方程校验 ---- */
["水分 = 39.94", "蛋白质 = 19.49", "脂肪量 = 24.62", "骨盐量 = 3.25"].forEach(line => {
  assert(draft.indexOf(line) >= 0, "草稿缺少四组分行：" + line);
});
const again = core.reportParseText(draft);
assert(again.equationOk === true, "草稿回流后方程应闭合");
assert(again.warnings.length === 0, "草稿回流不应有警告：" + again.warnings.join("|"));
near(again.resolved.fat_pct, 28.2, 0.05, "草稿回流后的脂肪率");

/* ---- 场景 6：修正四组分后，原始 OCR 草稿也能回流通过 ---- */
const ocrDraft = core.reportDraftText(ocrResult);
const fixedDraft = ocrDraft
  .replace(/^水分 = .*$/m, "水分 = 39.94")
  .replace(/^蛋白质 = .*$/m, "蛋白质 = 19.49")
  .replace(/^脂肪量 = .*$/m, "脂肪量 = 24.62")
  .replace(/^骨盐量 = .*$/m, "骨盐量 = 3.25");
const fixed = core.reportParseText(fixedDraft);
assert(fixed.equationOk === true, "修正四组分后 OCR 草稿回流应闭合");
assert(fixed.warnings.length === 0, "修正后的草稿不应有警告：" + fixed.warnings.join("|"));
assert(fixed.raw.bmi === undefined || fixed.raw.bmi === null,
  "草稿里 BMI 留空，不该被邻近行数字污染：" + fixed.raw.bmi);

console.log("报告解析测试通过");
console.log("  方程自洽 / 身高反推 / CSV 一致 / OCR 假闭合拦截 / 人工修正覆盖 / 草稿回流 全部通过");
