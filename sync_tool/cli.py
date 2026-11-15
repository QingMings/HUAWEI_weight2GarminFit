"""命令行入口： python -m sync_tool <command> [options]"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__
from .fit import build_weight_fit, parse_weight_scale_fit
from .garmin_csv import (
    build_garmin_csv, build_standard_csv, build_standard_json, summary,
)
from .huawei import inspect_source, load_path, manual_record
from .models import Measurement, dedupe_by_second, dedupe_per_day, half_up

# weight_scale 字段 -> (Measurement 属性, FIT scale)，用于 validate 逐字段比对
_VALIDATE_FIELDS = {
    "weight": ("weight_kg", 100),
    "percent_fat": ("fat_pct", 100),
    "percent_hydration": ("water_pct", 100),
    "bone_mass": ("bone_kg", 100),
    "muscle_mass": ("muscle_kg", 100),
    "basal_met": ("basal_met", 4),
    "metabolic_age": ("metabolic_age", 1),
    "visceral_fat_rating": ("visceral_fat", 1),
    "bmi": ("bmi", 10),
}


def _record_key(record: Measurement) -> str:
    return f"{int(record.timestamp.timestamp())}|{record.weight_kg}"


def _load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("synced_keys", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_state(path: Path, keys: set[str]) -> None:
    path.write_text(json.dumps({"synced_keys": sorted(keys)},
                               ensure_ascii=False, indent=2), encoding="utf-8")


def _collect(args: argparse.Namespace) -> list[Measurement]:
    records: list[Measurement] = []
    if args.input:
        records.extend(load_path(args.input, jin_to_kg=args.jin,
                                 user_filter=args.user,
                                 include_sub_users=args.include_sub_users))
    if args.weight:
        records.append(manual_record(
            args.date or datetime.now().astimezone().isoformat(timespec="seconds"),
            args.weight, fat_pct=args.fat,
            bmi=args.bmi, water_pct=args.water, muscle_kg=args.muscle_kg_input,
            bone_kg=args.bone, visceral_fat=args.visceral,
            basal_met=args.bmr, metabolic_age=args.body_age,
            jin_to_kg=args.jin,
        ))
    if not records:
        raise SystemExit("没有读到任何测量记录。请检查 --input 路径或用 --weight 手工录入。")
    records.sort(key=lambda r: r.timestamp)
    if args.dedupe == "second":
        records = dedupe_by_second(records)
    elif args.dedupe == "day":
        records = dedupe_per_day(records)
    if args.since:
        records = [r for r in records if r.date_key >= args.since]
    if args.until:
        records = [r for r in records if r.date_key <= args.until]
    if not records:
        raise SystemExit("按 --since/--until 筛选后没有记录。")
    if args.only_new:
        state = _load_state(Path(args.out) / "sync_state.json")
        records = [r for r in records if _record_key(r) not in state]
    return records


def _height(args: argparse.Namespace) -> float | None:
    if args.height and args.height > 0:
        return args.height / 100 if args.height > 3 else args.height
    return None


def _date_fmt(args: argparse.Namespace) -> str:
    return "%d-%m-%Y" if args.date_format == "dmy" else "%Y-%m-%d"


def _write_monthly_fit(records: list[Measurement], out: Path, *,
                       height_m: float | None, muscle: str,
                       include_metabolic_age: bool) -> list[tuple]:
    by_month: dict[str, list[Measurement]] = defaultdict(list)
    for record in records:
        by_month[record.month_key].append(record)
    month_dir = out / "fit_monthly"
    month_dir.mkdir(parents=True, exist_ok=True)
    for old in month_dir.glob("*.fit"):
        old.unlink()
    rows = []
    for month, group in sorted(by_month.items()):
        name = f"{month}.fit"
        data = build_weight_fit(group, height_m=height_m, muscle=muscle,
                                include_metabolic_age=include_metabolic_age)
        (month_dir / name).write_bytes(data)
        rows.append((month, name, len(group), group[0], group[-1], len(data)))
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["month", "file", "readings", "first_local", "last_local", "bytes"])
    for month, name, count, first, last, size in rows:
        writer.writerow([month, name, count,
                         f"{first.timestamp:%Y-%m-%d %H:%M}",
                         f"{last.timestamp:%Y-%m-%d %H:%M}", size])
    (out / "months.csv").write_bytes(buffer.getvalue().encode("utf-8-sig"))
    return rows


def cmd_convert(args: argparse.Namespace) -> int:
    records = _collect(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    height_m = _height(args)

    csv_path = out / "garmin_import.csv"
    csv_path.write_bytes(build_garmin_csv(
        records, height_m=height_m, date_format=_date_fmt(args)
    ).encode("utf-8"))

    fit_bytes = build_weight_fit(records, height_m=height_m, muscle=args.muscle,
                                 include_metabolic_age=args.include_metabolic_age)
    fit_path = out / "garmin_weight.fit"
    fit_path.write_bytes(fit_bytes)

    (out / "measurements.csv").write_bytes(
        build_standard_csv(records).encode("utf-8-sig"))
    (out / "measurements.json").write_text(
        build_standard_json(records), encoding="utf-8")

    split_mode = args.split_by
    if args.split_fit and split_mode == "none":
        split_mode = "each"  # 兼容旧参数

    if split_mode == "each":
        split_dir = out / "fit_per_day"
        split_dir.mkdir(exist_ok=True)
        for old in split_dir.glob("*.fit"):
            old.unlink()
        for record in records:
            name = f"weight_{record.timestamp:%Y-%m-%d_%H%M}.fit"
            (split_dir / name).write_bytes(build_weight_fit(
                [record], height_m=height_m, muscle=args.muscle,
                include_metabolic_age=args.include_metabolic_age))
        print(f"  逐条 FIT ：{split_dir} （{len(records)} 个文件）")

    if split_mode == "month":
        rows = _write_monthly_fit(records, out, height_m=height_m, muscle=args.muscle,
                                  include_metabolic_age=args.include_metabolic_age)
        print(f"  按月 FIT ：{out / 'fit_monthly'} （{len(rows)} 个文件）+ months.csv")
        smallest = sorted(rows, key=lambda r: (r[2], r[0]))[:3]
        print("  建议先试传记录数最少的文件："
              + ", ".join(f"{r[1]}（{r[2]} 条）" for r in smallest)
              + " —— 确认 Garmin 里数值正确后再导入其余月份。")

    decoded = parse_weight_scale_fit(fit_bytes)
    assert len(decoded) == len(records), "FIT 记录数自检失败"

    print(f"解析到 {len(records)} 条测量记录：{json.dumps(summary(records), ensure_ascii=False)}")
    print(f"  肌肉量口径：{args.muscle}（{'骨骼肌，与 Garmin Index 一致' if args.muscle == 'skeletal' else '华为总肌肉量'}）")
    print(f"  去重方式  ：{args.dedupe} | 家人记录：{'包含' if args.include_sub_users else '已排除'}")
    print("  新陈代谢年龄：" + ("已写入（Garmin 可能显示为超大数字）"
                              if args.include_metabolic_age else
                              "默认不写入 FIT（Garmin 当前显示异常；数值仍留在 measurements.csv）"))
    print(f"  Garmin CSV：{csv_path}")
    print(f"  体重 FIT  ：{fit_path} （回读校验通过，{len(decoded)} 条）")
    print(f"  留档数据  ：{out / 'measurements.csv'} / measurements.json")
    print()
    print("下一步：浏览器打开下面网址并登录，然后点右上角云朵图标 →「导入数据」，")
    print("  把 garmin_import.csv（体重/BMI/体脂率）或 garmin_weight.fit（全指标）拖进去。")
    print("  中国区账号： https://connect.garmin.cn/modern/import-data")
    print("  国际区账号： https://connect.garmin.com/modern/import-data")
    print(f"  导入时日期格式请选择：{args.date_format.upper()}")

    if args.only_new:
        state_path = Path(args.out) / "sync_state.json"
        keys = _load_state(state_path) | {_record_key(r) for r in records}
        _save_state(state_path, keys)
        print(f"  已更新增量状态：{state_path}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """拿到真实华为导出后先摸底，不写任何文件。"""
    if not args.input:
        raise SystemExit("请用 -i 指定华为数据副本解压目录。")
    info = inspect_source(args.input)
    print(f"文件数 {info['files']} | 体重原始记录 {info['raw_records']} 条 "
          f"| record type 分布 {info['by_type']}")
    print(f"重复读数 {info['duplicates']} 条 | 疑似家人/访客 {info['sub_user_skipped']} 条（默认排除）")
    print(f"有效读数 {info['readings']} 条 | 含骨骼肌字段 {info['with_skeletal_muscle']} 条 "
          f"| 含身高 {info['with_height']} 条")
    if info["first"]:
        print(f"时间范围：{info['first']:%Y-%m-%d %H:%M} → {info['last']:%Y-%m-%d %H:%M}")
    print("按年分布：", info["by_year"] or "（无）")
    print("samplePoints.value 里的字段（出现次数）：")
    for name, count in info["fields"]:
        print(f"  {name:<24} {count}")
    print()
    print("下一步：python -m sync_tool convert -i <目录> --height 175 --split-by month")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    data = Path(args.fit).read_bytes()
    records = parse_weight_scale_fit(data)
    print(f"FIT 文件：{args.fit}  ({len(data)} 字节)")
    print(f"CRC 校验通过，共 {len(records)} 条 weight_scale 记录：")
    for item in records:
        print("  " + json.dumps(
            {k: (v.isoformat() if hasattr(v, "isoformat") else v)
             for k, v in item.items()}, ensure_ascii=False))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """把生成的 FIT 与源数据逐字段比对，防止编码器/解码器同一个 bug 互相抵消。"""
    records = _collect(args)
    height_m = _height(args)
    fit_path = Path(args.fit)
    if not fit_path.exists():
        raise SystemExit(f"找不到 FIT 文件：{fit_path}（先运行 convert）")
    decoded = parse_weight_scale_fit(fit_path.read_bytes())
    problems: list[str] = []
    if len(decoded) != len(records):
        problems.append(f"记录数不符：源 {len(records)} 条，FIT {len(decoded)} 条")

    for index, (record, got) in enumerate(zip(records, decoded)):
        want_ts = int(record.timestamp.timestamp())
        got_ts = int(got["timestamp"].timestamp())
        if want_ts != got_ts:
            problems.append(f"#{index} 时间戳不符：{want_ts} != {got_ts}")
        for field, (attr, scale) in _VALIDATE_FIELDS.items():
            if field == "metabolic_age" and not args.include_metabolic_age:
                if "metabolic_age" in got:
                    problems.append(f"#{index} metabolic_age：默认已停用，但 FIT 中仍写入了该字段")
                continue  # 默认不写入，跳过数值比对
            expected = getattr(record, attr)
            if attr == "muscle_kg" and args.muscle == "total":
                expected = record.fit_muscle("total")
            if attr == "bmi" and expected is None:
                expected = record.computed_bmi(height_m)
            if expected is None:
                if field in got:
                    problems.append(f"#{index} {field}：源无值却出现在 FIT 中")
                continue
            want_raw = half_up(expected * scale)
            got_raw = int(round(float(got.get(field, -999)) * scale)) if field in got else None
            if got_raw != want_raw:
                problems.append(f"#{index} {field}：FIT={got.get(field)!r} "
                                f"期望={want_raw / scale!r}（raw {got_raw} != {want_raw}）")

    checked = len(_VALIDATE_FIELDS) - (0 if args.include_metabolic_age else 1)
    print(f"源记录 {len(records)} 条 | FIT 解码 {len(decoded)} 条 | 比对 "
          f"{checked + 1} 个字段/条"
          + ("" if args.include_metabolic_age else "（metabolic_age 已停用）"))
    print(_official_sdk_check(fit_path))
    if problems:
        print(f"发现 {len(problems)} 处不一致：")
        for problem in problems[:30]:
            print("  " + problem)
        return 1
    print("OK：每条读数恰好出现一次，时间戳与全部字段在 FIT 分辨率内一致。")
    return 0


def _official_sdk_check(fit_path: Path) -> str:
    try:
        from garmin_fit_sdk import Decoder, Stream  # type: ignore
    except ImportError:
        return "官方 SDK 复核：跳过（可选安装 pip install garmin-fit-sdk 后自动启用）"
    try:
        stream = Stream.from_file(str(fit_path))
        decoder = Decoder(stream)
        if not decoder.is_fit():
            return "官方 SDK 复核：失败（不是有效 FIT 文件）"
        integrity = decoder.check_integrity()
        stream.reset()
        messages, errors = decoder.read()
        count = len(messages.get("weight_scale_mesgs", []))
        return (f"官方 SDK 复核：integrity={integrity}，解析错误={errors}，"
                f"weight_scale 消息 {count} 条")
    except Exception as exc:  # noqa: BLE001
        return f"官方 SDK 复核：异常 {exc}"


def cmd_report(args: argparse.Namespace) -> int:
    """华为体成分报告图片（或 OCR / 键值文本）-> 一条 Garmin 体重记录。"""
    from .report import load_report, parse_report_text, to_measurement

    source = Path(args.input)
    if not source.exists():
        raise SystemExit(f"找不到输入文件：{source}")
    text, kind = load_report(source)
    result = parse_report_text(text, source_kind=kind)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # 只有图片 OCR 才写原始识别结果；把修正后的草稿再喂回来时不要覆盖它。
    if kind == "image+ocr":
        (out / "report_ocr.txt").write_text(text, encoding="utf-8")
    draft = out / "report_draft.txt"
    draft.write_text(result.as_keyvalue_text(), encoding="utf-8")

    print(f"输入：{source}（{kind}）")
    print(f"测量时间：{result.timestamp_text or '未识别'} | "
          f"体重：{result.raw.get('weight_kg') or '未识别'} kg | "
          f"反推身高：{result.inferred_height_cm or '—'} cm")
    print(f"方程校验：{'通过（四组分和 = 体重）' if result.equation_ok else '未通过'}")
    print()
    print("字段            读数           采纳        状态  说明")
    for line in result.summary_lines():
        print("  " + line)
    for warning in result.warnings:
        print("  [警告] " + warning)
    print()
    print(f"可编辑草稿：{draft}")
    if (not result.equation_ok or result.requires_review()) and not args.force:
        # 同一输出目录会被反复使用；校验失败时清掉上一轮成功留下的文件，
        # 避免把陈旧的 CSV/FIT 误当成这次的结果导入 Garmin。
        removed = []
        for name in ("garmin_import.csv", "garmin_weight.fit", "measurements.csv"):
            stale = out / name
            if stale.exists():
                stale.unlink()
                removed.append(name)
        if removed:
            print(f"已清除目录里的旧导入文件：{', '.join(removed)}")
        print("存在未验证/需复核的字段，已停止生成 Garmin 文件。")
        print("请把草稿里的数值（尤其四组分 kg）对照原图改好，再运行：")
        print(f"  python -m sync_tool report -i {draft} -o {out}")
        print("确认无误但仍要生成时加 --force。")
        return 2

    record = to_measurement(result)
    height_m = _height(args)
    csv_bytes = build_garmin_csv([record], height_m=height_m,
                                 date_format=_date_fmt(args)).encode("utf-8")
    fit_bytes = build_weight_fit([record], height_m=height_m, muscle="skeletal",
                                 include_metabolic_age=args.include_metabolic_age)
    (out / "garmin_import.csv").write_bytes(csv_bytes)
    (out / "garmin_weight.fit").write_bytes(fit_bytes)
    (out / "measurements.csv").write_bytes(
        build_standard_csv([record]).encode("utf-8-sig"))
    decoded = parse_weight_scale_fit(fit_bytes)
    assert len(decoded) == 1, "FIT 自检失败"
    print(f"Garmin CSV：{out / 'garmin_import.csv'}")
    print(f"体重 FIT ：{out / 'garmin_weight.fit'}（回读校验通过，1 条）")
    print("新陈代谢年龄：" + ("已写入" if args.include_metabolic_age else
                            "默认不写入（Garmin 当前会把 34 显示成 1072935484000 年）"))
    print("下一步：登录 https://connect.garmin.cn/modern/import-data → 云朵图标 →「导入数据」")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    from .uploader import WARNING, upload_records
    print(WARNING)
    records = _collect(args)
    email = args.email or input("Garmin 账号邮箱：").strip()
    password = args.password or __import__("getpass").getpass("Garmin 密码：")
    results = upload_records(
        records, email=email, password=password, is_cn=args.is_cn,
        tokenstore=args.tokenstore, height_m=_height(args),
        muscle=args.muscle, dry_run=args.dry_run,
        include_metabolic_age=args.include_metabolic_age,
        prompt_mfa=(lambda: input("MFA 验证码：").strip()) if args.mfa else None,
    )
    print(f"完成，共 {len(results)} 条。")
    if args.dry_run:
        print(json.dumps(results[:3], ensure_ascii=False, indent=2))
    if args.only_new:
        state_path = Path(args.out) / "sync_state.json"
        keys = _load_state(state_path) | {_record_key(r) for r in records}
        _save_state(state_path, keys)
    return 0


def _add_common(parser: argparse.ArgumentParser, *, require_input: bool = False) -> None:
    parser.add_argument("--input", "-i", required=require_input,
                        help="华为数据副本目录 / JSON / CSV / ZIP")
    parser.add_argument("--out", "-o", default="out", help="输出目录（默认 out）")
    parser.add_argument("--height", type=float,
                        help="身高，用于自动算 BMI；>3 按 cm，否则按 m。导出里自带 height 时可省略")
    parser.add_argument("--jin", action="store_true", help="源数据体重单位是「斤」，自动 ÷2")
    parser.add_argument("--dedupe", choices=("second", "day", "none"), default="second",
                        help="去重：second=按 FIT 秒精度合并重复读数（推荐）；day=每天留最后一条；none=不去重")
    parser.add_argument("--include-sub-users", action="store_true",
                        help="包含 subUser / extendAttribute 标记的家人、访客读数（默认排除）")
    parser.add_argument("--muscle", choices=("skeletal", "total"), default="skeletal",
                        help="FIT muscle_mass 口径：skeletal=骨骼肌（默认，与 Garmin Index 一致）；"
                             "total=华为总肌肉量")
    parser.add_argument("--include-metabolic-age", action="store_true",
                        help="写入 FIT 的 metabolic_age（身体年龄）。默认关闭：Garmin Connect "
                             "2026-11 起会把该值错误显示为「年龄×一年毫秒数」；官方修复后再启用")
    parser.add_argument("--since", help="只保留该本地日期之后的读数 YYYY-MM-DD")
    parser.add_argument("--until", help="只保留该本地日期之前的读数 YYYY-MM-DD")
    parser.add_argument("--only-new", action="store_true", help="只处理未同步过的记录（增量）")
    parser.add_argument("--user", help="只保留指定华为子用户 ID")
    parser.add_argument("--date-format", choices=("ymd", "dmy"), default="ymd",
                        help="Garmin CSV 日期格式：ymd=2026-11-15，dmy=15-11-2026")
    parser.add_argument("--weight", type=float, help="手工录入：体重")
    parser.add_argument("--date", help="手工录入：日期 YYYY-MM-DD[ HH:MM]")
    parser.add_argument("--fat", type=float, help="手工录入：体脂率 %%")
    parser.add_argument("--bmi", type=float, help="手工录入：BMI")
    parser.add_argument("--water", type=float, help="手工录入：体水分率 %%")
    parser.add_argument("--muscle-kg", dest="muscle_kg_input", type=float,
                        help="手工录入：骨骼肌量 kg")
    parser.add_argument("--bone", type=float, help="手工录入：骨盐量 kg")
    parser.add_argument("--visceral", type=float, help="手工录入：内脏脂肪等级")
    parser.add_argument("--bmr", type=float, help="手工录入：基础代谢 kcal/day")
    parser.add_argument("--body-age", type=float, help="手工录入：身体年龄")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_tool",
        description="荣耀体脂秤 / 华为运动健康 → Garmin Connect 体重数据转换工具",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_convert = sub.add_parser("convert", help="转换并生成 Garmin 导入文件")
    _add_common(p_convert)
    p_convert.add_argument("--split-by", choices=("none", "each", "month"), default="none",
                           help="额外拆分：each=每次测量一个 FIT；month=每月一个 FIT（历史迁移推荐）")
    p_convert.add_argument("--split-fit", action="store_true", help=argparse.SUPPRESS)
    p_convert.set_defaults(func=cmd_convert)

    p_inspect = sub.add_parser("inspect", help="摸底真实华为导出：字段/年份/重复/家人记录")
    _add_common(p_inspect, require_input=True)
    p_inspect.set_defaults(func=cmd_inspect)

    p_verify = sub.add_parser("verify", help="回读并打印生成的 FIT 文件内容")
    p_verify.add_argument("fit", help="FIT 文件路径")
    p_verify.set_defaults(func=cmd_verify)

    p_validate = sub.add_parser("validate", help="把 FIT 与源数据逐字段比对")
    _add_common(p_validate, require_input=True)
    p_validate.add_argument("--fit", default="out/garmin_weight.fit", help="待校验的 FIT 文件")
    p_validate.set_defaults(func=cmd_validate)

    p_report = sub.add_parser(
        "report", help="从体成分报告图片 / OCR 文本生成一条 Garmin 体重记录")
    p_report.add_argument("--input", "-i", required=True,
                          help="分享图片 .jpg/.png，或 OCR/键值文本 .txt")
    p_report.add_argument("--out", "-o", default="out/report", help="输出目录")
    p_report.add_argument("--height", type=float,
                          help="身高（>3 按 cm）；省略时用报告 BMI 反推值")
    p_report.add_argument("--date-format", choices=("ymd", "dmy"), default="ymd",
                          help="Garmin CSV 日期格式")
    p_report.add_argument("--force", action="store_true",
                          help="即使方程未通过/字段待复核也生成文件")
    p_report.add_argument("--include-metabolic-age", action="store_true",
                          help="写入 FIT 的 metabolic_age（默认关闭，Garmin 当前显示异常）")
    p_report.set_defaults(func=cmd_report)

    p_upload = sub.add_parser("upload", help="（可选）自动上传到 Garmin Connect")
    _add_common(p_upload)
    p_upload.add_argument("--email", help="Garmin 账号邮箱")
    p_upload.add_argument("--password", help="Garmin 密码（建议留空交互式输入）")
    p_upload.add_argument("--is-cn", action="store_true", default=True,
                          help="中国区账号 connect.garmin.cn（默认，已按你的账号区域设定）")
    p_upload.add_argument("--global", dest="is_cn", action="store_false",
                          help="改用国际区账号 connect.garmin.com")
    p_upload.add_argument("--tokenstore", help="OAuth token 持久化目录")
    p_upload.add_argument("--mfa", action="store_true", help="交互式输入二次验证码")
    p_upload.add_argument("--dry-run", action="store_true", help="不上传，只打印拟提交内容")
    p_upload.set_defaults(func=cmd_upload)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
