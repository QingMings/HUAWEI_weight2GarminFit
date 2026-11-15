"""可选：全自动写入 Garmin Connect（非官方通道，需本人账号）。

原理：借用 python-garminconnect 模拟 Garmin Connect 官方 App / 网页的登录与请求
身份（SSO + DI OAuth Bearer Token），调用 add_body_composition() 把一条体重 FIT
POST 到网页/App 自用但未公开的 /upload-service/upload 端点。
该接口不属于 Garmin 官方开放的 Personal API（后者只读），属社区维护的私有端点，
Garmin 改版时可能失效；请自行评估账号风险。

默认不启用；推荐优先使用官方网页版「导入数据」上传本工具生成的 CSV / FIT。
"""
from __future__ import annotations

from typing import Any, Callable

from .models import Measurement

WARNING = (
    "注意：自动上传使用 Garmin Connect 网页/App 的私有接口（非官方 Personal API），"
    "仅建议用于本人账号；Garmin 改版可能导致失效，建议先用 --dry-run 验证。"
)


def _import_garminconnect():
    try:
        from garminconnect import Garmin  # type: ignore
        return Garmin
    except ImportError as exc:
        raise RuntimeError(
            "未安装自动上传依赖。请先执行： pip install -r requirements.txt"
        ) from exc


def upload_records(records: list[Measurement], *, email: str, password: str,
                   is_cn: bool = False, tokenstore: str | None = None,
                   height_m: float | None = None, dry_run: bool = False,
                   muscle: str = "skeletal", include_metabolic_age: bool = False,
                   prompt_mfa: Callable[[], str] | None = None,
                   progress: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """逐条上传体重记录，返回每条的服务端响应（dry_run 时返回拟提交参数）。

    include_metabolic_age 默认 False，与网页导入通道保持一致：Garmin Connect
    当前会把该字段按「一年毫秒数」放大显示，故默认不提交。
    """
    results: list[dict[str, Any]] = []
    if dry_run:
        for record in records:
            results.append({
                "dry_run": True,
                "timestamp": record.timestamp.isoformat(),
                "weight": record.weight_kg,
                "percent_fat": record.fat_pct,
                "percent_hydration": record.water_pct,
                "bone_mass": record.bone_kg,
                "muscle_mass": record.fit_muscle(muscle),
                "basal_met": record.basal_met,
                "metabolic_age": record.metabolic_age if include_metabolic_age else None,
                "visceral_fat_rating": record.visceral_fat,
                "bmi": record.computed_bmi(height_m),
            })
        return results

    Garmin = _import_garminconnect()
    kwargs: dict[str, Any] = {"email": email, "password": password, "is_cn": is_cn}
    if prompt_mfa:
        kwargs["prompt_mfa"] = prompt_mfa
    client = Garmin(**kwargs)
    # 注意：tokenstore 是 login() 的参数，不是构造函数参数（0.3.x 起）
    client.login(tokenstore=tokenstore) if tokenstore else client.login()
    progress(f"已登录 Garmin Connect（{'中国区' if is_cn else '国际区'}）")

    for index, record in enumerate(records, 1):
        response = client.add_body_composition(
            timestamp=record.timestamp.isoformat(),
            weight=record.weight_kg,
            percent_fat=record.fat_pct,
            percent_hydration=record.water_pct,
            bone_mass=record.bone_kg,
            muscle_mass=record.fit_muscle(muscle),
            basal_met=record.basal_met,
            metabolic_age=(record.metabolic_age if include_metabolic_age else None),
            visceral_fat_rating=record.visceral_fat,
            bmi=record.computed_bmi(height_m),
        )
        results.append(response if isinstance(response, dict) else {"raw": str(response)})
        progress(f"  [{index}/{len(records)}] {record.timestamp:%Y-%m-%d %H:%M} "
                 f"{record.weight_kg} kg 已提交")
    return results
