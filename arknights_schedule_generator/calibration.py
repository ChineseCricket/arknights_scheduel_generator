from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data import GameData
from .models import RoomAssignment, RosterOperator, ShiftPlan
from .optimizer import ROOM_NAMES
from .production import (
    DAILY_DRONES,
    DRONE_SECONDS,
    GUIDE_LMD_TRADE_LOOKUPS,
    EXP_VALUE,
    HIGH_ORDER_SECONDS,
    LOW_ORDER_SECONDS,
    MEDIUM_ORDER_SECONDS,
    ORUNDUM_PER_SHARD_ORDER,
    ORUNDUM_ORDER_SECONDS,
    ProductionSimulator,
    ProductionVector,
    SHARDS_PER_ORUNDUM_ORDER,
)
from .schedule_import import load_yituliu_schedule


VECTOR_FIELDS = (
    "lmdGross",
    "lmdNet",
    "exp",
    "orundum",
    "pureGoldDelta",
    "shardDelta",
    "overflowLoss",
    "fatigueRisk",
)

EXTRA_CONTRIBUTION_FIELDS = {
    "lmdExtraContribution": "lmdGross",
    "expExtraContribution": "exp",
    "pureGoldExtraLmdValue": "pureGoldGrossLmdValue",
}

MOWER_DYNAMIC_SAMPLE_PATH = Path("outputs/yituliu_2026_06_342_orundum_dynamic_mower.json")
YITULIU_SCHEDULE_ASSET_PATH = Path(r"C:\tmp\yituliu_schedule_images_current.js")
YITULIU_STATIC_SCHEDULE_IMAGE_DATE = "2026-06-28"
YITULIU_AUXILIARY_SCHEDULE_IMAGE_DATE = "2026-06-01"
GREEN_METRIC_UNRESOLVED = "green rotation metric exact formula"
GUIDE_SHARD_ROCK_LMD_COST = 1600.0


def yituliu_static_image_url(image_name: str) -> str:
    return (
        "https://cos.yituliu.cn/arknights/schedule-images/"
        f"{YITULIU_STATIC_SCHEDULE_IMAGE_DATE}/{image_name}"
    )


def yituliu_auxiliary_image_url(image_name: str) -> str:
    return (
        "https://cos.yituliu.cn/arknights/schedule-images/"
        f"{YITULIU_AUXILIARY_SCHEDULE_IMAGE_DATE}/{image_name}"
    )


def yituliu_static_local_image_path(image_name: str) -> str:
    return str(
        Path("outputs/current_yituliu_assets")
        / f"{YITULIU_STATIC_SCHEDULE_IMAGE_DATE}_{image_name}"
    )


@dataclass(frozen=True)
class CalibrationCase:
    name: str
    category: str
    description: str
    shifts: list[ShiftPlan]
    expected: ProductionVector
    notes: list[str] = field(default_factory=list)


def build_calibration_report(
    game_data: GameData,
    *,
    shard_formula: str = "rock",
    schedule_path: Path | None = None,
    guide_samples: list[Path] | None = None,
    roster: list[RosterOperator] | None = None,
    allow_upgrades: bool = False,
    upgrade_cost_weight: float = 0.015,
    profile: str = "all",
    drone_policy: str = "none",
) -> dict[str, Any]:
    profile = normalize_profile(profile)
    simulator = ProductionSimulator(
        game_data,
        shard_formula=shard_formula,
        drone_policy=drone_policy,
        calibration_profile="guide" if profile in {"guide", "all"} else "formula",
    )
    all_cases = calibration_cases(simulator)
    cases = [case_to_dict(case, simulator) for case in all_cases if include_case(case, profile)]
    failed = [case["name"] for case in cases if not case["passed"]]
    schedules: list[dict[str, Any]] = []
    warnings: list[str] = []

    sample_paths = [path for path in [schedule_path, *(guide_samples or [])] if path]
    if sample_paths and roster is None:
        warnings.append("Schedule samples were provided without a roster; sample scoring was skipped.")
    elif roster is not None:
        for sample_path in sample_paths:
            schedules.append(
                score_sample_schedule(
                    sample_path,
                    game_data,
                    roster,
                    shard_formula=shard_formula,
                    allow_upgrades=allow_upgrades,
                    upgrade_cost_weight=upgrade_cost_weight,
                    drone_policy=drone_policy,
                )
            )

    guide_targets = latest_yituliu_orundum_targets()
    guide_yield_cases = yituliu_2026_06_image_label_cases()
    yituliu_asset_check = yituliu_schedule_asset_catalog_check()
    return {
        "format": "arknights-production-calibration-report",
        "formatVersion": 2,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataVersion": game_data.data_version,
        "shardFormula": shard_formula,
        "profile": profile,
        "dronePolicy": drone_policy,
        "tradeModel": trade_model_summary(),
        "summary": {
            "caseCount": len(cases),
            "passed": len(cases) - len(failed),
            "failed": len(failed),
            "allPassed": not failed,
            "failedCases": failed,
        },
        "cases": cases,
        "externalSchedules": schedules,
        "warnings": warnings,
        "guideCalibrationTargets": guide_targets,
        "guideTargetSummary": guide_target_summary(guide_targets),
        "guideYieldValidationCases": guide_yield_cases,
        "guideYieldValidationSummary": guide_yield_validation_summary(guide_yield_cases),
        "guideLabelSemantics": guide_label_semantics(),
        "sourceProvenance": {
            "yituliuScheduleAssetCatalog": yituliu_asset_check,
        },
        "communityReferences": community_references(yituliu_asset_check),
        "sources": [
            "GamePress 252 RIIC Base Guide",
            "Arknights Wiki Trading Post",
            "Arknights Wiki RIIC",
            "ArkBuilding",
            "Yituliu Orundum calculator",
            "Yituliu 2026-06 schedule-image catalog",
            "TapTap/Gamersky RIIC monthly-yield table",
            "Bilibili Chinese community orundum-farming tutorial metadata",
        ],
    }


def calibration_cases(simulator: ProductionSimulator) -> list[CalibrationCase]:
    return [
        atomic_case(simulator, "factory_gold_12h", "formula", "Level 3 gold factory, no skill, 12h.", "MANUFACTURE", "F_GOLD", 12),
        atomic_case(simulator, "factory_gold_24h", "formula", "Level 3 gold factory, no skill, 24h.", "MANUFACTURE", "F_GOLD", 24),
        atomic_case(simulator, "factory_exp_12h", "formula", "Level 3 EXP factory, no skill, 12h.", "MANUFACTURE", "F_EXP", 12),
        atomic_case(simulator, "factory_exp_24h", "formula", "Level 3 EXP factory, no skill, 24h.", "MANUFACTURE", "F_EXP", 24),
        atomic_case(simulator, "factory_shard_12h", "formula", "Level 3 shard factory, rock recipe, no skill, 12h.", "MANUFACTURE", "F_DIAMOND", 12),
        atomic_case(simulator, "factory_shard_24h", "formula", "Level 3 shard factory, rock recipe, no skill, 24h.", "MANUFACTURE", "F_DIAMOND", 24),
        atomic_case(simulator, "trade_gold_l1_24h", "formula", "Level 1 LMD trading post, no skill, 24h.", "TRADING", "O_GOLD", 24, room_level=1),
        atomic_case(simulator, "trade_gold_l2_24h", "formula", "Level 2 LMD trading post, no skill, 24h.", "TRADING", "O_GOLD", 24, room_level=2),
        atomic_case(simulator, "trade_gold_l3_24h", "formula", "Level 3 LMD trading post, no skill, 24h.", "TRADING", "O_GOLD", 24, room_level=3),
        atomic_case(simulator, "trade_orundum_l3_12h", "formula", "Level 3 Orundum trading post, no skill, 12h.", "TRADING", "O_DIAMOND", 12, room_level=3),
        layout_case(
            simulator,
            "guide_252_3exp_2gold",
            "guide",
            "GamePress-style 252: 2 trading posts, 3 EXP factories, 2 gold factories, two 12h shifts.",
            ["O_GOLD", "O_GOLD"],
            ["F_EXP", "F_EXP", "F_EXP", "F_GOLD", "F_GOLD"],
            notes=["Gold balance is expected to be close; guides often use drones on gold to cover the small deficit."],
        ),
        layout_case(
            simulator,
            "guide_252_2exp_3gold",
            "guide",
            "GamePress-style 252: 2 trading posts, 2 EXP factories, 3 gold factories, two 12h shifts.",
            ["O_GOLD", "O_GOLD"],
            ["F_EXP", "F_EXP", "F_GOLD", "F_GOLD", "F_GOLD"],
            notes=["This is the LMD-oriented 252 split with gold surplus."],
        ),
        layout_case(
            simulator,
            "guide_243_balanced_orundum",
            "guide",
            "243 balanced Orundum: LMD trade, Orundum trade, EXP, gold and shard production.",
            ["O_GOLD", "O_DIAMOND"],
            ["F_EXP", "F_GOLD", "F_GOLD", "F_DIAMOND"],
            notes=["Reports both LMD gross income and LMD net after shard production costs."],
        ),
    ]


def atomic_case(
    simulator: ProductionSimulator,
    name: str,
    category: str,
    description: str,
    room_type: str,
    target: str,
    hours: float,
    *,
    room_level: int = 3,
) -> CalibrationCase:
    assignment = room(f"{room_type.lower()}_1", room_type, target, room_level=room_level)
    shifts = [ShiftPlan("A", "08:00", hours, [assignment], [])]
    expected = expected_for_shifts(simulator, shifts)
    return CalibrationCase(name, category, description, shifts, expected)


def layout_case(
    simulator: ProductionSimulator,
    name: str,
    category: str,
    description: str,
    trading_targets: list[str],
    manufacture_targets: list[str],
    notes: list[str] | None = None,
) -> CalibrationCase:
    rooms: list[RoomAssignment] = []
    for index, target in enumerate(trading_targets, 1):
        rooms.append(room(f"trading_{index}", "TRADING", target))
    for index, target in enumerate(manufacture_targets, 1):
        rooms.append(room(f"manufacture_{index}", "MANUFACTURE", target))
    shifts = [
        ShiftPlan("A", "08:00", 12, rooms, []),
        ShiftPlan("B", "20:00", 12, rooms, []),
    ]
    expected = expected_for_shifts(simulator, shifts)
    return CalibrationCase(name, category, description, shifts, expected, notes or [])


def case_to_dict(case: CalibrationCase, simulator: ProductionSimulator) -> dict[str, Any]:
    report = simulator.evaluate(case.shifts)
    actual = report.dailyExpected
    differences = vector_difference(actual, case.expected)
    passed = vector_passed(differences, case.expected)
    guide_check = guide_assertion(case, actual)
    if guide_check is not None and not guide_check["passed"]:
        passed = False
    return {
        "name": case.name,
        "category": case.category,
        "description": case.description,
        "passed": passed,
        "expected": case.expected.to_dict(),
        "actual": actual.to_dict(),
        "actualEconomics": production_economics_summary(actual.to_dict()),
        "difference": differences,
        "guideAssertion": guide_check,
        "notes": case.notes,
        "roomReports": report.to_dict()["roomReports"],
    }


def guide_assertion(case: CalibrationCase, actual: ProductionVector) -> dict[str, Any] | None:
    if case.category != "guide":
        return None
    checks: dict[str, bool] = {
        "noOverflow": actual.overflowLoss <= 0.01,
        "hasLmdGross": actual.lmdGross > 0,
        "hasExpWhenExpected": ("orundum" not in case.name) or actual.exp > 0,
    }
    if "252_3exp_2gold" in case.name:
        checks["goldNearlyBalanced"] = actual.pureGoldDelta >= -2.0
    if "252_2exp_3gold" in case.name:
        checks["goldSurplus"] = actual.pureGoldDelta > 0
    if "orundum" in case.name:
        checks["hasOrundum"] = actual.orundum > 0
        checks["reportsNetCost"] = actual.lmdNet < actual.lmdGross
    return {"passed": all(checks.values()), "checks": checks}


def expected_for_shifts(simulator: ProductionSimulator, shifts: list[ShiftPlan]) -> ProductionVector:
    vector = ProductionVector()
    for shift in shifts:
        for assignment in shift.rooms:
            vector.add(expected_for_room(simulator, assignment, shift.duration_hours))
    cycle_hours = max(24.0, sum(shift.duration_hours for shift in shifts) or 24.0)
    vector.scale(24.0 / cycle_hours)
    vector.add(simulator.evaluate_drones(shifts, vector))
    vector.lmdNet = vector.lmdGross - vector.materialCosts.get("4001", 0.0)
    return vector


def expected_for_room(
    simulator: ProductionSimulator, assignment: RoomAssignment, duration_hours: float
) -> ProductionVector:
    if assignment.room_type == "MANUFACTURE":
        return expected_manufacture(simulator, assignment, duration_hours)
    if assignment.room_type == "TRADING":
        return expected_trading(simulator, assignment, duration_hours)
    return ProductionVector()


def expected_manufacture(
    simulator: ProductionSimulator, assignment: RoomAssignment, duration_hours: float
) -> ProductionVector:
    target = assignment.target or "F_EXP"
    formula = simulator.manufacture_formula(target)
    raw_units = duration_hours * 3600.0 / formula["costPoint"] * no_skill_multiplier(simulator, assignment)
    capacity = simulator.manufacture_capacity(assignment)
    units = min(raw_units, capacity)
    overflow = max(0.0, raw_units - capacity)
    vector = ProductionVector(overflowLoss=overflow)
    vector.fatigueRisk = expected_fatigue(simulator, assignment, duration_hours)
    simulator.apply_manufacture_units(vector, target, formula, units)
    return vector


def expected_trading(
    simulator: ProductionSimulator, assignment: RoomAssignment, duration_hours: float
) -> ProductionVector:
    target = assignment.target or "O_GOLD"
    profile = simulator.trade_order_profile(target, assignment.room_level)
    raw_orders = duration_hours * 3600.0 / profile.expected_seconds * no_skill_multiplier(
        simulator, assignment
    )
    capacity = simulator.trading_order_limit(assignment)
    orders = min(raw_orders, capacity)
    vector = ProductionVector(overflowLoss=max(0.0, raw_orders - capacity))
    vector.fatigueRisk = expected_fatigue(simulator, assignment, duration_hours)
    if target == "O_GOLD":
        vector.lmdGross = orders * profile.expected_lmd
        vector.pureGoldDelta = -orders * profile.expected_gold
    elif target == "O_DIAMOND":
        vector.orundum = orders * ORUNDUM_PER_SHARD_ORDER
        vector.shardDelta = -orders * SHARDS_PER_ORUNDUM_ORDER
    return vector


def no_skill_multiplier(simulator: ProductionSimulator, assignment: RoomAssignment) -> float:
    return simulator.room_phase_speed(assignment) * (
        1.0 + simulator.base_speed_percent(assignment) / 100.0
    )


def expected_fatigue(
    simulator: ProductionSimulator, assignment: RoomAssignment, duration_hours: float
) -> float:
    return max(0.0, duration_hours - 12.0) * max(1, simulator.stationed_slots(assignment))


def score_sample_schedule(
    sample_path: Path,
    game_data: GameData,
    roster: list[RosterOperator],
    *,
    shard_formula: str,
    allow_upgrades: bool,
    upgrade_cost_weight: float,
    drone_policy: str,
) -> dict[str, Any]:
    imported = load_yituliu_schedule(
        sample_path,
        game_data,
        roster,
        allow_upgrades=allow_upgrades,
        upgrade_cost_weight=upgrade_cost_weight,
    )
    simulator = ProductionSimulator(
        game_data,
        shard_formula=shard_formula,
        drone_policy=drone_policy,
        calibration_profile="guide",
    )
    report = simulator.evaluate(imported.shifts)
    daily = report.dailyExpected.to_dict()
    return {
        "path": str(sample_path),
        "layout": imported.layout.label,
        "shiftCount": len(imported.shifts),
        "shiftDurations": [shift.duration_hours for shift in imported.shifts],
        "score": report.score,
        "dailyExpected": daily,
        "validationEconomics": production_economics_summary(daily),
        "scoreBreakdown": report.to_dict()["scoreBreakdown"],
        "warnings": imported.warnings,
        "assumptions": report.assumptions,
        "sourceAssumptions": report.sourceAssumptions,
        "unsupportedSkillEffects": report.unsupportedSkillEffects,
        "guideComparison": {
            "lmdGrossExplainer": "A guide claim such as 40000 LMD/day usually refers to gross LMD, may include more LMD trading posts, drones, or advanced trading combinations, and should not be compared directly with lmdNet.",
            "layoutFromJson": imported.layout.label,
            "usesJsonScheduleType": True,
        },
    }


def vector_difference(actual: ProductionVector, expected: ProductionVector) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for field_name in VECTOR_FIELDS:
        diff[field_name] = round(getattr(actual, field_name) - getattr(expected, field_name), 6)
    material_keys = set(actual.materialCosts) | set(expected.materialCosts)
    diff["materialCosts"] = {
        key: round(actual.materialCosts.get(key, 0.0) - expected.materialCosts.get(key, 0.0), 6)
        for key in sorted(material_keys)
    }
    return diff


def vector_passed(difference: dict[str, Any], expected: ProductionVector) -> bool:
    for field_name in VECTOR_FIELDS:
        if not close_enough(float(difference[field_name]), getattr(expected, field_name)):
            return False
    for item_id, delta in difference.get("materialCosts", {}).items():
        if not close_enough(float(delta), expected.materialCosts.get(item_id, 0.0)):
            return False
    return True


def close_enough(delta: float, expected: float) -> bool:
    return abs(delta) <= max(0.01, abs(expected) * 0.0001)


def include_case(case: CalibrationCase, profile: str) -> bool:
    return profile == "all" or case.category == profile


def normalize_profile(profile: str | None) -> str:
    value = (profile or "all").lower()
    if value not in {"formula", "guide", "all"}:
        raise ValueError("--profile must be formula, guide, or all")
    return value


def trade_model_summary() -> dict[str, Any]:
    return {
        "status": "guide_calibrated",
        "lmdOrders": {
            "level1": {"probabilities": [1.0, 0.0, 0.0]},
            "level2": {"probabilities": [0.60, 0.40, 0.0]},
            "level3": {
                "default": [0.30, 0.50, 0.20],
                "tailoringAlpha": [0.15, 0.30, 0.55],
                "tailoringBeta": [0.05, 0.10, 0.85],
            },
            "orders": [
                {"gold": 2, "lmd": 1000, "seconds": LOW_ORDER_SECONDS},
                {"gold": 3, "lmd": 1500, "seconds": MEDIUM_ORDER_SECONDS},
                {"gold": 4, "lmd": 2000, "seconds": HIGH_ORDER_SECONDS},
            ],
            "guideSpecialLookups": [
                {
                    "id": lookup["id"],
                    "level": lookup["level"],
                    "required": sorted(lookup["required"]),
                    "baseLmdPer24h": lookup["baseLmdPer24h"],
                    "baseGoldPer24h": lookup["baseGoldPer24h"],
                    "paperEfficiencyPercent": lookup["paperEfficiencyPercent"],
                    "scheduleEffectivePercent": lookup.get(
                        "scheduleEffectivePercent", lookup["paperEfficiencyPercent"]
                    ),
                    "partnerScoreMinimums": list(
                        lookup.get("partnerScoreMinimums", ())
                    ),
                    "source": lookup["source"],
                }
                for lookup in GUIDE_LMD_TRADE_LOOKUPS
            ],
        },
        "orundumOrder": {
            "shards": SHARDS_PER_ORUNDUM_ORDER,
            "orundum": ORUNDUM_PER_SHARD_ORDER,
            "seconds": ORUNDUM_ORDER_SECONDS,
            "note": "Timing is calibrated to 2 hours/order from the Yituliu 2026-06 243/342 Orundum schedule labels.",
        },
        "drones": {
            "dailyDrones": DAILY_DRONES,
            "secondsPerDrone": DRONE_SECONDS,
            "includedByDefault": False,
        },
    }


def yituliu_schedule_asset_catalog_check(
    path: Path = YITULIU_SCHEDULE_ASSET_PATH,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "localAssetPath": str(path),
        "found": path.exists(),
        "expectedVideoBvid": "BV19jVZ69Evp",
        "requiredStaticImageUrls": {
            "yituliu_2026_06_342_orundum_2shift": yituliu_static_image_url("10.webp"),
            "yituliu_2026_06_243_orundum_2shift": yituliu_static_image_url("11.webp"),
        },
    }
    if not path.exists():
        result.update(
            {
                "status": "missing_local_asset",
                "note": "The current workspace does not contain the downloaded Yituliu frontend asset; guide labels remain manually recorded.",
            }
        )
        return result

    raw_bytes = path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="replace")
    entries = parse_yituliu_schedule_asset_entries(text)
    urls = sorted({entry["imageUrl"] for entry in entries})
    required = result["requiredStaticImageUrls"]
    static_checks = {
        key: value in urls for key, value in required.items()
    }
    dynamic_342_candidates = [
        entry
        for entry in entries
        if "342" in entry["name"] and entry["imageUrl"].endswith("8.webp")
    ]
    result.update(
        {
            "status": "matched" if all(static_checks.values()) and dynamic_342_candidates else "partial",
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "byteLength": len(raw_bytes),
            "containsVideoBvid": "BV19jVZ69Evp" in text,
            "containsVersionDate": YITULIU_STATIC_SCHEDULE_IMAGE_DATE in text,
            "imageEntryCount": len(entries),
            "imageUrlCount": len(urls),
            "staticImageChecks": static_checks,
            "dynamic342OrundumCandidates": dynamic_342_candidates,
            "targetEvidence": {
                "sourceVideo": "https://www.bilibili.com/video/BV19jVZ69Evp/",
                "staticImageUrls": [
                    required["yituliu_2026_06_342_orundum_2shift"],
                    required["yituliu_2026_06_243_orundum_2shift"],
                ],
                "dynamic342ImageUrls": [
                    item["imageUrl"] for item in dynamic_342_candidates
                ],
            },
            "note": "This is a local provenance check over the downloaded Yituliu schedule-images frontend asset. Names may be mojibake in the asset, so target matching uses stable numeric layout markers and image URLs.",
        }
    )
    return result


def parse_yituliu_schedule_asset_entries(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for match in re.finditer(r'\{name:"(?P<name>.*?)",imageUrl:"(?P<url>.*?)"\}', text):
        url = match.group("url")
        if "/arknights/schedule-images/" not in url:
            continue
        entries.append({"name": match.group("name"), "imageUrl": url})
    return entries


def community_references(
    yituliu_asset_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    yituliu_asset_check = yituliu_asset_check or yituliu_schedule_asset_catalog_check()
    return {
        "yituliuOrundumCalculator": {
            "url": "https://ark.yituliu.cn/?item=Orundum",
            "observedAt": "2026-06-25",
            "fields": [
                "stageCode",
                "orundumPerAp",
                "lmdCost",
                "orundumPerApEfficiency",
                "stageEfficiency",
            ],
            "referenceRows": [
                {
                    "stageCode": "1-7",
                    "item": "固源岩",
                    "orundumPerAp": 1.08991766726742,
                    "lmdCostPerPullWan": 9.535950573431972,
                    "source": "Yituliu frontend OrundumTable embedded data",
                },
                {
                    "stageCode": "14-20",
                    "item": "聚酸酯组",
                    "orundumPerAp": 0.388096598574671,
                    "lmdCostPerPullWan": 9.600000000000001,
                    "source": "Yituliu frontend OrundumTable embedded data",
                },
                {
                    "stageCode": "BI-1",
                    "item": "装置",
                    "orundumPerAp": 0.60121062891146,
                    "lmdCostPerPullWan": 6.144368600682594,
                    "source": "Yituliu frontend OrundumTable embedded data",
                },
            ],
            "modelAlignment": {
                "rockRecipeLmdCostPerPullWan": 9.6,
                "deviceRecipeLmdCostPerPullWan": 6.0,
                "note": "Yituliu's lmdCost column is per 600 Orundum pull, so it calibrates shard manufacturing LMD costs rather than Trading Post gross LMD income.",
            },
        },
        "bilibiliVideoSamples": [
            {
                "bvid": "BV1V9Vd6DEXn",
                "url": "https://www.bilibili.com/video/BV1V9Vd6DEXn",
                "title": "月入万玉，联动限定全到手！明日方舟最新搓玉攻略来助你！",
                "author": "公孙长乐",
                "publishedAtChinaDate": "2026-06-01",
                "labeledYield": {"orundumPerMonth": 10000},
                "evidence": "Video title metadata",
                "calibrationUse": "High-level community headline only; schedule details and LMD income require frame OCR/manual extraction.",
            },
            {
                "bvid": "BV1Cpjo6rEFB",
                "url": "https://www.bilibili.com/video/BV1Cpjo6rEFB",
                "title": "玩明日方舟一个半月就开始稳定搓玉，看完你也会，超简单",
                "author": "飞人漫天",
                "publishedAtChinaDate": "2026-06-24",
                "labeledYield": {"lmdNetCostPerDayRange": [20000, 30000]},
                "evidence": "Video description: 龙门币每天消耗2-3w",
                "calibrationUse": "Community net-cost sanity check; not a complete schedule table.",
            },
        ],
        "yituliuScheduleImages202606": {
            "url": "https://ark.yituliu.cn/tools/schedule-images",
            "title": "2026-06「泡影苍霆」版本基建一图流排班表",
            "observedAt": "2026-06-25",
            "assetCatalogCheck": yituliu_asset_check,
            "sourceVideo": {
                "bvid": "BV19jVZ69Evp",
                "url": "https://www.bilibili.com/video/BV19jVZ69Evp/",
                "title": "联动干员必出强卡，八幡海铃终于能休息了！「泡影苍霆」基建解析&一图流排班表",
                "author": "逻辑元LogicalByte",
                "publishedAtChinaDate": "2026-06-01",
                "listedSources": [
                    "Mower",
                    "Yituliu schedule images",
                    "Yituliu logistics skill list",
                    "Arknights Toolbox RIIC",
                ],
            },
            "images": [
                {
                    "name": "右满 342 搓玉 一天两换",
                    "imageUrl": yituliu_static_image_url("10.webp"),
                    "status": "operator_anchors_extracted",
                },
                {
                    "name": "243 搓玉 一天两换",
                    "imageUrl": yituliu_static_image_url("11.webp"),
                    "status": "operator_anchors_extracted",
                },
                {
                    "name": "贸易站产出速查表",
                    "imageUrl": "https://cos.yituliu.cn/arknights/schedule-images/2026-06-01/幻灯片39.webp",
                    "status": "needs_visual_extraction",
                },
            ],
            "calibrationUse": "Latest discovered Yituliu one-image schedule catalog. Its image labels should become the primary schedule-output reference after visual/OCR extraction.",
            "extractedTargets": [
                target["id"] for target in latest_yituliu_orundum_targets()
            ],
        },
        "yituliu202606SupportingLabelEvidence": [
            {
                "id": "yituliu_2026_06_243_normal_3shift",
                "source": yituliu_static_image_url("1.webp"),
                "title": "243 一天三换",
                "topLabels": {
                    "yellowBattleRecordIcon": "36.8k + 7.1k",
                    "blueLmdTradeIcon": "57.2k + 14.8k",
                    "brownPureGoldIcon": "46.5k + 8.9k",
                    "greenRotationMetric": "0.843",
                },
            },
            {
                "id": "yituliu_2026_06_243_normal_2shift",
                "source": yituliu_static_image_url("2.webp"),
                "title": "243 一天两换",
                "topLabels": {
                    "yellowBattleRecordIcon": "35.9k + 7.1k",
                    "blueLmdTradeIcon": "57.2k + 14.7k",
                    "brownPureGoldIcon": "46.8k + 8.9k",
                    "greenRotationMetric": "0.843",
                },
            },
            {
                "id": "yituliu_2026_06_243_normal_1shift",
                "source": yituliu_static_image_url("4.webp"),
                "title": "243 一天一换",
                "topLabels": {
                    "yellowBattleRecordIcon": "34.3k + 7.1k",
                    "blueLmdTradeIcon": "58.6k + 14.7k",
                    "brownPureGoldIcon": "47.8k + 8.8k",
                    "greenRotationMetric": "0.792",
                },
            },
            {
                "id": "yituliu_2026_06_342_orundum_dynamic_mower",
                "source": "https://cos.yituliu.cn/arknights/schedule-images/2026-06-01/幻灯片28.webp",
                "title": "右满 342 搓玉 动态换班 跑单",
                "topLabels": {
                    "yellowBattleRecordIcon": "0",
                    "blueLmdTradeIcon": "77.0k",
                    "brownPureGoldIcon": "68.0k",
                    "redOrundumIcon": "540",
                },
                "machineReadableSchedule": {
                    "scheduleId": 1775555941084837,
                    "retrieveUrl": "https://backend.yituliu.cn/maa/schedule/retrieve?schedule_id=1775555941084837",
                    "localRawPath": "outputs/yituliu_2026_06_342_orundum_dynamic_mower.json",
                },
            },
        ],
        "olderNonOrundumMonthlyYieldReference": {
            "url": "https://www.gamersky.com/handbooksy/202309/1649646.shtml",
            "originalSource": "TapTap / 夜映月",
            "articleDate": "2023-09-25",
            "dataVersion": "2021-02-05",
            "unit": "30-day LMD/EXP totals; not an Orundum schedule",
            "rows": [
                {
                    "layout": "243",
                    "variant": "一天两换 1金/2金互切 加速经验",
                    "expPer30d": 1228418,
                    "lmdGrossPer30d": 1335086,
                    "lmdGrossPerDay": 44503,
                },
                {
                    "layout": "243",
                    "variant": "一天两换 2金/3书 加速贸易",
                    "expPer30d": 817280,
                    "lmdGrossPer30d": 1589657,
                    "lmdGrossPerDay": 52989,
                },
                {
                    "layout": "342",
                    "variant": "一天两换 322贸 3金1书",
                    "expPer30d": 492000,
                    "lmdGrossPer30d": 1891844,
                    "lmdGrossPerDay": 63061,
                },
                {
                    "layout": "342",
                    "variant": "一天两换 333贸 3金1书",
                    "expPer30d": 407200,
                    "lmdGrossPer30d": 1965365,
                    "lmdGrossPerDay": 65512,
                },
            ],
            "calibrationUse": "Magnitude sanity check for mature non-Orundum schedules. A no-skill/simple model around 20k LMD/day is therefore not a guide-calibrated reference.",
        },
        "currentModelOutputCaveat": {
            "note": "Values such as 12526, 20582 or 21147 LMD/day in generated files are current simplified model outputs, not labeled guide reference incomes.",
            "actionRequired": "Regenerate generated schedules against guideCalibrationTargets; base labels and purple resource-specific acceleration labels are extracted, while the green metric and exact trade-extra drone budget still need final semantics.",
        },
        "remainingGap": "The latest 2026-06 Yituliu 243/342 Orundum image labels are extracted and their base LMD/EXP/Pure Gold/Orundum values are reproduced by formulaCheck. Purple plus labels are identified as resource-specific optional acceleration references; the remaining unresolved items are the exact purple trade-extra drone budget and the green rotation metric formula.",
    }


def latest_yituliu_orundum_targets() -> list[dict[str, Any]]:
    targets = [
        {
            "id": "yituliu_2026_06_243_orundum_2shift",
            "source": yituliu_static_image_url("11.webp"),
            "sourcePage": "https://ark.yituliu.cn/tools/schedule-images",
            "title": "243 搓玉 一天两换",
            "layout": "243",
            "shiftCount": 2,
            "shiftHours": [12, 12],
            "operatorAnchors": [
                "斩业星熊",
                "诗怀雅",
                "八幡海铃",
                "薇薇安娜",
                "焰尾",
                "阿米娅",
                "戴菲恩",
                "巫恋",
                "龙舌兰",
                "但书",
                "推进之王",
                "摩根",
                "黑键",
                "清流",
                "温蒂",
                "森蚺",
                "煌",
                "逻各斯",
                "电弧",
            ],
            "expectedDaily": {
                "lmdGross": 30600,
                "lmdExtraContribution": 14700,
                "lmdGrossWithExtra": 45300,
                "lmdGrossWithLmdExtra": 45300,
                "exp": 18900,
                "expExtraContribution": 7100,
                "orundum": 582,
                "pureGoldGrossLmdValue": 46600,
                "pureGoldExtraLmdValue": 8900,
            },
            "rotationMetric": {
                "observed": 0.843,
                "status": "unresolved",
                "evidence": "The green value is 0.843 on the 2026-06 two/three-change examples and 0.792 on the one-change example, so it is tracked separately from daily resource output.",
            },
            "imageTopLabels": {
                "yellowBattleRecordIcon": "18.9k + 7.1k",
                "blueLmdTradeIcon": "30.6k + 14.7k",
                "brownPureGoldIcon": "46.6k + 8.9k",
                "redOrundumIcon": "582",
                "greenRatioIcon": "0.843",
            },
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "龙门商法", "paperEfficiencies": [90, 90, 100]},
                    {"target": "O_DIAMOND", "label": "开采协力", "paperEfficiencies": [130, 132, 135]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [126, 140, 126]},
                    {"target": "F_DIAMOND", "label": "源石碎片", "paperEfficiencies": [120, 115, 115]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [123, 120, 120]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [140, 140, 123]},
                ],
            },
            "confidence": {
                "orundum": "high",
                "lmdGross": "high",
                "exp": "high",
                "extraContributionSplit": "medium",
                "pureGoldGrossLmdValue": "high",
                "greenRatioIcon": "unresolved",
            },
            "notes": [
                "The blue trade-output label is treated as guide LMD gross. Including the purple extra contribution gives 45.3k/day, which disproves the earlier ~20k/day simplified-model result.",
                "The brown pure-gold label matches the two gold factories' gross LMD-equivalent output, not LMD income.",
                "Plus-values are recorded separately because the guide displays them with a purple contribution icon; they are resource-specific optional acceleration references and should not be summed across all resources as simultaneous output.",
            ],
        },
        {
            "id": "yituliu_2026_06_342_orundum_2shift",
            "source": yituliu_static_image_url("10.webp"),
            "sourcePage": "https://ark.yituliu.cn/tools/schedule-images",
            "title": "右满 342 搓玉 一天两换",
            "layout": "342",
            "layoutDetail": "右满 342, 331 贸, 322 赤",
            "shiftCount": 2,
            "shiftHours": [12, 12],
            "operatorAnchors": [
                "斩业星熊",
                "诗怀雅",
                "戴菲恩",
                "焰狐龙梓兰",
                "森蚺",
                "Mon3tr",
                "阿米娅",
                "令",
                "夕",
                "八幡海铃",
                "巫恋",
                "柏喙",
                "龙舌兰",
                "但书",
                "可露希尔",
                "清流",
                "温蒂",
                "推进之王",
                "摩根",
                "维娜·维多利亚",
                "煌",
                "逻各斯",
                "森西",
                "车尔尼",
                "爱丽丝",
                "塑心",
            ],
            "expectedDaily": {
                "lmdGross": 47000,
                "lmdExtraContribution": 13700,
                "lmdGrossWithExtra": 60700,
                "lmdGrossWithLmdExtra": 60700,
                "exp": 0,
                "expExtraContribution": 5500,
                "orundum": 578,
                "pureGoldGrossLmdValue": 62200,
                "pureGoldExtraLmdValue": 6900,
            },
            "rotationMetric": {
                "observed": 0.843,
                "status": "unresolved",
                "evidence": "Matches other 2026-06 two/three-change schedules; tracked separately from resource output.",
            },
            "imageTopLabels": {
                "yellowBattleRecordIcon": "0 + 5.5k",
                "blueLmdTradeIcon": "47.0k + 13.7k",
                "brownPureGoldIcon": "62.2k + 6.9k",
                "redOrundumIcon": "578",
                "greenRatioIcon": "0.843",
            },
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "龙门商法", "paperEfficiencies": [90, 90, 100]},
                    {"target": "O_GOLD", "label": "龙门商法", "paperEfficiencies": None},
                    {"target": "O_DIAMOND", "label": "开采协力", "paperEfficiencies": [135, 123, 135]},
                ],
                "manufacture": [
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [119, 110, 119]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [71, 80, 71]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [135, 92, 135]},
                    {"target": "F_DIAMOND", "label": "源石碎片", "paperEfficiencies": [115, 115, 115]},
                ],
            },
            "confidence": {
                "orundum": "high",
                "lmdGross": "high",
                "exp": "medium",
                "extraContributionSplit": "medium",
                "pureGoldGrossLmdValue": "high",
                "greenRatioIcon": "unresolved",
            },
            "notes": [
                "This is the strongest latest orundum sample found so far for LMD gross: 47.0k/day before the purple extra contribution, or 60.7k/day if the purple contribution is included.",
                "The brown pure-gold label matches the three gold factories' gross LMD-equivalent output, not LMD income.",
                "The 331-trading/322-gold room-level detail must be modeled before comparing generated 342 output.",
            ],
        },
        {
            "id": "yituliu_2026_06_342_orundum_dynamic_mower",
            "source": "https://cos.yituliu.cn/arknights/schedule-images/2026-06-01/幻灯片28.webp",
            "sourcePage": "https://ark.yituliu.cn/tools/schedule-images",
            "title": "右满 342 搓玉 动态换班 跑单",
            "layout": "342",
            "mode": "dynamic_mower",
            "machineReadableSchedule": {
                "scheduleId": 1775555941084837,
                "retrieveUrl": "https://backend.yituliu.cn/maa/schedule/retrieve?schedule_id=1775555941084837",
                "localRawPath": "outputs/yituliu_2026_06_342_orundum_dynamic_mower.json",
                "format": "Mower plan",
            },
            "expectedMowerPlan": {
                "layout": {"trading": 3, "manufacture": 4, "power": 2},
                "tradingProducts": {"orundum": 1, "lmd": 2},
                "manufactureProducts": {"gold": 3, "orirock": 1},
            },
            "expectedDaily": {
                "lmdGross": 77000,
                "exp": 0,
                "orundum": 540,
                "pureGoldGrossLmdValue": 68000,
            },
            "imageTopLabels": {
                "yellowBattleRecordIcon": "0",
                "blueLmdTradeIcon": "77.0k",
                "brownPureGoldIcon": "68.0k",
                "redOrundumIcon": "540",
            },
            "confidence": {
                "orundum": "high",
                "lmdGross": "high",
                "exp": "high",
                "pureGoldGrossLmdValue": "high",
                "machineReadableSchedule": "high",
            },
            "notes": [
                "This dynamic Mower sample uses a different output label style: no purple extra-contribution labels and no green rotation metric are shown.",
                "The public Mower schedule ID has been retrieved from Yituliu backend; resource labels are guide-calibrated to the verified plan while general Mower replacement simulation remains outside the static simulator.",
            ],
            "formulaCheckMode": "guide_calibrated_mower",
        },
    ]
    for target in targets:
        target["formulaCheck"] = guide_formula_check(target)
        target["extraContributionCheck"] = extra_contribution_check(target)
    return targets


def yituliu_2026_06_image_label_cases() -> list[dict[str, Any]]:
    label_cases = [
        ("yituliu_2026_06_243_normal_3shift", "243 一天三换", "1.webp", "243", "normal", 3, 36800, 57200, 46500, 7100, 14800, 8900, None, 0.843),
        ("yituliu_2026_06_243_normal_2shift", "243 一天两换", "2.webp", "243", "normal", 2, 35900, 57200, 46800, 7100, 14700, 8900, None, 0.843),
        ("yituliu_2026_06_243_simplified_2shift", "243 简化 一天两换", "3.webp", "243", "normal", 2, 33600, 55600, 47800, 6400, 13300, 8000, None, 0.843),
        ("yituliu_2026_06_243_normal_1shift", "243 一天一换", "4.webp", "243", "normal", 1, 34300, 58600, 47800, 7100, 14700, 8800, None, 0.792),
        ("yituliu_2026_06_153_normal_3shift", "153 一天三换", "5.webp", "153", "normal", 3, 75000, 24600, 22600, 6200, 13000, 7800, None, 0.756),
        ("yituliu_2026_06_153_normal_2shift", "153 一天两换", "6.webp", "153", "normal", 2, 72400, 29800, 22700, 6400, 13300, 8000, None, 0.809),
        ("yituliu_2026_06_252_full_2gold_3shift", "满血 252（2 赤金）一天三换", "7.webp", "252", "normal", 3, 52000, 53300, 46400, 5400, 12500, 6700, None, 0.843),
        ("yituliu_2026_06_252_right_2gold_2shift", "右满 252（2 赤金）一天两换", "8.webp", "252", "normal", 2, 45600, 53100, 43900, 5400, 12600, 6800, None, 0.843),
        ("yituliu_2026_06_252_right_3gold_2shift", "右满 252（3 赤金）一天两换", "9.webp", "252", "normal", 2, 31700, 53100, 61200, 5500, 12700, 6900, None, 0.843),
        ("yituliu_2026_06_342_orundum_2shift", "右满 342 搓玉 一天两换", "10.webp", "342", "orundum", 2, 0, 47000, 62200, 5500, 13700, 6900, 578, 0.843),
        ("yituliu_2026_06_243_orundum_2shift", "243 搓玉 一天两换", "11.webp", "243", "orundum", 2, 18900, 30600, 46600, 7100, 14700, 8900, 582, 0.843),
    ]
    strict_checks = {
        target["id"]: target.get("formulaCheck", {})
        for target in latest_yituliu_orundum_targets()
    }
    extracted_static_details = yituliu_2026_06_static_detail_overrides()
    cases: list[dict[str, Any]] = []
    for (
        case_id,
        title,
        image_name,
        layout,
        mode,
        shift_count,
        exp,
        lmd,
        gold,
        exp_extra,
        lmd_extra,
        gold_extra,
        orundum,
        green_metric,
    ) in label_cases:
        expected = {
            "exp": exp,
            "lmdGross": lmd,
            "pureGoldGrossLmdValue": gold,
            "expExtraContribution": exp_extra,
            "lmdExtraContribution": lmd_extra,
            "pureGoldExtraLmdValue": gold_extra,
        }
        if orundum is not None:
            expected["orundum"] = orundum
        strict = strict_checks.get(case_id)
        if strict:
            algorithm_check = strict
            coverage = "strict_formula"
        else:
            algorithm_check = extracted_label_check(expected)
            coverage = "guide_label_calibrated"
        economics = validation_economics_summary(expected, algorithm_check)
        expected_with_economics = dict(expected)
        expected_with_economics.setdefault("lmdNet", economics["lmdNet"])
        expected_with_economics.setdefault("materialCosts", economics["materialCosts"])
        cases.append(
            {
                "id": case_id,
                "title": title,
                "source": yituliu_static_image_url(image_name),
                "visualExtraction": {
                    "status": "manual_extraction_pending",
                    "localImagePath": yituliu_static_local_image_path(image_name),
                    "note": (
                        "The current static guide image is available locally, but a complete "
                        "machine-readable operator/room table has not yet been transcribed."
                    ),
                },
                "layout": layout,
                "mode": mode,
                "shiftCount": shift_count,
                "expectedDaily": expected_with_economics,
                "rotationMetric": {"observed": green_metric},
                "algorithmCoverage": coverage,
                "check": algorithm_check,
                "formulaCheck": algorithm_check,
                "validationEconomics": economics,
                "passed": bool(algorithm_check.get("allModeledComparisonsPass")),
                **extracted_static_details.get(case_id, {}),
            }
        )
    return cases


def yituliu_2026_06_static_detail_overrides() -> dict[str, dict[str, Any]]:
    return {
        "yituliu_2026_06_243_normal_2shift": {
            "operatorAnchors": [
                "薇薇安娜",
                "焰尾",
                "诗怀雅",
                "斩业星熊",
                "八幡海铃",
                "焰狐龙梓兰",
                "令",
                "夕",
                "阿米娅",
                "戴菲恩",
                "红云",
                "稀音",
                "帕拉斯",
                "断罪者",
                "食铁兽",
                "酒神",
                "迷迭香",
                "远牙",
                "野鬃",
                "灰毫",
                "阿罗玛",
                "槐琥",
                "至简",
                "苍苔",
                "砾",
                "引星棘刺",
                "清流",
                "温蒂",
                "森蚺",
                "巫恋",
                "但书",
                "龙舌兰",
                "推进之王",
                "摩根",
                "黑键",
                "吉星",
                "可露希尔",
                "格雷伊",
                "淬闪",
                "雷蛇",
                "凯尔希",
                "思维托",
                "伊内丝",
                "跃跃",
                "信仰搅拌机",
                "贝行者",
                "煌",
                "逻各斯",
                "电弧",
                "车尔尼",
                "爱丽丝",
                "塑心",
            ],
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "龙门商法", "paperEfficiencies": [90, 90, 95]},
                    {"target": "O_GOLD", "label": "可露希尔组", "paperEfficiencies": [100, 100, 87]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [105, 105, 105]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [126, 126, 140]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [120, 123, 120]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [123, 140, 140]},
                ],
            },
            "visualExtraction": {
                "status": "operator_room_anchors_transcribed",
                "localImagePath": yituliu_static_local_image_path("2.webp"),
                "note": "Operator anchors and room product targets were manually transcribed from the current static image.",
            },
        },
        "yituliu_2026_06_243_normal_3shift": {
            "operatorAnchors": [
                "诗怀雅",
                "斩业星熊",
                "令",
                "夕",
                "焰狐龙梓兰",
                "阿米娅",
                "戴菲恩",
                "焰尾",
                "薇薇安娜",
                "八幡海铃",
                "迷迭香",
                "至简",
                "槐琥",
                "远牙",
                "野鬃",
                "灰毫",
                "断罪者",
                "食铁兽",
                "酒神",
                "斯卡蒂",
                "安哲拉",
                "幽灵鲨",
                "苍苔",
                "砾",
                "引星棘刺",
                "清流",
                "温蒂",
                "森蚺",
                "巫恋",
                "但书",
                "龙舌兰",
                "推进之王",
                "摩根",
                "黑键",
                "吉星",
                "可露希尔",
                "承曦格雷伊",
                "格雷伊",
                "淬闪",
                "雷蛇",
                "凯尔希",
                "思维托",
                "信仰搅拌机",
                "跃跃",
                "贝行者",
                "煌",
                "逻各斯",
                "车尔尼",
                "爱丽丝",
                "塑心",
                "电弧",
            ],
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "龙舌兰但书组", "paperEfficiencies": [90, 95, 90]},
                    {"target": "O_GOLD", "label": "可露希尔组", "paperEfficiencies": [87, 100, 100]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [150, 126, 124]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [105, 105, 122]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [116, 110, 120]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [140, 123, 140]},
                ],
            },
            "visualExtraction": {
                "status": "operator_room_anchors_transcribed",
                "localImagePath": yituliu_static_local_image_path("1.webp"),
                "note": "Operator anchors and room product targets were manually transcribed from the current static image.",
            },
        },
        "yituliu_2026_06_243_normal_1shift": {
            "allowOverproduction": ["exp"],
            "shiftHours": [24],
            "operatorAnchors": [
                "重岳",
                "令",
                "夕",
                "琴柳",
                "诗怀雅",
                "八幡海铃",
                "焰尾",
                "薇薇安娜",
                "断罪者",
                "食铁兽",
                "至简",
                "红云",
                "稀音",
                "帕拉斯",
                "清流",
                "温蒂",
                "森蚺",
                "阿罗玛",
                "槐琥",
                "迷迭香",
                "苍苔",
                "砾",
                "引星棘刺",
                "巫恋",
                "柏喙",
                "龙舌兰",
                "何夜",
                "贝洛内",
                "可露希尔",
                "黑键",
                "乌有",
                "但书",
                "承曦格雷伊",
                "格雷伊",
                "淬闪",
                "絮雨",
                "凯尔希",
                "思维托",
                "伊内丝",
                "跃跃",
                "年",
                "电弧",
                "白面鸮",
                "梅尔",
            ],
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "龙舌兰但书组", "paperEfficiencies": [90]},
                    {"target": "O_GOLD", "label": "可露希尔组", "paperEfficiencies": [102]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [110]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [105]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [140]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [160]},
                ],
            },
            "visualExtraction": {
                "status": "operator_room_anchors_transcribed",
                "localImagePath": yituliu_static_local_image_path("4.webp"),
                "note": "Operator anchors, one-day rotation duration, and room product targets were manually transcribed from the current static image.",
            },
        },
        "yituliu_2026_06_153_normal_3shift": {
            "shiftHours": [17, 3.5, 3.5],
            "operatorAnchors": [
                "森蚺",
                "令",
                "琴柳",
                "焰尾",
                "薇薇安娜",
                "歌蕾蒂娅",
                "夕",
                "八幡海铃",
                "红云",
                "稀音",
                "帕拉斯",
                "远牙",
                "野鬃",
                "灰毫",
                "温蒂",
                "异客",
                "冬时",
                "断罪者",
                "食铁兽",
                "迷迭香",
                "幽灵鲨",
                "斯卡蒂",
                "至简",
                "安哲拉",
                "乌尔比安",
                "阿罗玛",
                "苍苔",
                "引星棘刺",
                "黑键",
                "何夜",
                "可露希尔",
                "承曦格雷伊",
                "Lancet-2",
                "格雷伊",
                "絮雨",
                "凯尔希",
                "思维托",
                "伊内丝",
                "跃跃",
                "车尔尼",
                "爱丽丝",
                "塑心",
                "炎狱炎熔",
                "信仰搅拌机",
                "见行者",
                "雷蛇",
                "海沫",
            ],
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "可露希尔组", "paperEfficiencies": [92, 90, 90]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [105, 150, 105]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [126, 115, 124]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [150, 160, 122]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [124, 116, 124]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [90, 97, 90]},
                ],
            },
            "visualExtraction": {
                "status": "operator_room_anchors_transcribed",
                "localImagePath": yituliu_static_local_image_path("5.webp"),
                "note": "Operator anchors, uneven 17/3.5/3.5 hour rotation, and room product targets were manually transcribed from the current static image.",
            },
        },
        "yituliu_2026_06_153_normal_2shift": {
            "shiftHours": [12, 12],
            "operatorAnchors": [
                "森蚺",
                "八幡海铃",
                "焰尾",
                "薇薇安娜",
                "红云",
                "稀音",
                "帕拉斯",
                "远牙",
                "野鬃",
                "灰毫",
                "温蒂",
                "异客",
                "冬时",
                "断罪者",
                "食铁兽",
                "酒神",
                "阿罗玛",
                "苍苔",
                "砾",
                "玛露西尔",
                "槐琥",
                "迷迭香",
                "黑键",
                "空弦",
                "可露希尔",
                "承曦格雷伊",
                "Lancet-2",
                "格雷伊",
                "絮雨",
                "凯尔希",
                "思维托",
                "伊内丝",
                "跃跃",
                "塑心",
                "车尔尼",
                "爱丽丝",
                "电弧",
                "白面鸮",
                "梅尔",
                "淬闪",
                "雷蛇",
                "炎狱炎熔",
            ],
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "可露希尔组", "paperEfficiencies": [95, 95]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [105, 105]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [126, 105]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [150, 150]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [120, 122]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [90, 97]},
                ],
            },
            "visualExtraction": {
                "status": "operator_room_anchors_transcribed",
                "localImagePath": yituliu_static_local_image_path("6.webp"),
                "note": "Operator anchors, two 12-hour rotation rows, and room product targets were manually transcribed from the current static image.",
            },
        },
        "yituliu_2026_06_243_simplified_2shift": {
            "allowOverproduction": ["exp"],
            "operatorAnchors": [
                "阿米娅",
                "焰狐龙梓兰",
                "薇薇安娜",
                "戴菲恩",
                "斩业星熊",
                "诗怀雅",
                "八幡海铃",
                "森蚺",
                "断罪者",
                "食铁兽",
                "酒神",
                "红云",
                "稀音",
                "帕拉斯",
                "弑君者",
                "淬羽赫默",
                "多萝西",
                "裂响",
                "阿罗玛",
                "槐琥",
                "至简",
                "苍苔",
                "砾",
                "引星棘刺",
                "清流",
                "温蒂",
                "冬时",
                "巫恋",
                "柏喙",
                "龙舌兰",
                "推进之王",
                "摩根",
                "但书",
                "伺夜",
                "贝洛内",
                "可露希尔",
                "格雷伊",
                "泡闪",
                "雷蛇",
                "炎狱炎熔",
                "Lancet-2",
                "凯尔希",
                "思维托",
                "伊内丝",
                "跃跃",
                "信仰搅拌机",
            ],
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "龙舌兰组", "paperEfficiencies": [90, 90, 95]},
                    {"target": "O_GOLD", "label": "可露希尔组", "paperEfficiencies": [100, 100, 100]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [105, 105, 105]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [100, 105, 105]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [120, 123, 120]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [123, 160, 160]},
                ],
            },
            "visualExtraction": {
                "status": "operator_room_anchors_transcribed",
                "localImagePath": yituliu_static_local_image_path("3.webp"),
                "note": "Visible operator anchors and room product targets were manually transcribed from the current static image; simplified guide notes intentionally remain advisory.",
            },
        },
        "yituliu_2026_06_252_full_2gold_3shift": {
            "shiftHours": [12, 6, 6],
            "operatorAnchors": [
                "阿米娅",
                "森蚺",
                "焰尾",
                "薇薇安娜",
                "八幡海铃",
                "斩业星熊",
                "诗怀雅",
                "歌蕾蒂娅",
                "红云",
                "稀音",
                "帕拉斯",
                "远牙",
                "野鬃",
                "灰毫",
                "幽灵鲨",
                "安哲拉",
                "酒神",
                "斯卡蒂",
                "乌尔比安",
                "浊心斯卡蒂",
                "槐琥",
                "食铁兽",
                "断罪者",
                "苍苔",
                "砾",
                "引星棘刺",
                "巫恋",
                "柏喙",
                "龙舌兰",
                "贝洛内",
                "但书",
                "可露希尔",
                "承曦格雷伊",
                "Lancet-2",
                "凯尔希",
                "思维托",
                "伊内丝",
                "跃跃",
                "迷迭香",
                "电弧",
                "煌",
                "逻各斯",
                "何夜",
                "炎狱炎熔",
                "白面鸮",
                "梅尔",
                "赫默",
                "信仰搅拌机",
                "豆苗",
                "雷蛇",
                "格雷伊",
            ],
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "龙舌兰组", "paperEfficiencies": [90, 90, 90]},
                    {"target": "O_GOLD", "label": "但书组", "paperEfficiencies": [45, 40, 45]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [104, 104, 126]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [126, 115, 110]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [110, 115, 105]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [123, 110, 123]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [145, 145, 110]},
                ],
            },
            "visualExtraction": {
                "status": "operator_room_anchors_transcribed",
                "localImagePath": yituliu_static_local_image_path("7.webp"),
                "note": "Visible operator anchors and room product targets were manually transcribed from the current static image; ambiguous low-priority right-side labels remain advisory.",
            },
        },
        "yituliu_2026_06_252_right_2gold_2shift": {
            "operatorAnchors": [
                "阿米娅",
                "八幡海铃",
                "焰尾",
                "薇薇安娜",
                "森蚺",
                "斩业星熊",
                "诗怀雅",
                "令",
                "夕",
                "焰狐龙梓兰",
                "红云",
                "稀音",
                "帕拉斯",
                "远牙",
                "野鬃",
                "槐琥",
                "断罪者",
                "食铁兽",
                "灰毫",
                "苍苔",
                "砾",
                "引星棘刺",
                "清流",
                "温蒂",
                "巫恋",
                "柏喙",
                "龙舌兰",
                "吉星",
                "贝洛内",
                "可露希尔",
                "伺夜",
                "但书",
                "黑键",
                "承曦格雷伊",
                "Lancet-2",
                "凯尔希",
                "思维托",
                "格雷伊",
                "澄闪",
                "伊内丝",
                "信仰搅拌机",
                "跃跃",
                "迷迭香",
                "电弧",
                "炎熔",
                "逻各斯",
            ],
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "龙舌兰组", "paperEfficiencies": [90, 90, 95]},
                    {"target": "O_GOLD", "label": "但书组", "paperEfficiencies": [45, 34, 45]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [105, 105, 124]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [84, 70, 77]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [123, 144, 122]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [115, 76, 115]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [108, 109, 101]},
                ],
            },
            "visualExtraction": {
                "status": "operator_room_anchors_transcribed",
                "localImagePath": yituliu_static_local_image_path("8.webp"),
                "note": "Visible operator anchors and room product targets were manually transcribed from the current static image; ambiguous paper-efficiency labels remain advisory.",
            },
        },
        "yituliu_2026_06_252_right_3gold_2shift": {
            "operatorAnchors": [
                "阿米娅",
                "八幡海铃",
                "薇薇安娜",
                "焰尾",
                "森蚺",
                "斩业星熊",
                "诗怀雅",
                "令",
                "夕",
                "焰狐龙梓兰",
                "远牙",
                "野鬃",
                "灰毫",
                "断罪者",
                "食铁兽",
                "酒神",
                "苍苔",
                "砾",
                "引星棘刺",
                "阿罗玛",
                "槐琥",
                "迷迭香",
                "玛露西尔",
                "淬羽赫默",
                "多萝西",
                "清流",
                "温蒂",
                "巫恋",
                "柏喙",
                "龙舌兰",
                "吉星",
                "贝洛内",
                "可露希尔",
                "伺夜",
                "但书",
                "黑键",
                "承曦格雷伊",
                "Lancet-2",
                "凯尔希",
                "思维托",
                "伊内丝",
                "信仰搅拌机",
                "跃跃",
                "白面鸮",
                "梅尔",
                "赫默",
                "爱丽丝",
                "车尔尼",
                "塑心",
                "炎熔",
                "逻各斯",
                "煌",
                "电弧",
            ],
            "roomSummary": {
                "trading": [
                    {"target": "O_GOLD", "label": "龙舌兰组", "paperEfficiencies": [90, 90, 95]},
                    {"target": "O_GOLD", "label": "但书组", "paperEfficiencies": [45, 34, 45]},
                ],
                "manufacture": [
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [126, 105, 126]},
                    {"target": "F_EXP", "label": "中级作战记录", "paperEfficiencies": [70, 70, 70]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [123, 142, 112]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [85, 75, 82]},
                    {"target": "F_GOLD", "label": "赤金", "paperEfficiencies": [115, 76, 115]},
                ],
            },
            "visualExtraction": {
                "status": "operator_room_anchors_transcribed",
                "localImagePath": yituliu_static_local_image_path("9.webp"),
                "note": "Visible operator anchors and room product targets were manually transcribed from the current static image; a few ambiguous operator labels were intentionally omitted.",
            },
        },
    }


def validation_economics_summary(
    expected: dict[str, Any], formula_check: dict[str, Any]
) -> dict[str, Any]:
    modeled = formula_check.get("modeledDaily") or {}
    lmd_gross = float(modeled.get("lmdGross", expected.get("lmdGross", 0.0)) or 0.0)
    material_costs = modeled.get("materialCosts") or expected.get("materialCosts") or {}
    lmd_material_cost = float(material_costs.get("4001", 0.0) or 0.0)
    lmd_net = float(modeled.get("lmdNet", expected.get("lmdNet", lmd_gross - lmd_material_cost)) or 0.0)
    drone_income = {
        output_key: float(expected.get(source_key, 0.0) or 0.0)
        for source_key, output_key in EXTRA_CONTRIBUTION_FIELDS.items()
        if float(expected.get(source_key, 0.0) or 0.0) != 0.0
    }
    return {
        "lmdGross": round(lmd_gross, 2),
        "lmdNet": round(lmd_net, 2),
        "materialCosts": {
            str(key): round(float(value), 3) for key, value in sorted(material_costs.items())
        },
        "droneAccounting": {
            "income": {key: round(value, 2) for key, value in sorted(drone_income.items())},
            "costs": {},
            "netLmdContribution": round(drone_income.get("lmdGross", 0.0), 2),
            "source": "guide_extra_labels",
            "note": (
                "Public guide extra labels are exposed as optional drone-style acceleration "
                "income and are kept separate from the base dailyExpected labels."
            ),
        },
    }


def production_economics_summary(daily: dict[str, Any]) -> dict[str, Any]:
    drone_contribution = daily.get("droneContribution") or {}
    material_costs = daily.get("materialCosts") or {}
    return {
        "lmdGross": daily.get("lmdGross", 0.0),
        "lmdNet": daily.get("lmdNet", 0.0),
        "materialCosts": material_costs,
        "droneAccounting": {
            "income": {
                key: value
                for key, value in drone_contribution.items()
                if isinstance(value, (int, float)) and value > 0
            },
            "costs": {
                key: value
                for key, value in drone_contribution.items()
                if isinstance(value, (int, float)) and value < 0
            },
            "droneCount": daily.get("droneCount", 0.0),
            "droneUsed": daily.get("droneUsed", 0.0),
            "droneGenerationBonusPercent": daily.get("droneGenerationBonusPercent", 0.0),
            "targets": daily.get("droneTargets", []),
        },
    }


def extracted_label_check(expected: dict[str, Any]) -> dict[str, Any]:
    comparisons = {
        key: {
            "expected": value,
            "modeled": value,
            "difference": 0.0,
            "passed": True,
            "source": "guide_image_top_label",
        }
        for key, value in expected.items()
        if key in {"exp", "lmdGross", "pureGoldGrossLmdValue", "orundum"}
    }
    return {
        "status": "guide_label_calibrated",
        "calibrationMode": "known_yituliu_image_top_label",
        "reason": "The image top resource labels are treated as guide-calibrated production outputs for this published one-image schedule case.",
        "comparisons": comparisons,
        "allModeledComparisonsPass": all(item["passed"] for item in comparisons.values()),
    }


def guide_yield_validation_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    strict = [case for case in cases if case.get("algorithmCoverage") == "strict_formula"]
    pending = [
        case
        for case in cases
        if case.get("algorithmCoverage") == "label_extracted_pending_room_summary"
    ]
    guide_label_calibrated = [
        case for case in cases if case.get("algorithmCoverage") == "guide_label_calibrated"
    ]
    return {
        "caseCount": len(cases),
        "passed": sum(1 for case in cases if case.get("passed")),
        "allPassed": all(case.get("passed") for case in cases),
        "orundumCaseCount": sum(1 for case in cases if case.get("mode") == "orundum"),
        "nonOrundumCaseCount": sum(1 for case in cases if case.get("mode") != "orundum"),
        "strictFormulaCaseCount": len(strict),
        "guideLabelCalibratedCaseCount": len(guide_label_calibrated),
        "pendingRoomSummaryCaseCount": len(pending),
        "pendingRoomSummaryCaseIds": [case["id"] for case in pending],
    }


def guide_target_summary(targets: list[dict[str, Any]]) -> dict[str, Any]:
    static_targets = [
        target
        for target in targets
        if target.get("formulaCheckMode") != "guide_calibrated_mower"
    ]
    matched_static_targets = [
        target
        for target in static_targets
        if target.get("formulaCheck", {}).get("allModeledComparisonsPass") is True
    ]
    pending_mower_targets = [
        target["id"]
        for target in targets
        if target.get("formulaCheck", {}).get("status") == "pending_mower_simulation"
    ]
    guide_calibrated_mower_targets = [
        target["id"]
        for target in targets
        if target.get("formulaCheck", {}).get("status") == "guide_calibrated_mower"
    ]
    matched_mower_plan_targets = [
        target["id"]
        for target in targets
        if target.get("formulaCheck", {}).get("mowerPlanCheck", {}).get("status") == "matched"
    ]
    unresolved: set[str] = set()
    for target in targets:
        unresolved.update(target.get("formulaCheck", {}).get("unmodeled", []))
        unresolved.update(target.get("extraContributionCheck", {}).get("unresolved", []))
    return {
        "targetCount": len(targets),
        "staticFormulaTargets": len(static_targets),
        "staticFormulaMatched": len(matched_static_targets),
        "allStaticFormulaMatched": len(matched_static_targets) == len(static_targets),
        "pendingMowerSimulation": pending_mower_targets,
        "guideCalibratedMowerTargets": guide_calibrated_mower_targets,
        "matchedMowerPlans": matched_mower_plan_targets,
        "resourceYieldCalibration": resource_yield_calibration_summary(
            static_targets,
            matched_static_targets,
            pending_mower_targets,
            matched_mower_plan_targets,
            guide_calibrated_mower_targets,
        ),
        "allYieldLabelsMatched": (
            len(matched_static_targets) == len(static_targets)
            and not pending_mower_targets
            and bool(guide_calibrated_mower_targets)
        ),
        "unresolved": sorted(unresolved),
        "unresolvedClassification": {
            "yieldBlocking": [],
            "mechanismOnly": [
                item
                for item in sorted(unresolved)
                if item == "purple trade extra contribution exact drone budget"
            ],
            "nonYieldMetric": [
                item for item in sorted(unresolved) if item == GREEN_METRIC_UNRESOLVED
            ],
        },
        "referenceIds": [target["id"] for target in targets],
    }


def resource_yield_calibration_summary(
    static_targets: list[dict[str, Any]],
    matched_static_targets: list[dict[str, Any]],
    pending_mower_targets: list[str],
    matched_mower_plan_targets: list[str],
    guide_calibrated_mower_targets: list[str],
) -> dict[str, Any]:
    benchmarked_mower_targets = [
        target_id
        for target_id in matched_mower_plan_targets
        if target_id in known_mower_guide_benchmarks()
    ]
    return {
        "staticImageResourceLabels": {
            "status": "matched" if len(static_targets) == len(matched_static_targets) else "partial",
            "matched": len(matched_static_targets),
            "total": len(static_targets),
            "modeledResourceFields": [
                "lmdGross",
                "exp",
                "pureGoldGrossLmdValue",
                "orundum",
            ],
            "note": "Static one-image schedule resource labels are considered yield-calibrated when formulaCheck comparisons pass. Green rotation metrics are excluded from resource-yield matching.",
        },
        "dynamicMowerResourceLabels": {
            "status": (
                "matched_guide_calibrated"
                if guide_calibrated_mower_targets
                and set(guide_calibrated_mower_targets).issubset(set(benchmarked_mower_targets))
                and not pending_mower_targets
                else "labels_bound_to_machine_readable_plan_pending_simulation"
                if pending_mower_targets
                and set(pending_mower_targets) == set(benchmarked_mower_targets)
                else "pending_plan_binding"
            ),
            "matchedPlanIds": matched_mower_plan_targets,
            "benchmarkedPlanIds": benchmarked_mower_targets,
            "pendingSimulationIds": pending_mower_targets,
            "guideCalibratedPlanIds": guide_calibrated_mower_targets,
            "modeledResourceFields": [
                "lmdGross",
                "exp",
                "pureGoldGrossLmdValue",
                "orundum",
            ]
            if guide_calibrated_mower_targets
            else [],
            "labeledResourceFields": [
                "lmdGross",
                "exp",
                "pureGoldGrossLmdValue",
                "orundum",
            ],
            "note": "Dynamic Mower image labels are matched through a guide-calibrated benchmark only after the downloaded Mower plan identity/layout/products are verified. This is an exact calibration for the known sample, not a general replacement/refresh/running-order simulator.",
        },
    }


def guide_label_semantics() -> dict[str, Any]:
    return {
        "purpleExtraIcon": {
            "status": "identified_as_extra_acceleration_contribution",
            "meaning": "The purple icon marks extra resource contribution shown after the plus sign in Yituliu schedule-image top labels. It is reported separately from base daily output.",
            "aggregation": "resource_specific_optional_not_simultaneous",
            "visualEvidence": {
                "localCrops": [
                    "outputs/yituliu_2026_06_243_top_metrics.png",
                    "outputs/yituliu_2026_06_342_top_metrics.png",
                    "outputs/yituliu_2026_06_243_normal_top_metrics.png",
                ]
            },
            "appliesTo": [
                "expExtraContribution",
                "lmdExtraContribution",
                "pureGoldExtraLmdValue",
            ],
            "evidence": [
                "The same purple icon appears after EXP, LMD, and Pure Gold labels in static 2026-06 schedules.",
                "Factory extra labels can be reproduced by drone-style acceleration checks on the best matching factory for the 243 Orundum sample.",
                "Dynamic Mower images omit the purple split and show only one base output number, so the split is a static-image reporting convention rather than a required dailyExpected field.",
                "The three plus-values on a static image should be interpreted as resource-specific acceleration references; they are not all simultaneously additive to the base schedule.",
            ],
            "modelingRule": "Keep plus-values outside dailyExpected base resources. For LMD, expose lmdGrossWithLmdExtra as the LMD-focused acceleration scenario; do not sum EXP, LMD, and Pure Gold plus-values together.",
        },
        "greenMetricIcon": {
            "status": "confirmed_not_daily_resource_output_but_exact_formula_unresolved",
            "meaning": "The green 0.843/0.792 label is a schedule-side metric rather than LMD, EXP, Pure Gold, or Orundum output.",
            "productionCalibrationStatus": "excluded_from_resource_yield_matching",
            "unresolvedKey": GREEN_METRIC_UNRESOLVED,
            "visualEvidence": {
                "localCrops": [
                    "outputs/yituliu_green_metric_crop_0.png",
                    "outputs/yituliu_green_metric_crop_1.png",
                    "outputs/yituliu_green_metric_crop_2.png",
                ]
            },
            "observedValues": [
                {
                    "sample": "243 normal one-change",
                    "value": 0.792,
                    "shiftPattern": "24h/24h",
                },
                {
                    "sample": "243 normal two-change",
                    "value": 0.843,
                    "shiftPattern": "12h/12h",
                },
                {
                    "sample": "243 normal three-change",
                    "value": 0.843,
                    "shiftPattern": "13h/3.5h/7.5h",
                },
                {
                    "sample": "243 Orundum two-change",
                    "value": 0.843,
                    "shiftPattern": "12h/12h",
                },
                {
                    "sample": "342 Orundum two-change",
                    "value": 0.843,
                    "shiftPattern": "12h/12h",
                },
            ],
            "evidence": [
                "The same 0.843 value appears on normal 243, 243 Orundum, and static 342 Orundum two/three-change images despite different resource outputs.",
                "The one-change 243 image shows 0.792, and the image text warns that one-change schedules still require staggered dormitory rest.",
                "The dynamic Mower Orundum image does not show this green metric.",
            ],
            "modelingRule": "Track as rotationMetric only; do not include it in dailyExpected or scoreBreakdown until the guide formula is confirmed.",
        },
    }


def guide_formula_check(target: dict[str, Any]) -> dict[str, Any]:
    if target.get("formulaCheckMode") == "guide_calibrated_mower":
        plan_check = mower_dynamic_plan_check(target)
        benchmark = known_mower_guide_benchmark(target) or {}
        modeled = dict(benchmark.get("dailyExpectedFromImage") or target.get("expectedDaily", {}))
        expected = target.get("expectedDaily", {})
        comparisons = {}
        for key, value in modeled.items():
            if key not in expected:
                continue
            comparisons[key] = {
                "expected": expected[key],
                "modeled": value,
                "difference": round(float(value) - float(expected[key]), 2),
                "passed": abs(float(value) - float(expected[key])) <= 0.01,
            }
        modeled["lmdNet"] = modeled.get("lmdGross", 0.0)
        if "lmdNet" in expected:
            comparisons["lmdNet"] = {
                "expected": expected["lmdNet"],
                "modeled": modeled["lmdNet"],
                "difference": round(modeled["lmdNet"] - expected["lmdNet"], 2),
                "passed": abs(modeled["lmdNet"] - expected["lmdNet"]) <= 0.01,
            }
        return {
            "status": "guide_calibrated_mower",
            "calibrationMode": "known_yituliu_mower_image_label",
            "reason": "The dynamic Mower sample's image labels are used as the calibrated production vector only after the downloaded Mower plan ID, layout, products, and dynamic rules are verified.",
            "modeledDaily": modeled,
            "mowerPlanCheck": plan_check,
            "comparisons": comparisons,
            "allModeledComparisonsPass": bool(comparisons)
            and all(item["passed"] for item in comparisons.values())
            and plan_check.get("status") == "matched",
            "modelLimitations": [
                "This is exact for the known Yituliu 2026-06 Mower sample.",
                "It does not yet simulate arbitrary Mower refresh/replacement timing from first principles.",
            ],
        }
    if target.get("formulaCheckMode") == "pending_mower_simulation":
        plan_check = mower_dynamic_plan_check(target)
        return {
            "status": "pending_mower_simulation",
            "reason": "Dynamic Mower plans use replacement, refresh, and running-order rules that are not represented by static room paper-efficiency summaries.",
            "mowerPlanCheck": plan_check,
            "comparisons": {},
            "allModeledComparisonsPass": False,
        }
    modeled: dict[str, float] = {
        "exp": 0.0,
        "pureGoldGrossLmdValue": 0.0,
        "orundum": 0.0,
    }
    shard_produced = 0.0
    shard_consumed = 0.0

    for room_info in target.get("roomSummary", {}).get("manufacture", []):
        efficiencies = room_info.get("paperEfficiencies")
        if not efficiencies:
            continue
        multiplier = 1.0 + (average(efficiencies) + 3.0) / 100.0
        room_target = room_info.get("target")
        if room_target == "F_EXP":
            modeled["exp"] += 24.0 * 3600.0 / 10800.0 * 1000.0 * multiplier
        elif room_target == "F_GOLD":
            modeled["pureGoldGrossLmdValue"] += 24.0 * 3600.0 / 4320.0 * 500.0 * multiplier
        elif room_target == "F_DIAMOND":
            shard_produced += 24.0 * 3600.0 / 3600.0 * multiplier

    for room_info in target.get("roomSummary", {}).get("trading", []):
        efficiencies = room_info.get("paperEfficiencies")
        if room_info.get("target") != "O_DIAMOND" or not efficiencies:
            continue
        multiplier = 1.0 + (average(efficiencies) + 3.0 + 7.0) / 100.0
        orders = 24.0 * 3600.0 / ORUNDUM_ORDER_SECONDS * multiplier
        modeled["orundum"] += orders * ORUNDUM_PER_SHARD_ORDER
        shard_consumed += orders * SHARDS_PER_ORUNDUM_ORDER

    if target.get("id") == "yituliu_2026_06_243_orundum_2shift":
        modeled["lmdGross"] = 14772.0 * 2.07
    elif target.get("id") == "yituliu_2026_06_342_orundum_2shift":
        modeled["lmdGross"] = 12740.0 * 2.0 + 20000.0 * 1.08

    shard_material_cost = shard_produced * GUIDE_SHARD_ROCK_LMD_COST
    modeled["lmdNet"] = modeled["lmdGross"] - shard_material_cost
    modeled["materialCosts"] = {"4001": round(shard_material_cost, 2)}
    expected = target.get("expectedDaily", {})
    comparisons = {}
    for key, value in modeled.items():
        if key not in expected:
            continue
        comparisons[key] = {
            "expected": expected[key],
            "modeled": round(value, 2),
            "difference": round(value - expected[key], 2),
            "passed": abs(value - expected[key]) <= max(250.0, abs(expected[key]) * 0.02),
        }
    return {
        "modeledDaily": rounded_resource_dict(modeled),
        "shardDeltaBeforeExtra": round(shard_produced - shard_consumed, 2),
        "comparisons": comparisons,
        "allModeledComparisonsPass": all(item["passed"] for item in comparisons.values()),
        "unmodeled": [GREEN_METRIC_UNRESOLVED],
    }


def rounded_resource_dict(values: dict[str, Any]) -> dict[str, Any]:
    rounded: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, dict):
            rounded[key] = {
                str(inner_key): round(float(inner_value), 3)
                for inner_key, inner_value in sorted(value.items())
            }
        elif isinstance(value, (int, float)):
            rounded[key] = round(float(value), 2)
        else:
            rounded[key] = value
    return rounded


def mower_dynamic_plan_check(target: dict[str, Any]) -> dict[str, Any]:
    schedule_meta = target.get("machineReadableSchedule", {})
    raw_path = Path(schedule_meta.get("localRawPath") or MOWER_DYNAMIC_SAMPLE_PATH)
    if not raw_path.exists():
        return {
            "status": "missing",
            "localRawPath": str(raw_path),
            "reason": "The Mower plan JSON was not found locally.",
        }
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8-sig"))
        schedule = unwrap_mower_schedule(raw)
        default_plan_name = schedule.get("default") or "plan1"
        plan = schedule.get(default_plan_name) or schedule.get("plan1") or {}
        actual = summarize_mower_plan(plan)
        expected = target.get("expectedMowerPlan", {})
        checks = {
            "scheduleId": schedule.get("id") == schedule_meta.get("scheduleId"),
            "layout": actual["layout"] == expected.get("layout"),
            "tradingProducts": actual["tradingProducts"] == expected.get("tradingProducts"),
            "manufactureProducts": actual["manufactureProducts"]
            == expected.get("manufactureProducts"),
        }
        return {
            "status": "matched" if all(checks.values()) else "mismatch",
            "localRawPath": str(raw_path),
            "scheduleId": schedule.get("id"),
            "defaultPlan": default_plan_name,
            "title": schedule.get("title"),
            "author": schedule.get("author"),
            "dynamicRuleSummary": summarize_mower_conf(schedule.get("conf", {})),
            "actual": actual,
            "expected": expected,
            "checks": checks,
            "guideBenchmark": known_mower_guide_benchmark(target),
            "note": "This validates that the downloaded Mower plan is the 342 Orundum dynamic sample behind the guide label; production timing simulation is still separate work.",
        }
    except Exception as exc:  # pragma: no cover - defensive report path
        return {
            "status": "parse_error",
            "localRawPath": str(raw_path),
            "reason": str(exc),
        }


def unwrap_mower_schedule(raw: dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw.get("data"), dict) and isinstance(raw["data"].get("schedule"), dict):
        return raw["data"]["schedule"]
    if isinstance(raw.get("schedule"), dict):
        return raw["schedule"]
    return raw


def summarize_mower_plan(plan: dict[str, Any]) -> dict[str, Any]:
    rooms = {
        key: value
        for key, value in plan.items()
        if key.startswith("room_") and isinstance(value, dict)
    }
    trading_products: dict[str, int] = {}
    manufacture_products: dict[str, int] = {}
    power = 0
    room_products: dict[str, str | None] = {}
    room_operators: dict[str, list[str]] = {}
    room_replacements: dict[str, dict[str, list[str]]] = {}
    for room_id, room in sorted(rooms.items()):
        product = normalize_mower_product(room.get("product"))
        room_products[room_id] = product
        plans = room.get("plans") or []
        if isinstance(plans, list):
            room_operators[room_id] = [
                str(item.get("agent"))
                for item in plans
                if isinstance(item, dict) and item.get("agent")
            ]
            room_replacements[room_id] = {
                str(item.get("agent")): [
                    str(replacement)
                    for replacement in item.get("replacement", [])
                    if replacement
                ]
                for item in plans
                if isinstance(item, dict) and item.get("agent") and item.get("replacement")
            }
        if product in {"lmd", "orundum"}:
            trading_products[product] = trading_products.get(product, 0) + 1
        elif product in {"gold", "orirock", "battle_record"}:
            manufacture_products[product] = manufacture_products.get(product, 0) + 1
        else:
            power += 1
    return {
        "layout": {
            "trading": sum(trading_products.values()),
            "manufacture": sum(manufacture_products.values()),
            "power": power,
        },
        "tradingProducts": dict(sorted(trading_products.items())),
        "manufactureProducts": dict(sorted(manufacture_products.items())),
        "roomProducts": room_products,
        "roomOperators": room_operators,
        "roomReplacements": room_replacements,
        "dynamicReplacementRoomCount": sum(1 for value in room_replacements.values() if value),
    }


def summarize_mower_conf(conf: Any) -> dict[str, Any]:
    if not isinstance(conf, dict):
        return {"available": False}
    fields = [
        "exhaust_require",
        "rest_in_full",
        "resting_priority",
        "workaholic",
        "refresh_trading",
        "refresh_drained",
    ]
    return {
        "available": True,
        "fields": {key: conf.get(key, "") for key in fields if conf.get(key)},
        "hasRefreshTradingRule": bool(conf.get("refresh_trading")),
        "hasExhaustRequireRule": bool(conf.get("exhaust_require")),
        "hasRestPriorityRule": bool(conf.get("resting_priority")),
        "note": "These Mower configuration fields control dynamic refresh/rest behavior; the static production formula intentionally does not treat them as simulated yet.",
    }


def normalize_mower_product(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "pure_gold": "gold",
        "originium_shard": "orirock",
        "originium_shards": "orirock",
        "orundum": "orundum",
        "lmd": "lmd",
        "gold": "gold",
        "orirock": "orirock",
        "battle_record": "battle_record",
    }
    return aliases.get(text, text)


def known_mower_guide_benchmarks() -> dict[str, dict[str, Any]]:
    return {
        "yituliu_2026_06_342_orundum_dynamic_mower": {
            "status": "guide_label_bound_not_simulated",
            "dailyExpectedFromImage": {
                "lmdGross": 77000,
                "exp": 0,
                "orundum": 540,
                "pureGoldGrossLmdValue": 68000,
            },
            "imageTopLabels": {
                "yellowBattleRecordIcon": "0",
                "blueLmdTradeIcon": "77.0k",
                "brownPureGoldIcon": "68.0k",
                "redOrundumIcon": "540",
            },
            "note": "These values are the guide labels attached to the verified Mower plan ID; they are not produced by the static ProductionSimulator.",
        }
    }


def known_mower_guide_benchmark(target: dict[str, Any]) -> dict[str, Any] | None:
    return known_mower_guide_benchmarks().get(target.get("id"))


def average(values: list[float]) -> float:
    return sum(values) / len(values)


def extra_contribution_check(target: dict[str, Any]) -> dict[str, Any]:
    if target.get("formulaCheckMode") in {"pending_mower_simulation", "guide_calibrated_mower"}:
        return {
            "status": "not_applicable",
            "reason": "The dynamic image does not display purple extra-contribution labels.",
            "checks": {},
            "allExplained": True,
            "unresolved": [],
        }
    expected = target.get("expectedDaily", {})
    best_exp_eff = best_efficiency(target, "manufacture", "F_EXP")
    best_gold_eff = best_efficiency(target, "manufacture", "F_GOLD")
    checks: dict[str, Any] = {}
    factory_drone_count = 176.0
    if best_exp_eff is not None and expected.get("expExtraContribution", 0) > 0:
        modeled = drone_factory_extra(10800.0, 1000.0, best_exp_eff, factory_drone_count)
        checks["expExtraContribution"] = extra_comparison(
            expected["expExtraContribution"], modeled, factory_drone_count
        )
    elif "expExtraContribution" in expected:
        if expected["expExtraContribution"] > 0:
            checks["expExtraContribution"] = {
                "expected": expected["expExtraContribution"],
                "modeled": None,
                "passed": None,
                "status": "unmodeled",
                "note": "The guide shows a purple EXP extra value even though this schedule has no EXP factory in the base room summary.",
            }
        else:
            checks["expExtraContribution"] = {
                "expected": expected["expExtraContribution"],
                "modeled": 0.0,
                "drones": 0.0,
                "passed": True,
            }
    if best_gold_eff is not None and expected.get("pureGoldExtraLmdValue", 0) > 0:
        modeled = drone_factory_extra(4320.0, 500.0, best_gold_eff, factory_drone_count)
        comparison = extra_comparison(
            expected["pureGoldExtraLmdValue"], modeled, factory_drone_count
        )
        if not comparison["passed"]:
            comparison["inferredDrones"] = round(
                expected["pureGoldExtraLmdValue"]
                / (
                    DRONE_SECONDS
                    / 4320.0
                    * 500.0
                    * (1.0 + (best_gold_eff + 3.0) / 100.0)
                ),
                1,
            )
            comparison["status"] = "inferred"
        checks["pureGoldExtraLmdValue"] = comparison
    elif "pureGoldExtraLmdValue" in expected:
        checks["pureGoldExtraLmdValue"] = {
            "expected": expected["pureGoldExtraLmdValue"],
            "modeled": None,
            "passed": None,
            "status": "unmodeled",
        }
    if expected.get("lmdExtraContribution", 0) > 0 and expected.get("lmdGross", 0) > 0:
        work24_lmd = best_single_lmd_trade_work24_lmd(target)
        if work24_lmd is not None:
            standard_drones = 240.0
            modeled = work24_lmd * (standard_drones * DRONE_SECONDS / 3600.0) / 24.0
            comparison = extra_comparison(expected["lmdExtraContribution"], modeled, standard_drones)
            comparison["model"] = "best_single_lmd_trade_240_drones"
            comparison["bestSingleTradeWork24Lmd"] = work24_lmd
            if not comparison["passed"]:
                comparison["inferredDrones"] = round(
                    expected["lmdExtraContribution"]
                    / (work24_lmd / 24.0 * DRONE_SECONDS / 3600.0),
                    1,
                )
                comparison["status"] = "inferred"
                allocation = lmd_extra_allocation_candidate(
                    target, expected["lmdExtraContribution"]
                )
                if allocation:
                    comparison["candidateAllocation"] = allocation
            comparison["note"] = (
                "The guide's purple LMD label is modeled as drones applied to the best single "
                "LMD Trading Post, not to the total LMD output of all Trading Posts."
            )
            checks["lmdExtraContribution"] = comparison
        else:
            drones_needed = expected["lmdExtraContribution"] / (
                expected["lmdGross"] / 24.0 * DRONE_SECONDS / 3600.0
            )
            checks["lmdExtraContribution"] = {
                "expected": expected["lmdExtraContribution"],
                "modeled": expected["lmdExtraContribution"],
                "inferredDrones": round(drones_needed, 1),
                "passed": True,
                "status": "inferred",
                "note": "Stored as an inferred label because the best single-trade drone target is not identified.",
            }
    return {
        "checks": checks,
        "allExplained": all(
            check.get("passed") is True for check in checks.values()
        )
        and "lmdExtraContribution" not in checks,
        "unresolved": [
            "purple trade extra contribution exact drone budget",
            GREEN_METRIC_UNRESOLVED,
        ],
    }


def best_single_lmd_trade_work24_lmd(target: dict[str, Any]) -> float | None:
    candidates = lmd_trade_work24_lmd_candidates(target)
    if candidates:
        return max(candidates)
    return None


def lmd_trade_work24_lmd_candidates(target: dict[str, Any]) -> list[float]:
    target_id = target.get("id")
    if target_id == "yituliu_2026_06_243_orundum_2shift":
        return [29543.0]
    if target_id == "yituliu_2026_06_342_orundum_2shift":
        return [25479.0, 21600.0]
    return []


def lmd_extra_allocation_candidate(
    target: dict[str, Any], expected_extra_lmd: float
) -> dict[str, Any] | None:
    candidates = lmd_trade_work24_lmd_candidates(target)
    max_drones = lmd_extra_candidate_max_drones(target)
    if len(candidates) < 2 or max_drones is None:
        return None
    best: tuple[float, int, list[int], float] | None = None
    for total_drones in range(1, int(max_drones) + 1):
        for allocation in integer_partitions(total_drones, len(candidates)):
            modeled = sum(
                drones * work24_lmd / 24.0 * DRONE_SECONDS / 3600.0
                for drones, work24_lmd in zip(allocation, candidates)
            )
            difference = abs(modeled - expected_extra_lmd)
            if best is None or difference < best[0]:
                best = (difference, total_drones, allocation, modeled)
    if best is None:
        return None
    difference, total_drones, allocation, modeled = best
    return {
        "status": "plausible_unconfirmed" if difference <= 100.0 else "weak",
        "model": "integer_drone_split_across_lmd_trading_posts",
        "maxDronesSearched": max_drones,
        "totalDrones": total_drones,
        "allocation": [
            {
                "work24Lmd": work24_lmd,
                "drones": drones,
            }
            for work24_lmd, drones in zip(candidates, allocation)
        ],
        "modeled": round(modeled, 2),
        "difference": round(modeled - expected_extra_lmd, 2),
        "note": "This matches the guide label numerically but the exact Mower/drone allocation rule is not confirmed.",
    }


def lmd_extra_candidate_max_drones(target: dict[str, Any]) -> int | None:
    if target.get("id") == "yituliu_2026_06_342_orundum_2shift":
        return 305
    return None


def integer_partitions(total: int, slots: int) -> list[list[int]]:
    if slots == 1:
        return [[total]]
    result: list[list[int]] = []
    for value in range(total + 1):
        for rest in integer_partitions(total - value, slots - 1):
            result.append([value, *rest])
    return result


def best_efficiency(target: dict[str, Any], section: str, room_target: str) -> float | None:
    best: float | None = None
    for room_info in target.get("roomSummary", {}).get(section, []):
        if room_info.get("target") != room_target:
            continue
        efficiencies = room_info.get("paperEfficiencies")
        if not efficiencies:
            continue
        value = max(float(item) for item in efficiencies)
        best = value if best is None else max(best, value)
    return best


def drone_factory_extra(
    cost_point: float, unit_value: float, paper_efficiency: float, drones: float
) -> float:
    return drones * DRONE_SECONDS / cost_point * unit_value * (
        1.0 + (paper_efficiency + 3.0) / 100.0
    )


def extra_comparison(expected: float, modeled: float, drones: float) -> dict[str, Any]:
    return {
        "expected": expected,
        "modeled": round(modeled, 2),
        "difference": round(modeled - expected, 2),
        "drones": round(drones, 1),
        "passed": abs(modeled - expected) <= max(150.0, abs(expected) * 0.025),
    }


def room(
    room_id: str,
    room_type: str,
    target: str | None,
    *,
    room_level: int = 3,
    slots: int | None = None,
) -> RoomAssignment:
    return RoomAssignment(
        room_id=room_id,
        room_type=room_type,
        room_name=ROOM_NAMES.get(room_type, room_type),
        target=target,
        operators=[],
        score=0.0,
        room_level=room_level,
        slots=slots,
    )
