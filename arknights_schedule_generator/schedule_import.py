from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .data import GameData
from .models import BaseSkill, Layout, RoomAssignment, RosterOperator, ShiftPlan
from .optimizer import ROOM_NAMES, score_skill, shift_label, synthetic_skill
from .room_limits import clamp_station_count, station_limit


YITULIU_ROOM_TYPES = {
    "trading": "TRADING",
    "manufacture": "MANUFACTURE",
    "power": "POWER",
    "dormitory": "DORMITORY",
    "control": "CONTROL",
    "meeting": "MEETING",
    "hire": "HIRE",
    "processing": "WORKSHOP",
}

YITULIU_PRODUCTS = {
    "LMD": "O_GOLD",
    "Orundum": "O_DIAMOND",
    "Battle Record": "F_EXP",
    "Pure Gold": "F_GOLD",
    "Originium Shard": "F_DIAMOND",
}


@dataclass
class ImportedSchedule:
    layout: Layout
    shifts: list[ShiftPlan]
    raw: dict[str, Any]
    warnings: list[str]


def load_yituliu_schedule(
    path: Path,
    game_data: GameData,
    roster: list[RosterOperator],
    *,
    allow_upgrades: bool = False,
    upgrade_cost_weight: float = 0.015,
) -> ImportedSchedule:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    schedule_type = raw.get("scheduleType") or {}
    trading = int(schedule_type.get("trading") or infer_room_count(raw, "trading"))
    manufacture = int(schedule_type.get("manufacture") or infer_room_count(raw, "manufacture"))
    power = int(schedule_type.get("power") or max(0, 9 - trading - manufacture))
    dormitory = int(schedule_type.get("dormitory") or infer_room_count(raw, "dormitory") or 4)
    plans = raw.get("plans", [])
    plan_times = int(schedule_type.get("planTimes") or len(plans) or 2)
    default_duration_hours = max(1.0, 24.0 / plan_times)
    shift_starts = [extract_shift_start(plan) for plan in plans]
    shift_durations = infer_shift_durations(shift_starts, default_duration_hours)
    layout = Layout(
        raw=f"{trading}{manufacture}{power}",
        trading=trading,
        manufacture=manufacture,
        power=power,
        dormitory=dormitory,
    )

    roster_by_name = {operator.name: operator for operator in roster}
    skills_by_name = {
        operator.name: game_data.skills_for_roster_operator(operator, allow_upgrades)
        for operator in roster
        if operator.recruited
    }
    warnings: list[str] = []
    shifts: list[ShiftPlan] = []
    for plan_index, plan in enumerate(plans):
        rooms: list[RoomAssignment] = []
        dormitories: list[RoomAssignment] = []
        for room_key, room_entries in (plan.get("rooms") or {}).items():
            room_type = YITULIU_ROOM_TYPES.get(room_key)
            if not room_type:
                warnings.append(f"忽略未知房间类型: {room_key}")
                continue
            for room_index, room_entry in enumerate(room_entries, 1):
                target = YITULIU_PRODUCTS.get(room_entry.get("product"))
                operators = [
                    best_skill_for_imported_operator(
                        name,
                        room_type,
                        target,
                        roster_by_name,
                        skills_by_name,
                        upgrade_cost_weight,
                    )
                    for name in room_entry.get("operators", [])
                ]
                room_level = optional_int(room_entry.get("roomLevel") or room_entry.get("level"), 3)
                slots = optional_int(room_entry.get("slots") or room_entry.get("stationSlots"), None)
                if slots is not None:
                    slots = clamp_station_count(room_type, slots)
                hard_limit = station_limit(room_type)
                if hard_limit is not None and len(operators) > hard_limit:
                    warnings.append(
                        f"{ROOM_NAMES.get(room_type, room_type)} {room_key}_{room_index} "
                        f"只允许 {hard_limit} 名干员，已忽略多余干员。"
                    )
                    operators = operators[:hard_limit]
                product_capacity = optional_int(
                    room_entry.get("productCapacity") or room_entry.get("capacity"), None
                )
                order_limit = optional_int(room_entry.get("orderLimit"), None)
                assignment = RoomAssignment(
                    room_id=f"{room_key}_{room_index}",
                    room_type=room_type,
                    room_name=ROOM_NAMES.get(room_type, room_type),
                    target=target,
                    operators=operators,
                    score=round(sum(skill.parsed_score for skill in operators), 3),
                    room_level=room_level,
                    slots=slots,
                    product_capacity=product_capacity,
                    order_limit=order_limit,
                )
                if room_type == "DORMITORY":
                    dormitories.append(assignment)
                else:
                    rooms.append(assignment)
        shifts.append(
            ShiftPlan(
                name=plan.get("name") or shift_label(plan_index),
                start=shift_starts[plan_index]
                or f"{int((8 + plan_index * default_duration_hours) % 24):02d}:00",
                duration_hours=shift_durations[plan_index],
                rooms=rooms,
                dormitories=dormitories,
            )
        )
    return ImportedSchedule(layout=layout, shifts=shifts, raw=raw, warnings=warnings)


def infer_room_count(raw: dict[str, Any], room_key: str) -> int:
    plans = raw.get("plans") or []
    if not plans:
        return 0
    return len((plans[0].get("rooms") or {}).get(room_key) or [])


def extract_shift_start(plan: dict[str, Any]) -> str | None:
    for key in ("start", "startTime", "time", "description"):
        value = plan.get(key)
        if not isinstance(value, str):
            continue
        match = re.search(r"([01]?\d|2[0-3]):([0-5]\d)", value)
        if match:
            return f"{int(match.group(1)):02d}:{match.group(2)}"
    return None


def infer_shift_durations(starts: list[str | None], default_hours: float) -> list[float]:
    if starts and all(start is not None for start in starts):
        minutes = [time_to_minutes(str(start)) for start in starts]
        durations: list[float] = []
        for index, minute in enumerate(minutes):
            next_minute = minutes[(index + 1) % len(minutes)]
            delta = (next_minute - minute) % (24 * 60)
            durations.append(round((delta or default_hours * 60) / 60.0, 3))
        return durations
    return [default_hours for _ in starts]


def time_to_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def optional_int(value: Any, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def best_skill_for_imported_operator(
    name: str,
    room_type: str,
    target: str | None,
    roster_by_name: dict[str, RosterOperator],
    skills_by_name: dict[str, list[BaseSkill]],
    upgrade_cost_weight: float,
) -> BaseSkill:
    operator = roster_by_name.get(name)
    if not operator:
        return BaseSkill(
            char_id="",
            operator_name=name,
            room_type=room_type,
            buff_id="unknown",
            buff_name="未在练度表中找到",
            description="导入排班中的干员未在练度表中找到。",
            efficiency=0.0,
            targets=(),
            cond_elite=0,
            cond_level=1,
            unlocked=False,
            complex_condition=False,
            parsed_score=0.0,
            upgrade=None,
        )
    best: tuple[float, BaseSkill] | None = None
    for skill in skills_by_name.get(name, []):
        if skill.room_type != room_type:
            continue
        score = score_skill(skill, target, upgrade_cost_weight)
        if score is None:
            continue
        if best is None or score > best[0]:
            best = (score, skill)
    if best:
        return best[1]
    return synthetic_skill(operator, room_type)
