from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product
from typing import Any

from .data import GameData
from .models import Layout


RIGHT_SIDE_PRESETS = ("full", "strict-full", "guide", "basic", "ignore")
_POWER_LAYOUT_CACHE: dict[tuple[Any, ...], tuple[Layout, PowerStatus]] = {}


@dataclass(frozen=True)
class PowerStatus:
    feasible: bool
    supplied: int
    consumed: int
    margin: int
    preset: str
    rooms: list[dict[str, Any]]
    power_adjustment: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "supplied": self.supplied,
            "consumed": self.consumed,
            "margin": self.margin,
            "preset": self.preset,
            "rooms": self.rooms,
            "powerAdjustment": self.power_adjustment or {},
        }


def apply_right_side_preset(layout: Layout, preset: str | None) -> Layout:
    normalized = (preset or "full").strip().lower().replace("_", "-")
    if normalized in {"right-full", "max", "full"}:
        return replace(
            layout,
            right_side_preset="full",
            control_level=5,
            meeting_level=3,
            hire_level=3,
            training_level=3,
            workshop_level=3,
            dormitory_levels=(5,) * layout.dormitory,
        )
    if normalized in {"strict-full", "right-strict-full", "full-strict"}:
        return replace(
            layout,
            right_side_preset="strict-full",
            control_level=5,
            meeting_level=3,
            hire_level=3,
            training_level=3,
            workshop_level=3,
            dormitory_levels=(5,) * layout.dormitory,
        )
    if normalized in {"guide", "guide-full", "right-full-low-dorm"}:
        return replace(
            layout,
            right_side_preset="guide",
            control_level=5,
            meeting_level=3,
            hire_level=3,
            training_level=3,
            workshop_level=3,
            dormitory_levels=(1,) * layout.dormitory,
        )
    if normalized in {"not-full", "low", "basic", "minimal"}:
        return replace(
            layout,
            right_side_preset="basic",
            control_level=5,
            meeting_level=1,
            hire_level=1,
            training_level=1,
            workshop_level=1,
            dormitory_levels=(1,) * layout.dormitory,
        )
    if normalized in {"ignore", "off", "none"}:
        return replace(layout, right_side_preset="ignore")
    raise ValueError("--right-side must be full, strict-full, guide, basic, or ignore.")


def resolve_power_layout(game_data: GameData, layout: Layout) -> tuple[Layout, PowerStatus]:
    cache_key = power_layout_cache_key(game_data, layout)
    cached = _POWER_LAYOUT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    status = power_status(game_data, layout)
    if status.feasible or layout.right_side_preset != "full":
        result = (layout, status)
        _POWER_LAYOUT_CACHE[cache_key] = result
        return result
    adjusted = auto_adjust_full_power_layout(game_data, layout, initial_status=status)
    result = (adjusted, power_status(game_data, adjusted))
    _POWER_LAYOUT_CACHE[cache_key] = result
    return result


def power_layout_cache_key(game_data: GameData, layout: Layout) -> tuple[Any, ...]:
    return (
        id(game_data),
        layout.raw,
        layout.trading,
        layout.manufacture,
        layout.power,
        layout.dormitory,
        layout.control,
        layout.meeting,
        layout.hire,
        layout.training,
        layout.workshop,
        layout.trading_levels,
        layout.manufacture_levels,
        layout.manufacture_slots,
        layout.power_levels,
        layout.dormitory_levels,
        layout.control_level,
        layout.meeting_level,
        layout.hire_level,
        layout.training_level,
        layout.workshop_level,
        layout.right_side_preset,
    )


def auto_adjust_full_power_layout(
    game_data: GameData, layout: Layout, *, initial_status: PowerStatus | None = None
) -> Layout:
    before = initial_status or power_status(game_data, layout)
    original_manufacture_levels = tuple(
        levels_or_default(layout.manufacture_levels, layout.manufacture, 3)
    )
    original_dormitory_levels = tuple(
        levels_or_default(layout.dormitory_levels, layout.dormitory, 5)
    )
    manufacture_ranges = [range(1, level + 1) for level in original_manufacture_levels]
    best = best_power_fit(
        game_data,
        layout,
        manufacture_ranges=manufacture_ranges,
        dormitory_ranges=[[level] for level in original_dormitory_levels],
    )
    if best is not None:
        _score, adjusted, after = best
        return replace(
            adjusted,
            power_adjustment=power_adjustment_record(
                "applied",
                "right_full_power_fit",
                before,
                after,
                original_manufacture_levels,
                adjusted.manufacture_levels,
                adjusted.manufacture_slots,
                original_dormitory_levels,
                adjusted.dormitory_levels,
                manufacture_only_feasible=True,
            ),
        )

    dormitory_ranges = [range(1, level + 1) for level in original_dormitory_levels]
    best = best_power_fit(
        game_data,
        layout,
        manufacture_ranges=manufacture_ranges,
        dormitory_ranges=dormitory_ranges,
    )
    manufacture_only_floor = power_status(
        game_data,
        replace(
            layout,
            manufacture_levels=tuple(1 for _ in original_manufacture_levels),
            manufacture_slots=tuple(
                room_station_count(game_data, "MANUFACTURE", 1)
                for _ in original_manufacture_levels
            ),
            dormitory_levels=original_dormitory_levels,
        ),
    )
    if best is None:
        failed_status = power_status(
            game_data,
            replace(
                layout,
                manufacture_levels=tuple(1 for _ in original_manufacture_levels),
                manufacture_slots=tuple(
                    room_station_count(game_data, "MANUFACTURE", 1)
                    for _ in original_manufacture_levels
                ),
                dormitory_levels=tuple(1 for _ in original_dormitory_levels),
            ),
        )
        return replace(
            layout,
            power_adjustment=power_adjustment_record(
                "failed",
                "manufacture_and_dormitory_levels_exhausted",
                before,
                failed_status,
                original_manufacture_levels,
                tuple(1 for _ in original_manufacture_levels),
                tuple(
                    room_station_count(game_data, "MANUFACTURE", 1)
                    for _ in original_manufacture_levels
                ),
                original_dormitory_levels,
                tuple(1 for _ in original_dormitory_levels),
                manufacture_only_feasible=False,
                manufacture_only_after_margin=manufacture_only_floor.margin,
            ),
        )
    _score, adjusted, after = best
    return replace(
        adjusted,
        power_adjustment=power_adjustment_record(
            "applied",
            "right_full_power_fit_dormitory_fallback",
            before,
            after,
            original_manufacture_levels,
            adjusted.manufacture_levels,
            adjusted.manufacture_slots,
            original_dormitory_levels,
            adjusted.dormitory_levels,
            manufacture_only_feasible=False,
            manufacture_only_after_margin=manufacture_only_floor.margin,
        ),
    )


def best_power_fit(
    game_data: GameData,
    layout: Layout,
    *,
    manufacture_ranges: list[range],
    dormitory_ranges: list[range] | list[list[int]],
) -> tuple[tuple[int, int, int, int], Layout, PowerStatus] | None:
    best: tuple[tuple[int, int, int, int], Layout, PowerStatus] | None = None
    for manufacture_levels in product(*manufacture_ranges):
        manufacture_slots = tuple(
            room_station_count(game_data, "MANUFACTURE", level)
            for level in manufacture_levels
        )
        for dormitory_levels in product(*dormitory_ranges):
            candidate = replace(
                layout,
                manufacture_levels=tuple(manufacture_levels),
                manufacture_slots=manufacture_slots,
                dormitory_levels=tuple(dormitory_levels),
            )
            status = power_status(game_data, candidate)
            if not status.feasible:
                continue
            score = (
                sum(manufacture_slots),
                sum(manufacture_levels),
                sum(dormitory_levels),
                status.margin,
            )
            if best is None or score > best[0]:
                best = (score, candidate, status)
    return best


def power_adjustment_record(
    status: str,
    reason: str,
    before: PowerStatus,
    after: PowerStatus,
    original_manufacture_levels: tuple[int, ...],
    manufacture_levels: tuple[int, ...],
    manufacture_slots: tuple[int, ...],
    original_dormitory_levels: tuple[int, ...],
    dormitory_levels: tuple[int, ...],
    manufacture_only_feasible: bool | None = None,
    manufacture_only_after_margin: int | None = None,
) -> dict[str, Any]:
    steps = [
        {
            "roomType": "MANUFACTURE",
            "index": index,
            "fromLevel": original,
            "toLevel": current,
        }
        for index, (original, current) in enumerate(
            zip(original_manufacture_levels, manufacture_levels), 1
        )
        if original != current
    ]
    steps.extend(
        {
            "roomType": "DORMITORY",
            "index": index,
            "fromLevel": original,
            "toLevel": current,
        }
        for index, (original, current) in enumerate(
            zip(original_dormitory_levels, dormitory_levels), 1
        )
        if original != current
    )
    record = {
        "status": status,
        "reason": reason,
        "beforeMargin": before.margin,
        "afterMargin": after.margin,
        "originalManufactureLevels": list(original_manufacture_levels),
        "manufactureLevels": list(manufacture_levels),
        "manufactureSlots": list(manufacture_slots),
        "originalDormitoryLevels": list(original_dormitory_levels),
        "dormitoryLevels": list(dormitory_levels),
        "steps": steps,
    }
    if manufacture_only_feasible is not None:
        record["manufactureOnlyFeasible"] = manufacture_only_feasible
    if manufacture_only_after_margin is not None:
        record["manufactureOnlyAfterMargin"] = manufacture_only_after_margin
    return record


def power_status(game_data: GameData, layout: Layout) -> PowerStatus:
    rooms: list[dict[str, Any]] = []
    total = 0
    for room_type, levels in layout_room_levels(layout).items():
        for index, level in enumerate(levels, 1):
            electricity = room_electricity(game_data, room_type, level)
            total += electricity
            rooms.append(
                {
                    "roomType": room_type,
                    "index": index,
                    "level": level,
                    "electricity": electricity,
                }
            )
    supplied = sum(max(0, int(room["electricity"])) for room in rooms)
    consumed = -sum(min(0, int(room["electricity"])) for room in rooms)
    return PowerStatus(
        feasible=total >= 0,
        supplied=supplied,
        consumed=consumed,
        margin=total,
        preset=layout.right_side_preset,
        rooms=rooms,
        power_adjustment=layout.power_adjustment,
    )


def layout_room_levels(layout: Layout) -> dict[str, list[int]]:
    return {
        "CONTROL": [layout.control_level] * layout.control,
        "TRADING": levels_or_default(layout.trading_levels, layout.trading, 3),
        "MANUFACTURE": levels_or_default(layout.manufacture_levels, layout.manufacture, 3),
        "POWER": levels_or_default(layout.power_levels, layout.power, 3),
        "DORMITORY": levels_or_default(layout.dormitory_levels, layout.dormitory, 5),
        "MEETING": [layout.meeting_level] * layout.meeting,
        "HIRE": [layout.hire_level] * layout.hire,
        "TRAINING": [layout.training_level] * layout.training,
        "WORKSHOP": [layout.workshop_level] * layout.workshop,
    }


def levels_or_default(levels: tuple[int, ...], count: int, default: int) -> list[int]:
    result = list(levels[:count])
    if len(result) < count:
        result.extend([default] * (count - len(result)))
    return result


def room_electricity(game_data: GameData, room_type: str, level: int) -> int:
    room = game_data.building.get("rooms", {}).get(room_type, {})
    phases = room.get("phases") or []
    if not phases:
        return 0
    index = max(0, min(len(phases) - 1, level - 1))
    try:
        return int(phases[index].get("electricity") or 0)
    except (TypeError, ValueError):
        return 0


def room_station_count(game_data: GameData, room_type: str, level: int) -> int:
    room = game_data.building.get("rooms", {}).get(room_type, {})
    phases = room.get("phases") or []
    if not phases:
        return max(1, int(level))
    index = max(0, min(len(phases) - 1, level - 1))
    try:
        return max(1, int(phases[index].get("maxStationedNum") or level))
    except (TypeError, ValueError):
        return max(1, int(level))


__all__ = [
    "PowerStatus",
    "RIGHT_SIDE_PRESETS",
    "apply_right_side_preset",
    "resolve_power_layout",
    "power_status",
]
