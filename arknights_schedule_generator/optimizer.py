from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import combinations, product
from time import perf_counter
from typing import Any, Iterable

from . import dependency_parser
from .data import GameData
from .models import BaseSkill, Layout, RoomAssignment, RosterOperator, ShiftPlan
from .morale import MAX_CYCLE_HOURS, audit_morale_cycle
from .power import PowerStatus, levels_or_default, resolve_power_layout
from .presets import (
    manufacture_targets,
    mode_preset,
    normalize_mode,
    preset_contract,
    target_label,
    trading_targets,
)
from .production import (
    DEFAULT_MAX_DRONE_CYCLE_REPEATS,
    DEFAULT_PURE_GOLD_TARGET_PER_DAY,
    DEFAULT_PURE_GOLD_TOLERANCE,
    GUIDE_MANUFACTURE_LOOKUPS,
    GUIDE_SPECIAL_TRADE_ANCHORS,
    ProductionReport,
    ProductionSimulator,
    ProductionVector,
    pure_gold_balance_error,
    pure_gold_balance_quality,
    resource_balance_quality,
)
from .room_limits import clamp_station_count
from .skill_rules import evaluate_room_effect, fatigue_delta_from_text


ROOM_NAMES = {
    "CONTROL": "控制中枢",
    "TRADING": "贸易站",
    "MANUFACTURE": "制造站",
    "POWER": "发电站",
    "DORMITORY": "宿舍",
    "MEETING": "会客室",
    "HIRE": "办公室",
}

DIAGNOSTIC_INSERTION_GROUP_LIMIT = 64
JOINT_PRODUCTION_CANDIDATE_LIMIT = 64
COMBO_CANDIDATE_TOP_LIMIT = 14
COMBO_CANDIDATE_POOL_LIMIT = 32
OPTIMIZER_MODEL_VERSION = 22
LAYOUT_ROOM_COUNT_LIMITS = {
    "TRADING": 5,
    "MANUFACTURE": 5,
    "POWER": 3,
}


@dataclass(frozen=True)
class Candidate:
    skill: BaseSkill
    score: float


@dataclass(frozen=True)
class RoomCombo:
    operators: tuple[BaseSkill, ...]
    score: float


@dataclass(frozen=True)
class RoomSpec:
    room_id: str
    room_type: str
    target: str | None
    capacity: int
    room_level: int = 3
    slots: int | None = None


@dataclass(frozen=True)
class ShiftInsertionSpec:
    room_type: str
    target: str | None
    operator_name: str
    allow_no_skill: bool = False
    room_group: str | None = None


@dataclass(frozen=True)
class ShiftInsertionGroup:
    id: str
    specs: tuple[ShiftInsertionSpec, ...]


@dataclass
class OptimizerResult:
    layout: Layout
    mode: str
    shift_hours: int
    shifts: list[ShiftPlan]
    score: float
    warnings: list[str]
    score_breakdown: dict[str, float] = field(default_factory=dict)
    production_report: ProductionReport | None = None
    drone_policy: str = "none"
    metric_profile: str = "guide"
    power_status: PowerStatus | None = None
    diagnostic_insertion_search: dict[str, Any] = field(default_factory=dict)
    runtime_profile: dict[str, Any] = field(default_factory=dict)


def parse_layout(raw: str) -> Layout:
    digits = [char for char in raw if char.isdigit()]
    if len(digits) != 3:
        raise ValueError("基建形态应为三位数字，例如 243、333、342。")
    trading, manufacture, power = (int(digit) for digit in digits)
    if trading + manufacture + power != 9:
        raise ValueError("贸易站、制造站、发电站数量之和应为 9。")
    if (
        trading > LAYOUT_ROOM_COUNT_LIMITS["TRADING"]
        or manufacture > LAYOUT_ROOM_COUNT_LIMITS["MANUFACTURE"]
        or power > LAYOUT_ROOM_COUNT_LIMITS["POWER"]
    ):
        raise ValueError(
            f"Invalid base layout {raw}: Trading Posts <= 5, "
            "Factories <= 5, and Power Plants <= 3."
        )
    return Layout(raw=raw, trading=trading, manufacture=manufacture, power=power)


def apply_layout_variant(layout: Layout, variant: str | None) -> Layout:
    if not variant or variant in {"full", "default"}:
        return layout
    normalized = variant.lower().replace("_", "-")
    if normalized in {"342-guide-orundum", "342-yituliu-orundum", "guide-orundum", "yituliu-orundum"}:
        if layout.label != "342":
            raise ValueError("342 guide Orundum layout variant can only be used with --layout 342.")
        return replace(
            layout,
            raw="342-guide-orundum",
            trading_levels=(3, 3, 1),
            manufacture_levels=(3, 2, 2, 3),
            manufacture_slots=(3, 2, 2, 3),
        )
    if not normalized.startswith("252-"):
        raise ValueError(
            "--layout-variant currently supports full/default, 252-11 through 252-15, "
            "or 342-guide-orundum."
        )
    if layout.label != "252":
        raise ValueError("252 slot variants can only be used with --layout 252.")
    slot_count = int(normalized.rsplit("-", 1)[1])
    if slot_count not in {11, 12, 13, 14, 15}:
        raise ValueError("252 slot variants must be 252-11, 252-12, 252-13, 252-14, or 252-15.")
    level_three_factories = slot_count - 10
    manufacture_slots = tuple([3] * level_three_factories + [2] * (5 - level_three_factories))
    return Layout(
        raw=layout.raw,
        trading=layout.trading,
        manufacture=layout.manufacture,
        power=layout.power,
        dormitory=layout.dormitory,
        control=layout.control,
        meeting=layout.meeting,
        hire=layout.hire,
        training=layout.training,
        workshop=layout.workshop,
        trading_levels=(3,) * layout.trading,
        manufacture_levels=manufacture_slots,
        manufacture_slots=manufacture_slots,
        power_levels=layout.power_levels,
        dormitory_levels=layout.dormitory_levels,
        control_level=layout.control_level,
        meeting_level=layout.meeting_level,
        hire_level=layout.hire_level,
        training_level=layout.training_level,
        workshop_level=layout.workshop_level,
        right_side_preset=layout.right_side_preset,
    )


class ScheduleOptimizer:
    @staticmethod
    def diagnostic_insertion_group_limit() -> int:
        return DIAGNOSTIC_INSERTION_GROUP_LIMIT

    @staticmethod
    def joint_production_candidate_limit() -> int:
        return JOINT_PRODUCTION_CANDIDATE_LIMIT

    @staticmethod
    def optimizer_model_version() -> int:
        return OPTIMIZER_MODEL_VERSION

    def __init__(
        self,
        game_data: GameData,
        roster: Iterable[RosterOperator],
        *,
        allow_upgrades: bool = False,
        upgrade_cost_weight: float = 0.015,
        shard_formula: str = "rock",
    ) -> None:
        self.game_data = game_data
        self.roster = [operator for operator in roster if operator.recruited]
        self.roster_by_name = {operator.name: operator for operator in self.roster}
        self.allow_upgrades = allow_upgrades
        self.upgrade_cost_weight = upgrade_cost_weight
        self.shard_formula = shard_formula
        self.skills_by_operator = {
            operator.name: game_data.skills_for_roster_operator(operator, allow_upgrades)
            for operator in self.roster
        }
        self._combo_simulator = ProductionSimulator(
            self.game_data,
            shard_formula=self.shard_formula,
            calibration_profile="guide",
        )
        self._room_combo_score_cache: dict[tuple[Any, ...], float] = {}
        self._combo_candidate_pool_cache: dict[tuple[Any, ...], tuple[Candidate, ...]] = {}
        self._room_combos_cache: dict[tuple[Any, ...], tuple[RoomCombo, ...]] = {}
        self._complete_room_cache: dict[tuple[Any, ...], tuple[BaseSkill, ...] | None] = {}
        self._room_combo_vector_cache: dict[tuple[Any, ...], ProductionVector] = {}
        self.profile_runtime = False
        self._runtime_counters: dict[str, int] = {}
        self._runtime_timings: dict[str, float] = {}
        self.operator_anchor_preference: set[str] = set()
        self.operator_anchor_rank: dict[str, int] = {}
        self.forced_insertion_groups: tuple[ShiftInsertionGroup, ...] = ()

    def optimize(
        self,
        layout: Layout,
        *,
        mode: str = "normal",
        shift_count: int = 2,
        shift_hours: int = 12,
        shift_durations: Iterable[float] | None = None,
        shift_times: list[str] | None = None,
        drone_policy: str = "none",
        min_lmd_gross: float = 0.0,
        min_exp: float = 0.0,
        min_orundum: float = 0.0,
        operator_anchor_preference: Iterable[str] | None = None,
        forced_targets: tuple[Iterable[str], Iterable[str]] | None = None,
        forced_insertion_groups: Iterable[ShiftInsertionGroup] | None = None,
        drone_reference_daily: dict[str, Any] | None = None,
        pure_gold_target: float = DEFAULT_PURE_GOLD_TARGET_PER_DAY,
        pure_gold_tolerance: float = DEFAULT_PURE_GOLD_TOLERANCE,
        max_drone_cycle_repeats: int = DEFAULT_MAX_DRONE_CYCLE_REPEATS,
        profile_runtime: bool = False,
    ) -> OptimizerResult:
        self.profile_runtime = bool(profile_runtime)
        self._runtime_counters = {}
        self._runtime_timings = {}
        normalized_mode = normalize_mode(mode)
        self.operator_anchor_preference = set()
        self.operator_anchor_rank = {}
        for name in operator_anchor_preference or []:
            normalized_name = str(name)
            if not normalized_name or normalized_name in self.operator_anchor_preference:
                continue
            self.operator_anchor_preference.add(normalized_name)
            self.operator_anchor_rank[normalized_name] = len(self.operator_anchor_rank)
        self.forced_insertion_groups = tuple(forced_insertion_groups or ())
        normalized_shift_durations = normalize_shift_durations(
            shift_count=shift_count,
            shift_hours=shift_hours,
            shift_durations=shift_durations,
        )
        shift_count = len(normalized_shift_durations)
        shift_times = shift_times or default_shift_times_for_durations(normalized_shift_durations)
        layout, layout_power = resolve_power_layout(self.game_data, layout)
        if layout.right_side_preset != "ignore" and not layout_power.feasible:
            raise ValueError(
                "Insufficient base power for "
                f"{layout.label} with right-side preset {layout.right_side_preset}: "
                f"supply {layout_power.supplied}, demand {layout_power.consumed}, "
                f"margin {layout_power.margin}."
            )
        base_warnings = [
            f"请确认游戏内房间分布已改为 {layout.label}: "
            f"{layout.trading} 贸易站 / {layout.manufacture} 制造站 / {layout.power} 发电站。",
            "请确认每个制造站和贸易站的生产/订单目标与 JSON 中 targetLabel 一致。",
            "Power check: "
            f"supply {layout_power.supplied}, demand {layout_power.consumed}, "
            f"margin {layout_power.margin}, right-side preset {layout_power.preset}.",
        ]
        simulator = ProductionSimulator(
            self.game_data,
            shard_formula=self.shard_formula,
            drone_policy=drone_policy,
            calibration_profile="guide",
            reference_expected_daily=drone_reference_daily,
            pure_gold_target=pure_gold_target,
            pure_gold_tolerance=pure_gold_tolerance,
        )
        target_options = (
            forced_target_options(layout, forced_targets)
            if forced_targets is not None
            else enumerate_target_options(layout, normalized_mode)
        )
        best: tuple[
            float,
            list[ShiftPlan],
            list[str],
            ProductionReport,
            dict[str, Any],
            list[str],
            list[str],
        ] | None = None
        for trading, manufacture in target_options:
            room_specs = self._room_specs_for_targets(layout, trading, manufacture)
            shifts, warnings = self._build_shifts(
                layout,
                room_specs,
                shift_durations=normalized_shift_durations,
                shift_times=shift_times,
            )
            shifts, insertion_search = self._improve_shifts_with_insertions(
                layout,
                shifts,
                simulator,
                mode=normalized_mode,
            )
            production_report = simulator.evaluate(shifts)
            selection_score = target_selection_score(
                normalized_mode,
                production_report,
                min_lmd_gross=min_lmd_gross,
                min_exp=min_exp,
                min_orundum=min_orundum,
            )
            if best is None or selection_score > best[0]:
                best = (
                    selection_score,
                    shifts,
                    warnings,
                    production_report,
                    insertion_search,
                    trading,
                    manufacture,
                )
        assert best is not None
        _, shifts, build_warnings, production_report, insertion_search, selected_trading, selected_manufacture = best
        if forced_targets is not None:
            insertion_search["targetOptionSearch"] = {
                "version": 3,
                "source": "forced_target_distribution",
                "optionCount": len(target_options),
                "selectedTradingTargets": list(selected_trading),
                "selectedManufactureTargets": list(selected_manufacture),
            }
        shifts, local_audit, production_report = self._improve_shifts_with_local_replacements(
            layout,
            shifts,
            simulator,
            mode=normalized_mode,
            current_report=production_report,
        )
        insertion_search["localOptimalityAudit"] = local_audit
        shifts, morale_audit = self._ensure_sustainable_morale_cycle(layout, shifts)
        production_report = simulator.evaluate(shifts)
        insertion_search["moraleCycleAudit"] = morale_audit
        shifts, production_report, pure_gold_policy = self._apply_pure_gold_drone_cycle_repeats(
            shifts,
            simulator,
            current_report=production_report,
            drone_policy=drone_policy,
            pure_gold_target=pure_gold_target,
            pure_gold_tolerance=pure_gold_tolerance,
            max_drone_cycle_repeats=max_drone_cycle_repeats,
        )
        insertion_search["pureGoldBalancePolicy"] = pure_gold_policy
        final_morale_audit = audit_morale_cycle(layout, shifts)
        for key in (
            "rotationGroups",
            "fastRotationRooms",
            "basePlanCount",
            "expandedPlanCount",
        ):
            if key in morale_audit:
                final_morale_audit[key] = morale_audit[key]
        if not final_morale_audit["hardGatePassed"]:
            raise ValueError(
                "No sustainable morale cycle: "
                + ", ".join(final_morale_audit["failureReasons"])
            )
        insertion_search["moraleCycleAudit"] = final_morale_audit
        insertion_search["candidatePoolAudit"] = self._candidate_pool_audit(shifts)
        insertion_search["objectiveConflictAudit"] = objective_conflict_audit(
            insertion_search,
            local_audit.get("objectiveConflictAudit", {}),
        )
        insertion_search["pureGoldTarget"] = round(float(pure_gold_target), 6)
        insertion_search["pureGoldTolerance"] = round(float(pure_gold_tolerance), 6)
        insertion_search["maxDroneCycleRepeats"] = max(1, int(max_drone_cycle_repeats))
        insertion_search["cacheValidation"] = {
            "status": "fresh",
            "optimizerModelVersion": OPTIMIZER_MODEL_VERSION,
            "maxGroups": DIAGNOSTIC_INSERTION_GROUP_LIMIT,
            "jointProductionCandidateLimit": JOINT_PRODUCTION_CANDIDATE_LIMIT,
            "pureGoldTarget": round(float(pure_gold_target), 6),
            "pureGoldTolerance": round(float(pure_gold_tolerance), 6),
            "maxDroneCycleRepeats": max(1, int(max_drone_cycle_repeats)),
        }
        warnings = [*base_warnings, *build_warnings]
        if normalized_mode in {"balanced_orundum", "max_orundum"}:
            warnings.append("搓玉模式会消耗龙门币和低级材料；请查看 dailyExpected 的净变化。")
        warnings.extend(threshold_warnings(production_report, min_lmd_gross, min_exp, min_orundum))
        return OptimizerResult(
            layout=layout,
            mode=normalized_mode,
            shift_hours=shift_hours,
            shifts=shifts,
            score=production_report.score,
            warnings=dedupe(warnings),
            score_breakdown=production_report.scoreBreakdown,
            production_report=production_report,
            drone_policy=drone_policy,
            metric_profile="guide",
            power_status=layout_power,
            diagnostic_insertion_search=insertion_search,
            runtime_profile=self._runtime_profile() if self.profile_runtime else {},
        )

    def _cache_context_key(self) -> tuple[Any, ...]:
        return (
            OPTIMIZER_MODEL_VERSION,
            round(self.upgrade_cost_weight, 8),
            bool(self.allow_upgrades),
            self.shard_formula,
        )

    def _skill_cache_key(self, skill: BaseSkill) -> tuple[Any, ...]:
        return (
            skill.operator_name,
            skill.buff_id,
            skill.room_type,
            tuple(skill.targets),
            bool(skill.unlocked),
            round(float(skill.parsed_score), 6),
            tuple(skill.faction_tags),
            None
            if skill.upgrade is None
            else (
                skill.upgrade.char_id,
                skill.upgrade.to_elite,
                skill.upgrade.to_level,
                round(float(skill.upgrade.cost_score), 6),
            ),
        )

    def _record_runtime_count(self, key: str, amount: int = 1) -> None:
        if not self.profile_runtime:
            return
        self._runtime_counters[key] = self._runtime_counters.get(key, 0) + amount

    def _record_runtime_time(self, key: str, elapsed: float) -> None:
        if not self.profile_runtime:
            return
        self._runtime_timings[key] = self._runtime_timings.get(key, 0.0) + elapsed

    def _runtime_profile(self) -> dict[str, Any]:
        return {
            "version": 1,
            "optimizerModelVersion": OPTIMIZER_MODEL_VERSION,
            "counters": dict(sorted(self._runtime_counters.items())),
            "timingsSeconds": {
                key: round(value, 6)
                for key, value in sorted(self._runtime_timings.items())
            },
            "cacheSizes": {
                "comboCandidatePool": len(self._combo_candidate_pool_cache),
                "roomCombos": len(self._room_combos_cache),
                "completeRoomAfterInsertions": len(self._complete_room_cache),
                "roomComboScore": len(self._room_combo_score_cache),
                "roomComboVector": len(self._room_combo_vector_cache),
            },
        }

    def _apply_pure_gold_drone_cycle_repeats(
        self,
        shifts: list[ShiftPlan],
        simulator: ProductionSimulator,
        *,
        current_report: ProductionReport,
        drone_policy: str,
        pure_gold_target: float,
        pure_gold_tolerance: float,
        max_drone_cycle_repeats: int,
    ) -> tuple[list[ShiftPlan], ProductionReport, dict[str, Any]]:
        max_repeats = max(1, int(max_drone_cycle_repeats))
        base_quality = pure_gold_balance_quality(
            current_report.dailyExpected,
            target=pure_gold_target,
            tolerance=pure_gold_tolerance,
        )
        policy: dict[str, Any] = {
            "version": 1,
            "scope": "drone_allocation_only",
            "optimizerModelVersion": OPTIMIZER_MODEL_VERSION,
            "targetPerDay": round(float(pure_gold_target), 3),
            "tolerancePerDay": round(float(pure_gold_tolerance), 3),
            "externalPureGoldAssumptionPerDay": round(-float(pure_gold_target), 3),
            "externalPureGoldAssumptionSource": (
                "Conservative stable-income assumption: current daily/weekly missions and "
                "permanent shops are not modeled as reliable Pure Gold income."
            ),
            "maxDroneCycleRepeats": max_repeats,
            "baseShiftCount": len(shifts),
            "initialPureGoldDelta": base_quality["pureGoldDelta"],
            "initialDeltaFromTarget": base_quality["deltaFromTarget"],
        }
        if simulator.drone_policy != "auto" or str(drone_policy).lower().replace("_", "-") != "auto":
            policy.update(
                {
                    "status": "not_applicable_drone_policy",
                    "reason": "Pure Gold balancing is applied only by --drone-policy auto.",
                    "repeatCount": 1,
                    "cycleRepeated": False,
                    "finalPureGoldDelta": base_quality["pureGoldDelta"],
                    "finalDeltaFromTarget": base_quality["deltaFromTarget"],
                    "finalAbsDeltaFromTarget": base_quality["absDeltaFromTarget"],
                    "withinTolerance": base_quality["withinTolerance"],
                }
            )
            return shifts, current_report, policy

        best_shifts = shifts
        best_report = current_report
        best_repeat = 1
        best_error = pure_gold_balance_error(current_report.dailyExpected.pureGoldDelta, pure_gold_target)
        candidates: list[dict[str, Any]] = []
        for repeat_count in range(1, max_repeats + 1):
            candidate_shifts = shifts if repeat_count == 1 else repeat_shift_cycle(shifts, repeat_count)
            candidate_report = current_report if repeat_count == 1 else simulator.evaluate(candidate_shifts)
            quality = pure_gold_balance_quality(
                candidate_report.dailyExpected,
                target=pure_gold_target,
                tolerance=pure_gold_tolerance,
            )
            error = float(quality["absDeltaFromTarget"])
            candidates.append(
                {
                    "repeatCount": repeat_count,
                    "shiftCount": len(candidate_shifts),
                    "pureGoldDelta": quality["pureGoldDelta"],
                    "deltaFromTarget": quality["deltaFromTarget"],
                    "absDeltaFromTarget": quality["absDeltaFromTarget"],
                    "withinTolerance": quality["withinTolerance"],
                    "score": round(candidate_report.score, 3),
                    "droneUsed": round(candidate_report.dailyExpected.droneUsed, 3),
                    "droneCount": round(candidate_report.dailyExpected.droneCount, 3),
                }
            )
            if (
                error < best_error - 0.001
                or (
                    abs(error - best_error) <= 0.001
                    and candidate_report.score > best_report.score + 0.001
                )
            ):
                best_error = error
                best_shifts = candidate_shifts
                best_report = candidate_report
                best_repeat = repeat_count
            if error <= pure_gold_tolerance:
                best_shifts = candidate_shifts
                best_report = candidate_report
                best_repeat = repeat_count
                best_error = error
                break

        final_quality = pure_gold_balance_quality(
            best_report.dailyExpected,
            target=pure_gold_target,
            tolerance=pure_gold_tolerance,
        )
        within = bool(final_quality["withinTolerance"])
        initial_error = float(base_quality["absDeltaFromTarget"])
        status = "within_tolerance" if within else "unable_to_balance_within_repeat_limit"
        policy.update(
            {
                "status": status,
                "repeatCount": best_repeat,
                "cycleRepeated": best_repeat > 1,
                "finalShiftCount": len(best_shifts),
                "finalPureGoldDelta": final_quality["pureGoldDelta"],
                "finalDeltaFromTarget": final_quality["deltaFromTarget"],
                "finalAbsDeltaFromTarget": final_quality["absDeltaFromTarget"],
                "withinTolerance": within,
                "improvedByRepeating": best_error < initial_error - 0.001,
                "candidateRepeats": candidates,
            }
        )
        return best_shifts, best_report, policy

    def _build_shifts(
        self,
        layout: Layout,
        room_specs: list[RoomSpec],
        *,
        shift_durations: list[float] | None = None,
        shift_count: int | None = None,
        shift_hours: int | None = None,
        shift_times: list[str],
    ) -> tuple[list[ShiftPlan], list[str]]:
        if shift_durations is None:
            shift_durations = normalize_shift_durations(
                shift_count=shift_count or 2,
                shift_hours=shift_hours or 12,
            )
        dorm_specs = self._dorm_specs(layout)
        work_shifts: list[dict[str, Any]] = []
        warnings: list[str] = []
        previous_active: set[str] = set()
        first_active: set[str] | None = None
        shift_count = len(shift_durations)
        for shift_index, duration_hours in enumerate(shift_durations):
            active_names: set[str] = set()
            boundary_excluded = set(previous_active)
            if shift_index == shift_count - 1 and first_active:
                boundary_excluded.update(first_active)
            rooms = self._assign_work_rooms_jointly(
                room_specs,
                boundary_excluded,
                duration_hours=duration_hours,
            )
            for assignment, spec in zip(rooms, room_specs):
                active_names.update(skill.operator_name for skill in assignment.operators)
                if len(assignment.operators) < spec.capacity:
                    warnings.append(
                        f"{assignment.room_name} {spec.room_id} 在班次 {shift_label(shift_index)} "
                        f"只填入 {len(assignment.operators)}/{spec.capacity} 名干员。"
                    )

            if first_active is None:
                first_active = set(active_names)
            previous_active = set(active_names)
            work_shifts.append(
                {
                    "name": shift_label(shift_index),
                    "start": shift_times[shift_index % len(shift_times)],
                    "duration_hours": duration_hours,
                    "rooms": rooms,
                    "active_names": active_names,
                }
            )

        shifts: list[ShiftPlan] = []
        active_by_shift = [set(shift["active_names"]) for shift in work_shifts]
        visible_anchor_names = {
            name
            for active_names in active_by_shift
            for name in active_names
            if name in self.operator_anchor_preference
        }
        for shift_index, work_shift in enumerate(work_shifts):
            active_names = set(work_shift["active_names"])
            rest_priority = self._rest_priority(active_by_shift, shift_index)
            dormitories = self._assign_dormitories(
                dorm_specs,
                active_names,
                rest_priority,
                visible_anchor_names=visible_anchor_names,
            )
            visible_anchor_names.update(
                skill.operator_name
                for dormitory in dormitories
                for skill in dormitory.operators
                if skill.operator_name in self.operator_anchor_preference
            )
            shifts.append(
                ShiftPlan(
                    name=str(work_shift["name"]),
                    start=str(work_shift["start"]),
                    duration_hours=float(work_shift["duration_hours"]),
                    rooms=list(work_shift["rooms"]),
                    dormitories=dormitories,
                )
            )
        return self._improve_dormitory_anchor_coverage(shifts), warnings

    def _assign_work_rooms_jointly(
        self,
        room_specs: list[RoomSpec],
        boundary_excluded: set[str],
        *,
        duration_hours: float,
    ) -> list[RoomAssignment]:
        production_specs = [
            spec
            for spec in room_specs
            if spec.room_type in {"TRADING", "MANUFACTURE", "POWER"}
        ]
        production_ids = {spec.room_id for spec in production_specs}
        other_specs = [spec for spec in room_specs if spec.room_id not in production_ids]

        rooms_by_id = self._joint_production_room_assignments(
            production_specs,
            boundary_excluded,
            duration_hours=duration_hours,
        )
        active_names = {
            skill.operator_name
            for room in rooms_by_id.values()
            for skill in room.operators
        }
        for spec in other_specs:
            assignment = self._assign_room(
                spec=spec,
                excluded=active_names | boundary_excluded,
                duration_hours=duration_hours,
            )
            rooms_by_id[spec.room_id] = assignment
            active_names.update(skill.operator_name for skill in assignment.operators)
        return [rooms_by_id[spec.room_id] for spec in room_specs]

    def _joint_production_room_assignments(
        self,
        specs: list[RoomSpec],
        boundary_excluded: set[str],
        *,
        duration_hours: float,
        candidate_limit: int = JOINT_PRODUCTION_CANDIDATE_LIMIT,
        beam_width: int = 256,
    ) -> dict[str, RoomAssignment]:
        states: list[tuple[float, set[str], list[RoomAssignment]]] = [(0.0, set(), [])]
        for spec in specs:
            candidates = self._candidate_production_room_assignments(
                spec,
                boundary_excluded,
                duration_hours=duration_hours,
                limit=candidate_limit,
            )
            if not candidates:
                candidates = [
                    self._assign_room(
                        spec=spec,
                        excluded=boundary_excluded,
                        duration_hours=duration_hours,
                    )
                ]

            next_states: list[tuple[float, set[str], list[RoomAssignment]]] = []
            for score, used_names, rooms in states:
                for assignment in candidates:
                    names = {skill.operator_name for skill in assignment.operators}
                    if names & used_names:
                        continue
                    next_states.append(
                        (
                            score + assignment.score,
                            used_names | names,
                            [*rooms, assignment],
                        )
                    )
            if not next_states:
                score, used_names, rooms = states[0]
                greedy = self._assign_room(
                    spec=spec,
                    excluded=boundary_excluded | used_names,
                    duration_hours=duration_hours,
                )
                names = {skill.operator_name for skill in greedy.operators}
                next_states = [(score + greedy.score, used_names | names, [*rooms, greedy])]
            next_states.sort(key=lambda item: item[0], reverse=True)
            states = next_states[:beam_width]

        best_rooms = states[0][2] if states else []
        return {room.room_id: room for room in best_rooms}

    def _candidate_production_room_assignments(
        self,
        spec: RoomSpec,
        boundary_excluded: set[str],
        *,
        duration_hours: float,
        limit: int,
    ) -> list[RoomAssignment]:
        combos = self._room_combos(spec, boundary_excluded, duration_hours)[:limit]
        return [self._assignment_from_combo(spec, combo) for combo in combos]

    def _assignment_from_combo(self, spec: RoomSpec, combo: RoomCombo) -> RoomAssignment:
        return RoomAssignment(
            room_id=spec.room_id,
            room_type=spec.room_type,
            room_name=ROOM_NAMES.get(spec.room_type, spec.room_type),
            target=spec.target,
            operators=list(combo.operators),
            score=round(combo.score, 3),
            room_level=spec.room_level,
            slots=spec.slots,
        )

    def _improve_shifts_with_insertions(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        simulator: ProductionSimulator,
        *,
        mode: str = "normal",
    ) -> tuple[list[ShiftPlan], dict[str, Any]]:
        all_groups = self._diagnostic_shift_insertion_groups()
        groups = self._available_shift_insertion_groups(all_groups)
        audit: dict[str, Any] = {
            "source": "diagnostic_named_dependency",
            "optimizerModelVersion": OPTIMIZER_MODEL_VERSION,
            "maxGroups": DIAGNOSTIC_INSERTION_GROUP_LIMIT,
            "jointProductionCandidateLimit": JOINT_PRODUCTION_CANDIDATE_LIMIT,
            "operatorAnchorPreferenceCount": len(self.operator_anchor_preference),
            "availableGroupCount": len(groups),
            "accepted": [],
            "skipped": [
                *self._unsearched_faction_dependency_records(),
                *self._unavailable_shift_insertion_records(all_groups, groups),
                *self._group_limit_insertion_records(all_groups, groups),
            ],
        }
        if not groups:
            audit["summary"] = insertion_search_summary(audit)
            audit["localQualityAudit"] = local_quality_audit(audit)
            return shifts, audit

        best_shifts = shifts
        best_report = simulator.evaluate(best_shifts)
        best_score = insertion_objective_score(mode, best_report)
        max_passes = min(8, max(2, len(groups)))
        audit["maxPasses"] = max_passes
        stopped_by_pass_limit = False
        for pass_index in range(max_passes):
            accepted_this_pass = 0
            for group in groups:
                result, candidate, candidate_report = self._best_insertion_attempt(
                    layout,
                    best_shifts,
                    simulator,
                    group,
                    best_score,
                    best_report,
                    mode=mode,
                )
                if result["status"] != "improves" or candidate is None:
                    continue
                score = float(result.get("candidateScore") or best_score)
                if score <= best_score + 0.001:
                    continue
                accepted = dict(result)
                accepted["status"] = "accepted"
                accepted["pass"] = pass_index + 1
                audit["accepted"].append(accepted)
                best_shifts = candidate
                if candidate_report is not None:
                    best_report = candidate_report
                else:
                    best_report = simulator.evaluate(best_shifts)
                best_score = score
                accepted_this_pass += 1
            if accepted_this_pass == 0:
                break
        else:
            stopped_by_pass_limit = True

        accepted_history = list(audit["accepted"])
        groups_by_key = {insertion_group_key(group): group for group in groups}
        final_accepted: list[dict[str, Any]] = []
        displaced: list[dict[str, Any]] = []
        for item in accepted_history:
            group = groups_by_key.get(
                (str(item.get("groupId") or ""), str(item.get("specKey") or ""))
            )
            satisfied_shifts = (
                [shift.name for shift in best_shifts if self._insertion_group_satisfied(shift, group)]
                if group is not None
                else []
            )
            if satisfied_shifts:
                final_accepted.append(
                    {
                        **item,
                        "finalSatisfied": True,
                        "satisfiedShifts": satisfied_shifts,
                    }
                )
            else:
                displaced.append(
                    {
                        **item,
                        "status": "displaced_after_acceptance",
                        "finalSatisfied": False,
                    }
                )

        audit["accepted"] = final_accepted
        audit["displaced"] = displaced
        accepted_keys = {
            (item.get("groupId"), item.get("specKey"))
            for item in final_accepted
        }
        skipped: list[dict[str, Any]] = list(audit.get("skipped") or [])
        for group in groups:
            group_key = insertion_group_key(group)
            if group_key in accepted_keys:
                continue
            result, _, _ = self._best_insertion_attempt(
                layout,
                best_shifts,
                simulator,
                group,
                best_score,
                best_report,
                mode=mode,
            )
            if result["status"] == "improves" and stopped_by_pass_limit:
                result = {**result, "status": "remaining_improvement_after_pass_limit"}
            if any(
                (item.get("groupId"), item.get("specKey")) == group_key
                for item in displaced
            ):
                result = {**result, "previouslyAccepted": True}
            skipped.append(result)
        audit["skipped"] = skipped
        audit["summary"] = insertion_search_summary(audit)
        audit["localQualityAudit"] = local_quality_audit(audit)
        return best_shifts, audit

    def _best_insertion_attempt(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        simulator: ProductionSimulator,
        group: ShiftInsertionGroup,
        current_score: float,
        current_report: ProductionReport,
        *,
        mode: str = "normal",
    ) -> tuple[dict[str, Any], list[ShiftPlan] | None, ProductionReport | None]:
        started_at = perf_counter()

        def finalize(
            result: dict[str, Any],
            candidate: list[ShiftPlan] | None,
            report: ProductionReport | None,
        ) -> tuple[dict[str, Any], list[ShiftPlan] | None, ProductionReport | None]:
            elapsed = perf_counter() - started_at
            self._record_runtime_count("insertionAttempt.count")
            self._record_runtime_time("insertionAttempt.seconds", elapsed)
            status = str(result.get("status") or "unknown")
            self._record_runtime_count(f"insertionAttempt.status.{status}")
            if self.profile_runtime:
                result = {**result, "runtimeSeconds": round(elapsed, 6)}
            return result, candidate, report

        satisfied_shifts = [
            shift.name for shift in shifts if self._insertion_group_satisfied(shift, group)
        ]
        base = insertion_group_to_dict(group)
        if satisfied_shifts:
            return finalize(
                {
                    **base,
                    "status": "already_satisfied",
                    "satisfiedShifts": satisfied_shifts,
                    "currentScore": round(current_score, 3),
                },
                None,
                None,
            )

        best_result: dict[str, Any] | None = None
        best_candidate: list[ShiftPlan] | None = None
        best_report: ProductionReport | None = None
        attempted_shifts: list[str] = []
        for shift_index, shift in enumerate(shifts):
            attempted_shifts.append(shift.name)
            candidate = self._insert_group_into_shift(layout, shifts, shift_index, group)
            if candidate is None:
                candidate = self._insert_group_with_boundary_relocation(
                    layout,
                    shifts,
                    shift_index,
                    group,
                )
            if candidate is None:
                continue
            candidate_report = simulator.evaluate(candidate)
            score = insertion_objective_score(mode, candidate_report)
            delta = round(score - current_score, 3)
            result = {
                **base,
                "status": "improves" if delta > 0.001 else "evaluated_not_improving",
                "shift": shift.name,
                "currentScore": round(current_score, 3),
                "candidateScore": round(score, 3),
                "scoreDelta": delta,
                "dailyExpectedDelta": production_vector_delta(
                    current_report.dailyExpected.to_dict(),
                    candidate_report.dailyExpected.to_dict(),
                ),
                "assignmentChanges": shift_assignment_changes(shifts, candidate),
            }
            if delta <= 0.001:
                result["reason"] = (
                    "candidate_seen_but_not_selected: composite objective score did not improve; "
                    "Pure Gold inventory balance is not part of this rejection."
                )
            if best_result is None or delta > float(best_result.get("scoreDelta") or -1_000_000):
                best_result = result
                best_candidate = candidate
                best_report = candidate_report

        if best_result is None:
            return finalize(
                {
                    **base,
                    "status": "unplaceable",
                    "attemptedShifts": attempted_shifts,
                    "currentScore": round(current_score, 3),
                },
                None,
                None,
            )
        return finalize(best_result, best_candidate, best_report)

    def _insert_group_with_boundary_relocation(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        shift_index: int,
        group: ShiftInsertionGroup,
    ) -> list[ShiftPlan] | None:
        if len(shifts) <= 1:
            return None
        shift = shifts[shift_index]
        if self._insertion_group_satisfied(shift, group):
            return None
        forced_names = {spec.operator_name for spec in group.specs}
        boundary_indexes = {
            neighbor
            for neighbor in {(shift_index - 1) % len(shifts), (shift_index + 1) % len(shifts)}
            if neighbor != shift_index
        }
        if not any(forced_names & self._active_names(shifts[index]) for index in boundary_indexes):
            return None

        relocated = list(shifts)
        for index in boundary_indexes:
            relocated[index] = self._shift_without_forced_names(shifts[index], forced_names)
        inserted = self._insert_group_into_shift(layout, relocated, shift_index, group)
        if inserted is None:
            return None
        return self._refill_relocated_boundary_shifts(
            layout,
            inserted,
            boundary_indexes,
            forced_names,
        )

    def _shift_without_forced_names(
        self,
        shift: ShiftPlan,
        forced_names: set[str],
    ) -> ShiftPlan:
        rooms: list[RoomAssignment] = []
        for room in shift.rooms:
            operators = [
                skill
                for skill in room.operators
                if skill.operator_name not in forced_names
            ]
            if len(operators) == len(room.operators):
                rooms.append(room)
                continue
            rooms.append(
                RoomAssignment(
                    room_id=room.room_id,
                    room_type=room.room_type,
                    room_name=room.room_name,
                    target=room.target,
                    operators=operators,
                    score=round(self._score_assignment_room(room, operators, shift.duration_hours), 3),
                    room_level=room.room_level,
                    slots=room.slots,
                    product_capacity=room.product_capacity or len(room.operators),
                    order_limit=room.order_limit,
                )
            )
        return ShiftPlan(
            name=shift.name,
            start=shift.start,
            duration_hours=shift.duration_hours,
            rooms=rooms,
            dormitories=[],
        )

    def _refill_relocated_boundary_shifts(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        boundary_indexes: set[int],
        forced_names: set[str],
    ) -> list[ShiftPlan] | None:
        refilled = list(shifts)
        for index in boundary_indexes:
            shift = refilled[index]
            boundary_names = self._boundary_active_names(refilled, index)
            selected_in_shift: set[str] = set()
            rooms: list[RoomAssignment] = []
            for room in shift.rooms:
                replacement = self._room_with_insertions(
                    room,
                    [],
                    selected_in_shift,
                    forced_names,
                    boundary_names,
                    shift.duration_hours,
                )
                if replacement is None:
                    return None
                if (
                    replacement.room_type == "CONTROL"
                    and len(replacement.operators) < self._assignment_capacity(replacement)
                ):
                    completed = self._complete_room_after_insertions(
                        replacement,
                        list(replacement.operators),
                        selected_in_shift,
                        boundary_names,
                        forced_names,
                        shift.duration_hours,
                    )
                    if completed is None:
                        return None
                    replacement = replace(
                        replacement,
                        operators=completed,
                        score=round(
                            self._score_assignment_room(
                                replacement,
                                completed,
                                shift.duration_hours,
                            ),
                            3,
                        ),
                    )
                rooms.append(replacement)
                selected_in_shift.update(skill.operator_name for skill in replacement.operators)
            refilled[index] = ShiftPlan(
                name=shift.name,
                start=shift.start,
                duration_hours=shift.duration_hours,
                rooms=rooms,
                dormitories=[],
            )
        if not self._active_boundaries_are_valid(refilled):
            return None
        return self._rebuild_dormitories(layout, refilled)

    def _improve_shifts_with_local_replacements(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        simulator: ProductionSimulator,
        *,
        mode: str,
        current_report: ProductionReport,
    ) -> tuple[list[ShiftPlan], dict[str, Any], ProductionReport]:
        best_shifts = shifts
        best_report = current_report
        best_score = insertion_objective_score(mode, best_report)
        accepted: list[dict[str, Any]] = []
        objective_conflicts: list[dict[str, Any]] = []
        evaluated_count = 0
        remaining_positive: list[dict[str, Any]] = []
        max_passes = 4
        for pass_index in range(max_passes):
            (
                single_attempt,
                single_shifts,
                single_report,
                single_positives,
                single_conflicts,
                single_evaluated,
            ) = self._best_local_replacement_attempt(
                layout,
                best_shifts,
                simulator,
                mode=mode,
                current_report=best_report,
                current_score=best_score,
            )
            (
                room_attempt,
                room_shifts,
                room_report,
                room_positives,
                room_conflicts,
                room_evaluated,
            ) = self._best_room_replacement_attempt(
                layout,
                best_shifts,
                simulator,
                mode=mode,
                current_report=best_report,
                current_score=best_score,
            )
            evaluated_count += single_evaluated + room_evaluated
            objective_conflicts.extend(single_conflicts)
            objective_conflicts.extend(room_conflicts)
            positives = sorted(
                [*single_positives, *room_positives],
                key=lambda item: (
                    float(item.get("scoreDelta") or 0.0),
                    float(item.get("tieBreakDelta") or 0.0),
                    float((item.get("dailyExpectedDelta") or {}).get("lmdGross") or 0.0),
                ),
                reverse=True,
            )
            attempts = [
                (single_attempt, single_shifts, single_report),
                (room_attempt, room_shifts, room_report),
            ]
            attempts = [item for item in attempts if item[0] is not None]
            if attempts:
                attempt, candidate_shifts, candidate_report = max(
                    attempts,
                    key=lambda item: (
                        float(item[0].get("scoreDelta") or 0.0),
                        float(item[0].get("tieBreakDelta") or 0.0),
                        float((item[0].get("dailyExpectedDelta") or {}).get("lmdGross") or 0.0),
                    ),
                )
            else:
                attempt = None
                candidate_shifts = None
                candidate_report = None
            if attempt is None or candidate_shifts is None or candidate_report is None:
                remaining_positive = positives
                break
            score_delta = float(attempt.get("scoreDelta") or 0.0)
            tie_delta = float(attempt.get("tieBreakDelta") or 0.0)
            if score_delta <= 0.001 and tie_delta <= 0.001:
                remaining_positive = positives
                break
            accepted.append(
                {
                    **attempt,
                    "status": "accepted",
                    "pass": pass_index + 1,
                    "acceptanceReason": (
                        "objective_score" if score_delta > 0.001 else "tie_break_skill_quality"
                    ),
                }
            )
            best_shifts = candidate_shifts
            best_report = candidate_report
            best_score = insertion_objective_score(mode, best_report)
        else:
            (
                _single_attempt,
                _single_shifts,
                _single_report,
                single_positive,
                single_conflicts,
                single_evaluated,
            ) = self._best_local_replacement_attempt(
                layout,
                best_shifts,
                simulator,
                mode=mode,
                current_report=best_report,
                current_score=best_score,
            )
            (
                _room_attempt,
                _room_shifts,
                _room_report,
                room_positive,
                room_conflicts,
                room_evaluated,
            ) = self._best_room_replacement_attempt(
                layout,
                best_shifts,
                simulator,
                mode=mode,
                current_report=best_report,
                current_score=best_score,
            )
            remaining_positive = sorted(
                [*single_positive, *room_positive],
                key=lambda item: (
                    float(item.get("scoreDelta") or 0.0),
                    float(item.get("tieBreakDelta") or 0.0),
                    float((item.get("dailyExpectedDelta") or {}).get("lmdGross") or 0.0),
                ),
                reverse=True,
            )
            evaluated_count += single_evaluated + room_evaluated
            objective_conflicts.extend(single_conflicts)
            objective_conflicts.extend(room_conflicts)

        deduped_conflicts = dedupe_local_records(objective_conflicts)
        return (
            best_shifts,
            {
                "source": "single_operator_replacement_neighborhood",
                "optimizerModelVersion": OPTIMIZER_MODEL_VERSION,
                "maxPasses": max_passes,
                "acceptedCount": len(accepted),
                "accepted": accepted,
                "evaluatedCount": evaluated_count,
                "remainingPositiveCount": len(remaining_positive),
                "positiveNeighborhoods": remaining_positive[:20],
                "objectiveConflictAudit": {
                    "policy": "Local replacements use the active mode objective; Pure Gold inventory balance is handled by drones/report diagnostics and does not veto LMD-gross-positive Trading Post replacements.",
                    "lmdPositiveRejectedCount": len(deduped_conflicts),
                    "lmdPositiveRejected": deduped_conflicts[:120],
                },
            },
            best_report,
        )

    def _best_local_replacement_attempt(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        simulator: ProductionSimulator,
        *,
        mode: str,
        current_report: ProductionReport,
        current_score: float,
    ) -> tuple[
        dict[str, Any] | None,
        list[ShiftPlan] | None,
        ProductionReport | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
        int,
    ]:
        best_attempt: dict[str, Any] | None = None
        best_candidate: list[ShiftPlan] | None = None
        best_report: ProductionReport | None = None
        positives: list[dict[str, Any]] = []
        objective_conflicts: list[dict[str, Any]] = []
        evaluated_count = 0
        max_evaluations = 180
        active_by_shift = [self._active_names(shift) for shift in shifts]
        for shift_index, shift in enumerate(shifts):
            boundary_names = self._boundary_active_names(shifts, shift_index)
            same_shift_names = active_by_shift[shift_index]
            for room_index, room in enumerate(shift.rooms):
                if room.room_type == "POWER":
                    continue
                for operator_index, old_skill in enumerate(room.operators):
                    for candidate in self._local_replacement_candidates(
                        room,
                        old_skill,
                        same_shift_names,
                        boundary_names,
                    ):
                        if evaluated_count >= max_evaluations:
                            return (
                                best_attempt,
                                best_candidate,
                                best_report,
                                positives,
                                objective_conflicts,
                                evaluated_count,
                            )
                        if candidate.skill.operator_name == old_skill.operator_name:
                            continue
                        trial_operators = list(room.operators)
                        trial_operators[operator_index] = candidate.skill
                        if len({skill.operator_name for skill in trial_operators}) != len(trial_operators):
                            continue
                        room_score_delta = (
                            self._score_assignment_room(room, trial_operators, shift.duration_hours)
                            - room.score
                        )
                        pre_tie_delta = self._local_replacement_tiebreak_delta(
                            room,
                            old_skill,
                            candidate.skill,
                            0.0,
                        )
                        if room_score_delta <= 0.001 and pre_tie_delta <= 0.001:
                            continue
                        candidate_shifts = self._replace_operator_in_shift(
                            layout,
                            shifts,
                            shift_index,
                            room_index,
                            operator_index,
                            candidate.skill,
                        )
                        if candidate_shifts is None:
                            continue
                        candidate_report = simulator.evaluate(candidate_shifts)
                        candidate_score = insertion_objective_score(mode, candidate_report)
                        score_delta = round(candidate_score - current_score, 6)
                        daily_delta = production_vector_delta(
                            current_report.dailyExpected.to_dict(),
                            candidate_report.dailyExpected.to_dict(),
                        )
                        tie_delta = self._local_replacement_tiebreak_delta(
                            room,
                            old_skill,
                            candidate.skill,
                            score_delta,
                        )
                        record = {
                            "status": (
                                "improves"
                                if score_delta > 0.001
                                else (
                                    "tie_break_improves"
                                    if tie_delta > 0.001
                                    else "evaluated_not_improving"
                                )
                            ),
                            "shift": shift.name,
                            "roomId": room.room_id,
                            "roomType": room.room_type,
                            "target": room.target,
                            "replaced": old_skill.operator_name,
                            "inserted": candidate.skill.operator_name,
                            "insertedBuffId": candidate.skill.buff_id,
                            "currentScore": round(current_score, 3),
                            "candidateScore": round(candidate_score, 3),
                            "scoreDelta": round(score_delta, 3),
                            "tieBreakDelta": round(tie_delta, 3),
                            "dailyExpectedDelta": daily_delta,
                        }
                        evaluated_count += 1
                        if score_delta > 0.001:
                            positives.append(record)
                        if (
                            float(daily_delta.get("lmdGross") or 0.0) > 0.001
                            and score_delta <= 0.001
                        ):
                            objective_conflicts.append(
                                {
                                    **record,
                                    "status": "lmd_positive_rejected_by_objective",
                                }
                            )
                        if score_delta <= 0.001 and tie_delta <= 0.001:
                            continue
                        if best_attempt is None or (
                            score_delta,
                            tie_delta,
                        ) > (
                            float(best_attempt.get("scoreDelta") or 0.0),
                            float(best_attempt.get("tieBreakDelta") or 0.0),
                        ):
                            best_attempt = record
                            best_candidate = candidate_shifts
                            best_report = candidate_report
        positives.sort(
            key=lambda item: (
                float(item.get("scoreDelta") or 0.0),
                float((item.get("dailyExpectedDelta") or {}).get("lmdGross") or 0.0),
            ),
            reverse=True,
        )
        objective_conflicts.sort(
            key=lambda item: float((item.get("dailyExpectedDelta") or {}).get("lmdGross") or 0.0),
            reverse=True,
        )
        return best_attempt, best_candidate, best_report, positives, objective_conflicts, evaluated_count

    def _best_room_replacement_attempt(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        simulator: ProductionSimulator,
        *,
        mode: str,
        current_report: ProductionReport,
        current_score: float,
    ) -> tuple[
        dict[str, Any] | None,
        list[ShiftPlan] | None,
        ProductionReport | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
        int,
    ]:
        best_attempt: dict[str, Any] | None = None
        best_candidate: list[ShiftPlan] | None = None
        best_report: ProductionReport | None = None
        positives: list[dict[str, Any]] = []
        objective_conflicts: list[dict[str, Any]] = []
        evaluated_count = 0
        max_evaluations = 120
        active_by_shift = [self._active_names(shift) for shift in shifts]
        for shift_index, shift in enumerate(shifts):
            boundary_names = self._boundary_active_names(shifts, shift_index)
            same_shift_names = active_by_shift[shift_index]
            for room_index, room in enumerate(shift.rooms):
                if room.room_type not in {"TRADING", "MANUFACTURE"}:
                    continue
                current_names = {skill.operator_name for skill in room.operators}
                excluded = boundary_names | (same_shift_names - current_names)
                spec = RoomSpec(
                    room.room_id,
                    room.room_type,
                    room.target,
                    len(room.operators),
                    room.room_level,
                    room.slots,
                )
                current_key = tuple(
                    sorted((skill.operator_name, skill.buff_id) for skill in room.operators)
                )
                for combo in self._room_combos(spec, excluded, shift.duration_hours)[:16]:
                    if evaluated_count >= max_evaluations:
                        return (
                            best_attempt,
                            best_candidate,
                            best_report,
                            positives,
                            objective_conflicts,
                            evaluated_count,
                        )
                    replacement = list(combo.operators)
                    replacement_key = tuple(
                        sorted((skill.operator_name, skill.buff_id) for skill in replacement)
                    )
                    if replacement_key == current_key:
                        continue
                    protected_combo = any(
                        should_protect_combo_seed(skill, room.room_type, room.target)
                        for skill in replacement
                    )
                    room_score_delta = (
                        self._score_assignment_room(room, replacement, shift.duration_hours)
                        - room.score
                    )
                    if room_score_delta <= 0.001 and not protected_combo:
                        continue
                    candidate_shifts = self._replace_room_in_shift(
                        layout,
                        shifts,
                        shift_index,
                        room_index,
                        replacement,
                    )
                    if candidate_shifts is None:
                        continue
                    candidate_report = simulator.evaluate(candidate_shifts)
                    candidate_score = insertion_objective_score(mode, candidate_report)
                    score_delta = round(candidate_score - current_score, 6)
                    daily_delta = production_vector_delta(
                        current_report.dailyExpected.to_dict(),
                        candidate_report.dailyExpected.to_dict(),
                    )
                    record = {
                        "neighborhood": "same_room_combo_replacement",
                        "status": (
                            "improves" if score_delta > 0.001 else "evaluated_not_improving"
                        ),
                        "shift": shift.name,
                        "roomId": room.room_id,
                        "roomType": room.room_type,
                        "target": room.target,
                        "replaced": ", ".join(skill.operator_name for skill in room.operators),
                        "inserted": ", ".join(skill.operator_name for skill in replacement),
                        "insertedBuffId": ", ".join(skill.buff_id for skill in replacement),
                        "replacedOperators": [skill.operator_name for skill in room.operators],
                        "insertedOperators": [skill.operator_name for skill in replacement],
                        "insertedBuffIds": [skill.buff_id for skill in replacement],
                        "currentScore": round(current_score, 3),
                        "candidateScore": round(candidate_score, 3),
                        "scoreDelta": round(score_delta, 3),
                        "tieBreakDelta": 0.0,
                        "roomScoreDelta": round(room_score_delta, 3),
                        "dailyExpectedDelta": daily_delta,
                    }
                    evaluated_count += 1
                    if score_delta > 0.001:
                        positives.append(record)
                    if (
                        float(daily_delta.get("lmdGross") or 0.0) > 0.001
                        and score_delta <= 0.001
                    ):
                        objective_conflicts.append(
                            {
                                **record,
                                "status": "lmd_positive_rejected_by_objective",
                            }
                        )
                    if score_delta <= 0.001:
                        continue
                    if best_attempt is None or score_delta > float(best_attempt.get("scoreDelta") or 0.0):
                        best_attempt = record
                        best_candidate = candidate_shifts
                        best_report = candidate_report
        positives.sort(
            key=lambda item: (
                float(item.get("scoreDelta") or 0.0),
                float((item.get("dailyExpectedDelta") or {}).get("lmdGross") or 0.0),
            ),
            reverse=True,
        )
        objective_conflicts.sort(
            key=lambda item: float((item.get("dailyExpectedDelta") or {}).get("lmdGross") or 0.0),
            reverse=True,
        )
        return best_attempt, best_candidate, best_report, positives, objective_conflicts, evaluated_count

    def _local_replacement_candidates(
        self,
        room: RoomAssignment,
        old_skill: BaseSkill,
        same_shift_names: set[str],
        boundary_names: set[str],
    ) -> list[Candidate]:
        excluded = (same_shift_names - {old_skill.operator_name}) | boundary_names
        if room.room_type in {"TRADING", "MANUFACTURE"}:
            pool = self._combo_candidate_pool(room.room_type, room.target, excluded)
            protected = [
                candidate
                for candidate in pool
                if should_protect_combo_seed(candidate.skill, room.room_type, room.target)
            ]
            return unique_candidate_variants([*pool[:8], *protected])[:16]
        return self._candidates(
            room.room_type,
            room.target,
            excluded,
            allow_no_skill=room.room_type == "CONTROL",
        )[:10]

    def _replace_operator_in_shift(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        shift_index: int,
        room_index: int,
        operator_index: int,
        replacement: BaseSkill,
    ) -> list[ShiftPlan] | None:
        candidate_shifts = list(shifts)
        shift = candidate_shifts[shift_index]
        rooms = list(shift.rooms)
        room = rooms[room_index]
        operators = list(room.operators)
        operators[operator_index] = replacement
        if len({skill.operator_name for skill in operators}) != len(operators):
            return None
        rooms[room_index] = replace(
            room,
            operators=operators,
            score=round(self._score_assignment_room(room, operators, shift.duration_hours), 3),
        )
        candidate_shifts[shift_index] = ShiftPlan(
            name=shift.name,
            start=shift.start,
            duration_hours=shift.duration_hours,
            rooms=rooms,
            dormitories=[],
        )
        if not self._active_boundaries_are_valid(candidate_shifts):
            return None
        return self._rebuild_dormitories(layout, candidate_shifts)

    def _replace_room_in_shift(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        shift_index: int,
        room_index: int,
        replacement: list[BaseSkill],
    ) -> list[ShiftPlan] | None:
        if len({skill.operator_name for skill in replacement}) != len(replacement):
            return None
        candidate_shifts = list(shifts)
        shift = candidate_shifts[shift_index]
        rooms = list(shift.rooms)
        room = rooms[room_index]
        rooms[room_index] = replace(
            room,
            operators=list(replacement),
            score=round(self._score_assignment_room(room, replacement, shift.duration_hours), 3),
        )
        candidate_shifts[shift_index] = ShiftPlan(
            name=shift.name,
            start=shift.start,
            duration_hours=shift.duration_hours,
            rooms=rooms,
            dormitories=[],
        )
        if not self._active_boundaries_are_valid(candidate_shifts):
            return None
        return self._rebuild_dormitories(layout, candidate_shifts)

    def _local_replacement_tiebreak_delta(
        self,
        room: RoomAssignment,
        old_skill: BaseSkill,
        new_skill: BaseSkill,
        score_delta: float,
    ) -> float:
        if abs(score_delta) > 0.001:
            return 0.0
        if room.room_type != "CONTROL":
            return 0.0
        return self._skill_tiebreak_quality(new_skill, room) - self._skill_tiebreak_quality(old_skill, room)

    def _skill_tiebreak_quality(self, skill: BaseSkill, room: RoomAssignment) -> float:
        quality = score_skill(skill, room.target, self.upgrade_cost_weight) or 0.0
        if skill.buff_id and skill.buff_id != "none":
            quality += 0.25
        if skill.complex_condition:
            quality += 0.05
        return quality

    def _candidate_pool_audit(self, shifts: list[ShiftPlan]) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None, int, int | None]] = set()
        for shift in shifts:
            for room in shift.rooms:
                if room.room_type not in {"TRADING", "MANUFACTURE"}:
                    continue
                key = (room.room_type, room.target, room.room_level, room.slots)
                if key in seen:
                    continue
                seen.add(key)
                pool = self._combo_candidate_pool(room.room_type, room.target, set())
                names = [candidate.skill.operator_name for candidate in pool]
                checks.append(
                    {
                        "roomType": room.room_type,
                        "target": room.target,
                        "roomLevel": room.room_level,
                        "slots": room.slots,
                        "candidateCount": len(pool),
                        "containsLemuen": "蕾缪安" in names,
                        "containsExusiai": "能天使" in names,
                        "protectedVariants": [
                            {
                                "operator": candidate.skill.operator_name,
                                "buffId": candidate.skill.buff_id,
                                "score": candidate.score,
                            }
                            for candidate in pool
                            if should_protect_combo_seed(candidate.skill, room.room_type, room.target)
                        ][:20],
                    }
                )
        return {"version": 1, "comboPoolChecks": checks}

    def _shift_insertion_groups(self) -> list[ShiftInsertionGroup]:
        return self._available_shift_insertion_groups(self._diagnostic_shift_insertion_groups())

    def _available_shift_insertion_groups(
        self,
        groups: list[ShiftInsertionGroup],
    ) -> list[ShiftInsertionGroup]:
        return self._all_available_shift_insertion_groups(groups)[:DIAGNOSTIC_INSERTION_GROUP_LIMIT]

    def _all_available_shift_insertion_groups(
        self,
        groups: list[ShiftInsertionGroup],
    ) -> list[ShiftInsertionGroup]:
        available = [
            group
            for group in groups
            if all(self._skill_for_insertion(spec) is not None for spec in group.specs)
        ]
        available.sort(key=self._shift_insertion_group_priority, reverse=True)
        return available

    def _unavailable_shift_insertion_records(
        self,
        groups: list[ShiftInsertionGroup],
        available: list[ShiftInsertionGroup],
    ) -> list[dict[str, Any]]:
        available_keys = {insertion_group_key(group) for group in available}
        records: list[dict[str, Any]] = []
        for group in groups:
            if insertion_group_key(group) in available_keys:
                continue
            missing = [
                spec
                for spec in group.specs
                if self._skill_for_insertion(spec) is None
            ]
            if not missing:
                continue
            records.append(
                {
                    **insertion_group_to_dict(group),
                    "status": "unavailable_required_skill",
                    "missingSpecs": [
                        {
                            "roomType": spec.room_type,
                            "target": spec.target,
                            "operator": spec.operator_name,
                        }
                        for spec in missing
                    ],
                    "reason": (
                        "At least one required operator skill is unavailable for this profile, "
                        "often because cost-adjusted upgrade scoring filtered it out."
                    ),
                }
            )
        return records

    def _group_limit_insertion_records(
        self,
        groups: list[ShiftInsertionGroup],
        available: list[ShiftInsertionGroup],
    ) -> list[dict[str, Any]]:
        available_keys = {insertion_group_key(group) for group in available}
        records: list[dict[str, Any]] = []
        for group in self._all_available_shift_insertion_groups(groups):
            if insertion_group_key(group) in available_keys:
                continue
            records.append(
                {
                    **insertion_group_to_dict(group),
                    "status": "not_searched_group_limit",
                    "reason": "The diagnostic insertion group was available but not searched because of the per-run group limit.",
                }
            )
        return records

    def _diagnostic_shift_insertion_groups(self) -> list[ShiftInsertionGroup]:
        roster_names = set(self.roster_by_name)
        groups: list[ShiftInsertionGroup] = list(getattr(self, "forced_insertion_groups", ()))
        for operator_name, skills in self.skills_by_operator.items():
            for skill in skills:
                if skill.room_type not in {"TRADING", "MANUFACTURE", "CONTROL", "HIRE"}:
                    continue
                text = f"{skill.buff_name} {skill.description}"
                for partner in mentioned_operator_names(text, roster_names):
                    if partner == operator_name:
                        continue
                    specs = self._named_dependency_insertion_specs(skill, partner)
                    if not specs:
                        continue
                    groups.append(
                        ShiftInsertionGroup(
                            f"diagnostic_named_dependency:{operator_name}:{partner}",
                            specs,
                        )
                    )
                groups.extend(self._faction_dependency_insertion_groups(skill))
        groups.extend(self._guide_special_trade_anchor_insertion_groups())
        base_groups = dedupe_shift_insertion_groups(groups)
        return dedupe_shift_insertion_groups(
            [*base_groups, *compound_shift_insertion_groups(base_groups)]
        )

    def _guide_special_trade_anchor_insertion_groups(self) -> list[ShiftInsertionGroup]:
        groups: list[ShiftInsertionGroup] = []
        for operator_name in sorted(GUIDE_SPECIAL_TRADE_ANCHORS):
            if operator_name not in self.roster_by_name:
                continue
            groups.append(
                ShiftInsertionGroup(
                    f"diagnostic_guide_trade_anchor:{operator_name}",
                    (ShiftInsertionSpec("TRADING", "O_GOLD", operator_name),),
                )
            )
        return groups

    def _named_dependency_insertion_specs(
        self,
        skill: BaseSkill,
        partner: str,
    ) -> tuple[ShiftInsertionSpec, ...]:
        explanation = dependency_parser.explain_named_dependency(skill, partner)
        specs = dependency_parser.force_specs_from_explanation(
            explanation,
            operator=skill.operator_name,
            partner=partner,
        )
        return tuple(
            ShiftInsertionSpec(spec.room_type, spec.target, operator)
            for spec in specs
            for operator in spec.operators
        )

    def _faction_dependency_insertion_groups(
        self,
        skill: BaseSkill,
    ) -> list[ShiftInsertionGroup]:
        dependency = dependency_parser.explain_same_room_faction_dependency(skill)
        if dependency is None:
            return []
        return [
            ShiftInsertionGroup(
                f"diagnostic_faction_dependency:{skill.operator_name}:{dependency.faction_tag}",
                (
                    ShiftInsertionSpec(dependency.room_type, dependency.target, skill.operator_name),
                    ShiftInsertionSpec(
                        dependency.room_type,
                        dependency.target,
                        partner,
                        allow_no_skill=True,
                    ),
                ),
            )
            for partner in self._faction_partners(
                dependency.faction_tag,
                exclude=skill.operator_name,
                room_type=dependency.room_type,
                target=dependency.target,
            )
        ]

    def _unsearched_faction_dependency_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for skills in self.skills_by_operator.values():
            for skill in skills:
                dependency = dependency_parser.explain_same_room_faction_dependency(skill)
                if dependency is None:
                    continue
                partners = self._faction_partners(
                    dependency.faction_tag,
                    exclude=skill.operator_name,
                    room_type=dependency.room_type,
                    target=dependency.target,
                )
                if partners:
                    continue
                records.append(
                    {
                        "groupId": f"diagnostic_faction_dependency:{skill.operator_name}:{dependency.faction_tag}",
                        "status": "not_searched_no_faction_partner",
                        "operators": [skill.operator_name],
                        "expectedFaction": dependency.faction_tag,
                        "currentScore": None,
                        "reason": "No available same-faction partner with a compatible room skill.",
                    }
                )
        return records

    def _faction_partners(
        self,
        faction_tag: str,
        *,
        exclude: str,
        room_type: str,
        target: str | None,
    ) -> list[str]:
        candidates = self._faction_partner_candidates(
            faction_tag,
            exclude=exclude,
            room_type=room_type,
            target=target,
        )
        return [candidate.skill.operator_name for candidate in candidates]

    def _faction_partner_candidates(
        self,
        faction_tag: str,
        *,
        exclude: str,
        room_type: str,
        target: str | None,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for operator in self.roster:
            if operator.name == exclude:
                continue
            if not any(
                faction_tag in skill.faction_tags
                for skill in self.skills_by_operator.get(operator.name, [])
            ):
                continue
            candidate = self._skill_for_insertion(
                ShiftInsertionSpec(room_type, target, operator.name, allow_no_skill=True)
            )
            if candidate is not None:
                candidates.append(candidate)
        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)

    def _shift_insertion_group_priority(self, group: ShiftInsertionGroup) -> tuple[float, int]:
        score = 0.0
        room_types = {spec.room_type for spec in group.specs}
        if room_types & {"CONTROL", "MEETING", "HIRE"}:
            score += 120.0
        if len(room_types) > 1:
            score += 30.0
        if group.id.startswith("compound_dependency:"):
            score += 24.0
        if group.id.startswith("diagnostic_guide_trade_anchor:"):
            score += 45.0
        for spec in group.specs:
            candidate = self._skill_for_insertion(spec)
            if candidate is not None:
                score += candidate.score
        return (score, len(group.specs))

    def _insert_group_into_shift(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
        shift_index: int,
        group: ShiftInsertionGroup,
    ) -> list[ShiftPlan] | None:
        shift = shifts[shift_index]
        if self._insertion_group_satisfied(shift, group):
            return None

        boundary_names = self._boundary_active_names(shifts, shift_index)
        forced_names = {spec.operator_name for spec in group.specs}
        if forced_names & boundary_names:
            return None
        shift = self._shift_without_forced_names(shift, forced_names)

        target_specs_by_room: dict[int, list[ShiftInsertionSpec]] = {}
        used_room_indexes: set[int] = set()
        for room_specs in group_specs_by_room_requirement(group.specs):
            room_index = self._find_insertion_room(shift, room_specs, used_room_indexes)
            if room_index is None:
                return None
            target_specs_by_room.setdefault(room_index, []).extend(room_specs)
            used_room_indexes.add(room_index)

        selected_in_shift: set[str] = set()
        rooms: list[RoomAssignment] = []
        for room_index, room in enumerate(shift.rooms):
            room_specs = target_specs_by_room.get(room_index, [])
            replacement = self._room_with_insertions(
                room,
                room_specs,
                selected_in_shift,
                forced_names,
                boundary_names,
                shift.duration_hours,
            )
            if replacement is None:
                return None
            rooms.append(replacement)
            selected_in_shift.update(skill.operator_name for skill in replacement.operators)

        candidate_shifts = list(shifts)
        candidate_shifts[shift_index] = ShiftPlan(
            name=shift.name,
            start=shift.start,
            duration_hours=shift.duration_hours,
            rooms=rooms,
            dormitories=[],
        )
        if not self._active_boundaries_are_valid(candidate_shifts):
            return None
        return self._rebuild_dormitories(layout, candidate_shifts)

    def _room_with_insertions(
        self,
        room: RoomAssignment,
        specs: list[ShiftInsertionSpec],
        selected_in_shift: set[str],
        forced_names: set[str],
        boundary_names: set[str],
        duration_hours: float,
    ) -> RoomAssignment | None:
        capacity = self._assignment_capacity(room)
        if not specs and (
            room.product_capacity is None
            or len(room.operators) >= capacity
            or room.room_type == "CONTROL"
        ):
            names = {skill.operator_name for skill in room.operators}
            if (
                len(names) == len(room.operators)
                and not (names & selected_in_shift)
                and not (names & boundary_names)
                and not (names & forced_names)
            ):
                return room
        selected: list[BaseSkill] = []
        selected_names: set[str] = set()
        for spec in specs:
            candidate = self._skill_for_insertion(spec)
            if candidate is None:
                return None
            name = candidate.skill.operator_name
            if name in selected_names or name in selected_in_shift or name in boundary_names:
                return None
            selected.append(candidate.skill)
            selected_names.add(name)

        if len(selected) > capacity:
            return None

        selected = self._complete_room_after_insertions(
            room,
            selected,
            selected_in_shift,
            boundary_names,
            forced_names,
            duration_hours,
        )
        if selected is None:
            return None

        return RoomAssignment(
            room_id=room.room_id,
            room_type=room.room_type,
            room_name=room.room_name,
            target=room.target,
            operators=selected,
            score=round(self._score_assignment_room(room, selected, duration_hours), 3),
            room_level=room.room_level,
            slots=room.slots,
            product_capacity=room.product_capacity,
            order_limit=room.order_limit,
        )

    def _complete_room_after_insertions(
        self,
        room: RoomAssignment,
        forced: list[BaseSkill],
        selected_in_shift: set[str],
        boundary_names: set[str],
        globally_excluded_names: set[str],
        duration_hours: float,
    ) -> list[BaseSkill] | None:
        cache_key = (
            *self._cache_context_key(),
            "complete_room_after_insertions",
            room.room_id,
            room.room_type,
            room.target,
            room.room_level,
            room.slots,
            room.product_capacity,
            room.order_limit,
            round(duration_hours, 6),
            tuple(self._skill_cache_key(skill) for skill in room.operators),
            tuple(self._skill_cache_key(skill) for skill in forced),
            tuple(sorted(selected_in_shift)),
            tuple(sorted(boundary_names)),
            tuple(sorted(globally_excluded_names)),
        )
        cached = self._complete_room_cache.get(cache_key)
        if cache_key in self._complete_room_cache:
            self._record_runtime_count("completeRoomAfterInsertions.cacheHit")
            return None if cached is None else list(cached)
        self._record_runtime_count("completeRoomAfterInsertions.cacheMiss")
        capacity = self._assignment_capacity(room)
        if room.room_type == "CONTROL":
            desired_count = capacity
        else:
            desired_count = min(
                capacity,
                max(len(room.operators), len(forced), int(room.product_capacity or 0)),
            )
        if len(forced) >= desired_count:
            self._complete_room_cache[cache_key] = tuple(forced)
            return forced
        forced_names = {skill.operator_name for skill in forced}
        excluded = selected_in_shift | forced_names | boundary_names | globally_excluded_names
        allow_no_skill = room.room_type == "CONTROL"
        fill_candidates: list[Candidate] = []
        deferred_control_fillers: list[Candidate] = []
        if room.room_type in {"TRADING", "MANUFACTURE", "POWER"}:
            production_excluded = selected_in_shift | boundary_names | globally_excluded_names
            candidate_source = self._combo_candidate_pool(
                room.room_type,
                room.target,
                production_excluded,
            )
            candidate_source = [
                candidate
                for candidate in candidate_source
                if candidate.skill.operator_name not in forced_names
            ]
        else:
            candidate_source = self._candidates(
                room.room_type,
                room.target,
                excluded,
                allow_no_skill=allow_no_skill,
            )
        for candidate in candidate_source:
            name = candidate.skill.operator_name
            if (
                room.room_type == "CONTROL"
                and candidate.score <= 0
                and self._has_productive_skill_potential(name)
            ):
                deferred_control_fillers.append(candidate)
                continue
            fill_candidates.append(candidate)

        fill_count = desired_count - len(forced)
        if room.room_type == "CONTROL" and len(fill_candidates) < fill_count:
            fill_candidates.extend(deferred_control_fillers)
        if len(fill_candidates) < fill_count:
            fill_count = len(fill_candidates)

        candidate_limit = (
            JOINT_PRODUCTION_CANDIDATE_LIMIT
            if room.room_type in {"TRADING", "MANUFACTURE", "POWER"}
            else 12
        )
        best_operators = list(forced)
        best_score = self._score_assignment_room(room, best_operators, duration_hours)
        for group in combinations(fill_candidates[:candidate_limit], fill_count):
            operators = [*forced, *(candidate.skill for candidate in group)]
            if self._would_overflow(self._spec_from_assignment(room), operators, duration_hours):
                continue
            score = self._score_assignment_room(room, operators, duration_hours)
            if (
                room.room_type == "CONTROL"
                and len(best_operators) < desired_count
                and len(operators) > len(best_operators)
            ) or score > best_score + 0.0001:
                best_score = score
                best_operators = operators

        if room.room_type == "CONTROL" and len(best_operators) < desired_count:
            self._complete_room_cache[cache_key] = None
            return None
        if (
            room.room_type in {"TRADING", "MANUFACTURE", "POWER"}
            and not forced
            and len(best_operators) < desired_count
        ):
            self._complete_room_cache[cache_key] = None
            return None
        self._complete_room_cache[cache_key] = tuple(best_operators)
        return best_operators

    def _skill_for_insertion(self, spec: ShiftInsertionSpec) -> Candidate | None:
        operator = self.roster_by_name.get(spec.operator_name)
        if operator is None:
            return None
        candidate = self._best_contextual_skill_for_insertion(operator, spec.room_type, spec.target)
        if candidate is not None:
            return candidate
        if is_guide_special_trade_anchor(operator.name, spec.room_type, spec.target):
            synthetic = synthetic_guide_special_trade_skill(operator)
            return Candidate(synthetic, guide_special_trade_score(operator.name))
        if spec.allow_no_skill or spec.room_type == "CONTROL":
            return Candidate(synthetic_skill(operator, spec.room_type), 0.0)
        return None

    def _best_contextual_skill_for_insertion(
        self, operator: RosterOperator, room_type: str, target: str | None
    ) -> Candidate | None:
        candidates = self._skill_candidates_for(operator, room_type, target)
        if not candidates:
            return None
        contextual = [
            candidate
            for candidate in candidates
            if should_protect_combo_seed(candidate.skill, room_type, target)
        ]
        if contextual:
            return max(
                contextual,
                key=lambda candidate: (
                    combo_protection_priority(candidate.skill, room_type, target),
                    candidate.score,
                ),
            )
        return max(candidates, key=lambda candidate: candidate.score)

    def _insertion_group_satisfied(
        self, shift: ShiftPlan, group: ShiftInsertionGroup
    ) -> bool:
        used_room_indexes: set[int] = set()
        for room_specs in group_specs_by_room_requirement(group.specs):
            matched_index = None
            for room_index, room in enumerate(shift.rooms):
                if room_index in used_room_indexes:
                    continue
                if room.room_type != room_specs[0].room_type:
                    continue
                if room_specs[0].target is not None and room.target != room_specs[0].target:
                    continue
                if all(
                    any(skill.operator_name == spec.operator_name for skill in room.operators)
                    for spec in room_specs
                ):
                    matched_index = room_index
                    break
            if matched_index is None:
                return False
            used_room_indexes.add(matched_index)
        return True

    def _find_insertion_room(
        self,
        shift: ShiftPlan,
        specs: list[ShiftInsertionSpec],
        used_room_indexes: set[int],
    ) -> int | None:
        spec = specs[0]
        matches: list[tuple[int, RoomAssignment]] = []
        for index, room in enumerate(shift.rooms):
            if index in used_room_indexes:
                continue
            if room.room_type != spec.room_type:
                continue
            if spec.target is not None and room.target != spec.target:
                continue
            if len(specs) > self._assignment_capacity(room):
                continue
            matches.append((index, room))
        if not matches:
            return None
        if (
            len(specs) == 1
            and is_guide_special_trade_anchor(spec.operator_name, spec.room_type, spec.target)
        ):
            matches.sort(key=lambda item: (item[1].room_level, self._assignment_capacity(item[1])))
        return matches[0][0]

    def _boundary_active_names(self, shifts: list[ShiftPlan], shift_index: int) -> set[str]:
        if len(shifts) <= 1:
            return set()
        names: set[str] = set()
        for neighbor in {(shift_index - 1) % len(shifts), (shift_index + 1) % len(shifts)}:
            if neighbor == shift_index:
                continue
            names.update(self._active_names(shifts[neighbor]))
        return names

    def _active_boundaries_are_valid(self, shifts: list[ShiftPlan]) -> bool:
        active_by_shift = [self._active_names(shift) for shift in shifts]
        for shift, active in zip(shifts, active_by_shift):
            operator_count = sum(len(room.operators) for room in shift.rooms)
            if len(active) != operator_count:
                return False
        if len(active_by_shift) <= 1:
            return True
        for index, active in enumerate(active_by_shift):
            previous = active_by_shift[index - 1]
            if active & previous:
                return False
        return True

    def _rebuild_dormitories(self, layout: Layout, shifts: list[ShiftPlan]) -> list[ShiftPlan]:
        dorm_specs = self._dorm_specs(layout)
        active_by_shift = [self._active_names(shift) for shift in shifts]
        rebuilt: list[ShiftPlan] = []
        for shift_index, shift in enumerate(shifts):
            active_names = active_by_shift[shift_index]
            rest_priority = self._rest_priority(active_by_shift, shift_index)
            rebuilt.append(
                ShiftPlan(
                    name=shift.name,
                    start=shift.start,
                    duration_hours=shift.duration_hours,
                    rooms=list(shift.rooms),
                    dormitories=self._assign_dormitories(dorm_specs, active_names, rest_priority),
                )
            )
        return self._improve_dormitory_anchor_coverage(rebuilt)

    def _ensure_sustainable_morale_cycle(
        self,
        layout: Layout,
        shifts: list[ShiftPlan],
    ) -> tuple[list[ShiftPlan], dict[str, Any]]:
        audit = audit_morale_cycle(layout, shifts)
        if audit["hardGatePassed"]:
            return shifts, audit
        if len(shifts) < 2:
            raise ValueError(
                "No sustainable morale cycle within seven days: a one-change-per-day "
                "schedule cannot rotate a fully staffed base through the available dormitories."
            )

        dorm_specs = self._dorm_specs(layout)
        dorm_capacity = sum(spec[3] for spec in dorm_specs)
        room_order = [room.room_id for room in shifts[0].rooms]
        room_weights = {
            room_id: max(
                len(next(room for room in shift.rooms if room.room_id == room_id).operators)
                for shift in shifts
            )
            for room_id in room_order
        }
        total_active = sum(room_weights.values())
        fast_rotation_rooms = {
            room_id
            for room_id in room_order
            if any(
                max(0.0, fatigue_delta_from_text(skill.description)) > 0.0001
                for shift in shifts
                for room in shift.rooms
                if room.room_id == room_id
                for skill in room.operators
            )
        }
        fast_rotation_weight = sum(room_weights[room_id] for room_id in fast_rotation_rooms)
        stagger_capacity = dorm_capacity - fast_rotation_weight
        stagger_weight = total_active - fast_rotation_weight
        if stagger_capacity <= 0 and stagger_weight > 0:
            raise ValueError(
                "No sustainable morale cycle: high-consumption operators exhaust dormitory capacity."
            )
        group_count = max(
            1,
            (stagger_weight + stagger_capacity - 1) // stagger_capacity
            if stagger_weight
            else 1,
        )
        cycle_hours = sum(shift.duration_hours for shift in shifts) * group_count
        if cycle_hours > MAX_CYCLE_HOURS + 0.0001:
            raise ValueError(
                "No sustainable morale cycle within seven days: required staggered cycle "
                f"is {cycle_hours / 24.0:.2f} days."
            )

        groups: list[list[str]] = [[] for _ in range(group_count)]
        group_weights = [0] * group_count
        for room_id in sorted(
            (item for item in room_order if item not in fast_rotation_rooms),
            key=lambda item: room_weights[item],
            reverse=True,
        ):
            group_index = min(range(group_count), key=lambda index: group_weights[index])
            groups[group_index].append(room_id)
            group_weights[group_index] += room_weights[room_id]
        if max(group_weights, default=0) + fast_rotation_weight > dorm_capacity:
            raise ValueError(
                "No sustainable morale cycle: a staggered room group exceeds dormitory capacity."
            )

        source_rooms = [
            {room.room_id: room for room in shift.rooms}
            for shift in shifts
        ]
        room_group = {
            room_id: group_index
            for group_index, group in enumerate(groups)
            for room_id in group
        }
        expanded_rooms: list[list[RoomAssignment]] = []
        plan_count = len(shifts) * group_count
        for plan_index in range(plan_count):
            rooms: list[RoomAssignment] = []
            for room_id in room_order:
                if room_id in fast_rotation_rooms:
                    rooms.append(source_rooms[plan_index % len(shifts)][room_id])
                    continue
                group_index = room_group[room_id]
                source_index = (
                    (plan_index + group_count - 1 - group_index) // group_count
                ) % len(shifts)
                rooms.append(source_rooms[source_index][room_id])
            names = [skill.operator_name for room in rooms for skill in room.operators]
            if len(names) != len(set(names)):
                raise ValueError(
                    "No sustainable morale cycle: staggered room rotations create a same-shift operator conflict."
                )
            expanded_rooms.append(rooms)

        expanded: list[ShiftPlan] = []
        active_by_plan = [
            {skill.operator_name for room in rooms for skill in room.operators}
            for rooms in expanded_rooms
        ]
        for plan_index, rooms in enumerate(expanded_rooms):
            template = shifts[plan_index % len(shifts)]
            active_names = active_by_plan[plan_index]
            outgoing = active_by_plan[plan_index - 1] - active_names
            dormitories = self._assign_required_rest_dormitories(
                dorm_specs,
                active_names,
                outgoing,
            )
            resting = {
                skill.operator_name
                for room in dormitories
                for skill in room.operators
            }
            if not outgoing <= resting:
                raise ValueError(
                    "No sustainable morale cycle: required outgoing operators exceed dormitory capacity."
                )
            expanded.append(
                ShiftPlan(
                    name=shift_label(plan_index),
                    start=template.start,
                    duration_hours=template.duration_hours,
                    rooms=rooms,
                    dormitories=dormitories,
                )
            )

        audit = audit_morale_cycle(layout, expanded)
        audit["rotationGroups"] = [list(group) for group in groups]
        audit["fastRotationRooms"] = sorted(fast_rotation_rooms)
        audit["basePlanCount"] = len(shifts)
        audit["expandedPlanCount"] = len(expanded)
        if not audit["hardGatePassed"]:
            raise ValueError(
                "No sustainable morale cycle within seven days: "
                + ", ".join(audit["failureReasons"])
                + "; operators="
                + ", ".join(audit["unrestedOperators"][:12])
                + "; minimum="
                + ", ".join(
                    f"{name}:{audit['minimumMoraleByOperator'].get(name)}"
                    for name in audit["unrestedOperators"][:12]
                )
            )
        return expanded, audit

    def _assign_required_rest_dormitories(
        self,
        dorm_specs: list[tuple[str, str, None, int]],
        active_names: set[str],
        required_rest: set[str],
    ) -> list[RoomAssignment]:
        total_capacity = sum(spec[3] for spec in dorm_specs)
        if len(required_rest) > total_capacity:
            return []
        helper_limit = min(len(dorm_specs), total_capacity - len(required_rest))
        helper_candidates = [
            candidate
            for candidate in self._candidates(
                "DORMITORY",
                None,
                active_names | required_rest,
                allow_no_skill=False,
            )
            if "所有干员" in candidate.skill.description
            or "某一名" in candidate.skill.description
        ][:helper_limit]

        selected_by_room: list[list[Candidate]] = [[] for _ in dorm_specs]
        for room_index, helper in enumerate(helper_candidates):
            selected_by_room[room_index].append(helper)
        for name in sorted(required_rest):
            candidate = self._dorm_candidate(name)
            if candidate is None:
                continue
            available_rooms = [
                index
                for index, spec in enumerate(dorm_specs)
                if len(selected_by_room[index]) < spec[3]
            ]
            if not available_rooms:
                break
            room_index = min(available_rooms, key=lambda index: len(selected_by_room[index]))
            selected_by_room[room_index].append(candidate)

        selected_names = {
            candidate.skill.operator_name
            for room_candidates in selected_by_room
            for candidate in room_candidates
        }
        fillers = self._candidates(
            "DORMITORY",
            None,
            active_names | selected_names,
            allow_no_skill=True,
        )
        filler_index = 0
        for room_index, spec in enumerate(dorm_specs):
            while len(selected_by_room[room_index]) < spec[3] and filler_index < len(fillers):
                candidate = fillers[filler_index]
                filler_index += 1
                if candidate.skill.operator_name in selected_names:
                    continue
                selected_by_room[room_index].append(candidate)
                selected_names.add(candidate.skill.operator_name)

        return [
            RoomAssignment(
                room_id=room_id,
                room_type=room_type,
                room_name=ROOM_NAMES.get(room_type, room_type),
                target=target,
                operators=[candidate.skill for candidate in selected_by_room[index]],
                score=round(sum(candidate.score for candidate in selected_by_room[index]), 3),
            )
            for index, (room_id, room_type, target, _) in enumerate(dorm_specs)
        ]

    def _improve_dormitory_anchor_coverage(self, shifts: list[ShiftPlan]) -> list[ShiftPlan]:
        if not self.operator_anchor_preference:
            return shifts
        improved = list(shifts)
        for missing_name in sorted(
            self.operator_anchor_preference,
            key=lambda name: self.operator_anchor_rank.get(name, 1_000_000),
        ):
            if missing_name in self._visible_operator_names(improved):
                continue
            candidate = self._dorm_candidate(missing_name)
            if candidate is None:
                continue
            replacement = self._replace_dormitory_operator_for_anchor(improved, candidate)
            if replacement is not None:
                improved = replacement
        return improved

    def _visible_operator_names(self, shifts: list[ShiftPlan]) -> set[str]:
        return {
            skill.operator_name
            for shift in shifts
            for room in [*shift.rooms, *shift.dormitories]
            for skill in room.operators
        }

    def _replace_dormitory_operator_for_anchor(
        self,
        shifts: list[ShiftPlan],
        candidate: Candidate,
    ) -> list[ShiftPlan] | None:
        active_by_shift = [self._active_names(shift) for shift in shifts]
        anchor_counts: dict[str, int] = {}
        for shift in shifts:
            for room in [*shift.rooms, *shift.dormitories]:
                for skill in room.operators:
                    if skill.operator_name in self.operator_anchor_preference:
                        anchor_counts[skill.operator_name] = (
                            anchor_counts.get(skill.operator_name, 0) + 1
                        )

        for prefer_duplicate_anchor in (False, True):
            for shift_index, shift in enumerate(shifts):
                if candidate.skill.operator_name in self._active_names(shift):
                    continue
                required_rest_names = set(self._rest_priority(active_by_shift, shift_index))
                selected_names = {
                    skill.operator_name
                    for dormitory in shift.dormitories
                    for skill in dormitory.operators
                }
                if candidate.skill.operator_name in selected_names:
                    continue
                for dormitory_index, dormitory in enumerate(shift.dormitories):
                    for operator_index, existing in enumerate(dormitory.operators):
                        if existing.operator_name in required_rest_names:
                            continue
                        existing_is_anchor = (
                            existing.operator_name in self.operator_anchor_preference
                        )
                        replaceable = (
                            not existing_is_anchor
                            if not prefer_duplicate_anchor
                            else anchor_counts.get(existing.operator_name, 0) > 1
                        )
                        if not replaceable:
                            continue
                        new_operators = list(dormitory.operators)
                        new_operators[operator_index] = candidate.skill
                        new_dormitories = list(shift.dormitories)
                        new_dormitories[dormitory_index] = replace(
                            dormitory,
                            operators=new_operators,
                            score=round(
                                self._score_assignment_room(
                                    dormitory,
                                    new_operators,
                                    shift.duration_hours,
                                ),
                                3,
                            ),
                        )
                        new_shifts = list(shifts)
                        new_shifts[shift_index] = replace(
                            shift,
                            dormitories=new_dormitories,
                        )
                        return new_shifts
        return None

    def _active_names(self, shift: ShiftPlan) -> set[str]:
        return {
            skill.operator_name
            for room in shift.rooms
            for skill in room.operators
        }

    def _assignment_capacity(self, room: RoomAssignment) -> int:
        if room.slots is not None:
            return clamp_station_count(room.room_type, room.slots)
        return self._room_capacity(room.room_type, room.room_level)

    def _spec_from_assignment(self, room: RoomAssignment) -> RoomSpec:
        return RoomSpec(
            room.room_id,
            room.room_type,
            room.target,
            self._assignment_capacity(room),
            room.room_level,
            room.slots,
        )

    def _score_assignment_room(
        self,
        room: RoomAssignment,
        operators: list[BaseSkill],
        duration_hours: float,
    ) -> float:
        if room.room_type in {"TRADING", "MANUFACTURE", "POWER"}:
            return self._score_room_combo(self._spec_from_assignment(room), operators, duration_hours)
        score = 0.0
        for skill in operators:
            score += score_skill(skill, room.target, self.upgrade_cost_weight) or 0.0
        return score

    def _rest_priority(self, active_by_shift: list[set[str]], shift_index: int) -> list[str]:
        priority: list[str] = []
        current_active = active_by_shift[shift_index]
        for offset in range(1, len(active_by_shift)):
            source = active_by_shift[(shift_index - offset) % len(active_by_shift)]
            for name in sorted(source):
                if name in current_active or name in priority:
                    continue
                priority.append(name)
        return priority

    def _assign_dormitories(
        self,
        dorm_specs: list[tuple[str, str, None, int]],
        active_names: set[str],
        rest_priority: list[str],
        visible_anchor_names: set[str] | None = None,
    ) -> list[RoomAssignment]:
        visible_anchor_names = visible_anchor_names or set()
        dormitories: list[RoomAssignment] = []
        selected_names: set[str] = set()
        for room_id, room_type, target, capacity in dorm_specs:
            rest_priority_names: set[str] = set()
            room_candidates = [
                candidate
                for candidate in (
                    self._dorm_candidate(name)
                    for name in rest_priority
                    if name not in active_names and name not in selected_names
                )
                if candidate is not None
            ]
            rest_priority_names.update(candidate.skill.operator_name for candidate in room_candidates)
            if self.operator_anchor_preference:
                queued_names = set(rest_priority_names)
                for name in self.operator_anchor_preference:
                    if name in active_names or name in selected_names or name in queued_names:
                        continue
                    candidate = self._dorm_candidate(name)
                    if candidate is None:
                        continue
                    room_candidates.append(candidate)
                    queued_names.add(name)
            room_candidates.sort(
                key=lambda candidate: self._dorm_anchor_sort_key(
                    candidate,
                    rest_priority_names,
                    visible_anchor_names,
                ),
                reverse=True,
            )

            operators: list[BaseSkill] = []
            score = 0.0
            for candidate in room_candidates:
                operators.append(candidate.skill)
                selected_names.add(candidate.skill.operator_name)
                score += candidate.score
                if len(operators) >= capacity:
                    break

            if len(operators) < capacity:
                excluded = active_names | selected_names
                fill_candidates = self._candidates(room_type, target, excluded, allow_no_skill=True)
                fill_candidates.sort(
                    key=lambda candidate: self._dorm_anchor_sort_key(
                        candidate,
                        visible_anchor_names=visible_anchor_names,
                    ),
                    reverse=True,
                )
                for candidate in fill_candidates:
                    operators.append(candidate.skill)
                    selected_names.add(candidate.skill.operator_name)
                    score += candidate.score
                    if len(operators) >= capacity:
                        break

            dormitories.append(
                RoomAssignment(
                    room_id=room_id,
                    room_type=room_type,
                    room_name=ROOM_NAMES.get(room_type, room_type),
                    target=target,
                    operators=operators,
                    score=round(score, 3),
                )
            )
        return dormitories

    def _dorm_candidate(self, name: str) -> Candidate | None:
        operator = self.roster_by_name.get(name)
        if not operator:
            return None
        return self._best_skill_for(operator, "DORMITORY", None) or Candidate(
            synthetic_skill(operator, "DORMITORY"),
            0.0,
        )

    def _dorm_anchor_sort_key(
        self,
        candidate: Candidate,
        rest_priority_names: set[str] | None = None,
        visible_anchor_names: set[str] | None = None,
    ) -> tuple[int, int, int, int, float, bool, float]:
        rest_priority_names = rest_priority_names or set()
        visible_anchor_names = visible_anchor_names or set()
        is_anchor = candidate.skill.operator_name in self.operator_anchor_preference
        return (
            int(candidate.skill.operator_name in rest_priority_names),
            int(is_anchor),
            int(is_anchor and candidate.skill.operator_name not in visible_anchor_names),
            -self.operator_anchor_rank.get(candidate.skill.operator_name, 1_000_000),
            candidate.score,
            candidate.skill.unlocked,
            -float(candidate.skill.upgrade.cost_score if candidate.skill.upgrade else 0.0),
        )

    def _room_specs(
        self, layout: Layout, mode: str
    ) -> list[RoomSpec]:
        return self._room_specs_for_targets(
            layout,
            trading_targets(layout, mode),
            manufacture_targets(layout, mode),
        )

    def _room_specs_for_targets(
        self, layout: Layout, trading: list[str], manufacture: list[str]
    ) -> list[RoomSpec]:
        specs: list[RoomSpec] = [
            RoomSpec(
                "control_1",
                "CONTROL",
                None,
                self._room_capacity("CONTROL", layout.control_level),
                layout.control_level,
                self._room_capacity("CONTROL", layout.control_level),
            ),
        ]
        for index, target in enumerate(trading, start=1):
            level = layout.trading_levels[index - 1] if index <= len(layout.trading_levels) else 3
            slots = self._room_capacity("TRADING", level)
            specs.append(RoomSpec(f"trading_{index}", "TRADING", target, slots, level, slots))
        for index, target in enumerate(manufacture, start=1):
            level = (
                layout.manufacture_levels[index - 1]
                if index <= len(layout.manufacture_levels)
                else 3
            )
            slots = (
                layout.manufacture_slots[index - 1]
                if index <= len(layout.manufacture_slots)
                else self._room_capacity("MANUFACTURE", level)
            )
            specs.append(RoomSpec(f"manufacture_{index}", "MANUFACTURE", target, slots, level, slots))
        for index in range(1, layout.power + 1):
            specs.append(RoomSpec(f"power_{index}", "POWER", None, self._room_capacity("POWER")))
        specs.append(RoomSpec("meeting_1", "MEETING", None, self._room_capacity("MEETING")))
        specs.append(RoomSpec("hire_1", "HIRE", None, self._room_capacity("HIRE")))
        return specs

    def _dorm_specs(self, layout: Layout) -> list[tuple[str, str, None, int]]:
        capacity = self._room_capacity("DORMITORY")
        return [
            (f"dormitory_{index}", "DORMITORY", None, capacity)
            for index in range(1, layout.dormitory + 1)
        ]

    def _room_capacity(self, room_type: str, room_level: int = 3) -> int:
        room = self.game_data.building.get("rooms", {}).get(room_type, {})
        phases = room.get("phases") or []
        if not phases:
            defaults = {
                "CONTROL": 5,
                "TRADING": 3,
                "MANUFACTURE": 3,
                "POWER": 1,
                "DORMITORY": 5,
                "MEETING": 2,
                "HIRE": 1,
            }
            return clamp_station_count(room_type, defaults.get(room_type, 1))
        index = max(0, min(len(phases) - 1, room_level - 1))
        return clamp_station_count(room_type, int(phases[index].get("maxStationedNum") or 1))

    def _assign_room(
        self,
        *,
        spec: RoomSpec,
        excluded: set[str],
        duration_hours: float = 12.0,
        allow_no_skill: bool = False,
    ) -> RoomAssignment:
        room_id = spec.room_id
        room_type = spec.room_type
        target = spec.target
        capacity = spec.capacity
        if room_type in {"TRADING", "MANUFACTURE", "POWER"}:
            combo = self._best_room_combo(spec, excluded, duration_hours)
            if combo is not None:
                return RoomAssignment(
                    room_id=room_id,
                    room_type=room_type,
                    room_name=ROOM_NAMES.get(room_type, room_type),
                    target=target,
                    operators=list(combo.operators),
                    score=round(combo.score, 3),
                    room_level=spec.room_level,
                    slots=spec.slots,
                )

        candidates = self._candidates(room_type, target, excluded, allow_no_skill)
        selected: list[BaseSkill] = []
        selected_names: set[str] = set()
        score = 0.0
        for candidate in candidates:
            if candidate.skill.operator_name in selected_names:
                continue
            if self._would_overflow(spec, selected + [candidate.skill], duration_hours):
                continue
            selected.append(candidate.skill)
            selected_names.add(candidate.skill.operator_name)
            score += candidate.score
            if len(selected) >= capacity:
                break
        if room_type == "CONTROL" and len(selected) < capacity:
            fill_excluded = excluded | selected_names
            deferred_fillers: list[Candidate] = []
            for candidate in self._candidates(room_type, target, fill_excluded, allow_no_skill=True):
                if candidate.skill.operator_name in selected_names:
                    continue
                if candidate.score <= 0 and self._has_productive_skill_potential(
                    candidate.skill.operator_name
                ):
                    deferred_fillers.append(candidate)
                    continue
                selected.append(candidate.skill)
                selected_names.add(candidate.skill.operator_name)
                score += candidate.score
                if len(selected) >= capacity:
                    break
            if len(selected) < capacity:
                for candidate in deferred_fillers:
                    if candidate.skill.operator_name in selected_names:
                        continue
                    selected.append(candidate.skill)
                    selected_names.add(candidate.skill.operator_name)
                    score += candidate.score
                    if len(selected) >= capacity:
                        break
        return RoomAssignment(
            room_id=room_id,
            room_type=room_type,
            room_name=ROOM_NAMES.get(room_type, room_type),
            target=target,
            operators=selected,
            score=round(score, 3),
            room_level=spec.room_level,
            slots=spec.slots,
        )

    def _best_room_combo(
        self, spec: RoomSpec, excluded: set[str], duration_hours: float
    ) -> RoomCombo | None:
        combos = self._room_combos(spec, excluded, duration_hours)
        return combos[0] if combos else None

    def _room_combos(
        self, spec: RoomSpec, excluded: set[str], duration_hours: float
    ) -> list[RoomCombo]:
        cache_key = (
            *self._cache_context_key(),
            "room_combos",
            spec.room_id,
            spec.room_type,
            spec.target,
            spec.capacity,
            spec.room_level,
            spec.slots,
            round(duration_hours, 6),
            tuple(sorted(excluded)),
        )
        cached = self._room_combos_cache.get(cache_key)
        if cached is not None:
            self._record_runtime_count("roomCombos.cacheHit")
            return list(cached)
        self._record_runtime_count("roomCombos.cacheMiss")
        candidates = self._combo_candidate_pool(spec.room_type, spec.target, excluded)
        if not candidates:
            self._room_combos_cache[cache_key] = ()
            return []
        size = min(spec.capacity, len(candidates))
        combos: list[RoomCombo] = []
        for group in combinations(candidates, size):
            names = {candidate.skill.operator_name for candidate in group}
            if len(names) != len(group):
                continue
            operators = tuple(candidate.skill for candidate in group)
            score = self._score_room_combo(spec, list(operators), duration_hours)
            combos.append(RoomCombo(operators=operators, score=score))
        combos.sort(
            key=lambda combo: (
                combo.score,
                sum(skill.unlocked for skill in combo.operators),
                -sum(float(skill.upgrade.cost_score if skill.upgrade else 0.0) for skill in combo.operators),
            ),
            reverse=True,
        )
        self._room_combos_cache[cache_key] = tuple(combos)
        return combos

    def _combo_candidate_pool(
        self, room_type: str, target: str | None, excluded: set[str]
    ) -> list[Candidate]:
        cache_key = (
            *self._cache_context_key(),
            "combo_candidate_pool",
            room_type,
            target,
            tuple(sorted(excluded)),
        )
        cached = self._combo_candidate_pool_cache.get(cache_key)
        if cached is not None:
            self._record_runtime_count("comboCandidatePool.cacheHit")
            return list(cached)
        self._record_runtime_count("comboCandidatePool.cacheMiss")
        candidates = self._combo_candidates(room_type, target, excluded)
        if room_type == "POWER":
            result = candidates[:16]
            self._combo_candidate_pool_cache[cache_key] = tuple(result)
            return result

        top_limit = COMBO_CANDIDATE_TOP_LIMIT
        max_pool = COMBO_CANDIDATE_POOL_LIMIT
        top_candidates = unique_candidate_variants(candidates[:top_limit])
        protected_candidates = combo_closure_candidates(candidates, room_type, target)

        pool = unique_candidate_variants([*top_candidates, *protected_candidates])
        if len(pool) <= max_pool:
            self._combo_candidate_pool_cache[cache_key] = tuple(pool)
            return pool

        pool_keys = {candidate_variant_key(candidate) for candidate in pool}
        protected = [
            candidate
            for candidate in protected_candidates
            if candidate_variant_key(candidate) in pool_keys
        ]
        selected = list(top_candidates)
        selected_keys = {candidate_variant_key(candidate) for candidate in selected}
        for candidate in protected:
            key = candidate_variant_key(candidate)
            if key in selected_keys:
                continue
            selected.append(candidate)
            selected_keys.add(key)
            if len(selected) >= max_pool:
                break
        if len(selected) < max_pool:
            for candidate in sorted(pool, key=lambda item: item.score, reverse=True):
                key = candidate_variant_key(candidate)
                if key in selected_keys:
                    continue
                selected.append(candidate)
                selected_keys.add(key)
                if len(selected) >= max_pool:
                    break
        result = selected[:max_pool]
        self._combo_candidate_pool_cache[cache_key] = tuple(result)
        return result

    def _combo_candidates(
        self, room_type: str, target: str | None, excluded: set[str]
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for operator in self.roster:
            if operator.name in excluded:
                continue
            candidates.extend(self._skill_candidates_for(operator, room_type, target))
            if is_guide_special_trade_anchor(operator.name, room_type, target):
                synthetic = synthetic_guide_special_trade_skill(operator)
                candidates.append(Candidate(synthetic, guide_special_trade_score(operator.name)))
            if is_guide_manufacture_anchor(operator.name, room_type, target):
                synthetic = synthetic_guide_manufacture_skill(operator, target)
                candidates.append(
                    Candidate(
                        synthetic,
                        guide_manufacture_anchor_score(operator.name, target),
                    )
                )
        return sorted(
            unique_candidate_variants(candidates),
            key=lambda candidate: (
                candidate.score,
                combo_protection_priority(candidate.skill, room_type, target),
                candidate.skill.unlocked,
                -float(candidate.skill.upgrade.cost_score if candidate.skill.upgrade else 0),
            ),
            reverse=True,
        )

    def _score_room_combo(
        self, spec: RoomSpec, operators: list[BaseSkill], duration_hours: float
    ) -> float:
        cache_key = (
            spec.room_type,
            spec.target,
            spec.room_level,
            spec.slots,
            round(duration_hours, 6),
            tuple(sorted((skill.operator_name, skill.buff_id) for skill in operators)),
            round(self.upgrade_cost_weight, 8),
        )
        cached = self._room_combo_score_cache.get(cache_key)
        if cached is not None:
            self._record_runtime_count("roomComboScore.cacheHit")
            return cached
        self._record_runtime_count("roomComboScore.cacheMiss")
        if spec.room_type == "POWER":
            effect = evaluate_room_effect(operators, spec.target)
            score = effect.speed_percent
        else:
            vector = self._room_combo_vector(spec, operators, duration_hours)
            score = room_output_score(spec, vector)
        score -= sum(
            float(skill.upgrade.cost_score if skill.upgrade else 0.0)
            for skill in operators
        ) * self.upgrade_cost_weight
        score = round(score, 6)
        self._room_combo_score_cache[cache_key] = score
        return score

    def _room_combo_vector(
        self,
        spec: RoomSpec,
        operators: list[BaseSkill],
        duration_hours: float,
    ) -> ProductionVector:
        cache_key = (
            spec.room_type,
            spec.target,
            spec.room_level,
            spec.slots,
            round(duration_hours, 6),
            tuple(sorted((skill.operator_name, skill.buff_id) for skill in operators)),
        )
        cached = self._room_combo_vector_cache.get(cache_key)
        if cached is not None:
            self._record_runtime_count("roomComboVector.cacheHit")
            return cached
        self._record_runtime_count("roomComboVector.cacheMiss")
        self._record_runtime_count("evaluateRoom.comboVector")
        assignment = RoomAssignment(
            room_id=spec.room_id,
            room_type=spec.room_type,
            room_name=ROOM_NAMES.get(spec.room_type, spec.room_type),
            target=spec.target,
            operators=operators,
            score=0.0,
            room_level=spec.room_level,
            slots=spec.slots,
        )
        production = self._combo_simulator.evaluate_room(assignment, duration_hours)
        vector = production.vector
        vector.lmdNet = vector.lmdGross - vector.materialCosts.get("4001", 0.0)
        self._room_combo_vector_cache[cache_key] = vector
        return vector

    def _would_overflow(
        self,
        spec: RoomSpec,
        operators: list[BaseSkill],
        duration_hours: float,
    ) -> bool:
        room_type = spec.room_type
        if room_type not in {"TRADING", "MANUFACTURE"}:
            return False
        return self._room_combo_vector(spec, operators, duration_hours).overflowLoss > 0.01

    def _candidates(
        self,
        room_type: str,
        target: str | None,
        excluded: set[str],
        allow_no_skill: bool,
    ) -> list[Candidate]:
        best_by_operator: dict[str, Candidate] = {}
        for operator in self.roster:
            if operator.name in excluded:
                continue
            best = self._best_skill_for(operator, room_type, target)
            if best:
                best_by_operator[operator.name] = best
            elif is_guide_special_trade_anchor(operator.name, room_type, target):
                synthetic = synthetic_guide_special_trade_skill(operator)
                best_by_operator[operator.name] = Candidate(synthetic, guide_special_trade_score(operator.name))
            elif is_guide_manufacture_anchor(operator.name, room_type, target):
                synthetic = synthetic_guide_manufacture_skill(operator, target)
                best_by_operator[operator.name] = Candidate(
                    synthetic,
                    guide_manufacture_anchor_score(operator.name, target),
                )
            elif allow_no_skill:
                synthetic = synthetic_skill(operator, room_type)
                best_by_operator[operator.name] = Candidate(synthetic, 0.0)
        return sorted(
            best_by_operator.values(),
            key=lambda candidate: (
                candidate.score,
                candidate.skill.unlocked,
                -float(candidate.skill.upgrade.cost_score if candidate.skill.upgrade else 0),
            ),
            reverse=True,
        )

    def _best_skill_for(
        self, operator: RosterOperator, room_type: str, target: str | None
    ) -> Candidate | None:
        candidates = self._skill_candidates_for(operator, room_type, target)
        if not candidates:
            return None
        return max(candidates, key=lambda candidate: candidate.score)

    def _skill_candidates_for(
        self, operator: RosterOperator, room_type: str, target: str | None
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for skill in self.skills_by_operator.get(operator.name, []):
            if skill.room_type != room_type:
                continue
            score = score_skill(skill, target, self.upgrade_cost_weight)
            if score is None:
                continue
            candidates.append(Candidate(skill, score))
        return unique_candidate_variants(candidates)

    def _has_productive_skill_potential(self, operator_name: str) -> bool:
        productive_rooms = {"TRADING", "MANUFACTURE", "POWER"}
        for skill in self.skills_by_operator.get(operator_name, []):
            if skill.room_type in productive_rooms and (
                skill.parsed_score > 0 or skill.upgrade is not None
            ):
                return True
        return False


def score_skill(skill: BaseSkill, target: str | None, upgrade_cost_weight: float) -> float | None:
    if target and skill.targets and target not in skill.targets:
        return None
    score = skill.parsed_score
    if score <= 0 and skill.room_type not in {"CONTROL", "DORMITORY", "TRADING", "MANUFACTURE"}:
        return None
    if skill.complex_condition and score > 0:
        score *= 0.65
    if skill.upgrade:
        score -= skill.upgrade.cost_score * upgrade_cost_weight
        if score <= 0:
            return None
    return round(score, 4)


def is_combo_relevant(skill: BaseSkill) -> bool:
    text = f"{skill.buff_id} {skill.buff_name} {skill.description}"
    markers = (
        "当与",
        "同一个贸易站",
        "归零",
        "低语",
        "特别订单",
        "违约订单",
        "高品质贵金属订单",
        "trade_ord_wt",
        "仓库容量",
        "回收利用",
        "大就是好",
    )
    return skill.room_type in {"TRADING", "MANUFACTURE"} and any(
        marker in text for marker in markers
    )


def combo_closure_candidates(
    candidates: list[Candidate],
    room_type: str,
    target: str | None,
) -> list[Candidate]:
    protected_names: set[str] = set()
    protected_name_priority: dict[str, int] = {}
    protected_tags: set[str] = set()
    roster_names = {candidate.skill.operator_name for candidate in candidates}
    protect_trade_specialists = False

    def protect_name(name: str, priority: int) -> None:
        protected_names.add(name)
        protected_name_priority[name] = max(
            protected_name_priority.get(name, 0),
            priority,
        )

    for candidate in candidates:
        skill = candidate.skill
        if should_protect_combo_seed(skill, room_type, target):
            priority = combo_protection_priority(skill, room_type, target)
            protect_name(skill.operator_name, priority)
            for name in linked_combo_candidate_names(skill):
                protect_name(name, priority)
            for name in mentioned_operator_names(f"{skill.buff_name} {skill.description}", roster_names):
                protect_name(name, priority)
            protected_tags.update(linked_combo_faction_tags(skill))
        if trade_special_role(skill):
            protect_trade_specialists = True

    if protected_tags:
        for candidate in candidates:
            if any(tag in candidate.skill.faction_tags for tag in protected_tags):
                protect_name(candidate.skill.operator_name, 3)

    if protect_trade_specialists:
        for candidate in candidates:
            if trade_special_role(candidate.skill):
                protect_name(
                    candidate.skill.operator_name,
                    combo_protection_priority(candidate.skill, room_type, target),
                )

    best_by_name = best_candidates_by_name(candidates)
    protected = [
        candidate
        for candidate in candidates
        if candidate.skill.operator_name in protected_names
        and (
            candidate is best_by_name.get(candidate.skill.operator_name)
            or should_protect_combo_seed(candidate.skill, room_type, target)
        )
    ]
    return sorted(
        unique_candidate_variants(protected),
        key=lambda candidate: (
            max(
                combo_protection_priority(candidate.skill, room_type, target),
                protected_name_priority.get(candidate.skill.operator_name, 0),
            ),
            candidate.score,
        ),
        reverse=True,
    )


def best_candidates_by_name(candidates: list[Candidate]) -> dict[str, Candidate]:
    by_name: dict[str, Candidate] = {}
    for candidate in candidates:
        previous = by_name.get(candidate.skill.operator_name)
        if previous is None or candidate.score > previous.score:
            by_name[candidate.skill.operator_name] = candidate
    return by_name


def candidate_variant_key(candidate: Candidate) -> tuple[str, str, tuple[str, ...]]:
    return (
        candidate.skill.operator_name,
        candidate.skill.buff_id,
        tuple(candidate.skill.targets),
    )


def unique_candidate_variants(candidates: Iterable[Candidate]) -> list[Candidate]:
    by_key: dict[tuple[str, str, tuple[str, ...]], Candidate] = {}
    for candidate in candidates:
        key = candidate_variant_key(candidate)
        previous = by_key.get(key)
        if previous is None or candidate.score > previous.score:
            by_key[key] = candidate
    return list(by_key.values())


def should_protect_combo_seed(
    skill: BaseSkill,
    room_type: str,
    target: str | None,
) -> bool:
    if skill.room_type != room_type:
        return False
    return (
        is_combo_relevant(skill)
        or trade_special_role(skill) is not None
        or is_guide_manufacture_candidate_skill(skill, target)
        or bool(linked_combo_faction_tags(skill))
        or is_formula_target_specialist(skill, target)
    )


def combo_protection_priority(
    skill: BaseSkill,
    room_type: str,
    target: str | None,
) -> int:
    if is_guide_manufacture_candidate_skill(skill, target):
        return 5
    if is_formula_target_specialist(skill, target):
        return 4
    if linked_combo_faction_tags(skill):
        return 3
    if trade_special_role(skill):
        return 2
    if is_combo_relevant(skill) and skill.room_type == room_type:
        return 1
    return 0


def linked_combo_candidate_names(skill: BaseSkill) -> set[str]:
    text = f"{skill.buff_id} {skill.buff_name} {skill.description}".lower()
    names: set[str] = set()
    for name in ("Tequila", "Butushu", "Proviso", "Shamare"):
        if name.lower() in text:
            names.add(name)
    return names


def linked_combo_faction_tags(skill: BaseSkill) -> set[str]:
    if skill.room_type != "TRADING":
        return set()
    text = f"{skill.buff_id} {skill.buff_name} {skill.description}".lower()
    if "glasgow" in text or "glasgow" in skill.faction_tags:
        return {"glasgow"}
    return set()


def trade_special_role(skill: BaseSkill) -> str | None:
    if skill.room_type != "TRADING":
        return None
    text = f"{skill.buff_id} {skill.buff_name} {skill.description}".lower()
    if "guide_special_trade_anchor" in text:
        return "guide_special_anchor"
    if "shamare" in text or "whisper" in text:
        return "shamare"
    if "tequila" in text or "special_order" in text or "fixed_special_order" in text:
        return "tequila"
    if "butushu" in text or "proviso" in text or "claim" in text:
        return "proviso"
    return None


def is_formula_target_specialist(skill: BaseSkill, target: str | None) -> bool:
    return (
        skill.room_type == "MANUFACTURE"
        and target == "F_DIAMOND"
        and "F_DIAMOND" in skill.targets
        and skill.parsed_score >= 30
    )


def is_guide_manufacture_candidate_skill(skill: BaseSkill, target: str | None) -> bool:
    return is_guide_manufacture_anchor(skill.operator_name, skill.room_type, target)


def is_guide_manufacture_anchor(
    operator_name: str,
    room_type: str,
    target: str | None,
) -> bool:
    if room_type != "MANUFACTURE" or target is None:
        return False
    for lookup in GUIDE_MANUFACTURE_LOOKUPS:
        if lookup["target"] != target:
            continue
        if operator_name in lookup.get("required", set()):
            return True
    return False


def guide_manufacture_anchor_score(operator_name: str, target: str | None) -> float:
    scores = [
        float(lookup["paperEfficiencyPercent"])
        for lookup in GUIDE_MANUFACTURE_LOOKUPS
        if lookup["target"] == target and operator_name in lookup.get("required", set())
    ]
    return max(scores, default=0.0)


def synthetic_guide_manufacture_skill(operator: RosterOperator, target: str | None) -> BaseSkill:
    score = guide_manufacture_anchor_score(operator.name, target)
    return BaseSkill(
        char_id="",
        operator_name=operator.name,
        room_type="MANUFACTURE",
        buff_id=f"guide_manufacture_anchor[{target or 'any'}]",
        buff_name="Guide manufacture anchor",
        description=(
            "Internal guide-calibrated Manufacturing Station paper-efficiency anchor; "
            "used when official base skill parsing does not expose a usable skill."
        ),
        efficiency=score,
        targets=tuple([target] if target else []),
        cond_elite=operator.elite,
        cond_level=operator.level,
        unlocked=True,
        complex_condition=True,
        parsed_score=0.0,
        upgrade=None,
    )


def room_output_score(spec: RoomSpec, vector: ProductionVector) -> float:
    score = -vector.overflowLoss * 5.0 - vector.fatigueRisk * 2.0
    if spec.room_type == "TRADING":
        if spec.target == "O_DIAMOND":
            return score + vector.orundum
        return score + vector.lmdGross * 0.0036
    if spec.room_type == "MANUFACTURE":
        if spec.target == "F_EXP":
            return score + vector.exp / 1000.0 * 4.0
        if spec.target == "F_DIAMOND":
            return score + vector.shardDelta * 12.0 - vector.materialCosts.get("4001", 0.0) / 1000.0 * 5.0
        return score + vector.pureGoldDelta * 8.0
    return score


def synthetic_skill(operator: RosterOperator, room_type: str) -> BaseSkill:
    return BaseSkill(
        char_id="",
        operator_name=operator.name,
        room_type=room_type,
        buff_id="none",
        buff_name="无匹配基建技能",
        description="用于填补宿舍或空位。",
        efficiency=0.0,
        targets=(),
        cond_elite=operator.elite,
        cond_level=operator.level,
        unlocked=True,
        complex_condition=False,
        parsed_score=0.0,
        upgrade=None,
    )


def is_guide_special_trade_anchor(
    operator_name: str,
    room_type: str,
    target: str | None,
) -> bool:
    return room_type == "TRADING" and target == "O_GOLD" and operator_name in GUIDE_SPECIAL_TRADE_ANCHORS


def guide_special_trade_score(operator_name: str) -> float:
    anchor = GUIDE_SPECIAL_TRADE_ANCHORS[operator_name]
    intrinsic = anchor["intrinsicSpeedPercent"]
    return float(intrinsic[max(intrinsic)])


def synthetic_guide_special_trade_skill(operator: RosterOperator) -> BaseSkill:
    anchor = GUIDE_SPECIAL_TRADE_ANCHORS[operator.name]
    return BaseSkill(
        char_id="",
        operator_name=operator.name,
        room_type="TRADING",
        buff_id=f"guide_special_trade_anchor[{anchor['mechanismId']}]",
        buff_name="Guide special trade anchor",
        description=(
            "Internal guide-calibrated special Trading Post mechanism anchor; "
            "used when official base skill parsing does not expose a usable skill."
        ),
        efficiency=guide_special_trade_score(operator.name),
        targets=(),
        cond_elite=operator.elite,
        cond_level=operator.level,
        unlocked=True,
        complex_condition=True,
        parsed_score=0.0,
        upgrade=None,
    )


def shift_label(index: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if index < len(alphabet):
        return alphabet[index]
    return f"S{index + 1}"


def repeat_shift_cycle(shifts: list[ShiftPlan], repeat_count: int) -> list[ShiftPlan]:
    repeat_count = max(1, int(repeat_count))
    if repeat_count == 1:
        return shifts
    expanded: list[ShiftPlan] = []
    for repeat_index in range(1, repeat_count + 1):
        for shift in shifts:
            expanded.append(replace(shift, name=f"{shift.name}#{repeat_index}"))
    return expanded


def default_shift_times(shift_count: int, shift_hours: int) -> list[str]:
    if shift_count == 2 and shift_hours == 12:
        return ["08:00", "20:00"]
    return [f"{(8 + index * shift_hours) % 24:02d}:00" for index in range(shift_count)]


def normalize_shift_durations(
    *,
    shift_count: int,
    shift_hours: int,
    shift_durations: Iterable[float] | None = None,
) -> list[float]:
    if shift_durations is None:
        return [float(shift_hours)] * max(1, int(shift_count))
    durations = [float(duration) for duration in shift_durations]
    if len(durations) < 1:
        raise ValueError("shift_durations requires at least one shift.")
    if any(duration <= 0 for duration in durations):
        raise ValueError("shift_durations must contain positive hours.")
    return durations


def default_shift_times_for_durations(shift_durations: list[float]) -> list[str]:
    starts: list[str] = []
    current = 8.0
    for duration in shift_durations:
        starts.append(f"{int(current % 24):02d}:00")
        current += duration
    return starts


def normalize_forced_targets(
    layout: Layout, targets: tuple[Iterable[str], Iterable[str]]
) -> tuple[list[str], list[str]]:
    trading = [str(target) for target in targets[0]]
    manufacture = [str(target) for target in targets[1]]
    if len(trading) != layout.trading:
        raise ValueError(
            f"Forced trading targets contain {len(trading)} rooms, expected {layout.trading}."
        )
    if len(manufacture) != layout.manufacture:
        raise ValueError(
            "Forced manufacture targets contain "
            f"{len(manufacture)} rooms, expected {layout.manufacture}."
        )
    invalid_trading = sorted(set(trading) - {"O_GOLD", "O_DIAMOND"})
    invalid_manufacture = sorted(set(manufacture) - {"F_EXP", "F_GOLD", "F_DIAMOND"})
    if invalid_trading:
        raise ValueError(f"Unsupported forced trading targets: {', '.join(invalid_trading)}.")
    if invalid_manufacture:
        raise ValueError(
            f"Unsupported forced manufacture targets: {', '.join(invalid_manufacture)}."
        )
    return trading, manufacture


def forced_target_options(
    layout: Layout, targets: tuple[Iterable[str], Iterable[str]]
) -> list[tuple[list[str], list[str]]]:
    trading, manufacture = normalize_forced_targets(layout, targets)
    trading_order = preferred_target_order(
        trading,
        list(levels_or_default(layout.trading_levels, layout.trading, 3)),
        ["O_GOLD", "O_DIAMOND"],
        priority_target="O_DIAMOND" if "O_DIAMOND" in trading else "O_GOLD",
    )
    manufacture_priority = "F_DIAMOND" if "F_DIAMOND" in manufacture else "F_EXP"
    manufacture_order = preferred_target_order(
        manufacture,
        list(
            layout.manufacture_slots
            or levels_or_default(layout.manufacture_levels, layout.manufacture, 3)
        ),
        ["F_EXP", "F_GOLD", "F_DIAMOND"],
        priority_target=manufacture_priority,
    )
    return [(list(trading_order), list(manufacture_order))]


def preferred_target_order(
    targets: list[str],
    room_weights: list[int],
    target_order: list[str],
    *,
    priority_target: str | None = None,
) -> tuple[str, ...]:
    if not targets:
        return ()
    weights = room_weights or [1] * len(targets)
    if len(weights) < len(targets):
        weights = [*weights, *([weights[-1] if weights else 1] * (len(targets) - len(weights)))]
    weights = weights[: len(targets)]
    priority = priority_target or next((target for target in target_order if target in targets), None)
    if priority is None or targets.count(priority) == len(targets):
        return tuple(targets)
    weighted_indices = sorted(range(len(targets)), key=lambda index: (weights[index], -index), reverse=True)
    assigned: list[str | None] = [None] * len(targets)
    for index in weighted_indices[: targets.count(priority)]:
        assigned[index] = priority
    remaining: list[str] = []
    for target in target_order:
        if target == priority:
            continue
        remaining.extend([target] * targets.count(target))
    for index in range(len(assigned)):
        if assigned[index] is None:
            assigned[index] = remaining.pop(0)
    return tuple(str(target) for target in assigned)


def enumerate_target_options(layout: Layout, mode: str) -> list[tuple[list[str], list[str]]]:
    if mode == "normal":
        return [(trading_targets(layout, mode), manufacture_targets(layout, mode))]

    preset = mode_preset(mode)
    trading_options = []
    for targets in product(["O_GOLD", "O_DIAMOND"], repeat=layout.trading):
        if targets.count("O_GOLD") < preset.min_lmd_trading:
            continue
        if preset.prefer_orundum and layout.trading > preset.min_lmd_trading:
            if targets.count("O_DIAMOND") < 1:
                continue
        trading_options.append(
            list(targets)
            if has_variable_trading_rooms(layout)
            else order_targets(list(targets), ["O_GOLD", "O_DIAMOND"])
        )
    manufacture_options = []
    for targets in product(["F_EXP", "F_GOLD", "F_DIAMOND"], repeat=layout.manufacture):
        if targets.count("F_EXP") < preset.min_exp_factories:
            continue
        if preset.prefer_orundum and preset.min_exp_factories == 0 and targets.count("F_EXP"):
            continue
        if targets.count("F_DIAMOND") < preset.min_shard_factories:
            continue
        if targets.count("F_GOLD") < 1:
            continue
        manufacture_options.append(order_targets(list(targets), ["F_EXP", "F_GOLD", "F_DIAMOND"]))

    options: list[tuple[list[str], list[str]]] = []
    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    for trading in trading_options:
        for manufacture in manufacture_options:
            key = (tuple(trading), tuple(manufacture))
            if key in seen:
                continue
            seen.add(key)
            options.append((trading, manufacture))
    return options or [(trading_targets(layout, mode), manufacture_targets(layout, mode))]


def has_variable_trading_rooms(layout: Layout) -> bool:
    levels = tuple(layout.trading_levels[: layout.trading])
    return bool(levels) and len(set(levels)) > 1


def order_targets(targets: list[str], order: list[str]) -> list[str]:
    result: list[str] = []
    for target in order:
        result.extend([target] * targets.count(target))
    return result


def target_selection_score(
    mode: str,
    report: ProductionReport,
    *,
    min_lmd_gross: float = 0.0,
    min_exp: float = 0.0,
    min_orundum: float = 0.0,
) -> float:
    score = report.score
    daily = report.dailyExpected
    if min_lmd_gross and daily.lmdGross < min_lmd_gross:
        score -= 100000.0 + (min_lmd_gross - daily.lmdGross) / 100.0
    if min_exp and daily.exp < min_exp:
        score -= 100000.0 + (min_exp - daily.exp) / 100.0
    if min_orundum and daily.orundum < min_orundum:
        score -= 100000.0 + (min_orundum - daily.orundum) * 100.0
    contract = preset_contract(mode)
    penalties = set(contract["targetSelectionPenalties"])
    if "inventory_balance" in penalties:
        balance = resource_balance_quality(daily)
        inventory_scale = 1.0 if mode == "balanced_orundum" else 100.0
        score -= float(balance["penalty"]) * inventory_scale
    if "lmd_net_balance" in penalties and daily.orundum > 0.01:
        score -= abs(daily.lmdNet) / 100.0
    if mode == "balanced_orundum" and daily.orundum > 0.01 and daily.lmdGross > 0.01:
        exp_support_ceiling = daily.lmdGross * 0.75
        if daily.exp > exp_support_ceiling:
            score -= (daily.exp - exp_support_ceiling) / 60.0
    return score


def insertion_objective_score(mode: str, report: ProductionReport) -> float:
    daily = getattr(report, "dailyExpected", None)
    if daily is None:
        return report.score
    if mode == "max_orundum":
        return min(daily.orundum, 480.0) * 10000.0 + daily.lmdGross / 10.0 + report.score
    return report.score


def threshold_warnings(
    report: ProductionReport,
    min_lmd_gross: float,
    min_exp: float,
    min_orundum: float,
) -> list[str]:
    daily = report.dailyExpected
    warnings: list[str] = []
    if min_lmd_gross and daily.lmdGross < min_lmd_gross:
        warnings.append(
            f"未达到最低每日龙门币毛收入 {min_lmd_gross:.0f}；当前最好候选为 {daily.lmdGross:.2f}。"
        )
    if min_exp and daily.exp < min_exp:
        warnings.append(f"未达到最低每日经验 {min_exp:.0f}；当前最好候选为 {daily.exp:.2f}。")
    if min_orundum and daily.orundum < min_orundum:
        warnings.append(f"未达到最低每日合成玉 {min_orundum:.0f}；当前最好候选为 {daily.orundum:.2f}。")
    return warnings


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def dedupe_shift_insertion_groups(groups: list[ShiftInsertionGroup]) -> list[ShiftInsertionGroup]:
    seen: set[tuple[tuple[str, str | None, str, bool, str | None], ...]] = set()
    result: list[ShiftInsertionGroup] = []
    for group in groups:
        key = tuple(
            (
                spec.room_type,
                spec.target,
                spec.operator_name,
                spec.allow_no_skill,
                spec.room_group,
            )
            for spec in group.specs
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(group)
    return result


def compound_shift_insertion_groups(groups: list[ShiftInsertionGroup]) -> list[ShiftInsertionGroup]:
    compounds: list[ShiftInsertionGroup] = []
    for left, right in combinations(groups, 2):
        if not ({spec.operator_name for spec in left.specs} & {spec.operator_name for spec in right.specs}):
            continue
        merged = merge_insertion_specs(left.specs, right.specs)
        if merged is None:
            continue
        if len(merged) <= max(len(left.specs), len(right.specs)):
            continue
        if len(merged) > 4:
            continue
        if len(group_specs_by_room_requirement(merged)) > 2:
            continue
        compounds.append(
            ShiftInsertionGroup(
                f"compound_dependency:{left.id}+{right.id}",
                merged,
            )
        )
    return compounds


def merge_insertion_specs(
    left: tuple[ShiftInsertionSpec, ...],
    right: tuple[ShiftInsertionSpec, ...],
) -> tuple[ShiftInsertionSpec, ...] | None:
    merged: dict[str, ShiftInsertionSpec] = {}
    for spec in (*left, *right):
        previous = merged.get(spec.operator_name)
        if previous is None:
            merged[spec.operator_name] = spec
            continue
        if (
            previous.room_type != spec.room_type
            or previous.target != spec.target
            or previous.room_group != spec.room_group
        ):
            return None
        merged[spec.operator_name] = ShiftInsertionSpec(
            spec.room_type,
            spec.target,
            spec.operator_name,
            allow_no_skill=previous.allow_no_skill or spec.allow_no_skill,
            room_group=spec.room_group,
        )
    return tuple(merged.values())


def insertion_group_to_dict(group: ShiftInsertionGroup) -> dict[str, Any]:
    return {
        "groupId": group.id,
        "specKey": insertion_spec_key(group.specs),
        "operators": [spec.operator_name for spec in group.specs],
        "specs": [
            {
                "roomType": spec.room_type,
                "target": spec.target,
                "operator": spec.operator_name,
                **({"allowNoSkill": True} if spec.allow_no_skill else {}),
                **({"roomGroup": spec.room_group} if spec.room_group else {}),
            }
            for spec in group.specs
        ],
    }


def production_vector_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float]:
    keys = (
        "lmdGross",
        "lmdNet",
        "exp",
        "orundum",
        "officeSpeed",
        "meetingSpeed",
        "pureGoldDelta",
        "shardDelta",
        "droneCount",
        "droneUsed",
    )
    result: dict[str, float] = {}
    for key in keys:
        delta = float(after.get(key) or 0.0) - float(before.get(key) or 0.0)
        if abs(delta) > 0.0001:
            result[key] = round(delta, 3)
    return result


def shift_assignment_changes(
    before: list[ShiftPlan],
    after: list[ShiftPlan],
) -> list[dict[str, Any]]:
    before_rooms = room_assignments_by_key(before)
    changes: list[dict[str, Any]] = []
    for shift in after:
        for room in shift.rooms:
            key = (shift.name, room.room_id)
            previous = before_rooms.get(key)
            before_names = (
                [skill.operator_name for skill in previous.operators]
                if previous is not None
                else []
            )
            after_names = [skill.operator_name for skill in room.operators]
            if before_names == after_names:
                continue
            changes.append(
                {
                    "shift": shift.name,
                    "roomId": room.room_id,
                    "roomType": room.room_type,
                    "target": room.target,
                    "before": before_names,
                    "after": after_names,
                }
            )
    return changes


def room_assignments_by_key(
    shifts: list[ShiftPlan],
) -> dict[tuple[str, str], RoomAssignment]:
    return {
        (shift.name, room.room_id): room
        for shift in shifts
        for room in shift.rooms
    }


def insertion_group_key(group: ShiftInsertionGroup) -> tuple[str, str]:
    return (group.id, insertion_spec_key(group.specs))


def insertion_spec_key(specs: Iterable[ShiftInsertionSpec]) -> str:
    parts = sorted(
        f"{spec.room_type}:{spec.target or ''}:{spec.room_group or ''}:{spec.operator_name}"
        for spec in specs
    )
    return "|".join(parts)


def insertion_search_summary(audit: dict[str, Any]) -> dict[str, int]:
    skipped = audit.get("skipped") or []
    counts: dict[str, int] = {}
    for item in skipped:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "availableGroupCount": int(audit.get("availableGroupCount") or 0),
        "acceptedCount": len(audit.get("accepted") or []),
        "displacedCount": len(audit.get("displaced") or []),
        "skippedCount": len(skipped),
        "alreadySatisfiedCount": counts.get("already_satisfied", 0),
        "evaluatedNotImprovingCount": counts.get("evaluated_not_improving", 0),
        "unplaceableCount": counts.get("unplaceable", 0),
        "remainingImprovementCount": counts.get("remaining_improvement_after_pass_limit", 0),
    }


def local_quality_audit(audit: dict[str, Any]) -> dict[str, Any]:
    skipped = audit.get("skipped") or []
    positive = [
        item
        for item in skipped
        if float(item.get("scoreDelta") or 0.0) > 0.001
        or item.get("status") == "remaining_improvement_after_pass_limit"
    ]
    return {
        "remainingPositiveCount": len(positive),
        "positiveNeighborhoods": [
            {
                "groupId": item.get("groupId"),
                "specKey": item.get("specKey"),
                "status": item.get("status"),
                "shift": item.get("shift"),
                "scoreDelta": item.get("scoreDelta"),
                "candidateScore": item.get("candidateScore"),
                "operators": item.get("operators"),
            }
            for item in positive[:20]
        ],
    }


def dedupe_local_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for record in records:
        key = (
            record.get("shift"),
            record.get("roomId"),
            record.get("replaced"),
            record.get("inserted"),
            record.get("insertedBuffId"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def objective_conflict_audit(
    insertion_search: dict[str, Any],
    local_conflict_audit: dict[str, Any],
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for record in local_conflict_audit.get("lmdPositiveRejected") or []:
        records.append({"source": "local_optimality_audit", **dict(record)})
    for record in insertion_search.get("skipped") or []:
        daily_delta = record.get("dailyExpectedDelta") or {}
        if float(daily_delta.get("lmdGross") or 0.0) <= 0.001:
            continue
        if float(record.get("scoreDelta") or 0.0) > 0.001:
            continue
        records.append(
            {
                "source": "diagnostic_insertion_search",
                "status": "lmd_positive_rejected_by_objective",
                "groupId": record.get("groupId"),
                "specKey": record.get("specKey"),
                "operators": record.get("operators"),
                "shift": record.get("shift"),
                "currentScore": record.get("currentScore"),
                "candidateScore": record.get("candidateScore"),
                "scoreDelta": record.get("scoreDelta"),
                "dailyExpectedDelta": daily_delta,
                "assignmentChanges": record.get("assignmentChanges"),
            }
        )
    records = dedupe_objective_conflict_records(records)
    records.sort(
        key=lambda item: (
            float((item.get("dailyExpectedDelta") or {}).get("lmdGross") or 0.0),
            float(item.get("scoreDelta") or 0.0),
        ),
        reverse=True,
    )
    return {
        "policy": (
            "normal keeps the existing composite objective. LMD-gross-positive replacements "
            "are reported here when they are rejected because pure-gold balance or another "
            "objective term worsens enough to lower the composite score."
        ),
        "lmdPositiveRejectedCount": len(records),
        "lmdPositiveRejected": records[:120],
    }


def dedupe_objective_conflict_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for record in records:
        key = (
            record.get("source"),
            record.get("shift"),
            record.get("roomId"),
            record.get("groupId"),
            record.get("specKey"),
            record.get("replaced"),
            record.get("inserted"),
            record.get("insertedBuffId"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def group_specs_by_room_requirement(
    specs: tuple[ShiftInsertionSpec, ...],
) -> list[list[ShiftInsertionSpec]]:
    groups: list[list[ShiftInsertionSpec]] = []
    for spec in specs:
        for group in groups:
            first = group[0]
            if (
                first.room_type == spec.room_type
                and first.target == spec.target
                and first.room_group == spec.room_group
            ):
                group.append(spec)
                break
        else:
            groups.append([spec])
    return groups


def mentioned_operator_names(text: str, roster_names: set[str]) -> list[str]:
    return dependency_parser.mentioned_operator_names(text, roster_names)


def first_skill_target(skill: BaseSkill) -> str | None:
    return dependency_parser.first_skill_target(skill)


def same_room_dependency(text: str) -> tuple[str, str | None] | None:
    return dependency_parser.same_room_dependency(text)


def named_partner_room_type(text: str, partner: str) -> str | None:
    return dependency_parser.named_partner_room_type(text, partner)


def default_target_for_room(room_type: str) -> str | None:
    return dependency_parser.default_target_for_room(room_type)


__all__ = [
    "Candidate",
    "OptimizerResult",
    "ROOM_NAMES",
    "RoomSpec",
    "ScheduleOptimizer",
    "apply_layout_variant",
    "default_shift_times",
    "parse_layout",
    "score_skill",
    "target_label",
]
