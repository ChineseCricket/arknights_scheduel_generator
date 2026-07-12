from __future__ import annotations

import re
from typing import Any

from .models import Layout, ShiftPlan
from .skill_rules import fatigue_delta_from_text


MAX_MORALE = 24.0
MAX_CYCLE_HOURS = 24.0 * 7.0


def active_operator_names(shift: ShiftPlan) -> set[str]:
    return {
        skill.operator_name
        for room in shift.rooms
        for skill in room.operators
    }


def dormitory_operator_names(shift: ShiftPlan) -> set[str]:
    return {
        skill.operator_name
        for room in shift.dormitories
        for skill in room.operators
    }


def audit_morale_cycle(layout: Layout, shifts: list[ShiftPlan]) -> dict[str, Any]:
    if not shifts:
        return _failed_audit("empty_schedule", [], 0.0, [])

    cycle_hours = sum(float(shift.duration_hours) for shift in shifts)
    active_by_shift = [active_operator_names(shift) for shift in shifts]
    dorm_by_shift = [dormitory_operator_names(shift) for shift in shifts]
    all_names = set().union(*active_by_shift, *dorm_by_shift)
    morale = {name: MAX_MORALE for name in all_names}
    for _ in range(16):
        previous = dict(morale)
        _advance_morale(layout, shifts, morale)
        if all(abs(morale[name] - previous[name]) <= 0.0001 for name in morale):
            break
    cycle_start_morale = dict(morale)
    minimum = dict(morale)
    transitions: list[dict[str, Any]] = []
    unrested: set[str] = set()
    conflicts: set[str] = set()

    for index, shift in enumerate(shifts):
        active = active_by_shift[index]
        dorm = dorm_by_shift[index]
        outgoing = active_by_shift[index - 1] - active
        missing_rest = outgoing - dorm
        duplicate = active & dorm
        unrested.update(missing_rest)
        conflicts.update(duplicate)

        for room in shift.rooms:
            for skill in room.operators:
                extra_consumption = max(0.0, fatigue_delta_from_text(skill.description))
                morale[skill.operator_name] -= (1.0 + extra_consumption) * shift.duration_hours
        for room_index, room in enumerate(shift.dormitories):
            room_level = (
                layout.dormitory_levels[room_index]
                if room_index < len(layout.dormitory_levels)
                else 5
            )
            all_bonus, self_bonuses, single_bonus = dormitory_recovery_bonuses(room)
            single_target = dormitory_single_recovery_target(room, morale, single_bonus)
            for skill in room.operators:
                recovery_rate = (
                    dormitory_base_recovery_rate(room_level)
                    + all_bonus
                    + self_bonuses.get(skill.operator_name, 0.0)
                    + (single_bonus if skill.operator_name == single_target else 0.0)
                )
                morale[skill.operator_name] = min(
                    MAX_MORALE,
                    morale[skill.operator_name] + recovery_rate * shift.duration_hours,
                )
        for name, value in morale.items():
            minimum[name] = min(minimum[name], value)

        transitions.append(
            {
                "shift": shift.name,
                "start": shift.start,
                "durationHours": round(float(shift.duration_hours), 6),
                "activeCount": len(active),
                "outgoingCount": len(outgoing),
                "dormitoryOccupancy": len(dorm),
                "requiredRestCount": len(outgoing),
                "missingRestOperators": sorted(missing_rest),
                "activeDormitoryConflicts": sorted(duplicate),
            }
        )

    depleted = {name for name, value in minimum.items() if value < -0.0001}
    end_deficit = {
        name
        for name, value in morale.items()
        if value < cycle_start_morale[name] - 0.0001
    }
    unrested.update(depleted)
    unrested.update(end_deficit)
    hard_gate = (
        cycle_hours <= MAX_CYCLE_HOURS + 0.0001
        and not unrested
        and not conflicts
    )
    failure_reasons: list[str] = []
    if cycle_hours > MAX_CYCLE_HOURS + 0.0001:
        failure_reasons.append("cycle_exceeds_seven_days")
    if any(item["missingRestOperators"] for item in transitions):
        failure_reasons.append("required_rest_exceeds_dormitory_assignment")
    if conflicts:
        failure_reasons.append("operator_active_and_resting_in_same_shift")
    if depleted:
        failure_reasons.append("operator_morale_depleted")
    if end_deficit:
        failure_reasons.append("cycle_does_not_restore_initial_morale")

    return {
        "version": 1,
        "hardGatePassed": hard_gate,
        "cycleHours": round(cycle_hours, 6),
        "cycleDays": round(cycle_hours / 24.0, 6),
        "planCount": len(shifts),
        "maxMorale": MAX_MORALE,
        "minimumMorale": round(min(minimum.values(), default=MAX_MORALE), 6),
        "minimumMoraleByOperator": {
            name: round(value, 6) for name, value in sorted(minimum.items())
        },
        "endingMoraleByOperator": {
            name: round(value, 6) for name, value in sorted(morale.items())
        },
        "unrestedOperators": sorted(unrested),
        "failureReasons": failure_reasons,
        "transitions": transitions,
        "assumptions": [
            "Working operators consume one morale per hour plus parsed positive skill consumption.",
            "Dormitory recovery uses the game-data level base rate (1.6 through 2.0 morale per hour).",
            "Parsed self, single-target, and room-wide recovery bonuses are counted; other bonuses are excluded.",
        ],
    }


def _advance_morale(
    layout: Layout,
    shifts: list[ShiftPlan],
    morale: dict[str, float],
) -> None:
    for shift in shifts:
        for room in shift.rooms:
            for skill in room.operators:
                extra_consumption = max(0.0, fatigue_delta_from_text(skill.description))
                morale[skill.operator_name] -= (1.0 + extra_consumption) * shift.duration_hours
        for room_index, room in enumerate(shift.dormitories):
            room_level = (
                layout.dormitory_levels[room_index]
                if room_index < len(layout.dormitory_levels)
                else 5
            )
            all_bonus, self_bonuses, single_bonus = dormitory_recovery_bonuses(room)
            single_target = dormitory_single_recovery_target(room, morale, single_bonus)
            for skill in room.operators:
                recovery_rate = (
                    dormitory_base_recovery_rate(room_level)
                    + all_bonus
                    + self_bonuses.get(skill.operator_name, 0.0)
                    + (single_bonus if skill.operator_name == single_target else 0.0)
                )
                morale[skill.operator_name] = min(
                    MAX_MORALE,
                    morale[skill.operator_name] + recovery_rate * shift.duration_hours,
                )


def dormitory_base_recovery_rate(room_level: int) -> float:
    return 1.5 + 0.1 * min(5, max(1, int(room_level)))


def dormitory_recovery_bonuses(room: Any) -> tuple[float, dict[str, float], float]:
    all_bonus = 0.0
    single_bonus = 0.0
    self_bonuses: dict[str, float] = {}
    for skill in room.operators:
        description = re.sub(r"<[^>]+>", "", skill.description)
        all_matches = re.findall(
            r"所有干员(?:的)?心情每小时恢复(?:速度)?\+([0-9]+(?:\.[0-9]+)?)",
            description,
        )
        if all_matches:
            all_bonus = max(all_bonus, *(float(value) for value in all_matches))
        self_matches = re.findall(
            r"自身心情每小时恢复(?:速度)?\+([0-9]+(?:\.[0-9]+)?)",
            description,
        )
        if self_matches:
            self_bonuses[skill.operator_name] = max(float(value) for value in self_matches)
        single_matches = re.findall(
            r"(?:某一名|前一位).*?心情每小时恢复(?:速度)?\+([0-9]+(?:\.[0-9]+)?)",
            description,
        )
        if single_matches:
            single_bonus = max(single_bonus, *(float(value) for value in single_matches))
    return all_bonus, self_bonuses, single_bonus


def dormitory_single_recovery_target(
    room: Any,
    morale: dict[str, float],
    single_bonus: float,
) -> str | None:
    if single_bonus <= 0.0:
        return None
    candidates = [skill.operator_name for skill in room.operators]
    return min(candidates, key=lambda name: morale.get(name, MAX_MORALE), default=None)


def _failed_audit(
    reason: str,
    unrested: list[str],
    cycle_hours: float,
    transitions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "version": 1,
        "hardGatePassed": False,
        "cycleHours": round(cycle_hours, 6),
        "cycleDays": round(cycle_hours / 24.0, 6),
        "planCount": len(transitions),
        "maxMorale": MAX_MORALE,
        "minimumMorale": None,
        "minimumMoraleByOperator": {},
        "endingMoraleByOperator": {},
        "unrestedOperators": sorted(unrested),
        "failureReasons": [reason],
        "transitions": transitions,
        "assumptions": [],
    }
