"""详情页控件树直读 → 完整 Measurement（14 项全量，不分享/不出图/不 OCR）。

华为运动健康「测量记录」详情页把人体成分指标全部渲染成 TextView：
  * level_value_text                              体重（公斤）
  * hw_show_health_data_weight_bodyindex_mid_time 测量时间
  * body_index_recycle_title + ..._value          成对的「指标名 / 数值」
  * hw_show_health_data_weight_mid_weight_score   身体得分

这些数字是 App 自己算好并显示出来的，与「分享 → 保存长图 → OCR」的
那张图完全同源，但省掉分享/存图/拉取/OCR/方程校验整条链路，且不受
OCR 误识影响。真正做到了「控件树直读既有列表的速度、又有报告的字段」。

普通称重（只有体重、无体脂）的详情页没有这些指标节点，只留 体重+时间
（+BMI），照样解析成 Measurement（fat_pct=None），天然兼容。

本模块是纯逻辑：不碰 Qt、不碰 adb，便于单测。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from phone import UiNode, detect_page, find_node  # noqa: E402
from sync_tool.models import Measurement, parse_time  # noqa: E402

RID_TIME = "hw_show_health_data_weight_bodyindex_mid_time"
RID_TIME_ALT = "ta_weight_bodyindex_mid_time"
RID_WEIGHT = "level_value_text"
RID_SCORE = "hw_show_health_data_weight_mid_weight_score"
RID_INDEX_TITLE = "body_index_recycle_title"
RID_INDEX_VALUE = "body_index_recycle_value"

# 详情页「指标名」→ Measurement 字段名。去脂体重 / 身体类型没有对应字段，
# 留在 raw 里留档（Garmin 也不接收这两项）。
LABEL_TO_FIELD: dict[str, str] = {
    "bmi": "bmi",
    "脂肪率": "fat_pct",
    "体脂率": "fat_pct",
    "骨骼肌量": "muscle_kg",
    "骨骼肌": "muscle_kg",
    "内脏脂肪等级": "visceral_fat",
    "内脏脂肪": "visceral_fat",
    "基础代谢率": "basal_met",
    "基础代谢": "basal_met",
    "水分率": "water_pct",
    "体水分率": "water_pct",
    "骨盐量": "bone_kg",
    "骨量": "bone_kg",
    "蛋白质": "protein_pct",
    "蛋白质率": "protein_pct",
    "身体年龄": "metabolic_age",
    # 以下仅留档
    "去脂体重": "fat_free_kg",
    "身体类型": "body_type",
}

_SUFFIXES = ("千卡/日", "公斤", "千克", "%", "级", "岁", "斤", "分")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def parse_number(text: object) -> float | None:
    """把详情页数值文本转成数字。

    基础代谢率可能带千分位逗号（'1,767'），此时逗号是千分位要去掉；
    其余情况逗号才当小数点。单位后缀（%/公斤/级/岁/千卡/日）一律剥离。
    """
    value = str(text or "").strip().replace(" ", "")
    if not value:
        return None
    for suffix in _SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    if re.search(r"\d,\d{3}(?:\D|$)", value):
        value = value.replace(",", "")
    else:
        value = value.replace(",", ".")
    match = _NUM_RE.search(value)
    return float(match.group()) if match else None


def pair_index_metrics(nodes: list[UiNode]) -> dict[str, str]:
    """把成对的 title / value 拼成 {指标名: 数值文本}。

    真机布局是三列网格：title 在上方、value 正下方（x 基本相同、value.y1
    紧接 title.y2）。用坐标配对而非数组顺序，更能抵抗控件顺序变化。
    """
    titles = [n for n in nodes if n.rid.endswith(RID_INDEX_TITLE) and n.text.strip()]
    values = [n for n in nodes if n.rid.endswith(RID_INDEX_VALUE) and n.text.strip()]
    result: dict[str, str] = {}
    for title in titles:
        # 用左边界对齐：像「内脏脂肪等级」这种长标题中心点会明显右偏，
        # 但 title 与 value 的左边界在同一列，稳定得多。
        tx = title.bounds[0]
        candidates = [
            v for v in values
            if abs(v.bounds[0] - tx) <= 30
            and title.bounds[3] - 10 <= v.bounds[1] <= title.bounds[3] + 30
        ]
        if not candidates:
            continue
        value = min(candidates, key=lambda v: v.bounds[1])
        result[title.text.strip()] = value.text.strip()
    return result


def parse_detail(nodes: list[UiNode]) -> dict:
    """从详情页控件树读出所有可读字段（原始文本 + 已配对指标）。"""
    time_node = (find_node(nodes, rid=RID_TIME)
                 or find_node(nodes, rid=RID_TIME_ALT))
    weight_node = find_node(nodes, rid=RID_WEIGHT)
    score_node = find_node(nodes, rid=RID_SCORE)
    metrics = pair_index_metrics(nodes)
    return {
        "time_text": time_node.text.strip() if time_node else "",
        "weight_text": weight_node.text.strip() if weight_node else "",
        "score_text": score_node.text.strip() if score_node else "",
        "metrics": metrics,
    }


def collect_detail_data(batches: list[list[UiNode]]) -> dict:
    """把详情页「多次下滚 dump」的若干屏合并成一份完整数据。

    真机上 14 项指标排成一列（真机夹具里 y=1551~2610），远超一屏可视区，
    uiautomator 只渲染可视区控件；刚打开详情页就 dump，只能拿到第一行的
    BMI/脂肪率/骨骼肌量，下面的水分率/骨盐量/蛋白质/身体年龄还没进控件树。

    合并规则：
      * 时间/体重/得分固定在页面顶部，滚动后会移出屏幕 → 取首次读到的非空值；
      * 指标按「指标名」合并，后一屏补上前一屏没渲染出来的项。
        绝不跨屏按坐标配对：滚动后同一个指标 y 变了，跨屏坐标配对会张冠李戴。
    """
    merged: dict[str, object] = {
        "time_text": "", "weight_text": "", "score_text": "", "metrics": {},
    }
    metrics: dict[str, str] = merged["metrics"]  # type: ignore[assignment]
    for nodes in batches:
        data = parse_detail(nodes)
        for key in ("time_text", "weight_text", "score_text"):
            if not merged[key] and data[key]:
                merged[key] = data[key]
        metrics.update(data["metrics"])
    return merged


def detail_data_to_measurement(data: dict) -> Measurement | None:
    """合并后的 detail data → 一条 Measurement；缺时间/体重时返回 None。

    身体年龄写进 metabolic_age 字段留档，但 generate_files 默认不把该字段
    写进 FIT（Garmin Connect 2026-11 起会错误显示年龄），故不影响导出安全。
    """
    timestamp = parse_time(str(data["time_text"]).replace("/", "-"))
    weight = parse_number(data["weight_text"])
    if timestamp is None or not weight or weight <= 0:
        return None

    fields: dict[str, float] = {}
    raw: dict[str, object] = {}
    for label, text in data["metrics"].items():
        target = (LABEL_TO_FIELD.get(label)
                  or LABEL_TO_FIELD.get(label.lower()))
        number = parse_number(text)
        if target and number is not None:
            fields[target] = number
        raw[label] = text
    score = parse_number(data["score_text"])
    if score is not None:
        fields["body_score"] = score

    def get(name: str) -> float | None:
        return fields.get(name)

    return Measurement(
        timestamp=timestamp,
        weight_kg=round(weight, 3),
        fat_pct=get("fat_pct"),
        bmi=get("bmi"),
        water_pct=get("water_pct"),
        muscle_kg=get("muscle_kg"),
        bone_kg=get("bone_kg"),
        visceral_fat=get("visceral_fat"),
        basal_met=get("basal_met"),
        metabolic_age=get("metabolic_age"),
        protein_pct=get("protein_pct"),
        body_score=get("body_score"),
        source="huawei_detail_controltree",
        raw=raw,
    )


def detail_to_measurement(nodes: list[UiNode]) -> Measurement | None:
    """单屏便捷入口：一屏控件树 → Measurement（兼容旧调用/单测）。"""
    if detect_page(nodes) != "detail":
        return None
    return detail_data_to_measurement(parse_detail(nodes))
