from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data import RAW_BASE_URL, GameData
from .models import BaseSkill, RoomAssignment, ShiftPlan, UpgradeRequirement
from .optimizer import OptimizerResult
from .presets import PRESETS, preset_contract, target_label
from .production import ProductionSimulator


ROOM_TO_YITULIU = {
    "CONTROL": "control",
    "TRADING": "trading",
    "MANUFACTURE": "manufacture",
    "POWER": "power",
    "DORMITORY": "dormitory",
    "MEETING": "meeting",
    "HIRE": "hire",
    "WORKSHOP": "processing",
}

TARGET_TO_YITULIU_PRODUCT = {
    "O_GOLD": "LMD",
    "O_DIAMOND": "Orundum",
    "F_EXP": "Battle Record",
    "F_GOLD": "Pure Gold",
    "F_DIAMOND": "Originium Shard",
}


def result_to_dict(result: OptimizerResult, game_data: GameData) -> dict[str, Any]:
    upgrades = collect_upgrades(result)
    conflicts = find_conflicts(result)
    warnings = list(result.warnings)
    if conflicts:
        warnings.append(f"检测到内部冲突: {', '.join(conflicts)}")
    production_report = result.production_report or ProductionSimulator(game_data).evaluate(result.shifts)
    production_dict = production_report.to_dict()

    plan_times = len(result.shifts)
    generated_at = datetime.now(timezone.utc).isoformat()
    drone_targets = production_dict["dailyExpected"].get("droneTargets") or []
    analysis = {
        "reminders": warnings,
        "upgrades": [upgrade_to_dict(upgrade) for upgrade in upgrades],
        "diagnosticInsertionSearch": result.diagnostic_insertion_search,
        "localOptimalityAudit": result.diagnostic_insertion_search.get(
            "localOptimalityAudit", {}
        ),
        "candidatePoolAudit": result.diagnostic_insertion_search.get(
            "candidatePoolAudit", {}
        ),
        "cacheValidation": result.diagnostic_insertion_search.get("cacheValidation", {}),
        "pureGoldBalancePolicy": result.diagnostic_insertion_search.get(
            "pureGoldBalancePolicy", {}
        ),
        "objectiveConflictAudit": result.diagnostic_insertion_search.get(
            "objectiveConflictAudit", {}
        ),
        "unsupportedSkillEffects": production_dict["unsupportedSkillEffects"],
        "assumptions": production_dict["assumptions"],
        "calibrationProfile": production_dict.get("calibrationProfile", "guide"),
        "sourceAssumptions": production_dict.get("sourceAssumptions", []),
        "guideComparison": production_dict.get("guideComparison"),
        "roomReports": production_dict["roomReports"],
        "powerStatus": result.power_status.to_dict() if result.power_status else None,
    }
    if result.runtime_profile:
        analysis["runtimeProfile"] = result.runtime_profile

    return {
        "author": "Codex",
        "description": (
            f"{result.layout.label} {PRESETS.get(result.mode, PRESETS['normal']).label} "
            f"自动推荐；保留一图流 plans[].rooms 导入结构。"
        ),
        "id": int(datetime.now(timezone.utc).timestamp() * 1000),
        "title": f"{result.layout.label} {result.mode} {plan_times}班推荐排班",
        "planTimes": f"{plan_times}班",
        "plans": [
            shift_to_yituliu_plan(
                shift,
                index,
                drone_targets=drone_targets_for_shift(drone_targets, shift.name)
                if result.drone_policy != "none"
                else [],
            )
            for index, shift in enumerate(result.shifts, 1)
        ],
        "scheduleType": {
            "planTimes": plan_times,
            "trading": result.layout.trading,
            "manufacture": result.layout.manufacture,
            "power": result.layout.power,
            "dormitory": result.layout.dormitory,
        },
        "format": "yituliu-base-schedule-json",
        "formatVersion": 2,
        "generatedAt": generated_at,
        "source": {
            "gameData": "Kengxxiao/ArknightsGameData zh_CN",
            "dataVersion": game_data.data_version,
            "buildingDataUrl": f"{RAW_BASE_URL}/building_data.json",
            "characterDataUrl": f"{RAW_BASE_URL}/character_table.json",
        },
        "base": {
            "layout": result.layout.label,
            "trading": result.layout.trading,
            "manufacture": result.layout.manufacture,
            "power": result.layout.power,
            "dormitory": result.layout.dormitory,
            "control": result.layout.control,
            "meeting": result.layout.meeting,
            "hire": result.layout.hire,
            "training": result.layout.training,
            "workshop": result.layout.workshop,
            "rightSidePreset": result.layout.right_side_preset,
        },
        "mode": {
            "id": result.mode,
            "label": PRESETS.get(result.mode, PRESETS["normal"]).label,
            "contract": preset_contract(result.mode),
        },
        "score": result.score,
        "scoreBreakdown": production_dict["scoreBreakdown"],
        "dailyExpected": production_dict["dailyExpected"],
        "analysis": analysis,
        "shifts": [
            {
                "name": shift.name,
                "start": shift.start,
                "durationHours": shift.duration_hours,
                "rooms": [room_to_dict(room) for room in shift.rooms],
                "dormitories": [room_to_dict(room) for room in shift.dormitories],
            }
            for shift in result.shifts
        ],
    }


def write_result_json(path: Path, result: OptimizerResult, game_data: GameData) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result_to_dict(result, game_data), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def room_to_dict(room: RoomAssignment) -> dict[str, Any]:
    return {
        "id": room.room_id,
        "type": room.room_type,
        "name": room.room_name,
        "target": room.target,
        "targetLabel": target_label(room.target),
        "score": room.score,
        "roomLevel": room.room_level,
        "slots": room.slots,
        "productCapacity": room.product_capacity,
        "orderLimit": room.order_limit,
        "operators": [skill_to_dict(skill) for skill in room.operators],
    }


def shift_to_yituliu_plan(
    shift: ShiftPlan, index: int, drone_targets: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    rooms: dict[str, list[dict[str, Any]]] = {
        "trading": [],
        "manufacture": [],
        "power": [],
        "dormitory": [],
        "control": [],
        "meeting": [],
        "hire": [],
        "processing": [],
    }
    for room in shift.rooms:
        key = ROOM_TO_YITULIU.get(room.room_type)
        if not key:
            continue
        rooms[key].append(yituliu_room(room))
    for room in shift.dormitories:
        rooms["dormitory"].append(yituliu_room(room))
    return {
        "name": f"第{index}班",
        "description": f"{shift.start} 开始，持续 {shift.duration_hours} 小时。",
        "description_post": "",
        "Fiammetta": {"enable": False, "target": "", "order": "pre"},
        "drones": yituliu_drone_config(drone_targets or []),
        "rooms": rooms,
    }


def yituliu_drone_config(drone_targets: list[dict[str, Any]]) -> dict[str, Any]:
    if not drone_targets:
        return {
            "room": "trading",
            "index": 1,
            "enable": False,
            "order": "post",
            "targets": [],
            "modeledDroneCount": 0.0,
        }
    primary = max(drone_targets, key=lambda target: float(target.get("droneCount") or 0))
    config = yituliu_single_drone_config(primary)
    config.update(
        {
            "targets": [yituliu_drone_target(target) for target in drone_targets],
            "modeledDroneCount": round(
                sum(float(target.get("droneCount") or 0) for target in drone_targets),
                3,
            ),
            "note": (
                "room/index is the primary Yituliu-compatible drone target; "
                "targets records the single shift-change drone target selected by the model."
            ),
        }
    )
    return config


def yituliu_single_drone_config(drone_target: dict[str, Any]) -> dict[str, Any]:
    room_key, index = yituliu_room_ref(drone_target)
    return {"room": room_key, "index": index, "enable": True, "order": "post"}


def yituliu_drone_target(drone_target: dict[str, Any]) -> dict[str, Any]:
    room_key, index = yituliu_room_ref(drone_target)
    return {
        "room": room_key,
        "index": index,
        "policy": drone_target.get("policy"),
        "roomId": drone_target.get("roomId"),
        "roomType": drone_target.get("roomType"),
        "target": drone_target.get("target"),
        "droneCount": drone_target.get("droneCount"),
        "durationHours": drone_target.get("durationHours"),
        "operators": drone_target.get("operators", []),
        "contribution": drone_target.get("contribution", {}),
    }


def yituliu_room_ref(drone_target: dict[str, Any]) -> tuple[str, int]:
    room_id = str(drone_target.get("roomId") or "")
    room_type = str(drone_target.get("roomType") or "")
    room_key = ROOM_TO_YITULIU.get(room_type, "trading")
    index = 1
    if "_" in room_id:
        try:
            index = int(room_id.rsplit("_", 1)[1])
        except ValueError:
            index = 1
    return room_key, index


def drone_targets_for_shift(
    drone_targets: list[dict[str, Any]], shift_name: str
) -> list[dict[str, Any]]:
    return [target for target in drone_targets if target.get("shift") == shift_name]


def yituliu_room(room: RoomAssignment) -> dict[str, Any]:
    data: dict[str, Any] = {
        "skip": False,
        "operators": [skill.operator_name for skill in room.operators],
        "sort": False,
        "autofill": False,
    }
    product = TARGET_TO_YITULIU_PRODUCT.get(room.target or "")
    if product:
        data["product"] = product
    return data


def skill_to_dict(skill: BaseSkill) -> dict[str, Any]:
    data: dict[str, Any] = {
        "charId": skill.char_id or None,
        "name": skill.operator_name,
        "buffId": skill.buff_id,
        "buffName": skill.buff_name,
        "roomType": skill.room_type,
        "description": skill.description,
        "condition": skill.condition_label,
        "unlocked": skill.unlocked,
        "score": skill.parsed_score,
        "targets": list(skill.targets),
        "complexCondition": skill.complex_condition,
    }
    if skill.upgrade:
        data["upgrade"] = upgrade_to_dict(skill.upgrade)
    return data


def upgrade_to_dict(upgrade: UpgradeRequirement) -> dict[str, Any]:
    return {
        "charId": upgrade.char_id,
        "name": upgrade.name,
        "from": {"elite": upgrade.from_elite, "level": upgrade.from_level},
        "to": {"elite": upgrade.to_elite, "level": upgrade.to_level, "label": upgrade.target_label},
        "costScore": round(upgrade.cost_score, 2),
        "materials": upgrade.materials,
        "note": upgrade.note,
    }


def collect_upgrades(result: OptimizerResult) -> list[UpgradeRequirement]:
    by_key: dict[tuple[str, int, int], UpgradeRequirement] = {}
    for shift in result.shifts:
        for room in [*shift.rooms, *shift.dormitories]:
            for skill in room.operators:
                if not skill.upgrade:
                    continue
                key = (skill.upgrade.char_id, skill.upgrade.to_elite, skill.upgrade.to_level)
                current = by_key.get(key)
                if current is None or skill.upgrade.cost_score > current.cost_score:
                    by_key[key] = skill.upgrade
    return sorted(by_key.values(), key=lambda upgrade: (upgrade.cost_score, upgrade.name))


def find_conflicts(result: OptimizerResult) -> list[str]:
    conflicts: list[str] = []
    for shift in result.shifts:
        seen: dict[str, str] = {}
        for room in [*shift.rooms, *shift.dormitories]:
            for skill in room.operators:
                previous = seen.get(skill.operator_name)
                if previous:
                    conflicts.append(f"{shift.name}/{skill.operator_name}: {previous} + {room.room_id}")
                else:
                    seen[skill.operator_name] = room.room_id
    return conflicts
