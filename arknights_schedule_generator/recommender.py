from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook

from .calibration import (
    latest_yituliu_orundum_targets,
    production_economics_summary,
    summarize_mower_plan,
    unwrap_mower_schedule,
    validation_economics_summary,
    yituliu_2026_06_image_label_cases,
)
from .data import GameData
from .diagnostics import build_recommendation_diagnostics, force_specs_from_explanation
from .exporter import collect_upgrades, find_conflicts, upgrade_to_dict, write_result_json
from .models import BaseSkill, Layout, RoomAssignment, RosterOperator, ShiftPlan
from .optimizer import (
    OptimizerResult,
    ScheduleOptimizer,
    ShiftInsertionGroup,
    ShiftInsertionSpec,
    apply_layout_variant,
    parse_layout,
)
from .power import apply_right_side_preset, power_status
from .presets import normalize_mode, preset_contract, preset_contracts, target_label
from .production import (
    DEFAULT_MAX_DRONE_CYCLE_REPEATS,
    DEFAULT_PURE_GOLD_TARGET_PER_DAY,
    DEFAULT_PURE_GOLD_TOLERANCE,
    ProductionReport,
    ProductionSimulator,
    ProductionVector,
    pure_gold_balance_quality,
    resource_balance_quality,
)
from .schedule_import import load_yituliu_schedule


DEFAULT_LAYOUTS = ("243", "252", "342", "324", "333", "153")
DEFAULT_MODES = ("normal", "balanced-orundum", "max-orundum")


@dataclass
class CandidateEvaluation:
    id: str
    result: OptimizerResult
    allow_upgrades: bool
    profile: str
    profile_label: str
    summary: dict[str, Any]
    export_path: Path


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    layout: Layout
    mode: str
    count: int
    hours: int
    profile: str
    profile_label: str
    allow_upgrades: bool
    profile_weight: float
    anchor_preference: list[str]
    forced_targets: tuple[list[str], list[str]] | None = None
    expected_target_counts: dict[str, int] | None = None
    forced_insertion_groups: list[ShiftInsertionGroup] | None = None
    candidate_drone_policy: str | None = None
    drone_reference_daily: dict[str, Any] | None = None
    candidate_shift_durations: list[float] | None = None
    candidate_role: str = "primary"


@dataclass(frozen=True)
class CandidateWorkerConfig:
    game_data: GameData
    roster: list[RosterOperator]
    output_dir: Path
    candidate_dir: Path
    baseline_score: float | None
    shard_formula: str
    drone_policy: str
    right_side: str
    shift_times: list[str] | None
    min_lmd_gross: float
    min_exp: float
    min_orundum: float
    pure_gold_target: float
    pure_gold_tolerance: float
    max_drone_cycle_repeats: int
    cache_policy: str
    roster_cache_key: str
    data_cache_key: str
    profile_runtime: bool = False


_CANDIDATE_WORKER_CONFIG: CandidateWorkerConfig | None = None


def init_candidate_worker(config: CandidateWorkerConfig) -> None:
    global _CANDIDATE_WORKER_CONFIG
    _CANDIDATE_WORKER_CONFIG = config


def evaluate_candidate_spec_from_worker(spec: CandidateSpec) -> dict[str, Any]:
    if _CANDIDATE_WORKER_CONFIG is None:
        raise RuntimeError("Candidate worker was not initialized.")
    return evaluate_candidate_spec(_CANDIDATE_WORKER_CONFIG, spec)


def evaluate_candidate_spec(
    config: CandidateWorkerConfig,
    spec: CandidateSpec,
) -> dict[str, Any]:
    optimizer = ScheduleOptimizer(
        config.game_data,
        config.roster,
        allow_upgrades=spec.allow_upgrades,
        upgrade_cost_weight=spec.profile_weight,
        shard_formula=config.shard_formula,
    )
    candidate_drone_policy = spec.candidate_drone_policy or config.drone_policy
    optimize_drone_policy = (
        reference_fit_seed_drone_policy(spec.drone_reference_daily or {})
        if candidate_drone_policy == "reference-fit"
        else candidate_drone_policy
    )
    export_path = config.candidate_dir / f"{spec.candidate_id}.json"
    cache_key = candidate_cache_key(
        candidate_id=spec.candidate_id,
        layout=spec.layout,
        mode=spec.mode,
        count=spec.count,
        hours=spec.hours,
        profile=spec.profile,
        allow_upgrades=spec.allow_upgrades,
        anchor_preference=spec.anchor_preference,
        forced_targets=spec.forced_targets,
        forced_insertion_groups=spec.forced_insertion_groups,
        candidate_drone_policy=candidate_drone_policy,
        optimize_drone_policy=optimize_drone_policy,
        drone_reference_daily=spec.drone_reference_daily,
        candidate_shift_durations=spec.candidate_shift_durations,
        candidate_role=spec.candidate_role,
        roster_key=config.roster_cache_key,
        data_key=config.data_cache_key,
        shard_formula=config.shard_formula,
        right_side=config.right_side,
        min_lmd_gross=config.min_lmd_gross,
        min_exp=config.min_exp,
        min_orundum=config.min_orundum,
        pure_gold_target=config.pure_gold_target,
        pure_gold_tolerance=config.pure_gold_tolerance,
        max_drone_cycle_repeats=config.max_drone_cycle_repeats,
        upgrade_cost_weight=spec.profile_weight,
        shift_times=config.shift_times,
    )
    if config.cache_policy == "auto":
        cached = load_cached_candidate_evaluation(
            export_path,
            candidate_id=spec.candidate_id,
            expected_layout_raw=spec.layout.raw,
            allow_upgrades=spec.allow_upgrades,
            profile=spec.profile,
            profile_label=spec.profile_label,
            baseline_score=config.baseline_score,
            output_dir=config.output_dir,
            expected_max_groups=ScheduleOptimizer.diagnostic_insertion_group_limit(),
            expected_joint_candidate_limit=ScheduleOptimizer.joint_production_candidate_limit(),
            expected_optimizer_model_version=ScheduleOptimizer.optimizer_model_version(),
            expected_anchor_count=len(spec.anchor_preference),
            expected_target_counts=spec.expected_target_counts,
            expected_pure_gold_target=config.pure_gold_target,
            expected_pure_gold_tolerance=config.pure_gold_tolerance,
            expected_max_drone_cycle_repeats=config.max_drone_cycle_repeats,
            candidate_role=spec.candidate_role,
            expected_candidate_cache_key=cache_key,
        )
        if cached is not None:
            return {"candidate": cached, "skipped": None}
    try:
        result = optimizer.optimize(
            spec.layout,
            mode=spec.mode,
            shift_count=spec.count,
            shift_hours=spec.hours,
            shift_durations=spec.candidate_shift_durations,
            shift_times=config.shift_times,
            drone_policy=optimize_drone_policy,
            min_lmd_gross=config.min_lmd_gross,
            min_exp=config.min_exp,
            min_orundum=config.min_orundum,
            operator_anchor_preference=spec.anchor_preference,
            forced_targets=spec.forced_targets,
            forced_insertion_groups=spec.forced_insertion_groups,
            pure_gold_target=config.pure_gold_target,
            pure_gold_tolerance=config.pure_gold_tolerance,
            max_drone_cycle_repeats=config.max_drone_cycle_repeats,
            profile_runtime=config.profile_runtime,
        )
        if candidate_drone_policy == "reference-fit":
            reference_report = ProductionSimulator(
                config.game_data,
                shard_formula=config.shard_formula,
                drone_policy="reference-fit",
                calibration_profile="guide",
                reference_expected_daily=spec.drone_reference_daily or {},
                pure_gold_target=config.pure_gold_target,
                pure_gold_tolerance=config.pure_gold_tolerance,
            ).evaluate(result.shifts)
            result = replace(
                result,
                score=reference_report.score,
                score_breakdown=reference_report.scoreBreakdown,
                production_report=reference_report,
                drone_policy="reference-fit",
                diagnostic_insertion_search={
                    **result.diagnostic_insertion_search,
                    "pureGoldBalancePolicy": replacement_drone_pure_gold_policy(
                        reference_report,
                        "reference-fit",
                        pure_gold_target=config.pure_gold_target,
                        pure_gold_tolerance=config.pure_gold_tolerance,
                        max_drone_cycle_repeats=config.max_drone_cycle_repeats,
                    ),
                },
            )
    except ValueError as exc:
        return {
            "candidate": None,
            "skipped": {
                "layout": spec.layout.label,
                "mode": str(spec.mode),
                "shiftCount": str(spec.count),
                "profile": spec.profile,
                "candidateId": spec.candidate_id,
                "reason": str(exc),
            },
        }
    result.diagnostic_insertion_search.setdefault("cacheValidation", {})[
        "candidateCacheKey"
    ] = cache_key
    write_result_json(export_path, result, config.game_data)
    summary = result_summary(
        spec.candidate_id,
        result,
        allow_upgrades=spec.allow_upgrades,
        profile=spec.profile,
        profile_label=spec.profile_label,
        baseline_score=config.baseline_score,
        export_path=export_path,
        output_dir=config.output_dir,
        candidate_role=spec.candidate_role,
    )
    return {
        "candidate": CandidateEvaluation(
            id=spec.candidate_id,
            result=result,
            allow_upgrades=spec.allow_upgrades,
            profile=spec.profile,
            profile_label=spec.profile_label,
            summary=summary,
            export_path=export_path,
        ),
        "skipped": None,
    }


def replacement_drone_pure_gold_policy(
    report: ProductionReport,
    candidate_drone_policy: str,
    *,
    pure_gold_target: float,
    pure_gold_tolerance: float,
    max_drone_cycle_repeats: int,
) -> dict[str, Any]:
    quality = pure_gold_balance_quality(
        report.dailyExpected,
        target=pure_gold_target,
        tolerance=pure_gold_tolerance,
    )
    normalized_policy = str(candidate_drone_policy).lower().replace("_", "-")
    if normalized_policy != "auto":
        status = "not_applicable_drone_policy"
        reason = "Pure Gold balancing is applied only by --drone-policy auto."
    else:
        status = str(quality["status"])
        reason = ""
    return {
        "version": 1,
        "scope": "drone_allocation_only",
        "targetPerDay": quality["targetPerDay"],
        "tolerancePerDay": quality["tolerancePerDay"],
        "externalPureGoldAssumptionPerDay": quality["externalPureGoldAssumptionPerDay"],
        "status": status,
        "reason": reason,
        "repeatCount": 1,
        "cycleRepeated": False,
        "maxDroneCycleRepeats": max(1, int(max_drone_cycle_repeats)),
        "finalPureGoldDelta": quality["pureGoldDelta"],
        "finalDeltaFromTarget": quality["deltaFromTarget"],
        "finalAbsDeltaFromTarget": quality["absDeltaFromTarget"],
        "withinTolerance": quality["withinTolerance"],
    }


def roster_fingerprint(roster: Iterable[RosterOperator]) -> str:
    rows = []
    for operator in sorted(roster, key=lambda item: item.name):
        rows.append(
            {
                "name": operator.name,
                "recruited": operator.recruited,
                "rarity": operator.rarity,
                "level": operator.level,
                "elite": operator.elite,
                "potential": operator.potential,
                "skillLevel": operator.skill_level,
                "masteries": list(operator.masteries),
                "modules": dict(sorted(operator.modules.items())),
            }
        )
    return stable_hash(rows)


def data_cache_fingerprint(game_data: GameData) -> str:
    building = game_data.building or {}
    return stable_hash(
        {
            "dataVersion": game_data.data_version,
            "buildingBuffCount": len(building.get("buffs") or {}),
            "buildingCharCount": len(building.get("chars") or {}),
            "characterCount": len(game_data.characters),
            "itemCount": len(game_data.items),
        }
    )


def candidate_cache_key(
    *,
    candidate_id: str,
    layout: Layout,
    mode: str,
    count: int,
    hours: int,
    profile: str,
    allow_upgrades: bool,
    anchor_preference: list[str],
    forced_targets: tuple[list[str], list[str]] | None,
    forced_insertion_groups: list[ShiftInsertionGroup] | None,
    candidate_drone_policy: str,
    optimize_drone_policy: str,
    drone_reference_daily: dict[str, Any] | None,
    candidate_shift_durations: list[float] | None,
    candidate_role: str,
    roster_key: str,
    data_key: str,
    shard_formula: str,
    right_side: str,
    min_lmd_gross: float,
    min_exp: float,
    min_orundum: float,
    pure_gold_target: float,
    pure_gold_tolerance: float,
    max_drone_cycle_repeats: int,
    upgrade_cost_weight: float,
    shift_times: list[str] | None,
) -> str:
    return stable_hash(
        {
            "version": 2,
            "candidateId": candidate_id,
            "layout": layout.raw,
            "layoutLabel": layout.label,
            "rightSidePreset": layout.right_side_preset,
            "mode": str(mode),
            "shiftCount": int(count),
            "shiftHours": int(hours),
            "shiftDurations": candidate_shift_durations,
            "shiftTimes": shift_times,
            "profile": profile,
            "candidateRole": candidate_role,
            "allowUpgrades": bool(allow_upgrades),
            "upgradeCostWeight": round(float(upgrade_cost_weight), 8),
            "anchorPreference": list(anchor_preference),
            "forcedTargets": forced_targets,
            "forcedInsertionGroups": [
                insertion_group_fingerprint(group)
                for group in (forced_insertion_groups or [])
            ],
            "candidateDronePolicy": candidate_drone_policy,
            "optimizeDronePolicy": optimize_drone_policy,
            "droneReferenceDaily": drone_reference_daily or {},
            "roster": roster_key,
            "gameData": data_key,
            "shardFormula": shard_formula,
            "rightSide": right_side,
            "minimums": {
                "lmdGross": round(float(min_lmd_gross), 6),
                "exp": round(float(min_exp), 6),
                "orundum": round(float(min_orundum), 6),
            },
            "pureGold": {
                "target": round(float(pure_gold_target), 6),
                "tolerance": round(float(pure_gold_tolerance), 6),
                "maxDroneCycleRepeats": max(1, int(max_drone_cycle_repeats)),
            },
            "optimizer": {
                "modelVersion": ScheduleOptimizer.optimizer_model_version(),
                "maxGroups": ScheduleOptimizer.diagnostic_insertion_group_limit(),
                "jointCandidateLimit": ScheduleOptimizer.joint_production_candidate_limit(),
            },
        }
    )


def insertion_group_fingerprint(group: ShiftInsertionGroup) -> dict[str, Any]:
    return {
        "id": group.id,
        "specs": [
            {
                "roomType": spec.room_type,
                "target": spec.target,
                "operatorName": spec.operator_name,
                "allowNoSkill": spec.allow_no_skill,
                "roomGroup": spec.room_group,
            }
            for spec in group.specs
        ],
    }


def stable_hash(data: Any) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_cache_policy(value: str) -> str:
    normalized = str(value or "auto").lower().replace("_", "-")
    if normalized not in {"auto", "refresh", "off"}:
        raise ValueError("cache_policy must be auto, refresh, or off.")
    return normalized


def normalize_jobs(value: str | int) -> int:
    if isinstance(value, int):
        return max(1, value)
    text = str(value or "1").strip().lower()
    if text == "auto":
        cpu = os.cpu_count() or 1
        return max(1, min(cpu - 1, 8))
    return max(1, int(text))


def recommend_schedules(
    game_data: GameData,
    roster: Iterable[RosterOperator],
    *,
    output_dir: Path,
    baseline_schedule: Path | None = None,
    layouts: Iterable[str] = DEFAULT_LAYOUTS,
    modes: Iterable[str] = DEFAULT_MODES,
    shift_count: int = 2,
    shift_counts: Iterable[int] | None = None,
    shift_hours: int = 12,
    shift_patterns: Iterable[tuple[int, int]] | None = None,
    shift_times: list[str] | None = None,
    shard_formula: str = "rock",
    drone_policy: str = "auto",
    upgrade_cost_weight: float = 0.015,
    right_side: str = "full",
    min_lmd_gross: float = 0.0,
    min_exp: float = 0.0,
    min_orundum: float = 0.0,
    include_upgrades: bool = True,
    pure_gold_target: float = DEFAULT_PURE_GOLD_TARGET_PER_DAY,
    pure_gold_tolerance: float = DEFAULT_PURE_GOLD_TOLERANCE,
    max_drone_cycle_repeats: int = DEFAULT_MAX_DRONE_CYCLE_REPEATS,
    jobs: str | int = 1,
    cache_policy: str = "auto",
    profile_runtime: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir = output_dir / "candidate_schedules"
    candidate_dir.mkdir(parents=True, exist_ok=True)

    roster_list = list(roster)
    layout_list = list(layouts)
    mode_list = list(modes)
    search_layout_list = expanded_layout_tokens(layout_list, mode_list)
    pattern_list = normalize_shift_patterns(
        shift_patterns=shift_patterns,
        shift_counts=shift_counts,
        shift_count=shift_count,
        shift_hours=shift_hours,
    )
    cache_policy = normalize_cache_policy(cache_policy)
    worker_count = normalize_jobs(jobs)
    roster_cache_key = roster_fingerprint(roster_list)
    data_cache_key = data_cache_fingerprint(game_data)

    baseline = score_baseline(
        baseline_schedule,
        game_data,
        roster_list,
        shard_formula=shard_formula,
        drone_policy=drone_policy,
        upgrade_cost_weight=upgrade_cost_weight,
        right_side=right_side,
        pure_gold_target=pure_gold_target,
        pure_gold_tolerance=pure_gold_tolerance,
    )
    baseline_score = baseline["score"] if baseline else None

    profiles = [
        ("current", "当前练度", False, 0.0),
        ("upgrades_raw", "补练最高产出", True, 0.0),
        ("upgrades_cost_adjusted", "补练成本调整", True, upgrade_cost_weight),
    ]
    maxed_roster = not roster_has_upgrade_potential(game_data, roster_list)
    profiles_to_optimize = profiles if include_upgrades and not maxed_roster else profiles[:1]
    candidates: list[CandidateEvaluation] = []
    skipped: list[dict[str, str]] = []
    yituliu_checks = recommendation_yituliu_case_checks()

    def replacement_drone_pure_gold_policy(
        report: ProductionReport,
        candidate_drone_policy: str,
    ) -> dict[str, Any]:
        quality = pure_gold_balance_quality(
            report.dailyExpected,
            target=pure_gold_target,
            tolerance=pure_gold_tolerance,
        )
        normalized_policy = str(candidate_drone_policy).lower().replace("_", "-")
        if normalized_policy != "auto":
            status = "not_applicable_drone_policy"
            reason = "Pure Gold balancing is applied only by --drone-policy auto."
        else:
            status = str(quality["status"])
            reason = ""
        return {
            "version": 1,
            "scope": "drone_allocation_only",
            "targetPerDay": quality["targetPerDay"],
            "tolerancePerDay": quality["tolerancePerDay"],
            "externalPureGoldAssumptionPerDay": quality["externalPureGoldAssumptionPerDay"],
            "status": status,
            "reason": reason,
            "repeatCount": 1,
            "cycleRepeated": False,
            "maxDroneCycleRepeats": max(1, int(max_drone_cycle_repeats)),
            "finalPureGoldDelta": quality["pureGoldDelta"],
            "finalDeltaFromTarget": quality["deltaFromTarget"],
            "finalAbsDeltaFromTarget": quality["absDeltaFromTarget"],
            "withinTolerance": quality["withinTolerance"],
        }

    def generate_candidate(
        optimizer: ScheduleOptimizer,
        *,
        candidate_id: str,
        layout: Layout,
        mode: str,
        count: int,
        hours: int,
        profile: str,
        profile_label: str,
        allow_upgrades: bool,
        anchor_preference: list[str],
        forced_targets: tuple[list[str], list[str]] | None = None,
        expected_target_counts: dict[str, int] | None = None,
        forced_insertion_groups: list[ShiftInsertionGroup] | None = None,
        candidate_drone_policy: str | None = None,
        drone_reference_daily: dict[str, Any] | None = None,
        candidate_shift_durations: list[float] | None = None,
        candidate_role: str = "primary",
    ) -> CandidateEvaluation | None:
        candidate_drone_policy = candidate_drone_policy or drone_policy
        optimize_drone_policy = (
            reference_fit_seed_drone_policy(drone_reference_daily or {})
            if candidate_drone_policy == "reference-fit"
            else candidate_drone_policy
        )
        export_path = candidate_dir / f"{candidate_id}.json"
        profile_weight = 0.0 if profile != "upgrades_cost_adjusted" else upgrade_cost_weight
        cache_key = candidate_cache_key(
            candidate_id=candidate_id,
            layout=layout,
            mode=mode,
            count=count,
            hours=hours,
            profile=profile,
            allow_upgrades=allow_upgrades,
            anchor_preference=anchor_preference,
            forced_targets=forced_targets,
            forced_insertion_groups=forced_insertion_groups,
            candidate_drone_policy=candidate_drone_policy,
            optimize_drone_policy=optimize_drone_policy,
            drone_reference_daily=drone_reference_daily,
            candidate_shift_durations=candidate_shift_durations,
            candidate_role=candidate_role,
            roster_key=roster_cache_key,
            data_key=data_cache_key,
            shard_formula=shard_formula,
            right_side=right_side,
            min_lmd_gross=min_lmd_gross,
            min_exp=min_exp,
            min_orundum=min_orundum,
            pure_gold_target=pure_gold_target,
            pure_gold_tolerance=pure_gold_tolerance,
            max_drone_cycle_repeats=max_drone_cycle_repeats,
            upgrade_cost_weight=profile_weight,
            shift_times=shift_times,
        )
        if cache_policy == "auto":
            cached = load_cached_candidate_evaluation(
                export_path,
                candidate_id=candidate_id,
                expected_layout_raw=layout.raw,
                allow_upgrades=allow_upgrades,
                profile=profile,
                profile_label=profile_label,
                baseline_score=baseline_score,
                output_dir=output_dir,
                expected_max_groups=ScheduleOptimizer.diagnostic_insertion_group_limit(),
                expected_joint_candidate_limit=ScheduleOptimizer.joint_production_candidate_limit(),
                expected_optimizer_model_version=ScheduleOptimizer.optimizer_model_version(),
                expected_anchor_count=len(anchor_preference),
                expected_target_counts=expected_target_counts,
                expected_pure_gold_target=pure_gold_target,
                expected_pure_gold_tolerance=pure_gold_tolerance,
                expected_max_drone_cycle_repeats=max_drone_cycle_repeats,
                candidate_role=candidate_role,
                expected_candidate_cache_key=cache_key,
            )
            if cached is not None:
                candidates.append(cached)
                return cached
        try:
            result = optimizer.optimize(
                layout,
                mode=mode,
                shift_count=count,
                shift_hours=hours,
                shift_durations=candidate_shift_durations,
                shift_times=shift_times,
                drone_policy=optimize_drone_policy,
                min_lmd_gross=min_lmd_gross,
                min_exp=min_exp,
                min_orundum=min_orundum,
                operator_anchor_preference=anchor_preference,
                forced_targets=forced_targets,
                forced_insertion_groups=forced_insertion_groups,
                pure_gold_target=pure_gold_target,
                pure_gold_tolerance=pure_gold_tolerance,
                max_drone_cycle_repeats=max_drone_cycle_repeats,
                profile_runtime=profile_runtime,
            )
            if candidate_drone_policy == "reference-fit":
                reference_report = ProductionSimulator(
                    game_data,
                    shard_formula=shard_formula,
                    drone_policy="reference-fit",
                    calibration_profile="guide",
                    reference_expected_daily=drone_reference_daily or {},
                    pure_gold_target=pure_gold_target,
                    pure_gold_tolerance=pure_gold_tolerance,
                ).evaluate(result.shifts)
                result = replace(
                    result,
                    score=reference_report.score,
                    score_breakdown=reference_report.scoreBreakdown,
                    production_report=reference_report,
                    drone_policy="reference-fit",
                    diagnostic_insertion_search={
                        **result.diagnostic_insertion_search,
                        "pureGoldBalancePolicy": replacement_drone_pure_gold_policy(
                            reference_report,
                            "reference-fit",
                        ),
                    },
                )
        except ValueError as exc:
            skipped.append(
                {
                    "layout": layout.label,
                    "mode": str(mode),
                    "shiftCount": str(count),
                    "profile": profile,
                    "candidateId": candidate_id,
                    "reason": str(exc),
                }
            )
            return None
        result.diagnostic_insertion_search.setdefault("cacheValidation", {})[
            "candidateCacheKey"
        ] = cache_key
        write_result_json(export_path, result, game_data)
        summary = result_summary(
            candidate_id,
            result,
            allow_upgrades=allow_upgrades,
            profile=profile,
            profile_label=profile_label,
            baseline_score=baseline_score,
            export_path=export_path,
            output_dir=output_dir,
            candidate_role=candidate_role,
        )
        candidate = CandidateEvaluation(
            id=candidate_id,
            result=result,
            allow_upgrades=allow_upgrades,
            profile=profile,
            profile_label=profile_label,
            summary=summary,
            export_path=export_path,
        )
        candidates.append(candidate)
        return candidate

    def derive_drone_candidate(
        source: CandidateEvaluation,
        *,
        candidate_id: str,
        candidate_drone_policy: str,
        drone_reference_daily: dict[str, Any] | None = None,
    ) -> CandidateEvaluation:
        export_path = candidate_dir / f"{candidate_id}.json"
        report = ProductionSimulator(
            game_data,
            shard_formula=shard_formula,
            drone_policy=candidate_drone_policy,
            calibration_profile="guide",
            reference_expected_daily=(
                drone_reference_daily if candidate_drone_policy == "reference-fit" else None
            ),
            pure_gold_target=pure_gold_target,
            pure_gold_tolerance=pure_gold_tolerance,
        ).evaluate(source.result.shifts)
        result = replace(
            source.result,
            score=report.score,
            score_breakdown=report.scoreBreakdown,
            production_report=report,
            drone_policy=candidate_drone_policy,
            diagnostic_insertion_search={
                **source.result.diagnostic_insertion_search,
                "pureGoldBalancePolicy": replacement_drone_pure_gold_policy(
                    report,
                    candidate_drone_policy,
                ),
            },
        )
        result.diagnostic_insertion_search.setdefault("cacheValidation", {})[
            "candidateCacheKey"
        ] = stable_hash(
            {
                "version": 1,
                "sourceCandidateId": source.id,
                "derivedCandidateId": candidate_id,
                "candidateDronePolicy": candidate_drone_policy,
                "droneReferenceDaily": drone_reference_daily or {},
                "sourceCandidateCacheKey": (
                    (source.result.diagnostic_insertion_search.get("cacheValidation") or {}).get(
                        "candidateCacheKey"
                    )
                ),
            }
        )
        write_result_json(export_path, result, game_data)
        summary = result_summary(
            candidate_id,
            result,
            allow_upgrades=source.allow_upgrades,
            profile=source.profile,
            profile_label=source.profile_label,
            baseline_score=baseline_score,
            export_path=export_path,
            output_dir=output_dir,
            candidate_role="reference_drone_variant",
        )
        candidate = CandidateEvaluation(
            id=candidate_id,
            result=result,
            allow_upgrades=source.allow_upgrades,
            profile=source.profile,
            profile_label=source.profile_label,
            summary=summary,
            export_path=export_path,
        )
        candidates.append(candidate)
        return candidate

    if worker_count > 1:
        parallel_specs: list[CandidateSpec] = []
        drone_derivations: list[dict[str, Any]] = []
        for profile, profile_label, allow_upgrades, profile_weight in profiles_to_optimize:
            for raw_layout in search_layout_list:
                try:
                    layout_base, layout_variant = split_layout_token(raw_layout)
                    layout = apply_right_side_preset(
                        apply_layout_variant(parse_layout(layout_base), layout_variant),
                        right_side,
                    )
                except ValueError as exc:
                    skipped.append(
                        {
                            "layout": str(raw_layout),
                            "profile": profile,
                            "reason": str(exc),
                        }
                    )
                    continue
                for mode in mode_list:
                    if layout.raw == "342-guide-orundum" and safe_normalize_mode(mode) != "max_orundum":
                        continue
                    for count, hours in pattern_list:
                        if layout.raw == "342-guide-orundum" and (count, hours) != (2, 12):
                            skipped.append(
                                {
                                    "layout": layout.raw,
                                    "mode": str(mode),
                                    "shiftCount": str(count),
                                    "profile": profile,
                                    "reason": "342 guide Orundum variant is scoped to the 2x12 reference image.",
                                }
                            )
                            continue
                        candidate_mode = safe_normalize_mode(mode)
                        candidate_id = (
                            f"{safe_layout_token(layout.raw)}_{candidate_mode}_{count}x{hours}_{profile}"
                        )
                        anchor_preference = guide_operator_anchor_preference(
                            layout.label,
                            mode,
                            count,
                            hours,
                        )
                        parallel_specs.append(
                            CandidateSpec(
                                candidate_id=candidate_id,
                                layout=layout,
                                mode=mode,
                                count=count,
                                hours=hours,
                                profile=profile,
                                profile_label=profile_label,
                                allow_upgrades=allow_upgrades,
                                profile_weight=profile_weight,
                                anchor_preference=anchor_preference,
                            )
                        )
                        for reference in reference_target_variants(
                            yituliu_checks,
                            layout=layout,
                            mode=candidate_mode,
                            shift_count=count,
                            shift_hours=hours,
                        ):
                            forced_targets = reference["targets"]
                            ref_candidate_id = (
                                f"{safe_layout_token(layout.raw)}_{candidate_mode}_{count}x{hours}_"
                                f"{profile}_ref_{safe_layout_token(str(reference['id']))}"
                            )
                            parallel_specs.append(
                                CandidateSpec(
                                    candidate_id=ref_candidate_id,
                                    layout=layout,
                                    mode=mode,
                                    count=count,
                                    hours=hours,
                                    profile=profile,
                                    profile_label=profile_label,
                                    allow_upgrades=allow_upgrades,
                                    profile_weight=profile_weight,
                                    anchor_preference=reference["operatorAnchors"],
                                    forced_targets=forced_targets,
                                    expected_target_counts=reference["targetCounts"],
                                    forced_insertion_groups=reference["insertionGroups"],
                                    candidate_shift_durations=reference.get("shiftHours"),
                                    candidate_role="reference_target",
                                )
                            )
                            reference_policies = (
                                []
                                if str(drone_policy).lower().replace("_", "-") == "auto"
                                else reference_drone_policies(reference.get("expectedDaily") or {})
                            )
                            for reference_drone_policy in reference_policies:
                                ref_drone_candidate_id = (
                                    f"{ref_candidate_id}_drone_"
                                    f"{safe_layout_token(reference_drone_policy)}"
                                )
                                if reference_drone_policy == "reference-fit":
                                    parallel_specs.append(
                                        CandidateSpec(
                                            candidate_id=ref_drone_candidate_id,
                                            layout=layout,
                                            mode=mode,
                                            count=count,
                                            hours=hours,
                                            profile=profile,
                                            profile_label=profile_label,
                                            allow_upgrades=allow_upgrades,
                                            profile_weight=profile_weight,
                                            anchor_preference=reference["operatorAnchors"],
                                            forced_targets=forced_targets,
                                            expected_target_counts=reference["targetCounts"],
                                            forced_insertion_groups=reference["insertionGroups"],
                                            candidate_drone_policy=reference_drone_policy,
                                            drone_reference_daily=reference["expectedDaily"],
                                            candidate_shift_durations=reference.get("shiftHours"),
                                            candidate_role="reference_drone_variant",
                                        )
                                    )
                                    continue
                                drone_derivations.append(
                                    {
                                        "sourceId": ref_candidate_id,
                                        "candidateId": ref_drone_candidate_id,
                                        "policy": reference_drone_policy,
                                        "expectedDaily": reference.get("expectedDaily") or {},
                                    }
                                )
        worker_config = CandidateWorkerConfig(
            game_data=game_data,
            roster=roster_list,
            output_dir=output_dir,
            candidate_dir=candidate_dir,
            baseline_score=baseline_score,
            shard_formula=shard_formula,
            drone_policy=drone_policy,
            right_side=right_side,
            shift_times=shift_times,
            min_lmd_gross=min_lmd_gross,
            min_exp=min_exp,
            min_orundum=min_orundum,
            pure_gold_target=pure_gold_target,
            pure_gold_tolerance=pure_gold_tolerance,
            max_drone_cycle_repeats=max_drone_cycle_repeats,
            cache_policy=cache_policy,
            roster_cache_key=roster_cache_key,
            data_cache_key=data_cache_key,
            profile_runtime=profile_runtime,
        )
        if parallel_specs:
            with ProcessPoolExecutor(
                max_workers=worker_count,
                initializer=init_candidate_worker,
                initargs=(worker_config,),
            ) as executor:
                for item in executor.map(evaluate_candidate_spec_from_worker, parallel_specs):
                    candidate = item.get("candidate")
                    if candidate is not None:
                        candidates.append(candidate)
                    skipped_item = item.get("skipped")
                    if skipped_item is not None:
                        skipped.append(skipped_item)
        candidates_by_id = {candidate.id: candidate for candidate in candidates}
        for derivation in drone_derivations:
            source = candidates_by_id.get(str(derivation["sourceId"]))
            if source is None:
                continue
            derived = derive_drone_candidate(
                source,
                candidate_id=str(derivation["candidateId"]),
                candidate_drone_policy=str(derivation["policy"]),
                drone_reference_daily=dict(derivation.get("expectedDaily") or {}),
            )
            candidates_by_id[derived.id] = derived
        profiles_to_optimize = []

    for profile, profile_label, allow_upgrades, profile_weight in profiles_to_optimize:
        optimizer = ScheduleOptimizer(
            game_data,
            roster_list,
            allow_upgrades=allow_upgrades,
            upgrade_cost_weight=profile_weight,
            shard_formula=shard_formula,
        )
        for raw_layout in search_layout_list:
            try:
                layout_base, layout_variant = split_layout_token(raw_layout)
                layout = apply_right_side_preset(
                    apply_layout_variant(parse_layout(layout_base), layout_variant),
                    right_side,
                )
            except ValueError as exc:
                skipped.append(
                    {
                        "layout": str(raw_layout),
                        "profile": profile,
                        "reason": str(exc),
                    }
                )
                continue
            for mode in mode_list:
                if layout.raw == "342-guide-orundum" and safe_normalize_mode(mode) != "max_orundum":
                    continue
                for count, hours in pattern_list:
                    if layout.raw == "342-guide-orundum" and (count, hours) != (2, 12):
                        skipped.append(
                            {
                                "layout": layout.raw,
                                "mode": str(mode),
                                "shiftCount": str(count),
                                "profile": profile,
                                "reason": "342 guide Orundum variant is scoped to the 2x12 reference image.",
                            }
                        )
                        continue
                    candidate_mode = safe_normalize_mode(mode)
                    candidate_id = (
                        f"{safe_layout_token(layout.raw)}_{candidate_mode}_{count}x{hours}_{profile}"
                    )
                    anchor_preference = guide_operator_anchor_preference(
                        layout.label,
                        mode,
                        count,
                        hours,
                    )
                    base_candidate = generate_candidate(
                        optimizer,
                        candidate_id=candidate_id,
                        layout=layout,
                        mode=mode,
                        count=count,
                        hours=hours,
                        profile=profile,
                        profile_label=profile_label,
                        allow_upgrades=allow_upgrades,
                        anchor_preference=anchor_preference,
                    )
                    if base_candidate is None:
                        continue
                    for reference in reference_target_variants(
                        yituliu_checks,
                        layout=layout,
                        mode=candidate_mode,
                        shift_count=count,
                        shift_hours=hours,
                    ):
                        forced_targets = reference["targets"]
                        ref_candidate_id = (
                            f"{safe_layout_token(layout.raw)}_{candidate_mode}_{count}x{hours}_"
                            f"{profile}_ref_{safe_layout_token(str(reference['id']))}"
                        )
                        ref_candidate = generate_candidate(
                            optimizer,
                            candidate_id=ref_candidate_id,
                            layout=layout,
                            mode=mode,
                            count=count,
                            hours=hours,
                            profile=profile,
                            profile_label=profile_label,
                            allow_upgrades=allow_upgrades,
                            anchor_preference=reference["operatorAnchors"],
                            forced_targets=forced_targets,
                            expected_target_counts=reference["targetCounts"],
                            forced_insertion_groups=reference["insertionGroups"],
                            candidate_shift_durations=reference.get("shiftHours"),
                            candidate_role="reference_target",
                        )
                        if ref_candidate is None:
                            continue
                        reference_policies = (
                            []
                            if str(drone_policy).lower().replace("_", "-") == "auto"
                            else reference_drone_policies(reference.get("expectedDaily") or {})
                        )
                        for reference_drone_policy in reference_policies:
                            ref_drone_candidate_id = (
                                f"{ref_candidate_id}_drone_"
                                f"{safe_layout_token(reference_drone_policy)}"
                            )
                            if reference_drone_policy == "reference-fit":
                                generate_candidate(
                                    optimizer,
                                    candidate_id=ref_drone_candidate_id,
                                    layout=layout,
                                    mode=mode,
                                    count=count,
                                    hours=hours,
                                    profile=profile,
                                    profile_label=profile_label,
                                    allow_upgrades=allow_upgrades,
                                    anchor_preference=reference["operatorAnchors"],
                                    forced_targets=forced_targets,
                                    expected_target_counts=reference["targetCounts"],
                                    forced_insertion_groups=reference["insertionGroups"],
                                    candidate_drone_policy=reference_drone_policy,
                                    drone_reference_daily=reference["expectedDaily"],
                                    candidate_shift_durations=reference.get("shiftHours"),
                                    candidate_role="reference_drone_variant",
                                )
                                continue
                            derive_drone_candidate(
                                ref_candidate,
                                candidate_id=ref_drone_candidate_id,
                                candidate_drone_policy=reference_drone_policy,
                                drone_reference_daily=(
                                    reference["expectedDaily"]
                                    if reference_drone_policy == "reference-fit"
                                    else None
                                ),
                            )

    if maxed_roster or not include_upgrades:
        candidates.extend(
            duplicate_maxed_profile_candidates(
                candidates,
                profiles[1:],
                candidate_dir,
                output_dir,
                game_data,
                baseline_score,
            )
        )

    if not candidates:
        raise ValueError("No feasible recommendation candidates were generated.")

    intent = recommendation_intent(
        baseline,
        layout_list,
        mode_list,
        min_orundum=min_orundum,
    )
    for candidate in candidates:
        candidate.summary["targetFit"] = candidate_target_fit(candidate.summary, intent)

    best_current = best_candidate(candidates, "current")
    best_upgrades = best_candidate(candidates, "upgrades_raw")
    best_cost_adjusted = best_candidate(candidates, "upgrades_cost_adjusted")
    best_overall = max(
        [best_current, best_upgrades, best_cost_adjusted],
        key=lambda item: item.result.score,
    )
    best_target_current = best_target_candidate(candidates, "current")
    best_target_upgrades = best_target_candidate(candidates, "upgrades_raw")
    best_target_cost_adjusted = best_target_candidate(candidates, "upgrades_cost_adjusted")

    current_path = output_dir / "best_current_schedule.json"
    upgrades_path = output_dir / "best_upgrades_schedule.json"
    cost_adjusted_path = output_dir / "best_upgrades_cost_adjusted_schedule.json"
    write_result_json(current_path, best_current.result, game_data)
    write_result_json(upgrades_path, best_upgrades.result, game_data)
    write_result_json(cost_adjusted_path, best_cost_adjusted.result, game_data)
    target_current_path = output_dir / "best_target_compatible_current_schedule.json"
    target_upgrades_path = output_dir / "best_target_compatible_upgrades_schedule.json"
    target_cost_adjusted_path = output_dir / "best_target_compatible_cost_adjusted_schedule.json"
    if best_target_current:
        write_result_json(target_current_path, best_target_current.result, game_data)
    if best_target_upgrades:
        write_result_json(target_upgrades_path, best_target_upgrades.result, game_data)
    if best_target_cost_adjusted:
        write_result_json(target_cost_adjusted_path, best_target_cost_adjusted.result, game_data)

    upgrade_requirements = [upgrade_to_dict(item) for item in collect_upgrades(best_upgrades.result)]
    upgrade_requirements_cost_adjusted = [
        upgrade_to_dict(item) for item in collect_upgrades(best_cost_adjusted.result)
    ]
    upgrade_path = output_dir / "upgrade_requirements.json"
    upgrade_cost_path = output_dir / "upgrade_requirements_cost_adjusted.json"
    upgrade_xlsx_path = output_dir / "upgrade_requirements.xlsx"
    upgrade_cost_xlsx_path = output_dir / "upgrade_requirements_cost_adjusted.xlsx"
    write_json(upgrade_path, upgrade_requirements)
    write_json(upgrade_cost_path, upgrade_requirements_cost_adjusted)
    write_upgrade_requirements_xlsx(upgrade_xlsx_path, upgrade_requirements)
    write_upgrade_requirements_xlsx(upgrade_cost_xlsx_path, upgrade_requirements_cost_adjusted)

    manual_check = dict(baseline, source=str(baseline_schedule)) if baseline and baseline_schedule else None
    diagnostic_candidates = (
        [
            best_current,
            best_target_current,
        ]
        if not include_upgrades
        else [
            best_current,
            best_upgrades,
            best_cost_adjusted,
            best_target_current,
            best_target_upgrades,
            best_target_cost_adjusted,
        ]
    )
    diagnostics_by_solution = build_diagnostics_by_solution(
        game_data,
        roster_list,
        candidates,
        diagnostic_candidates=diagnostic_candidates,
        shard_formula=shard_formula,
    )
    diagnostics = primary_diagnostics(diagnostics_by_solution, best_upgrades.id)
    anomaly_candidates = aggregate_anomaly_candidates(diagnostics_by_solution)
    diagnostic_insertion_coverage = aggregate_diagnostic_insertion_coverage(
        diagnostics_by_solution
    )

    report: dict[str, Any] = {
        "format": "arknights-schedule-recommendation-report",
        "formatVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataVersion": game_data.data_version,
        "inputs": {
            "layouts": layout_list,
            "modes": mode_list,
            "shiftCount": shift_count,
            "shiftCounts": sorted({count for count, _ in pattern_list}),
            "shiftHours": shift_hours,
            "shiftPatterns": [
                {"shiftCount": count, "shiftHours": hours}
                for count, hours in pattern_list
            ],
            "shiftTimes": shift_times,
            "shardFormula": shard_formula,
            "dronePolicy": drone_policy,
            "upgradeCostWeight": upgrade_cost_weight,
            "includeUpgrades": include_upgrades,
            "rightSide": right_side,
            "jobs": worker_count,
            "cachePolicy": cache_policy,
            "profileRuntime": profile_runtime,
            "minimums": {
                "lmdGross": min_lmd_gross,
                "exp": min_exp,
                "orundum": min_orundum,
            },
        },
        "baseline": baseline,
        "presetContracts": preset_contracts(),
        "recommendationIntent": intent,
        "yituliuCaseChecks": yituliu_checks,
        "manualScheduleCheck": manual_check,
        "counterintuitiveDiagnostics": diagnostics["counterintuitiveDiagnostics"],
        "anomalyCandidates": anomaly_candidates,
        "diagnosticInsertionCoverage": diagnostic_insertion_coverage,
        "referenceBenchmark": reference_benchmark_summary(
            candidates,
            [
                best_target_current,
                best_target_upgrades,
                best_target_cost_adjusted,
            ],
            yituliu_checks,
            diagnostic_insertion_coverage,
            roster_list,
        ),
        "diagnostics": diagnostics,
        "diagnosticsBySolution": diagnostics_by_solution,
        "decision": recommendation_decision(
            best_current,
            best_upgrades,
            best_cost_adjusted,
            baseline_score,
        ),
        "trainingImpact": training_impact(best_current, best_upgrades, "upgrade"),
        "costAdjustedTrainingImpact": training_impact(
            best_current,
            best_cost_adjusted,
            "cost_adjusted",
        ),
        "baselineComparison": baseline_comparison(
            baseline_score,
            best_current,
            best_upgrades,
            best_cost_adjusted,
            best_overall,
        ),
        "bestCurrent": best_current.summary,
        "bestWithUpgrades": best_upgrades.summary,
        "bestWithUpgradesCostAdjusted": best_cost_adjusted.summary,
        "bestOverall": best_overall.summary,
        "bestTargetCompatibleCurrent": (
            best_target_current.summary if best_target_current else None
        ),
        "bestTargetCompatibleWithUpgrades": (
            best_target_upgrades.summary if best_target_upgrades else None
        ),
        "bestTargetCompatibleCostAdjusted": (
            best_target_cost_adjusted.summary if best_target_cost_adjusted else None
        ),
        "objectiveComparison": objective_comparison(
            intent,
            [
                ("current", "当前练度", best_current, best_target_current),
                ("upgrades_raw", "补练最高产出", best_upgrades, best_target_upgrades),
                (
                    "upgrades_cost_adjusted",
                    "补练成本调整",
                    best_cost_adjusted,
                    best_target_cost_adjusted,
                ),
            ],
        ),
        "droneUsagePlan": collect_drone_usage_plan(
            [
                best_target_current,
                best_current,
                best_target_cost_adjusted,
                best_cost_adjusted,
            ]
        ),
        "candidates": [
            candidate.summary
            for candidate in sorted(candidates, key=lambda item: item.result.score, reverse=True)
        ],
        "skipped": skipped,
        "upgradeRequirements": upgrade_requirements,
        "upgradeRequirementsCostAdjusted": upgrade_requirements_cost_adjusted,
        "writtenFiles": {
            "bestCurrentSchedule": str(current_path),
            "bestUpgradesSchedule": str(upgrades_path),
            "bestUpgradesCostAdjustedSchedule": str(cost_adjusted_path),
            **(
                {"bestTargetCompatibleCurrentSchedule": str(target_current_path)}
                if best_target_current
                else {}
            ),
            **(
                {"bestTargetCompatibleUpgradesSchedule": str(target_upgrades_path)}
                if best_target_upgrades
                else {}
            ),
            **(
                {"bestTargetCompatibleCostAdjustedSchedule": str(target_cost_adjusted_path)}
                if best_target_cost_adjusted
                else {}
            ),
            "upgradeRequirements": str(upgrade_path),
            "upgradeRequirementsCostAdjusted": str(upgrade_cost_path),
            "upgradeRequirementsXlsx": str(upgrade_xlsx_path),
            "upgradeRequirementsCostAdjustedXlsx": str(upgrade_cost_xlsx_path),
        },
    }

    report_path = output_dir / "recommendation_report.json"
    html_path = output_dir / "recommendation_report.html"
    report["writtenFiles"]["report"] = str(report_path)
    report["writtenFiles"]["htmlReport"] = str(html_path)
    write_json(report_path, report)
    html_path.write_text(render_html_report(report, output_dir), encoding="utf-8")
    return report


def best_candidate(candidates: list[CandidateEvaluation], profile: str) -> CandidateEvaluation:
    items = [candidate for candidate in candidates if candidate.profile == profile]
    if not items:
        raise ValueError(f"No feasible candidates for profile {profile}.")
    primary_items = [
        candidate
        for candidate in items
        if str(candidate.summary.get("candidateRole") or "primary") == "primary"
    ]
    return max(primary_items or items, key=lambda item: item.result.score)


def roster_has_upgrade_potential(game_data: GameData, roster: list[RosterOperator]) -> bool:
    recruited = [operator for operator in roster if operator.recruited]
    if len(recruited) >= 400 and all(
        operator.potential == 6
        and operator.skill_level >= 7
        and operator.masteries == (3, 3, 3)
        and all(value >= 3 for value in operator.modules.values())
        for operator in recruited
    ):
        return False
    char_id_by_name = game_data.char_id_by_name
    for operator in recruited:
        char_id = char_id_by_name.get(operator.name)
        character = game_data.characters.get(char_id or "", {})
        phases = character.get("phases") or []
        if not phases:
            continue
        max_elite = len(phases) - 1
        max_level = int((phases[max_elite] or {}).get("maxLevel") or 1)
        if operator.elite < max_elite or operator.level < max_level:
            return True
        if operator.skill_level < 7:
            return True
        if any(mastery < 3 for mastery in operator.masteries):
            return True
    return False


def duplicate_maxed_profile_candidates(
    candidates: list[CandidateEvaluation],
    duplicate_profiles: list[tuple[str, str, bool, float]],
    candidate_dir: Path,
    output_dir: Path,
    game_data: GameData,
    baseline_score: float | None,
) -> list[CandidateEvaluation]:
    current_candidates = [candidate for candidate in candidates if candidate.profile == "current"]
    duplicates: list[CandidateEvaluation] = []
    for source in current_candidates:
        current_suffix = "_current"
        for profile, profile_label, allow_upgrades, _profile_weight in duplicate_profiles:
            candidate_id = (
                source.id[: -len(current_suffix)] + f"_{profile}"
                if source.id.endswith(current_suffix)
                else f"{source.id}_{profile}"
            )
            export_path = candidate_dir / f"{candidate_id}.json"
            write_result_json(export_path, source.result, game_data)
            summary = result_summary(
                candidate_id,
                source.result,
                allow_upgrades=allow_upgrades,
                profile=profile,
                profile_label=profile_label,
                baseline_score=baseline_score,
                export_path=export_path,
                output_dir=output_dir,
                candidate_role=str(source.summary.get("candidateRole") or "primary"),
            )
            duplicates.append(
                CandidateEvaluation(
                    id=candidate_id,
                    result=source.result,
                    allow_upgrades=allow_upgrades,
                    profile=profile,
                    profile_label=profile_label,
                    summary=summary,
                    export_path=export_path,
                )
            )
    return duplicates


def load_cached_candidate_evaluation(
    export_path: Path,
    *,
    candidate_id: str,
    expected_layout_raw: str,
    allow_upgrades: bool,
    profile: str,
    profile_label: str,
    baseline_score: float | None,
    output_dir: Path,
    expected_max_groups: int,
    expected_joint_candidate_limit: int,
    expected_optimizer_model_version: int,
    expected_anchor_count: int,
    expected_target_counts: dict[str, int] | None = None,
    expected_pure_gold_target: float = DEFAULT_PURE_GOLD_TARGET_PER_DAY,
    expected_pure_gold_tolerance: float = DEFAULT_PURE_GOLD_TOLERANCE,
    expected_max_drone_cycle_repeats: int = DEFAULT_MAX_DRONE_CYCLE_REPEATS,
    candidate_role: str = "primary",
    expected_candidate_cache_key: str | None = None,
) -> CandidateEvaluation | None:
    if not export_path.exists():
        return None
    try:
        data = json.loads(export_path.read_text(encoding="utf-8"))
        search = (data.get("analysis") or {}).get("diagnosticInsertionSearch") or {}
        if int(search.get("optimizerModelVersion") or 0) != expected_optimizer_model_version:
            return None
        if int(search.get("maxGroups") or 0) != expected_max_groups:
            return None
        cached_joint_limit = search.get("jointProductionCandidateLimit")
        if (
            cached_joint_limit is not None
            and int(cached_joint_limit or 0) != expected_joint_candidate_limit
        ):
            return None
        if int(search.get("operatorAnchorPreferenceCount") or 0) != expected_anchor_count:
            return None
        cache_validation = search.get("cacheValidation") or {}
        cached_candidate_key = cache_validation.get("candidateCacheKey")
        if expected_candidate_cache_key is not None and cached_candidate_key != expected_candidate_cache_key:
            return None
        cached_pure_gold_target = search.get(
            "pureGoldTarget",
            cache_validation.get("pureGoldTarget"),
        )
        cached_pure_gold_tolerance = search.get(
            "pureGoldTolerance",
            cache_validation.get("pureGoldTolerance"),
        )
        cached_max_repeats = search.get(
            "maxDroneCycleRepeats",
            cache_validation.get("maxDroneCycleRepeats"),
        )
        if cached_pure_gold_target is None or round(float(cached_pure_gold_target), 6) != round(
            float(expected_pure_gold_target), 6
        ):
            return None
        if cached_pure_gold_tolerance is None or round(
            float(cached_pure_gold_tolerance), 6
        ) != round(float(expected_pure_gold_tolerance), 6):
            return None
        if cached_max_repeats is None or int(cached_max_repeats) != max(
            1, int(expected_max_drone_cycle_repeats)
        ):
            return None
        if not search.get("localQualityAudit"):
            return None
        summary = search.get("summary") or {}
        if int(summary.get("remainingImprovementCount") or 0) != 0:
            return None
        if not exported_schedule_capacity_valid(data):
            return None
        result = optimizer_result_from_export(data, expected_layout_raw=expected_layout_raw)
        if expected_target_counts is not None and target_counts(result) != expected_target_counts:
            return None
        if expected_target_counts is not None and (
            (search.get("targetOptionSearch") or {}).get("version") != 3
        ):
            return None
        summary = result_summary(
            candidate_id,
            result,
            allow_upgrades=allow_upgrades,
            profile=profile,
            profile_label=profile_label,
            baseline_score=baseline_score,
            export_path=export_path,
            output_dir=output_dir,
            candidate_role=candidate_role,
        )
        return CandidateEvaluation(
            id=candidate_id,
            result=result,
            allow_upgrades=allow_upgrades,
            profile=profile,
            profile_label=profile_label,
            summary=summary,
            export_path=export_path,
        )
    except Exception:
        return None


def optimizer_result_from_export(
    data: dict[str, Any], *, expected_layout_raw: str | None = None
) -> OptimizerResult:
    base = data.get("base") or {}
    mode = str((data.get("mode") or {}).get("id") or "normal")
    shifts = [shift_from_export(item) for item in data.get("shifts") or []]
    daily = production_vector_from_dict(data.get("dailyExpected") or {})
    analysis = data.get("analysis") or {}
    report = ProductionReport(
        dailyExpected=daily,
        scoreBreakdown=dict(data.get("scoreBreakdown") or {}),
        roomReports=list(analysis.get("roomReports") or []),
        unsupportedSkillEffects=list(analysis.get("unsupportedSkillEffects") or []),
        assumptions=list(analysis.get("assumptions") or []),
        calibrationProfile=str(analysis.get("calibrationProfile") or "guide"),
        sourceAssumptions=list(analysis.get("sourceAssumptions") or []),
        guideComparison=analysis.get("guideComparison"),
    )
    layout = Layout(
        raw=str(
            (expected_layout_raw or "").replace("_", "-")
            or base.get("layoutRaw")
            or base.get("layout")
            or data.get("base", {}).get("layout")
            or ""
        ),
        trading=int(base.get("trading") or 0),
        manufacture=int(base.get("manufacture") or 0),
        power=int(base.get("power") or 0),
        dormitory=int(base.get("dormitory") or 4),
        control=int(base.get("control") or 1),
        meeting=int(base.get("meeting") or 1),
        hire=int(base.get("hire") or 1),
        training=int(base.get("training") or 1),
        workshop=int(base.get("workshop") or 1),
        right_side_preset=str(base.get("rightSidePreset") or "full"),
    )
    return OptimizerResult(
        layout=layout,
        mode=mode,
        shift_hours=int(shifts[0].duration_hours if shifts else 12),
        shifts=shifts,
        score=float(data.get("score") or report.score),
        warnings=list(analysis.get("reminders") or []),
        score_breakdown=dict(data.get("scoreBreakdown") or {}),
        production_report=report,
        drone_policy="auto" if daily.droneUsed > 0.001 else "none",
        metric_profile=str(analysis.get("calibrationProfile") or "guide"),
        power_status=None,
        diagnostic_insertion_search=dict(analysis.get("diagnosticInsertionSearch") or {}),
        runtime_profile=dict(analysis.get("runtimeProfile") or {}),
    )


def shift_from_export(data: dict[str, Any]) -> ShiftPlan:
    return ShiftPlan(
        name=str(data.get("name") or ""),
        start=str(data.get("start") or ""),
        duration_hours=float(data.get("durationHours") or 12.0),
        rooms=[room_from_export(item) for item in data.get("rooms") or []],
        dormitories=[room_from_export(item) for item in data.get("dormitories") or []],
    )


def room_from_export(data: dict[str, Any]) -> RoomAssignment:
    return RoomAssignment(
        room_id=str(data.get("id") or ""),
        room_type=str(data.get("type") or ""),
        room_name=str(data.get("name") or data.get("type") or ""),
        target=data.get("target"),
        operators=[skill_from_export(item) for item in data.get("operators") or []],
        score=float(data.get("score") or 0.0),
        room_level=int(data.get("roomLevel") or 3),
        slots=None if data.get("slots") is None else int(data.get("slots")),
        product_capacity=(
            None if data.get("productCapacity") is None else int(data.get("productCapacity"))
        ),
        order_limit=None if data.get("orderLimit") is None else int(data.get("orderLimit")),
    )


def skill_from_export(data: dict[str, Any]) -> BaseSkill:
    return BaseSkill(
        char_id=str(data.get("charId") or ""),
        operator_name=str(data.get("name") or ""),
        room_type=str(data.get("roomType") or ""),
        buff_id=str(data.get("buffId") or ""),
        buff_name=str(data.get("buffName") or ""),
        description=str(data.get("description") or ""),
        efficiency=float(data.get("score") or 0.0),
        targets=tuple(str(item) for item in data.get("targets") or []),
        cond_elite=0,
        cond_level=1,
        unlocked=bool(data.get("unlocked", True)),
        complex_condition=bool(data.get("complexCondition", False)),
        parsed_score=float(data.get("score") or 0.0),
        upgrade=None,
    )


def production_vector_from_dict(data: dict[str, Any]) -> ProductionVector:
    return ProductionVector(
        lmdGross=float(data.get("lmdGross") or 0.0),
        lmdNet=float(data.get("lmdNet") or 0.0),
        exp=float(data.get("exp") or 0.0),
        orundum=float(data.get("orundum") or 0.0),
        officeSpeed=float(data.get("officeSpeed") or 0.0),
        meetingSpeed=float(data.get("meetingSpeed") or 0.0),
        pureGoldDelta=float(data.get("pureGoldDelta") or 0.0),
        shardDelta=float(data.get("shardDelta") or 0.0),
        materialCosts={
            str(key): float(value)
            for key, value in (data.get("materialCosts") or {}).items()
        },
        overflowLoss=float(data.get("overflowLoss") or 0.0),
        fatigueRisk=float(data.get("fatigueRisk") or 0.0),
        droneContribution={
            str(key): float(value)
            for key, value in (data.get("droneContribution") or {}).items()
        },
        droneCount=float(data.get("droneCount") or 0.0),
        droneUsed=float(data.get("droneUsed") or 0.0),
        droneGenerationBonusPercent=float(data.get("droneGenerationBonusPercent") or 0.0),
        droneTarget=dict(data.get("droneTarget") or {}),
        droneTargets=list(data.get("droneTargets") or []),
        cycleHours=float(data.get("cycleHours") or 24.0),
        dailyScale=float(data.get("dailyScale") or 1.0),
    )


def normalize_shift_patterns(
    *,
    shift_patterns: Iterable[tuple[int, int]] | None,
    shift_counts: Iterable[int] | None,
    shift_count: int,
    shift_hours: int,
) -> list[tuple[int, int]]:
    if shift_patterns is not None:
        raw_patterns = [(int(count), int(hours)) for count, hours in shift_patterns]
    elif shift_counts is not None:
        raw_patterns = [(int(count), int(shift_hours)) for count in shift_counts]
    else:
        raw_patterns = [(1, 24), (2, 12), (3, 8), (3, 12)]
    patterns: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for count, hours in raw_patterns:
        key = (max(1, count), hours)
        if key in seen:
            continue
        if hours <= 0:
            raise ValueError("Shift hours must be positive.")
        seen.add(key)
        patterns.append(key)
    return patterns


def split_layout_token(raw_layout: str) -> tuple[str, str | None]:
    raw = str(raw_layout).strip()
    digits = "".join(char for char in raw if char.isdigit())
    if len(digits) != 3:
        return raw, None
    suffix = raw[raw.find(digits) + len(digits):].strip("-_: ")
    return digits, suffix or None


def expanded_layout_tokens(layouts: list[str], modes: list[str]) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    normalized_modes = {safe_normalize_mode(mode) for mode in modes}
    for layout in layouts:
        raw = str(layout)
        if raw not in seen:
            seen.add(raw)
            tokens.append(raw)
        base, variant = split_layout_token(raw)
        if (
            base == "342"
            and variant is None
            and "max_orundum" in normalized_modes
            and "342-guide-orundum" not in seen
        ):
            seen.add("342-guide-orundum")
            tokens.append("342-guide-orundum")
    return tokens


def guide_operator_anchor_preference(
    layout_label: str,
    mode: str,
    shift_count: int,
    shift_hours: int,
) -> list[str]:
    normalized_mode = safe_normalize_mode(mode)
    anchors: list[str] = []
    for target in [*latest_yituliu_orundum_targets(), *yituliu_2026_06_image_label_cases()]:
        target_layout = str(target.get("layout") or "")
        if target_layout != str(layout_label):
            continue
        if int(target.get("shiftCount") or 0) != int(shift_count):
            continue
        if not reference_shift_hours_match_pattern(
            target.get("shiftHours"),
            shift_count=shift_count,
            shift_hours=shift_hours,
        ):
            continue
        target_mode = reference_mode(target)
        if target_mode is None and (target.get("expectedDaily") or {}).get("orundum") is not None:
            expected = target.get("expectedDaily") or {}
            target_mode = (
                "max_orundum"
                if target_layout == "342" and float(expected.get("exp") or 0.0) <= 0.001
                else "balanced_orundum"
            )
        if target_mode is None or target_mode != normalized_mode:
            continue
        visual = target.get("visualExtraction") or {}
        if target.get("mode") == "normal" and visual.get("status") == "manual_extraction_pending":
            continue
        anchors.extend(str(name) for name in target.get("operatorAnchors") or [])
    return list(dict.fromkeys(name for name in anchors if name))


def reference_target_variants(
    yituliu_checks: list[dict[str, Any]],
    *,
    layout: Layout,
    mode: str,
    shift_count: int,
    shift_hours: int,
) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for check in yituliu_checks:
        if str(check.get("layout") or "") != layout.label:
            continue
        if reference_mode(check) != mode:
            continue
        if int(check.get("shiftCount") or 0) != int(shift_count):
            continue
        if not reference_shift_hours_match_pattern(
            check.get("shiftHours"),
            shift_count=shift_count,
            shift_hours=shift_hours,
        ):
            continue
        target_lists = reference_target_lists(check)
        if target_lists is None:
            continue
        trading, manufacture = target_lists
        if len(trading) != layout.trading or len(manufacture) != layout.manufacture:
            continue
        anchors = [
            str(name)
            for name in check.get("operatorAnchors") or check.get("referenceOperatorAnchors") or []
            if str(name)
        ]
        variants.append(
            {
                "id": check.get("id"),
                "targets": target_lists,
                "targetCounts": target_counts_from_lists(target_lists),
                "operatorAnchors": list(dict.fromkeys(anchors)),
                "insertionGroups": reference_trade_insertion_groups(check),
                "expectedDaily": check.get("expectedDaily") or {},
                "shiftHours": normalized_reference_shift_hours(check.get("shiftHours")),
            }
        )
    return variants


def normalized_reference_shift_hours(raw: Any) -> list[float] | None:
    if not raw:
        return None
    if not isinstance(raw, list):
        return None
    hours = [float(item) for item in raw]
    return hours or None


def reference_shift_hours_match_pattern(
    raw: Any,
    *,
    shift_count: int,
    shift_hours: int,
) -> bool:
    hours = normalized_reference_shift_hours(raw)
    if not hours:
        return True
    if len(hours) != int(shift_count):
        return False
    if all(abs(hour - float(shift_hours)) < 0.001 for hour in hours):
        return True
    return abs(sum(hours) / len(hours) - float(shift_hours)) < 0.001


def reference_drone_policies(expected: dict[str, Any]) -> list[str]:
    policies: list[str] = []
    if any(expected.get(key) is not None for key in ("lmdExtraContribution", "expExtraContribution")):
        policies.append("reference-fit")
    if expected.get("lmdExtraContribution") is not None:
        policies.append("lmd-trade")
    if expected.get("expExtraContribution") is not None:
        policies.append("exp-factory")
    if expected.get("pureGoldExtraLmdValue") is not None:
        policies.append("gold-factory")
    return policies


def reference_fit_seed_drone_policy(expected: dict[str, Any]) -> str:
    if expected.get("lmdExtraContribution") is not None:
        return "lmd-trade"
    if expected.get("expExtraContribution") is not None:
        return "exp-factory"
    if expected.get("pureGoldExtraLmdValue") is not None:
        return "gold-factory"
    return "auto"


def reference_trade_insertion_groups(check: dict[str, Any]) -> list[ShiftInsertionGroup]:
    specs: list[ShiftInsertionSpec] = []
    for index, room in enumerate((check.get("roomSummary") or {}).get("trading", []) or []):
        if room.get("target") != "O_GOLD":
            continue
        labels = reference_trade_label_operators(str(room.get("label") or ""))
        if not labels:
            continue
        room_group = f"reference_trade:{index}:{room.get('label') or 'O_GOLD'}"
        specs.extend(
            ShiftInsertionSpec("TRADING", "O_GOLD", operator, room_group=room_group)
            for operator in labels
        )
    if not specs:
        return []
    return [
        ShiftInsertionGroup(
            f"reference_trade_room_shape:{check.get('id') or 'unknown'}",
            tuple(specs),
        )
    ]


def reference_trade_label_operators(label: str) -> list[str]:
    operators: list[str] = []
    if "巫恋" in label:
        operators.append("巫恋")
    if "龙舌兰" in label or "龙门商法" in label:
        operators.append("龙舌兰")
    if "但书" in label:
        operators.append("但书")
    if "可露希尔" in label:
        operators.append("可露希尔")
    return list(dict.fromkeys(operators))


def reference_target_lists(check: dict[str, Any]) -> tuple[list[str], list[str]] | None:
    summary = check.get("roomSummary") or {}
    trading = [
        str(room.get("target"))
        for room in summary.get("trading", []) or []
        if room.get("target")
    ]
    manufacture = [
        str(room.get("target"))
        for room in summary.get("manufacture", []) or []
        if room.get("target")
    ]
    if trading or manufacture:
        return trading, manufacture
    counts = reference_target_counts(check)
    if not counts:
        return None
    return (
        ["O_GOLD"] * int(counts.get("O_GOLD") or 0)
        + ["O_DIAMOND"] * int(counts.get("O_DIAMOND") or 0),
        ["F_EXP"] * int(counts.get("F_EXP") or 0)
        + ["F_GOLD"] * int(counts.get("F_GOLD") or 0)
        + ["F_DIAMOND"] * int(counts.get("F_DIAMOND") or 0),
    )


def target_lists_from_result(result: OptimizerResult) -> tuple[list[str], list[str]]:
    if not result.shifts:
        return [], []
    trading: list[str] = []
    manufacture: list[str] = []
    for room in result.shifts[0].rooms:
        if room.target is None:
            continue
        if room.room_type == "TRADING":
            trading.append(room.target)
        elif room.room_type == "MANUFACTURE":
            manufacture.append(room.target)
    return trading, manufacture


def target_counts_from_lists(targets: tuple[list[str], list[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for target in [*targets[0], *targets[1]]:
        counts[target] = counts.get(target, 0) + 1
    return counts


def safe_layout_token(raw: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in str(raw)).strip("_")


def reference_benchmark_summary(
    candidates: list[CandidateEvaluation],
    target_candidates: list[CandidateEvaluation | None],
    yituliu_checks: list[dict[str, Any]],
    insertion_coverage: dict[str, Any],
    roster: Iterable[RosterOperator] | None = None,
) -> dict[str, Any]:
    target_items = [candidate for candidate in target_candidates if candidate is not None]
    hard_gates = benchmark_hard_gates(target_items, candidates, insertion_coverage)
    guide_deltas = guide_reference_deltas(candidates, yituliu_checks, roster)
    return {
        "status": "passed" if hard_gates["passed"] else "needs_attention",
        "purpose": "Full maxed roster optimizer search benchmark; this is not a real account training recommendation.",
        "hardGates": hard_gates,
        "guideDeltas": guide_deltas,
        "gapSummary": guide_gap_summary(guide_deltas),
    }


def benchmark_hard_gates(
    target_candidates: list[CandidateEvaluation],
    all_candidates: list[CandidateEvaluation],
    insertion_coverage: dict[str, Any],
) -> dict[str, Any]:
    conflict_rows = [
        {"candidateId": candidate.id, "conflicts": find_conflicts(candidate.result)}
        for candidate in target_candidates
    ]
    contract_rows = [
        {
            "candidateId": candidate.id,
            "contractStatus": (candidate.summary.get("targetFit") or {}).get("contractStatus"),
        }
        for candidate in target_candidates
    ]
    office_rows = [
        {"candidateId": candidate.id, "valid": office_capacity_valid(candidate.result)}
        for candidate in target_candidates
    ]
    capacity_rows = [
        {"candidateId": candidate.id, "valid": schedule_capacity_valid(candidate.result)}
        for candidate in target_candidates
    ]
    insertion_summary = insertion_coverage.get("summary") or {}
    remaining_improvements = sum(
        int(
            ((candidate.summary.get("diagnosticInsertionSearch") or {}).get("summary") or {}).get(
                "remainingImprovementCount"
            )
            or 0
        )
        for candidate in all_candidates
    )
    checks = {
        "targetCompatibleCandidatesExist": bool(target_candidates),
        "targetCompatibleConflictFree": all(not row["conflicts"] for row in conflict_rows),
        "targetCompatibleContractsNotViolated": all(
            row["contractStatus"] != "violated" for row in contract_rows
        ),
        "targetCompatibleOfficeCapacityValid": all(row["valid"] for row in office_rows),
        "targetCompatibleRoomCapacityValid": all(row["valid"] for row in capacity_rows),
        "missingInsertionGroupZero": int(insertion_summary.get("missingInsertionGroup") or 0) == 0,
    }
    advisory_checks = {
        "remainingImprovementZero": remaining_improvements == 0,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "advisoryGates": {
            "passed": all(advisory_checks.values()),
            "checks": advisory_checks,
            "remainingImprovementCount": remaining_improvements,
            "note": (
                "Remaining diagnostic improvements are reported as advisory search-quality "
                "signals. They do not invalidate hard schedule constraints unless they create "
                "conflicts, contract violations, office overfill, or missing insertion groups."
            ),
        },
        "conflicts": conflict_rows,
        "contracts": contract_rows,
        "officeCapacity": office_rows,
        "roomCapacity": capacity_rows,
        "diagnosticInsertionCoverage": insertion_summary,
        "remainingImprovementCount": remaining_improvements,
    }


def schedule_capacity_valid(result: OptimizerResult) -> bool:
    for shift in result.shifts:
        for room in shift.rooms:
            if room.room_type == "HIRE" and len(room.operators) > 1:
                return False
            if room.room_type == "CONTROL":
                slots = room.slots or 5
                if len(room.operators) < slots:
                    return False
    return True


def office_capacity_valid(result: OptimizerResult) -> bool:
    for shift in result.shifts:
        for room in shift.rooms:
            if room.room_type == "HIRE" and len(room.operators) > 1:
                return False
    return True


def exported_schedule_capacity_valid(data: dict[str, Any]) -> bool:
    for shift in data.get("shifts") or []:
        for room in shift.get("rooms") or []:
            operators = room.get("operators") or []
            if room.get("type") == "HIRE" and len(operators) > 1:
                return False
            if room.get("type") == "CONTROL":
                slots = int(room.get("slots") or 5)
                if len(operators) < slots:
                    return False
    return True


def guide_reference_deltas(
    candidates: list[CandidateEvaluation],
    yituliu_checks: list[dict[str, Any]],
    roster: Iterable[RosterOperator] | None = None,
) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    current_candidates = [candidate for candidate in candidates if candidate.profile == "current"]
    searched_layouts = {str(candidate.summary.get("layout") or "") for candidate in current_candidates}
    available_operator_names = (
        {operator.name for operator in roster if operator.recruited}
        if roster is not None
        else None
    )
    for check in yituliu_checks:
        target_mode = reference_mode(check)
        if target_mode is None:
            continue
        layout = str(check.get("layout") or "")
        shift_count = int(check.get("shiftCount") or 0)
        if layout not in searched_layouts or shift_count < 1:
            deltas.append(
                {
                    "referenceId": check.get("id"),
                    "title": check.get("title"),
                    "source": check.get("source"),
                    "layout": layout,
                    "mode": target_mode,
                    "shiftCount": shift_count,
                    "status": "out_of_scope",
                    "gapReason": "reference_outside_search_matrix",
                    "shiftComparison": {
                        "expected": shift_count,
                        "modeled": None,
                        "passed": False,
                    },
                    "operatorComparison": {
                        "status": "reference_outside_search_matrix",
                    },
                    "roomTargetComparison": room_target_comparison(None, check),
                }
            )
            continue
        candidate = best_reference_candidate(
            current_candidates,
            layout=layout,
            mode=target_mode,
            shift_count=shift_count,
            reference_check=check,
        )
        expected = check.get("expectedDaily") or {}
        if candidate is None:
            has_layout_mode = any(
                item.summary.get("layout") == layout and item.summary.get("mode") == target_mode
                for item in current_candidates
            )
            deltas.append(
                {
                    "referenceId": check.get("id"),
                    "title": check.get("title"),
                    "layout": layout,
                    "mode": target_mode,
                    "shiftCount": shift_count,
                    "status": "shift_pattern_missing" if has_layout_mode else "missing_candidate",
                    "gapReason": (
                        "shift_pattern_missing" if has_layout_mode else "target_selection_or_power_filter"
                    ),
                    "shiftComparison": {
                        "expected": shift_count,
                        "modeled": None,
                        "passed": False,
                    },
                    "operatorComparison": {
                        "status": "candidate_unavailable",
                    },
                    "roomTargetComparison": room_target_comparison(None, check),
                }
            )
            continue
        modeled = candidate.summary.get("dailyExpected") or {}
        expected_for_comparison = {
            **expected,
            "allowOverproduction": check.get("allowOverproduction") or [],
        }
        comparisons = {
            field: guide_field_comparison(modeled, expected_for_comparison, field)
            for field in ("lmdGross", "exp", "orundum")
            if field in expected
        }
        shift_comparison = guide_shift_comparison(candidate, check)
        operator_match = operator_comparison(candidate, check, available_operator_names)
        room_comparison = room_target_comparison(candidate, check)
        status = (
            "within_tolerance"
            if all(item["passed"] for item in comparisons.values()) and shift_comparison["passed"]
            else "gap"
        )
        gap_reason = (
            "within_tolerance"
            if status == "within_tolerance"
            else guide_delta_gap_reason(
                candidate, comparisons, operator_match, room_comparison
            )
        )
        deltas.append(
            {
                "referenceId": check.get("id"),
                "title": check.get("title"),
                "source": check.get("source"),
                "candidateId": candidate.id,
                "layout": check.get("layout"),
                "mode": target_mode,
                "shiftCount": check.get("shiftCount"),
                "shiftHours": candidate.summary.get("shiftHours"),
                "status": status,
                "comparisons": comparisons,
                "shiftComparison": shift_comparison,
                "operatorComparison": operator_match,
                "roomTargetComparison": room_comparison,
                "dependencyTradeoff": dependency_tradeoff_summary(candidate)
                if gap_reason == "roster_or_dependency_tradeoff"
                else {"status": "not_applicable"},
                "gapReason": gap_reason,
            }
        )
    return deltas


def dependency_tradeoff_summary(candidate: CandidateEvaluation) -> dict[str, Any]:
    unsatisfied: list[dict[str, Any]] = []
    seen_unsatisfied: set[tuple[str, str, str, str]] = set()
    room_context = shift_room_operator_context(candidate)
    for shift in candidate.result.shifts:
        for room in shift.rooms:
            if room.room_type != "TRADING":
                continue
            current_operators = room_context.get((shift.name, room.room_id), [])
            for item in room_report_skill_audit(candidate, shift.name, room.room_id):
                status = str(item.get("status") or "")
                reason = str(item.get("reason") or "")
                if status not in {"unsupported", "diagnostic_only"}:
                    continue
                if not tradeoff_audit_reason(reason):
                    continue
                key = (
                    shift.name,
                    room.room_id,
                    str(item.get("operator") or ""),
                    str(item.get("buffName") or item.get("buffId") or ""),
                )
                if key in seen_unsatisfied:
                    continue
                seen_unsatisfied.add(key)
                unsatisfied.append(
                    {
                        "shift": shift.name,
                        "roomId": room.room_id,
                        "target": room.target,
                        "operator": item.get("operator"),
                        "skill": item.get("buffName") or item.get("buffId"),
                        "status": status,
                        "reason": reason,
                        "currentOperators": current_operators,
                    }
                )

    search = candidate.result.diagnostic_insertion_search or {}
    searched: list[dict[str, Any]] = []
    seen_search: set[tuple[str, str]] = set()
    for bucket in ("accepted", "skipped"):
        for item in search.get(bucket) or []:
            status = str(item.get("status") or bucket)
            if status not in {
                "accepted",
                "already_satisfied",
                "evaluated_not_improving",
                "unplaceable",
            }:
                continue
            operators = [str(name) for name in item.get("operators") or []]
            if not operators:
                continue
            if not any(
                op == str(detail.get("operator") or "") or op in str(detail.get("reason") or "")
                for op in operators
                for detail in unsatisfied
            ):
                continue
            key = (str(item.get("specKey") or item.get("groupId") or operators), status)
            if key in seen_search:
                continue
            seen_search.add(key)
            searched.append(
                {
                    "groupId": item.get("groupId"),
                    "operators": operators,
                    "status": status,
                    "shift": item.get("shift"),
                    "scoreDelta": item.get("scoreDelta"),
                    "dailyExpectedDelta": item.get("dailyExpectedDelta") or {},
                    "assignmentChanges": item.get("assignmentChanges") or [],
                    "satisfiedShifts": item.get("satisfiedShifts"),
                }
            )

    return {
        "status": "has_tradeoffs" if unsatisfied or searched else "none",
        "unsatisfiedEffects": unsatisfied[:8],
        "searchedGroups": searched[:8],
        "unsatisfiedCount": len(unsatisfied),
        "searchedGroupCount": len(searched),
    }


def shift_room_operator_context(candidate: CandidateEvaluation) -> dict[tuple[str, str], list[str]]:
    context: dict[tuple[str, str], list[str]] = {}
    for shift in candidate.result.shifts:
        for room in shift.rooms:
            names = [
                skill.operator_name
                for skill in room.operators
                if getattr(skill, "operator_name", None)
            ]
            context[(shift.name, room.room_id)] = dedupe_preserve_order(names)
    return context


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def room_report_skill_audit(
    candidate: CandidateEvaluation, shift_name: str, room_id: str
) -> list[dict[str, Any]]:
    report = candidate.result.production_report
    if report is None:
        return []
    for room in report.roomReports:
        if room.get("shift") == shift_name and room.get("roomId") == room_id:
            return list(room.get("skillEffectAudit") or [])
    return []


def tradeoff_audit_reason(reason: str) -> bool:
    return any(
        marker in reason
        for marker in (
            "同站条件未满足",
            "未计入条件收益",
            "same-room condition not satisfied",
            "conditional income not counted",
            "Complex or upgrade-only skill",
            "requires an order distribution model",
            "违约订单收益需要订单分布模型",
        )
    )


def guide_delta_gap_reason(
    candidate: CandidateEvaluation,
    comparisons: dict[str, dict[str, Any]],
    operator_comparison: dict[str, Any],
    room_comparison: dict[str, Any],
) -> str:
    operator_status = operator_comparison.get("status")
    if (
        any(item.get("passed") is False for item in comparisons.values())
        and operator_status
        in {
            "reference_operator_list_unavailable",
            "reference_operator_anchor_extraction_pending",
        }
        and room_comparison.get("status")
        in {
            "reference_room_targets_unavailable",
            "reference_room_target_extraction_pending",
        }
    ):
        return "reference_static_image_detail_unavailable"
    if (
        any(item.get("passed") is False for item in comparisons.values())
        and operator_status
        in {
            "reference_operator_list_unavailable",
            "reference_operator_anchor_compared",
        }
        and room_comparison.get("status") == "compared"
        and room_comparison.get("passed") is True
        and int(candidate.summary.get("unsupportedSkillEffectCount") or 0) > 0
    ):
        return "roster_or_dependency_tradeoff"
    if (
        comparisons.get("orundum", {}).get("passed") is False
        and room_comparison.get("status") == "compared"
        and float(room_comparison.get("matchRate") or 0.0) >= 0.999
    ):
        return "model_gap_orundum_trade_efficiency_lookup_missing"
    return candidate_gap_reason(candidate)


def guide_shift_comparison(
    candidate: CandidateEvaluation, check: dict[str, Any]
) -> dict[str, Any]:
    modeled = int(candidate.summary.get("shiftCount") or 0)
    expected = int(check.get("shiftCount") or 0)
    modeled_durations = candidate.summary.get("shiftDurations") or []
    expected_durations = normalized_reference_shift_hours(check.get("shiftHours"))
    durations_pass = True
    if expected_durations:
        durations_pass = (
            len(modeled_durations) == len(expected_durations)
            and all(
                abs(float(modeled) - float(expected)) < 0.001
                for modeled, expected in zip(modeled_durations, expected_durations)
            )
        )
    return {
        "modeled": modeled,
        "expected": expected,
        "modeledShiftHours": candidate.summary.get("shiftHours"),
        "expectedShiftHours": check.get("shiftHours"),
        "modeledShiftDurations": modeled_durations,
        "expectedShiftDurations": expected_durations,
        "passed": modeled == expected and durations_pass,
    }


def operator_comparison(
    candidate: CandidateEvaluation | None,
    check: dict[str, Any],
    available_operator_names: set[str] | None = None,
) -> dict[str, Any]:
    reference = reference_operator_set(check)
    if reference is None:
        anchors = reference_operator_anchors(check)
        if anchors is None:
            visual = check.get("visualExtraction") or {}
            if visual.get("status") == "manual_extraction_pending":
                return {
                    "status": "reference_operator_anchor_extraction_pending",
                    "reason": (
                        "Static guide image is available locally, but complete operator "
                        "anchors have not yet been transcribed."
                    ),
                    "localImagePath": visual.get("localImagePath"),
                }
            return {
                "status": "reference_operator_list_unavailable",
                "reason": "Static guide image labels do not include a complete operator roster.",
            }
        if candidate is None:
            return {"status": "candidate_unavailable"}
        modeled = active_operator_set(candidate)
        matched = modeled & anchors
        missing = anchors - modeled
        availability = missing_anchor_availability(missing, available_operator_names)
        return {
            "status": "reference_operator_anchor_compared",
            "referenceCompleteness": "partial_anchors",
            "modeledCount": len(modeled),
            "anchorCount": len(anchors),
            "matchedAnchorCount": len(matched),
            "anchorCoverage": round(len(matched) / len(anchors), 3) if anchors else 1.0,
            "missingAnchors": sorted(missing),
            **availability,
            "matchedAnchors": sorted(matched),
        }
    if candidate is None:
        return {"status": "candidate_unavailable"}
    modeled = active_operator_set(candidate)
    union = modeled | reference
    intersection = modeled & reference
    return {
        "status": "compared",
        "modeledCount": len(modeled),
        "referenceCount": len(reference),
        "sharedCount": len(intersection),
        "jaccard": round(len(intersection) / len(union), 3) if union else 1.0,
        "missingFromCandidate": sorted(reference - modeled),
        "extraInCandidate": sorted(modeled - reference),
    }


def missing_anchor_availability(
    missing: set[str],
    available_operator_names: set[str] | None,
) -> dict[str, Any]:
    if available_operator_names is None:
        return {"missingAnchorAvailability": "unknown"}
    available = missing & available_operator_names
    unavailable = missing - available_operator_names
    return {
        "missingAnchorAvailability": "compared",
        "missingAvailableAnchors": sorted(available),
        "missingUnavailableAnchors": sorted(unavailable),
        "missingAvailableAnchorCount": len(available),
        "missingUnavailableAnchorCount": len(unavailable),
    }


def active_operator_set(candidate: CandidateEvaluation) -> set[str]:
    return {
        skill.operator_name
        for shift in candidate.result.shifts
        for room in [*shift.rooms, *shift.dormitories]
        for skill in room.operators
    }


def reference_operator_set(check: dict[str, Any]) -> set[str] | None:
    schedule_meta = check.get("machineReadableSchedule") or {}
    raw_path = schedule_meta.get("localRawPath")
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        summary = summarize_mower_plan(unwrap_mower_schedule(raw))
    except Exception:
        return None
    operators = {
        operator
        for room_operators in (summary.get("roomOperators") or {}).values()
        for operator in room_operators
        if operator and operator != "Free"
    }
    return operators or None


def reference_operator_anchors(check: dict[str, Any]) -> set[str] | None:
    anchors = check.get("operatorAnchors") or check.get("referenceOperatorAnchors")
    if not anchors:
        return None
    values = {str(name) for name in anchors if str(name)}
    return values or None


def room_target_comparison(
    candidate: CandidateEvaluation | None, check: dict[str, Any]
) -> dict[str, Any]:
    expected = reference_target_counts(check)
    if not expected:
        visual = check.get("visualExtraction") or {}
        if visual.get("status") == "manual_extraction_pending":
            return {
                "status": "reference_room_target_extraction_pending",
                "reason": (
                    "Static guide image is available locally, but room-level product "
                    "targets have not yet been transcribed."
                ),
                "localImagePath": visual.get("localImagePath"),
            }
        return {
            "status": "reference_room_targets_unavailable",
            "reason": "Static guide image labels do not include room-level product targets.",
        }
    modeled = candidate.summary.get("targetCounts") if candidate else {}
    modeled = modeled or {}
    keys = sorted(set(expected) | set(modeled))
    rows = {
        key: {
            "modeled": int(modeled.get(key) or 0),
            "expected": int(expected.get(key) or 0),
            "delta": int(modeled.get(key) or 0) - int(expected.get(key) or 0),
        }
        for key in keys
    }
    matched = sum(min(row["modeled"], row["expected"]) for row in rows.values())
    total = sum(row["expected"] for row in rows.values())
    return {
        "status": "compared",
        "matchedReferenceRooms": matched,
        "referenceRooms": total,
        "matchRate": round(matched / total, 3) if total else 1.0,
        "targets": rows,
        "passed": all(row["delta"] == 0 for row in rows.values()),
    }


def reference_target_counts(check: dict[str, Any]) -> dict[str, int]:
    summary = check.get("roomSummary") or {}
    counts: dict[str, int] = {}
    for section in ("trading", "manufacture"):
        for room in summary.get(section, []) or []:
            target = room.get("target")
            if target:
                counts[str(target)] = counts.get(str(target), 0) + 1
    mower = check.get("expectedMowerPlan") or {}
    for product, count in (mower.get("tradingProducts") or {}).items():
        target = {"lmd": "O_GOLD", "orundum": "O_DIAMOND"}.get(str(product))
        if target:
            counts[target] = counts.get(target, 0) + int(count)
    for product, count in (mower.get("manufactureProducts") or {}).items():
        target = {
            "battle_record": "F_EXP",
            "gold": "F_GOLD",
            "orirock": "F_DIAMOND",
        }.get(str(product))
        if target:
            counts[target] = counts.get(target, 0) + int(count)
    return counts


def reference_mode(check: dict[str, Any]) -> str | None:
    mode = str(check.get("mode") or "")
    if mode == "normal":
        return "normal"
    if mode == "orundum":
        expected = check.get("expectedDaily") or {}
        if str(check.get("layout") or "") == "342" and float(expected.get("exp") or 0.0) <= 0.001:
            return "max_orundum"
        return "balanced_orundum"
    return None


def best_reference_candidate(
    candidates: list[CandidateEvaluation],
    *,
    layout: str,
    mode: str,
    shift_count: int,
    reference_check: dict[str, Any] | None = None,
) -> CandidateEvaluation | None:
    matches = [
        candidate
        for candidate in candidates
        if candidate.summary.get("layout") == layout
        and candidate.summary.get("mode") == mode
        and int(candidate.summary.get("shiftCount") or 0) == shift_count
    ]
    if not matches:
        return None
    expected_targets = reference_target_counts(reference_check or {}) if reference_check else {}

    def reference_rank(candidate: CandidateEvaluation) -> tuple[int, int, int, float, float]:
        if not expected_targets:
            passed, error = reference_resource_fit(candidate, reference_check or {})
            return (0, 0, passed, -error, candidate.result.score)
        modeled = candidate.summary.get("targetCounts") or {}
        keys = set(expected_targets) | set(modeled)
        exact = all(int(modeled.get(key) or 0) == int(expected_targets.get(key) or 0) for key in keys)
        matched = sum(
            min(int(modeled.get(key) or 0), int(expected_targets.get(key) or 0))
            for key in keys
        )
        passed, error = reference_resource_fit(candidate, reference_check or {})
        return (int(exact), matched, passed, -error, candidate.result.score)

    return max(matches, key=reference_rank)


def reference_resource_fit(
    candidate: CandidateEvaluation,
    reference_check: dict[str, Any],
) -> tuple[int, float]:
    expected = {
        **(reference_check.get("expectedDaily") or {}),
        "allowOverproduction": reference_check.get("allowOverproduction") or [],
    }
    modeled = candidate.summary.get("dailyExpected") or {}
    comparisons = [
        guide_field_comparison(modeled, expected, field)
        for field in ("lmdGross", "exp", "orundum")
        if field in expected
    ]
    if not comparisons:
        return 0, 0.0
    passed = sum(1 for item in comparisons if item.get("passed"))
    error = sum(
        abs(float(item.get("delta") or 0.0))
        / max(1.0, float(item.get("tolerance") or 1.0))
        for item in comparisons
    )
    return passed, round(error, 6)


def guide_field_comparison(
    modeled: dict[str, Any],
    expected: dict[str, Any],
    field: str,
) -> dict[str, Any]:
    expected_field, expected_value, modeled_field, modeled_value = guide_comparison_values(
        modeled,
        expected,
        field,
    )
    delta = modeled_value - expected_value
    floor = 5.0 if field == "orundum" else 1000.0
    tolerance_ratio = 0.025
    if field == "lmdGross" and expected.get("lmdExtraContribution") is not None:
        tolerance_ratio = 0.035
    if field == "lmdGross" and expected_field in {"lmdGrossWithLmdExtra", "lmdGrossWithExtra"}:
        tolerance_ratio = 0.06
    tolerance = max(floor, abs(expected_value) * tolerance_ratio)
    allow_overproduction = field in {
        str(item) for item in expected.get("allowOverproduction") or []
    }
    passed = abs(delta) <= tolerance or (allow_overproduction and delta >= 0)
    return {
        "modeled": round(modeled_value, 3),
        "expected": round(expected_value, 3),
        "expectedField": expected_field,
        "modeledField": modeled_field,
        "delta": round(delta, 3),
        "tolerance": round(tolerance, 3),
        "passed": passed,
        "passMode": "overproduction_allowed" if allow_overproduction and delta >= 0 else "tolerance",
    }


def guide_comparison_values(
    modeled: dict[str, Any], expected: dict[str, Any], field: str
) -> tuple[str, float, str, float]:
    drone = modeled.get("droneContribution") or {}
    if drone.get("referenceFit"):
        return field, float(expected.get(field) or 0.0), field, float(modeled.get(field) or 0.0)
    mixed_drone_resources = [
        key
        for key in ("lmdGross", "exp")
        if float(drone.get(key) or 0.0) > 0.001
    ]
    if field in {"lmdGross", "exp"} and len(mixed_drone_resources) > 1:
        modeled_base = float(modeled.get(field) or 0.0) - float(drone.get(field) or 0.0)
        return field, float(expected.get(field) or 0.0), f"{field}WithoutModeledDrones", modeled_base
    if field == "lmdGross" and float(drone.get("lmdGross") or 0.0) > 0:
        if "lmdExtraContribution" in expected and not any(
            key in expected for key in ("lmdGrossWithLmdExtra", "lmdGrossWithExtra")
        ):
            modeled_total = float(modeled.get(field) or 0.0)
            expected_base = float(expected.get(field) or 0.0)
            expected_total = expected_base + float(expected.get("lmdExtraContribution") or 0.0)
            modeled_base = modeled_total - float(drone.get(field) or 0.0)
            total_tolerance = max(1000.0, abs(expected_total) * 0.025)
            base_tolerance = max(1000.0, abs(expected_base) * 0.035)
            if (
                abs(modeled_total - expected_total) > total_tolerance
                and expected_base > 0.001
                and abs(modeled_base - expected_base) <= base_tolerance
            ):
                return field, expected_base, f"{field}WithoutModeledDrones", modeled_base
            return field, expected_base, field, modeled_total
        for key in ("lmdGrossWithLmdExtra", "lmdGrossWithExtra"):
            if key in expected:
                modeled_total = float(modeled.get(field) or 0.0)
                expected_total = float(expected.get(key) or 0.0)
                expected_base = float(expected.get(field) or 0.0)
                modeled_base = modeled_total - float(drone.get(field) or 0.0)
                total_tolerance = max(1000.0, abs(expected_total) * 0.025)
                base_ratio = 0.035 if expected.get("lmdExtraContribution") is not None else 0.025
                base_tolerance = max(1000.0, abs(expected_base) * base_ratio)
                if (
                    abs(modeled_total - expected_total) > total_tolerance
                    and expected_base > 0.001
                    and abs(modeled_base - expected_base) <= base_tolerance
                ):
                    return field, expected_base, f"{field}WithoutModeledDrones", modeled_base
                return key, float(expected.get(key) or 0.0), field, float(modeled.get(field) or 0.0)
    if field == "exp" and float(drone.get("exp") or 0.0) > 0:
        if "expExtraContribution" in expected:
            modeled_total = float(modeled.get(field) or 0.0)
            expected_total = float(expected.get(field) or 0.0) + float(
                expected.get("expExtraContribution") or 0.0
            )
            expected_base = float(expected.get(field) or 0.0)
            modeled_base = modeled_total - float(drone.get(field) or 0.0)
            total_tolerance = max(1000.0, abs(expected_total) * 0.025)
            base_tolerance = max(1000.0, abs(expected_base) * 0.025)
            if (
                abs(modeled_total - expected_total) > total_tolerance
                and expected_base > 0.001
                and abs(modeled_base - expected_base) <= base_tolerance
            ):
                return field, expected_base, f"{field}WithoutModeledDrones", modeled_base
            return (
                "expWithExtra",
                expected_total,
                field,
                modeled_total,
            )
    return field, float(expected.get(field) or 0.0), field, float(modeled.get(field) or 0.0)


def candidate_gap_reason(candidate: CandidateEvaluation) -> str:
    if int(candidate.summary.get("unsupportedSkillEffectCount") or 0) > 0:
        return "model_gap_unsupported_skill_effects"
    insertion_summary = (
        (candidate.summary.get("diagnosticInsertionSearch") or {}).get("summary") or {}
    )
    if int(insertion_summary.get("remainingImprovementCount") or 0) > 0:
        return "search_pass_limit_gap"
    if int(insertion_summary.get("unplaceableCount") or 0) > 0:
        return "dependency_or_target_room_unplaceable"
    if (candidate.summary.get("targetFit") or {}).get("contractStatus") == "violated":
        return "target_contract_violation"
    return "modeled_candidate_delta"


def guide_gap_summary(deltas: list[dict[str, Any]]) -> dict[str, Any]:
    scoped = [item for item in deltas if item.get("status") != "out_of_scope"]
    gaps = [item for item in scoped if item.get("status") != "within_tolerance"]
    reasons: dict[str, int] = {}
    for item in gaps:
        reason = str(item.get("gapReason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "referenceCount": len(deltas),
        "scopedReferenceCount": len(scoped),
        "outOfScope": len(deltas) - len(scoped),
        "withinTolerance": len(scoped) - len(gaps),
        "gapCount": len(gaps),
        "gapReasons": reasons,
    }


def best_target_candidate(
    candidates: list[CandidateEvaluation],
    profile: str,
) -> CandidateEvaluation | None:
    items = [
        candidate
        for candidate in candidates
        if candidate.profile == profile
        and str(candidate.summary.get("candidateRole") or "primary")
        in {"primary", "reference_target"}
        and candidate.summary.get("targetFit", {}).get("fitLevel") != "off_target"
        and candidate.summary.get("targetFit", {}).get("contractStatus") != "violated"
    ]
    if not items:
        return None
    return max(items, key=lambda item: item.result.score)


def recommendation_intent(
    baseline: dict[str, Any] | None,
    layouts: list[str],
    modes: list[str],
    *,
    min_orundum: float,
) -> dict[str, Any]:
    normalized_modes = [safe_normalize_mode(mode) for mode in modes]
    baseline_daily = (baseline or {}).get("dailyExpected") or {}
    baseline_orundum = float(baseline_daily.get("orundum") or 0.0)
    requires_orundum = (
        baseline_orundum > 0.001
        or min_orundum > 0.001
        or any("orundum" in mode for mode in normalized_modes)
    )
    preferred_mode = None
    if requires_orundum:
        if "balanced_orundum" in normalized_modes:
            preferred_mode = "balanced_orundum"
        elif "max_orundum" in normalized_modes:
            preferred_mode = "max_orundum"
    elif "normal" in normalized_modes:
        preferred_mode = "normal"
    elif normalized_modes:
        preferred_mode = normalized_modes[0]
    preferred_layout = str(baseline["layout"]) if baseline and baseline.get("layout") else None
    return {
        "preferredLayout": preferred_layout,
        "preferredMode": preferred_mode,
        "preferredContract": preset_contract(preferred_mode) if preferred_mode else None,
        "requiresOrundum": requires_orundum,
        "baselineOrundum": round(baseline_orundum, 3),
        "requestedLayouts": list(layouts),
        "requestedModes": list(modes),
        "normalizedModes": normalized_modes,
        "modePreferenceReason": mode_preference_reason(requires_orundum, normalized_modes),
    }


def safe_normalize_mode(mode: str) -> str:
    try:
        return normalize_mode(str(mode))
    except ValueError:
        return str(mode).strip().lower().replace("-", "_")


def mode_preference_reason(requires_orundum: bool, normalized_modes: list[str]) -> str:
    if requires_orundum and "balanced_orundum" in normalized_modes:
        return "Orundum planning prefers balanced_orundum when it is available; max_orundum remains compatible but is not the exact balanced target."
    if requires_orundum:
        return "Orundum planning uses the available orundum-producing mode as the target."
    return "No orundum intent was inferred, so normal/non-orundum optimization is the target when available."


def candidate_target_fit(summary: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    daily = summary.get("dailyExpected") or {}
    target_counts = summary.get("targetCounts") or {}
    preferred_layout = intent.get("preferredLayout")
    preferred_mode = intent.get("preferredMode")
    requires_orundum = bool(intent.get("requiresOrundum"))
    layout_match = preferred_layout is None or summary.get("layout") == preferred_layout
    mode_match = preferred_mode is None or summary.get("mode") == preferred_mode
    orundum_value = float(daily.get("orundum") or 0.0)
    orundum_compatible = (not requires_orundum) or orundum_value > 0.001
    balance_status = (summary.get("balanceQuality") or {}).get("status")
    mode = str(summary.get("mode") or "")
    contract = preset_contract(mode)
    contract_checks = contract_fit_checks(daily, target_counts, contract, balance_status)
    contract_status = contract_fit_status(contract_checks)

    if not layout_match:
        fit_level = "off_target"
        reason = f"Layout {summary.get('layout')} does not match preferred layout {preferred_layout}."
    elif not orundum_compatible:
        fit_level = "off_target"
        reason = "The inferred intent requires orundum production, but this candidate produces no orundum."
    elif mode_match:
        fit_level = "exact"
        reason = "Candidate matches the preferred layout and mode for the inferred intent."
    elif requires_orundum and "orundum" in mode:
        fit_level = "compatible"
        reason = "Candidate produces orundum in the preferred layout, but uses a different orundum mode than the exact target."
    elif not requires_orundum:
        fit_level = "compatible"
        reason = "Candidate matches the preferred layout but uses a different non-orundum mode."
    else:
        fit_level = "off_target"
        reason = f"Mode {mode} does not match preferred mode {preferred_mode}."

    return {
        "layoutMatch": layout_match,
        "modeMatch": mode_match,
        "orundumCompatible": orundum_compatible,
        "balanceStatus": balance_status,
        "contractId": contract["id"],
        "contractStatus": contract_status,
        "contractChecks": contract_checks,
        "fitLevel": fit_level,
        "reason": reason,
    }


def contract_fit_checks(
    daily: dict[str, Any],
    target_counts: dict[str, Any],
    contract: dict[str, Any],
    balance_status: str | None,
) -> list[dict[str, Any]]:
    chains = set(contract.get("requiredResourceChains") or [])
    minimums = contract.get("minimumRooms") or {}
    checks = [
        {
            "id": "lmd_trading",
            "required": "lmd_trading" in chains,
            "passed": (not ("lmd_trading" in chains))
            or int(target_counts.get("O_GOLD") or 0) >= int(minimums.get("lmdTrading") or 1),
            "value": target_counts.get("O_GOLD", 0),
            "metric": "targetCounts.O_GOLD",
        },
        {
            "id": "gold_factory",
            "required": "gold_factory" in chains,
            "passed": (not ("gold_factory" in chains)) or int(target_counts.get("F_GOLD") or 0) >= 1,
            "value": target_counts.get("F_GOLD", 0),
            "metric": "targetCounts.F_GOLD",
        },
        {
            "id": "exp_factory",
            "required": "exp_factory" in chains,
            "passed": (not ("exp_factory" in chains))
            or int(target_counts.get("F_EXP") or 0) >= int(minimums.get("expFactories") or 1),
            "value": target_counts.get("F_EXP", 0),
            "metric": "targetCounts.F_EXP",
        },
        {
            "id": "shard_factory",
            "required": "shard_factory" in chains,
            "passed": (not ("shard_factory" in chains))
            or int(target_counts.get("F_DIAMOND") or 0) >= int(minimums.get("shardFactories") or 1),
            "value": target_counts.get("F_DIAMOND", 0),
            "metric": "targetCounts.F_DIAMOND",
        },
        {
            "id": "orundum_trading",
            "required": "orundum_trading" in chains,
            "passed": (not ("orundum_trading" in chains)) or int(target_counts.get("O_DIAMOND") or 0) >= 1,
            "value": target_counts.get("O_DIAMOND", 0),
            "metric": "targetCounts.O_DIAMOND",
        },
        {
            "id": "inventory_balance",
            "required": "inventory_balance" in set(contract.get("targetSelectionPenalties") or []),
            "passed": balance_status == "balanced",
            "value": balance_status,
            "metric": "balanceQuality.status",
        },
    ]
    return checks


def contract_fit_status(checks: list[dict[str, Any]]) -> str:
    required_checks = [check for check in checks if check["required"]]
    if all(check["passed"] for check in required_checks):
        return "satisfied"
    if any(not check["passed"] for check in required_checks if check["id"] != "inventory_balance"):
        return "violated"
    return "warning"


def objective_comparison(
    intent: dict[str, Any],
    profiles: list[
        tuple[str, str, CandidateEvaluation, CandidateEvaluation | None]
    ],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    scalar_off_target = False
    for profile, label, scalar, target in profiles:
        scalar_fit = scalar.summary.get("targetFit", {})
        target_summary = target.summary if target else None
        scalar_is_off_target = scalar_fit.get("fitLevel") == "off_target"
        scalar_off_target = scalar_off_target or scalar_is_off_target
        entries.append(
            {
                "profile": profile,
                "label": label,
                "scalarCandidateId": scalar.id,
                "scalarScore": scalar.result.score,
                "scalarFitLevel": scalar_fit.get("fitLevel"),
                "targetCandidateId": None if target is None else target.id,
                "targetScore": None if target is None else target.result.score,
                "scoreDeltaFromScalar": None
                if target is None
                else round(target.result.score - scalar.result.score, 3),
                "message": objective_profile_message(label, scalar, target),
                "targetCandidate": target_summary,
            }
        )
    return {
        "status": "scalar_winner_off_target" if scalar_off_target else "scalar_winner_matches_intent",
        "summary": objective_summary(intent, scalar_off_target),
        "profiles": entries,
    }


def objective_summary(intent: dict[str, Any], scalar_off_target: bool) -> str:
    if scalar_off_target:
        return "最高标量分候选与推断的目标不完全一致；请同时查看目标匹配最佳候选。"
    return "最高标量分候选与推断的目标一致。"


def objective_profile_message(
    label: str,
    scalar: CandidateEvaluation,
    target: CandidateEvaluation | None,
) -> str:
    scalar_fit = scalar.summary.get("targetFit", {})
    if scalar_fit.get("fitLevel") != "off_target":
        return f"{label}标量冠军已经匹配目标。"
    if target is None:
        return f"{label}标量冠军偏离目标，且没有找到目标匹配候选。"
    return (
        f"{label}标量冠军 {scalar.id} 偏离目标；目标匹配候选为 {target.id}。"
    )


def build_diagnostics_by_solution(
    game_data: GameData,
    roster: list[RosterOperator],
    candidates: list[CandidateEvaluation],
    *,
    diagnostic_candidates: list[CandidateEvaluation | None] | None = None,
    shard_formula: str,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for candidate in representative_diagnostic_candidates(
        candidates,
        diagnostic_candidates=diagnostic_candidates,
    ):
        group = mode_group(candidate.result.mode)
        item = build_recommendation_diagnostics(
            game_data,
            roster,
            candidate.result,
            shard_formula=shard_formula,
            solution_id=candidate.id,
            solution_group=group,
            profile=candidate.profile,
        )
        item["solutionSummary"] = {
            "id": candidate.id,
            "profile": candidate.profile,
            "profileLabel": candidate.profile_label,
            "group": group,
            "layout": candidate.result.layout.label,
            "mode": candidate.result.mode,
            "score": candidate.result.score,
            "upgradeCount": candidate.summary.get("upgradeCount", 0),
        }
        item["diagnosticInsertionSearch"] = candidate.summary.get(
            "diagnosticInsertionSearch", {}
        )
        diagnostics.append(item)
    return diagnostics


def representative_diagnostic_candidates(
    candidates: list[CandidateEvaluation],
    diagnostic_candidates: list[CandidateEvaluation | None] | None = None,
) -> list[CandidateEvaluation]:
    if diagnostic_candidates:
        unique: dict[str, CandidateEvaluation] = {}
        for candidate in diagnostic_candidates:
            if candidate is not None:
                unique[candidate.id] = candidate
        return sorted(
            unique.values(),
            key=lambda item: (
                item.profile,
                mode_group(item.result.mode),
                item.result.mode,
                -item.result.score,
            ),
        )
    return sorted(
        (
            best_candidate(candidates, profile)
            for profile in sorted({candidate.profile for candidate in candidates})
        ),
        key=lambda item: item.id,
    )


def mode_group(mode: str) -> str:
    return "orundum" if "orundum" in mode else "non_orundum"


def primary_diagnostics(
    diagnostics_by_solution: list[dict[str, Any]],
    preferred_solution_id: str,
) -> dict[str, Any]:
    for item in diagnostics_by_solution:
        if item.get("solutionSummary", {}).get("id") == preferred_solution_id:
            return item
    return diagnostics_by_solution[0]


def aggregate_anomaly_candidates(
    diagnostics_by_solution: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for diagnostics in diagnostics_by_solution:
        solution = diagnostics.get("solutionSummary", {})
        for anomaly in diagnostics.get("anomalyCandidates", []):
            key = (
                anomaly.get("type"),
                anomaly.get("operator"),
                anomaly.get("expectedPartner"),
                anomaly.get("expectedFaction"),
                str(anomaly.get("expectedRoomType")),
                str(anomaly.get("expectedTarget")),
            )
            entry = merged.get(key)
            if entry is None:
                entry = dict(anomaly)
                entry["solutions"] = []
                merged[key] = entry
            else:
                entry["priorityScore"] = max(
                    float(entry.get("priorityScore") or 0.0),
                    float(anomaly.get("priorityScore") or 0.0),
                )
                if better_experiment(
                    anomaly.get("forcedExperiment") or {},
                    entry.get("forcedExperiment") or {},
                ):
                    entry["forcedExperiment"] = anomaly.get("forcedExperiment")
                    entry["conclusion"] = anomaly.get("conclusion")
            entry["solutions"].append(
                {
                    "id": solution.get("id"),
                    "group": solution.get("group"),
                    "profile": solution.get("profile"),
                    "mode": solution.get("mode"),
                    "score": solution.get("score"),
                    "currentlySatisfied": anomaly.get("currentlySatisfied"),
                    "forcedExperiment": anomaly.get("forcedExperiment"),
                    "insertionSearch": insertion_search_for_anomaly(
                        diagnostics.get("diagnosticInsertionSearch") or {},
                        anomaly,
                    ),
                    "conclusion": anomaly.get("conclusion"),
                }
            )
    for entry in merged.values():
        solutions = entry.get("solutions", [])
        entry["solutionCount"] = len(solutions)
        entry["satisfiedSolutionCount"] = sum(
            1 for solution in solutions if solution.get("currentlySatisfied")
        )
        entry["unsatisfiedSolutionCount"] = entry["solutionCount"] - entry["satisfiedSolutionCount"]
        entry["currentlySatisfied"] = (
            entry["solutionCount"] > 0 and entry["unsatisfiedSolutionCount"] == 0
        )
        entry["insertionSearchSummary"] = aggregate_insertion_search_summary(entry)
        entry["conclusion"] = aggregate_anomaly_conclusion(entry)
    return sorted(
        merged.values(),
        key=lambda item: (
            max_solution_delta(item),
            float(item.get("priorityScore") or 0.0),
            item.get("solutionCount", 0),
        ),
        reverse=True,
    )


def better_experiment(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    return experiment_delta(candidate) > experiment_delta(current)


def experiment_delta(experiment: dict[str, Any]) -> float:
    if experiment.get("status") != "evaluated":
        return -1_000_000.0
    return float(experiment.get("scoreDelta") or 0.0)


def max_solution_delta(item: dict[str, Any]) -> float:
    deltas = [
        experiment_delta((solution.get("forcedExperiment") or {}))
        for solution in item.get("solutions", [])
    ]
    return max(deltas, default=experiment_delta(item.get("forcedExperiment") or {}))


def aggregate_anomaly_conclusion(item: dict[str, Any]) -> str:
    solution_count = int(item.get("solutionCount") or 0)
    unsatisfied = int(item.get("unsatisfiedSolutionCount") or 0)
    best_delta = max_solution_delta(item)
    has_evaluated = any(
        (solution.get("forcedExperiment") or {}).get("status") == "evaluated"
        for solution in item.get("solutions", [])
    )
    if solution_count and unsatisfied == 0:
        return "所有搓玉/不搓玉候选解均已满足该组合。"
    if not has_evaluated:
        return "已识别可疑点，但尚缺少可执行强制实验或当前房间目标不匹配。"
    if best_delta > 0.01:
        return "至少一个搓玉/不搓玉候选解中强制组合更优，疑似规则缺失、搜索漏解或候选池截断。"
    if best_delta < -0.01:
        return "未满足的候选解中，强制组合低于当前方案；当前算法认为该直觉组合不优。"
    return "强制组合与候选解近似持平，可按人工偏好选择。"


def insertion_group_id_for_anomaly(anomaly: dict[str, Any]) -> str | None:
    if anomaly.get("type") == "same_room_faction_dependency":
        operator = anomaly.get("operator")
        faction = anomaly.get("expectedFaction")
        if not operator or not faction:
            return None
        return f"diagnostic_faction_dependency:{operator}:{faction}"
    if anomaly.get("type") not in {"named_dependency", "named_cross_room_dependency"}:
        return None
    explanation = anomaly.get("dependencyExplanation") or {}
    if explanation.get("confidence") == "low" or explanation.get("relation") == "same_shift":
        return None
    operator = anomaly.get("operator")
    partner = anomaly.get("expectedPartner")
    if not operator or not partner:
        return None
    return f"diagnostic_named_dependency:{operator}:{partner}"


def insertion_spec_key_for_anomaly(anomaly: dict[str, Any]) -> str | None:
    operator = str(anomaly.get("operator") or "")
    partner = str(anomaly.get("expectedPartner") or "")
    if anomaly.get("type") == "same_room_faction_dependency":
        if not operator or not partner:
            return None
        specs = [
            SimpleInsertionSpec("TRADING", "O_GOLD", operator),
            SimpleInsertionSpec("TRADING", "O_GOLD", partner),
        ]
        return "|".join(
            sorted(
                f"{spec.room_type}:{spec.target or ''}:{name}"
                for spec in specs
                for name in spec.operators
            )
        )
    if not operator or not partner:
        return None
    specs = force_specs_from_explanation(
        anomaly.get("dependencyExplanation") or {},
        operator=operator,
        partner=partner,
    )
    if not specs and anomaly.get("type") == "named_cross_room_dependency":
        room_types = str(anomaly.get("expectedRoomType") or "").split("/")
        targets = str(anomaly.get("expectedTarget") or "").split("/")
        if len(room_types) == 2:
            specs = [
                SimpleInsertionSpec(room_types[0], targets[0] if targets else None, operator),
                SimpleInsertionSpec(
                    room_types[1],
                    targets[1] if len(targets) > 1 else None,
                    partner,
                ),
            ]
    if not specs:
        return None
    return "|".join(
        sorted(
            f"{spec.room_type}:{spec.target or ''}:{name}"
            for spec in specs
            for name in spec.operators
        )
    )


@dataclass(frozen=True)
class SimpleInsertionSpec:
    room_type: str
    target: str | None
    operator: str

    @property
    def operators(self) -> tuple[str, ...]:
        return (self.operator,)


def insertion_search_for_anomaly(
    search: dict[str, Any],
    anomaly: dict[str, Any],
) -> dict[str, Any] | None:
    group_id = insertion_group_id_for_anomaly(anomaly)
    if not group_id:
        return None
    if not search:
        return {"groupId": group_id, "status": "no_search_record"}
    spec_key = insertion_spec_key_for_anomaly(anomaly)
    allow_group_only_match = (
        anomaly.get("type") == "same_room_faction_dependency"
        and not spec_key
    )
    has_keyed_group = any(
        item.get("groupId") == group_id and item.get("specKey")
        for bucket in ("accepted", "displaced", "skipped")
        for item in (search.get(bucket) or [])
    )
    if not spec_key and has_keyed_group:
        return {
            "groupId": group_id,
            "status": "no_spec_key",
            "reason": "The diagnostic anomaly did not contain enough room/target information to match a specific insertion record.",
        }
    for bucket in ("accepted", "displaced", "skipped"):
        for item in search.get(bucket) or []:
            if item.get("groupId") == group_id and (
                allow_group_only_match
                or
                (not item.get("specKey") and not spec_key)
                or item.get("specKey") == spec_key
            ):
                return {
                    "groupId": group_id,
                    "specKey": spec_key,
                    "status": item.get("status"),
                    "shift": item.get("shift"),
                    "scoreDelta": item.get("scoreDelta"),
                    "candidateScore": item.get("candidateScore"),
                    "sourceBucket": bucket,
                }
    if not has_keyed_group:
        for bucket in ("accepted", "displaced", "skipped"):
            for item in search.get(bucket) or []:
                if (
                    item.get("groupId") == group_id
                    and item.get("status") == "not_searched_no_faction_partner"
                ):
                    return {
                        "groupId": group_id,
                        "specKey": spec_key,
                        "status": item.get("status"),
                        "shift": item.get("shift"),
                        "scoreDelta": item.get("scoreDelta"),
                        "candidateScore": item.get("candidateScore"),
                        "sourceBucket": bucket,
                    }
    if has_keyed_group:
        return {
            "groupId": group_id,
            "specKey": spec_key,
            "status": "matched_alternative_spec",
            "reason": "The optimizer searched the diagnostic group with a different concrete partner/spec, usually an equivalent faction or alias partner.",
        }
    return {"groupId": group_id, "status": "no_matching_group"}


def aggregate_insertion_search_summary(item: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    seen: set[tuple[Any, ...]] = set()
    matched = 0
    for solution in item.get("solutions", []):
        search = solution.get("insertionSearch")
        if not search:
            continue
        key = (
            solution.get("id"),
            search.get("groupId"),
            search.get("specKey"),
            search.get("status"),
        )
        if key in seen:
            continue
        seen.add(key)
        status = str(search.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
        if status not in {
            "no_search_record",
            "no_matching_group",
            "no_spec_key",
            "not_searched_no_faction_partner",
            "not_searched_group_limit",
            "unavailable_required_skill",
        }:
            matched += 1
    return {
        "matchedSolutionCount": matched,
        "statusCounts": counts,
    }


def aggregate_diagnostic_insertion_coverage(
    diagnostics_by_solution: list[dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals = {
        "solutions": 0,
        "namedDependencyAnomalies": 0,
        "matchedInsertionRecords": 0,
        "accepted": 0,
        "evaluatedNotImproving": 0,
        "unplaceable": 0,
        "displaced": 0,
        "notSearched": 0,
        "groupLimitNotSearched": 0,
        "unavailableRequiredSkill": 0,
        "noSpecKey": 0,
        "missingInsertionGroup": 0,
    }
    for diagnostics in diagnostics_by_solution:
        solution = diagnostics.get("solutionSummary") or {}
        search = diagnostics.get("diagnosticInsertionSearch") or {}
        named = [
            anomaly
            for anomaly in diagnostics.get("anomalyCandidates", [])
            if insertion_group_id_for_anomaly(anomaly)
        ]
        matches = [
            insertion_search_for_anomaly(search, anomaly)
            for anomaly in named
        ]
        matches = [match for match in matches if match]
        status_counts: dict[str, int] = {}
        seen_matches: set[tuple[Any, ...]] = set()
        for match in matches:
            key = (
                solution.get("id"),
                match.get("groupId"),
                match.get("specKey"),
                match.get("status"),
            )
            if key in seen_matches:
                continue
            seen_matches.add(key)
            status = str(match.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

        matched_records = sum(
            count
            for status, count in status_counts.items()
            if status not in {
                "no_search_record",
                "no_matching_group",
                "no_spec_key",
                "not_searched_no_faction_partner",
                "not_searched_group_limit",
                "unavailable_required_skill",
            }
        )
        row = {
            "solutionId": solution.get("id"),
            "group": solution.get("group"),
            "profile": solution.get("profile"),
            "layout": solution.get("layout"),
            "mode": solution.get("mode"),
            "score": solution.get("score"),
            "namedDependencyAnomalies": len(named),
            "matchedInsertionRecords": matched_records,
            "optimizerSummary": search.get("summary") or {},
            "statusCounts": status_counts,
        }
        rows.append(row)
        totals["solutions"] += 1
        totals["namedDependencyAnomalies"] += len(named)
        totals["matchedInsertionRecords"] += matched_records
        totals["accepted"] += status_counts.get("accepted", 0)
        totals["evaluatedNotImproving"] += status_counts.get("evaluated_not_improving", 0)
        totals["unplaceable"] += status_counts.get("unplaceable", 0)
        totals["displaced"] += status_counts.get("displaced_after_acceptance", 0)
        totals["notSearched"] += (
            status_counts.get("not_searched_no_faction_partner", 0)
            + status_counts.get("not_searched_group_limit", 0)
            + status_counts.get("unavailable_required_skill", 0)
        )
        totals["groupLimitNotSearched"] += status_counts.get("not_searched_group_limit", 0)
        totals["unavailableRequiredSkill"] += status_counts.get("unavailable_required_skill", 0)
        totals["noSpecKey"] += status_counts.get("no_spec_key", 0)
        totals["missingInsertionGroup"] += status_counts.get("no_matching_group", 0)
    return {"summary": totals, "solutions": rows}


def result_summary(
    candidate_id: str,
    result: OptimizerResult,
    *,
    allow_upgrades: bool,
    profile: str,
    profile_label: str,
    baseline_score: float | None,
    export_path: Path,
    output_dir: Path,
    candidate_role: str = "primary",
) -> dict[str, Any]:
    report = result.production_report
    if report is None:
        raise ValueError("Optimizer result does not contain a production report.")
    report_dict = report.to_dict()
    daily = report_dict["dailyExpected"]
    upgrades = [upgrade_to_dict(upgrade) for upgrade in collect_upgrades(result)]
    baseline_margin = None if baseline_score is None else round(result.score - baseline_score, 3)
    return {
        "id": candidate_id,
        "profile": profile,
        "profileLabel": profile_label,
        "candidateRole": candidate_role,
        "dronePolicy": result.drone_policy,
        "layout": result.layout.label,
        "layoutRaw": result.layout.raw,
        "mode": result.mode,
        "presetContract": preset_contract(result.mode),
        "allowUpgrades": allow_upgrades,
        "shiftCount": len(result.shifts),
        "shiftHours": result.shift_hours,
        "shiftDurations": [shift.duration_hours for shift in result.shifts],
        "targetCounts": target_counts(result),
        "score": result.score,
        "baselineMargin": baseline_margin,
        "passesBaseline": None if baseline_score is None else result.score >= baseline_score,
        "dailyExpected": daily,
        "validationEconomics": production_economics_summary(daily),
        "scoreBreakdown": report_dict["scoreBreakdown"],
        "balanceQuality": resource_balance_quality(report.dailyExpected),
        "powerStatus": result.power_status.to_dict() if result.power_status else None,
        "unsupportedSkillEffectCount": len(report_dict.get("unsupportedSkillEffects") or []),
        "diagnosticInsertionSearch": result.diagnostic_insertion_search,
        "localOptimalityAudit": result.diagnostic_insertion_search.get("localOptimalityAudit", {}),
        "candidatePoolAudit": result.diagnostic_insertion_search.get("candidatePoolAudit", {}),
        "cacheValidation": result.diagnostic_insertion_search.get("cacheValidation", {}),
        "pureGoldBalancePolicy": result.diagnostic_insertion_search.get(
            "pureGoldBalancePolicy", {}
        ),
        "objectiveConflictAudit": result.diagnostic_insertion_search.get("objectiveConflictAudit", {}),
        "scheduleExport": {
            "path": str(export_path),
            "relativePath": relative_path(export_path, output_dir),
        },
        "upgradeCount": len(upgrades),
        "upgrades": upgrades,
        "warnings": result.warnings,
    }


def collect_drone_usage_plan(
    candidates: Iterable[CandidateEvaluation | None],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for candidate in candidates:
        if candidate is None or candidate.id in seen_candidates:
            continue
        seen_candidates.add(candidate.id)
        daily = candidate.summary.get("dailyExpected") or {}
        for target in daily.get("droneTargets") or []:
            rows.append(
                {
                    "candidateId": candidate.id,
                    "shift": target.get("shift"),
                    "policy": target.get("policy"),
                    "roomId": target.get("roomId"),
                    "roomType": target.get("roomType"),
                    "target": target.get("target"),
                    "targetLabel": target_label(target.get("target")),
                    "droneCount": target.get("droneCount"),
                    "durationHours": target.get("durationHours"),
                    "operators": list(target.get("operators") or []),
                    "contribution": target.get("contribution") or {},
                }
            )
    return rows


def target_counts(result: OptimizerResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not result.shifts:
        return counts
    for room in result.shifts[0].rooms:
        if room.target is None:
            continue
        counts[room.target] = counts.get(room.target, 0) + 1
    return counts


def score_baseline(
    baseline_schedule: Path | None,
    game_data: GameData,
    roster: list[RosterOperator],
    *,
    shard_formula: str,
    drone_policy: str,
    upgrade_cost_weight: float,
    right_side: str,
    pure_gold_target: float = DEFAULT_PURE_GOLD_TARGET_PER_DAY,
    pure_gold_tolerance: float = DEFAULT_PURE_GOLD_TOLERANCE,
) -> dict[str, Any] | None:
    if baseline_schedule is None:
        return None
    imported = load_yituliu_schedule(
        baseline_schedule,
        game_data,
        roster,
        allow_upgrades=False,
        upgrade_cost_weight=upgrade_cost_weight,
    )
    simulator = ProductionSimulator(
        game_data,
        shard_formula=shard_formula,
        drone_policy=drone_policy,
        calibration_profile="guide",
        pure_gold_target=pure_gold_target,
        pure_gold_tolerance=pure_gold_tolerance,
    )
    report = simulator.evaluate(imported.shifts)
    daily = report.dailyExpected.to_dict()
    layout = apply_right_side_preset(imported.layout, right_side)
    status = power_status(game_data, layout)
    return {
        "path": str(baseline_schedule),
        "layout": imported.layout.label,
        "shiftCount": len(imported.shifts),
        "shiftDurations": [shift.duration_hours for shift in imported.shifts],
        "score": report.score,
        "dailyExpected": daily,
        "validationEconomics": production_economics_summary(daily),
        "scoreBreakdown": report.to_dict()["scoreBreakdown"],
        "balanceQuality": resource_balance_quality(report.dailyExpected),
        "powerStatus": status.to_dict(),
        "warnings": imported.warnings,
    }


def recommendation_yituliu_case_checks() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    latest_by_id = {str(target.get("id")): target for target in latest_yituliu_orundum_targets()}
    for case in yituliu_2026_06_image_label_cases():
        latest = latest_by_id.get(str(case.get("id"))) or {}
        formula_check = case.get("formulaCheck") or case.get("check") or {}
        expected = dict(case.get("expectedDaily") or {})
        for key, value in (latest.get("expectedDaily") or {}).items():
            expected.setdefault(key, value)
        economics = case.get("validationEconomics") or validation_economics_summary(
            expected,
            formula_check,
        )
        expected.setdefault("lmdNet", economics["lmdNet"])
        expected.setdefault("materialCosts", economics["materialCosts"])
        checks.append(
            {
                "id": case.get("id"),
                "title": case.get("title"),
                "source": case.get("source"),
                "layout": case.get("layout"),
                "mode": case.get("mode"),
                "shiftCount": case.get("shiftCount"),
                "shiftHours": case.get("shiftHours"),
                "algorithmCoverage": case.get("algorithmCoverage"),
                "expectedDaily": expected,
                "allowOverproduction": case.get("allowOverproduction") or [],
                "roomSummary": latest.get("roomSummary") or case.get("roomSummary"),
                "operatorAnchors": latest.get("operatorAnchors") or case.get("operatorAnchors"),
                "visualExtraction": case.get("visualExtraction"),
                "machineReadableSchedule": latest.get("machineReadableSchedule"),
                "expectedMowerPlan": latest.get("expectedMowerPlan"),
                "formulaCheck": formula_check,
                "validationEconomics": economics,
                "passed": bool(case.get("passed")),
            }
        )
    return checks


def recommendation_decision(
    current: CandidateEvaluation,
    upgrades: CandidateEvaluation,
    cost_adjusted: CandidateEvaluation,
    baseline_score: float | None,
) -> dict[str, Any]:
    current_daily = current.summary["dailyExpected"]
    return {
        "shouldFarmOrundumNow": float(current_daily.get("orundum") or 0.0) > 0,
        "currentBestMode": current.result.mode,
        "currentBestLayout": current.result.layout.label,
        "shouldConsiderTraining": upgrades.result.score > current.result.score,
        "upgradeBestMode": upgrades.result.mode,
        "upgradeBestLayout": upgrades.result.layout.label,
        "shouldConsiderCostAdjustedTraining": cost_adjusted.result.score > current.result.score,
        "costAdjustedBestMode": cost_adjusted.result.mode,
        "costAdjustedBestLayout": cost_adjusted.result.layout.label,
        "baselineScore": baseline_score,
    }


def training_impact(
    current: CandidateEvaluation,
    upgraded: CandidateEvaluation,
    profile: str,
) -> dict[str, Any]:
    delta = round(upgraded.result.score - current.result.score, 3)
    upgrade_count = upgraded.summary["upgradeCount"]
    if upgrade_count == 0:
        status = "unused"
        message = "允许补练后没有选中任何额外练干员，因此效率与当前练度最佳方案相同。"
    elif delta > 0:
        status = "improved"
        message = f"允许补练后综合分提升 {delta:.3f}。"
    else:
        status = "not_improved"
        message = "该补练口径没有超过当前练度最佳方案。"
    return {
        "status": status,
        "scoreDelta": delta,
        "upgradeCount": upgrade_count,
        "currentCandidateId": current.id,
        "upgradeCandidateId": upgraded.id,
        "profile": profile,
        "message": message,
    }


def baseline_comparison(
    baseline_score: float | None,
    current: CandidateEvaluation,
    upgrades: CandidateEvaluation,
    cost_adjusted: CandidateEvaluation,
    best_overall: CandidateEvaluation,
) -> dict[str, Any] | None:
    if baseline_score is None:
        return None
    return {
        "baselineScore": baseline_score,
        "bestCurrent": comparison_entry(current, baseline_score),
        "bestWithUpgrades": comparison_entry(upgrades, baseline_score),
        "bestWithUpgradesCostAdjusted": comparison_entry(cost_adjusted, baseline_score),
        "bestOverall": comparison_entry(best_overall, baseline_score),
    }


def comparison_entry(candidate: CandidateEvaluation, baseline_score: float) -> dict[str, Any]:
    return {
        "candidateId": candidate.id,
        "score": candidate.result.score,
        "margin": round(candidate.result.score - baseline_score, 3),
        "passes": candidate.result.score >= baseline_score,
    }


def write_upgrade_requirements_xlsx(path: Path, upgrades: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "upgrade_requirements"
    headers = [
        "Operator",
        "From elite",
        "From level",
        "To elite",
        "To level",
        "Target",
        "Cost score",
        "Materials",
        "Note",
    ]
    sheet.append(headers)
    for upgrade in upgrades:
        current = upgrade.get("from") or {}
        target = upgrade.get("to") or {}
        materials = upgrade.get("materials") or {}
        sheet.append(
            [
                upgrade.get("name"),
                current.get("elite"),
                current.get("level"),
                target.get("elite"),
                target.get("level"),
                target.get("label"),
                upgrade.get("costScore"),
                json.dumps(materials, ensure_ascii=False, sort_keys=True),
                upgrade.get("note"),
            ]
        )
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = [22, 11, 11, 10, 10, 18, 12, 42, 36]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width
    workbook.save(path)
    workbook.close()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def render_html_report(report: dict[str, Any], output_dir: Path) -> str:
    cards = "\n".join(
        render_candidate_card(title, report[key])
        for title, key in [
            ("当前练度最佳", "bestCurrent"),
            ("最高产出补练", "bestWithUpgrades"),
            ("考虑成本补练", "bestWithUpgradesCostAdjusted"),
        ]
        if report.get(key)
    )
    target_cards = "\n".join(
        render_candidate_card(title, report[key])
        for title, key in [
            ("当前练度目标匹配最佳", "bestTargetCompatibleCurrent"),
            ("补练目标匹配最佳", "bestTargetCompatibleWithUpgrades"),
            ("成本补练目标匹配最佳", "bestTargetCompatibleCostAdjusted"),
        ]
        if report.get(key)
    )
    baseline = report.get("baseline")
    manual = render_baseline_section(baseline) if baseline else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>明日方舟基建排班推荐报告</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #172026;
      --muted: #5e6a73;
      --line: #d9e1e7;
      --panel: #f6f8fa;
      --accent: #176b87;
      --good: #147a4b;
      --warn: #9a6700;
    }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      color: var(--ink);
      background: #fff;
      line-height: 1.55;
    }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px 24px 48px; }}
    h1, h2, h3 {{ margin: 0; line-height: 1.25; }}
    h1 {{ font-size: 28px; margin-bottom: 8px; }}
    h2 {{ font-size: 19px; margin-top: 28px; margin-bottom: 12px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }}
    h3 {{ font-size: 16px; margin-bottom: 10px; }}
    .meta, .note {{ color: var(--muted); font-size: 13px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-top: 18px; }}
    .card {{ border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 14px; }}
    .pill {{ display: inline-block; border: 1px solid var(--line); background: #fff; border-radius: 999px; padding: 2px 8px; margin: 0 6px 6px 0; font-size: 12px; color: var(--muted); }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 7px 8px; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; background: #f9fbfc; }}
    .scroll {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    a.download {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
  </style>
</head>
<body>
<main>
  <h1>明日方舟基建排班推荐报告</h1>
  <p class="meta">生成时间：{escape(str(report.get("generatedAt", "")))}；无人机策略：{escape(str(report.get("inputs", {}).get("dronePolicy", "")))}</p>
  <section class="cards">
    {cards}
  </section>
  <h2>目标匹配说明</h2>
  {render_objective_section(report)}
  <section class="cards">
    {target_cards}
  </section>
  <h2>Drone Usage Plan</h2>
  {render_drone_plan(report)}
  <h2>补练影响</h2>
  {render_impact_table(report)}
  <h2>Download Tables</h2>
  {render_downloads_section(report, output_dir)}
  {manual}
  <h2>一图流案例验算</h2>
  {render_yituliu_table(report.get("yituliuCaseChecks", []))}
  <h2>Full Roster Search Benchmark</h2>
  {render_reference_benchmark(report.get("referenceBenchmark", {}))}
  <h2>反常识诊断</h2>
  {render_counterintuitive_diagnostics(report.get("counterintuitiveDiagnostics", []))}
  <h2>搓玉/不搓玉候选诊断</h2>
  {render_solution_diagnostics_summary(report.get("diagnosticsBySolution", []))}
  <h2>诊断派生插入搜索</h2>
  {render_diagnostic_insertion_coverage(report.get("diagnosticInsertionCoverage", {}))}
  <h2>大规模异常挖掘</h2>
  {render_anomaly_table(report.get("anomalyCandidates", []))}
  <h2>候选排行</h2>
  {render_candidate_table(report.get("candidates", []))}
</main>
</body>
</html>
"""


def render_downloads_section(report: dict[str, Any], output_dir: Path) -> str:
    files = report.get("writtenFiles") or {}
    rows = []
    for key, label in [
        ("upgradeRequirementsXlsx", "Upgrade requirements XLSX"),
        ("upgradeRequirementsCostAdjustedXlsx", "Cost-adjusted upgrade requirements XLSX"),
        ("upgradeRequirements", "Upgrade requirements JSON"),
        ("upgradeRequirementsCostAdjusted", "Cost-adjusted upgrade requirements JSON"),
        ("report", "Recommendation report JSON"),
        ("bestCurrentSchedule", "Best current schedule JSON"),
        ("bestUpgradesSchedule", "Best upgrade schedule JSON"),
        ("bestUpgradesCostAdjustedSchedule", "Best cost-adjusted upgrade schedule JSON"),
    ]:
        raw_path = files.get(key)
        if not raw_path:
            continue
        href = escape(relative_path(Path(raw_path), output_dir))
        rows.append(
            "<tr>"
            f"<td>{escape(label)}</td>"
            f"<td><a class=\"download\" href=\"{href}\" download>Download</a></td>"
            f"<td>{escape(str(raw_path))}</td>"
            "</tr>"
        )
    if not rows:
        return "<p class=\"note\">No downloadable tables are available.</p>"
    return (
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>File</th><th>Download</th><th>Path</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_drone_plan(report: dict[str, Any]) -> str:
    plan_rows = report.get("droneUsagePlan")
    if plan_rows is None:
        plan_rows = []
        seen: set[str] = set()
        for key in [
            "bestTargetCompatibleCurrent",
            "bestCurrent",
            "bestTargetCompatibleCostAdjusted",
            "bestWithUpgradesCostAdjusted",
        ]:
            candidate = report.get(key)
            if not candidate:
                continue
            candidate_id = str(candidate.get("id") or key)
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            daily = candidate.get("dailyExpected") or {}
            for target in daily.get("droneTargets") or []:
                plan_rows.append({"candidateId": candidate_id, **target})

    rows = []
    for target in plan_rows:
        rows.append(
            "<tr>"
            f"<td>{escape(str(target.get('candidateId', '-')))}</td>"
            f"<td>{escape(str(target.get('shift', '-')))}</td>"
            f"<td>{escape(str(target.get('policy', '-')))}</td>"
            f"<td>{escape(str(target.get('roomId', '-')))}</td>"
            f"<td>{escape(str(target.get('roomType', '-')))}</td>"
            f"<td>{escape(str(target.get('targetLabel') or target_label(target.get('target'))))}</td>"
            f"<td>{fmt(target.get('droneCount'))}</td>"
            f"<td>{fmt(target.get('durationHours'))}</td>"
            f"<td>{escape(', '.join(str(name) for name in target.get('operators', [])))}</td>"
            f"<td>{resource_map(target.get('contribution') or {})}</td>"
            "</tr>"
        )
    if not rows:
        policy = (report.get("inputs") or {}).get("dronePolicy")
        return (
            "<p class=\"note\">No modeled drone targets are available for the selected "
            f"best candidates. Drone policy: {escape(str(policy or '-'))}.</p>"
        )
    return (
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>Candidate</th><th>Shift</th><th>Policy</th><th>Room</th><th>Type</th>"
        "<th>Target</th><th>Drones</th><th>Hours</th><th>Operators</th><th>Contribution</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_reference_benchmark(benchmark: dict[str, Any]) -> str:
    if not benchmark:
        return "<p class=\"note\">No full-roster benchmark summary is available.</p>"
    hard = benchmark.get("hardGates") or {}
    checks = hard.get("checks") or {}
    advisory = hard.get("advisoryGates") or {}
    advisory_checks = advisory.get("checks") or {}
    gap_summary = benchmark.get("gapSummary") or {}
    rows = [
        ("Status", benchmark.get("status")),
        ("Purpose", benchmark.get("purpose")),
        ("Hard gates passed", hard.get("passed")),
        ("Target candidates exist", checks.get("targetCompatibleCandidatesExist")),
        ("Conflict free", checks.get("targetCompatibleConflictFree")),
        ("Contracts not violated", checks.get("targetCompatibleContractsNotViolated")),
        ("Office capacity valid", checks.get("targetCompatibleOfficeCapacityValid")),
        ("Room capacity valid", checks.get("targetCompatibleRoomCapacityValid")),
        ("Missing insertion groups zero", checks.get("missingInsertionGroupZero")),
        ("Advisory gates passed", advisory.get("passed")),
        ("Remaining improvements zero", advisory_checks.get("remainingImprovementZero")),
        ("Remaining improvement count", advisory.get("remainingImprovementCount")),
        ("Guide references", gap_summary.get("referenceCount")),
        ("Scoped references", gap_summary.get("scopedReferenceCount")),
        ("Out of scope references", gap_summary.get("outOfScope")),
        ("Within tolerance", gap_summary.get("withinTolerance")),
        ("Gap count", gap_summary.get("gapCount")),
        ("Gap reasons", resource_map(gap_summary.get("gapReasons") or {})),
    ]
    delta_rows = []
    for item in benchmark.get("guideDeltas", []):
        comparisons = item.get("comparisons") or {}
        shift = item.get("shiftComparison") or {}
        operators = item.get("operatorComparison") or {}
        rooms = item.get("roomTargetComparison") or {}
        tradeoff = item.get("dependencyTradeoff") or {}
        delta_rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('referenceId', '-')))}</td>"
            f"<td>{escape(str(item.get('candidateId', '-')))}</td>"
            f"<td>{escape(str(item.get('status', '-')))}</td>"
            f"<td>{escape(str(item.get('gapReason', '-')))}</td>"
            f"<td>{comparison_delta(comparisons.get('lmdGross'))}</td>"
            f"<td>{comparison_delta(comparisons.get('exp'))}</td>"
            f"<td>{comparison_delta(comparisons.get('orundum'))}</td>"
            f"<td>{shift_delta(shift)}</td>"
            f"<td>{operator_delta(operators)}</td>"
            f"<td>{room_target_delta(rooms)}</td>"
            f"<td>{dependency_tradeoff_delta(tradeoff)}</td>"
            "</tr>"
        )
    return (
        "<div class=\"card\">"
        + small_table(rows)
        + "</div>"
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>Reference</th><th>Candidate</th><th>Status</th><th>Reason</th>"
        "<th>LMD gross delta</th><th>EXP delta</th><th>Orundum delta</th>"
        "<th>Shift count</th><th>Operators</th><th>Room targets</th><th>Tradeoff details</th>"
        "</tr></thead><tbody>"
        + "".join(delta_rows)
        + "</tbody></table></div>"
    )


def comparison_delta(item: dict[str, Any] | None) -> str:
    if not item:
        return "-"
    return (
        f"{fmt(item.get('delta'))} "
        f"({fmt(item.get('modeled'))}/{fmt(item.get('expected'))})"
    )


def shift_delta(item: dict[str, Any]) -> str:
    if not item:
        return "-"
    status = "ok" if item.get("passed") else "gap"
    return f"{status}: {fmt(item.get('modeled'))}/{fmt(item.get('expected'))}"


def operator_delta(item: dict[str, Any]) -> str:
    if not item:
        return "-"
    status = str(item.get("status") or "-")
    if status == "reference_operator_anchor_compared":
        availability = ""
        if item.get("missingAnchorAvailability") == "compared":
            availability = (
                f"; missing available {fmt(item.get('missingAvailableAnchorCount'))}; "
                f"missing unavailable {fmt(item.get('missingUnavailableAnchorCount'))}"
            )
        return (
            f"anchors {fmt(item.get('anchorCoverage'))}; "
            f"matched {fmt(item.get('matchedAnchorCount'))}/{fmt(item.get('anchorCount'))}"
            f"{availability}"
        )
    if status != "compared":
        return escape(status)
    return (
        f"Jaccard {fmt(item.get('jaccard'))}; "
        f"shared {fmt(item.get('sharedCount'))}/{fmt(item.get('referenceCount'))}"
    )


def room_target_delta(item: dict[str, Any]) -> str:
    if not item:
        return "-"
    status = str(item.get("status") or "-")
    if status != "compared":
        return escape(status)
    return (
        f"match {fmt(item.get('matchRate'))}; "
        f"rooms {fmt(item.get('matchedReferenceRooms'))}/{fmt(item.get('referenceRooms'))}"
    )


def dependency_tradeoff_delta(item: dict[str, Any]) -> str:
    if not item or item.get("status") in {"none", "not_applicable"}:
        return "-"
    effects = item.get("unsatisfiedEffects") or []
    groups = item.get("searchedGroups") or []
    parts = []
    if effects:
        labels = []
        for effect in effects[:3]:
            current = "/".join(str(name) for name in effect.get("currentOperators") or [])
            suffix = f" [{current}]" if current else ""
            labels.append(
                f"{effect.get('operator')}@{effect.get('roomId')}:{effect.get('skill')}{suffix}"
            )
        parts.append(
            f"unmet {fmt(item.get('unsatisfiedCount'))}: "
            + escape("; ".join(str(label) for label in labels))
        )
    if groups:
        labels = []
        for group in groups[:3]:
            detail_parts = []
            delta = resource_map(group.get("dailyExpectedDelta") or {})
            if delta != "-":
                detail_parts.append(delta)
            changes = group.get("assignmentChanges") or []
            if changes:
                detail_parts.append(assignment_change_delta(changes[0]))
            detail = f"; {'; '.join(detail_parts)}" if detail_parts else ""
            labels.append(
                f"{'/'.join(str(name) for name in group.get('operators') or [])}"
                f" {group.get('status')} ({fmt(group.get('scoreDelta'))}){detail}"
            )
        parts.append(
            f"searched {fmt(item.get('searchedGroupCount'))}: "
            + escape("; ".join(str(label) for label in labels))
        )
    return "<br>".join(parts) if parts else "-"


def assignment_change_delta(change: dict[str, Any]) -> str:
    before = "/".join(str(name) for name in change.get("before") or [])
    after = "/".join(str(name) for name in change.get("after") or [])
    return f"{change.get('roomId', '-')}: {before} -> {after}"


def power_adjustment_summary(power: dict[str, Any]) -> str:
    adjustment = power.get("powerAdjustment") or {}
    if not adjustment:
        if power.get("feasible") is False:
            return "infeasible"
        return "none"
    status = str(adjustment.get("status") or "-")
    reason = str(adjustment.get("reason") or "-")
    before = adjustment.get("beforeMargin")
    after = adjustment.get("afterMargin")
    manufacture_only = adjustment.get("manufactureOnlyFeasible")
    parts = [reason, f"{status}: {fmt(before)} -> {fmt(after)}"]
    if manufacture_only is not None:
        parts.append(f"factory-only={fmt(manufacture_only)}")
    if adjustment.get("manufactureOnlyAfterMargin") is not None:
        parts.append(f"factory-only margin {fmt(adjustment.get('manufactureOnlyAfterMargin'))}")
    manufacture_levels = adjustment.get("manufactureLevels")
    if manufacture_levels:
        parts.append("M" + "/".join(str(level) for level in manufacture_levels))
    dormitory_levels = adjustment.get("dormitoryLevels")
    if dormitory_levels:
        parts.append("D" + "/".join(str(level) for level in dormitory_levels))
    return "; ".join(parts)


def render_candidate_card(title: str, candidate: dict[str, Any]) -> str:
    daily = candidate.get("dailyExpected", {})
    economics = candidate.get("validationEconomics", {})
    drone = economics.get("droneAccounting", {})
    power = candidate.get("powerStatus") or {}
    target_fit = candidate.get("targetFit") or {}
    contract = candidate.get("presetContract") or {}
    rows = [
        ("综合分", candidate.get("score")),
        ("候选类型", candidate.get("candidateRole")),
        ("Drone Policy", candidate.get("dronePolicy")),
        ("目标匹配", target_fit.get("fitLevel")),
        ("契约", contract.get("id")),
        ("契约状态", target_fit.get("contractStatus")),
        ("LMD 口径", contract.get("lmdPolicy")),
        ("龙门币毛", daily.get("lmdGross")),
        ("龙门币净", daily.get("lmdNet")),
        ("经验", daily.get("exp")),
        ("合成玉", daily.get("orundum")),
        ("办公室效率", daily.get("officeSpeed")),
        ("赤金变化", daily.get("pureGoldDelta")),
        ("碎片变化", daily.get("shardDelta")),
        ("无人机/日", daily.get("droneCount")),
        ("实际使用无人机", daily.get("droneUsed")),
        ("无人机充能加成", daily.get("droneGenerationBonusPercent")),
        ("无人机收入", resource_map(drone.get("income", {}))),
        ("无人机支出", resource_map(drone.get("costs", {}))),
        ("Power Adjustment", power_adjustment_summary(power)),
        ("补练数", candidate.get("upgradeCount")),
    ]
    return (
        "<div class=\"card\">"
        f"<h3>{escape(title)}</h3>"
        f"<span class=\"pill\">{escape(str(candidate.get('profileLabel', '')))}</span>"
        f"<span class=\"pill\">{escape(str(candidate.get('candidateRole', 'primary')))}</span>"
        f"<span class=\"pill\">Drone {escape(str(candidate.get('dronePolicy', '-')))}</span>"
        f"<span class=\"pill\">布局 {escape(str(candidate.get('layout', '')))}</span>"
        f"<span class=\"pill\">模式 {escape(str(candidate.get('mode', '')))}</span>"
        f"<span class=\"pill\">目标 {escape(str(target_fit.get('fitLevel', '-')))}</span>"
        f"<span class=\"pill\">电力余量 {escape(str(power.get('margin', '-')))}</span>"
        f"<span class=\"pill\">Power {escape(power_adjustment_summary(power))}</span>"
        f"{small_table(rows)}"
        "</div>"
    )


def render_objective_section(report: dict[str, Any]) -> str:
    intent = report.get("recommendationIntent") or {}
    comparison = report.get("objectiveComparison") or {}
    contract = intent.get("preferredContract") or {}
    rows = [
        ("偏好布局", intent.get("preferredLayout")),
        ("偏好模式", intent.get("preferredMode")),
        ("预设契约", contract.get("id")),
        ("主指标", ", ".join(contract.get("primaryMetrics") or [])),
        ("LMD 口径", contract.get("lmdPolicy")),
        ("无人机口径", contract.get("drones")),
        ("要求合成玉", "是" if intent.get("requiresOrundum") else "否"),
        ("基线合成玉", intent.get("baselineOrundum")),
        ("模式偏好", intent.get("modePreferenceReason")),
        ("结论", comparison.get("summary")),
    ]
    profile_rows = []
    for item in comparison.get("profiles", []):
        profile_rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('label', '-')))}</td>"
            f"<td>{escape(str(item.get('scalarCandidateId', '-')))}</td>"
            f"<td>{escape(str(item.get('scalarFitLevel', '-')))}</td>"
            f"<td>{escape(str(item.get('targetCandidateId', '-')))}</td>"
            f"<td>{fmt(item.get('scoreDeltaFromScalar'))}</td>"
            f"<td>{escape(str(item.get('message', '-')))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"card\">"
        + small_table(rows)
        + "</div>"
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>口径</th><th>标量冠军</th><th>标量目标等级</th><th>目标匹配冠军</th><th>目标-标量分差</th><th>说明</th>"
        "</tr></thead><tbody>"
        + "".join(profile_rows)
        + "</tbody></table></div>"
    )


def render_impact_table(report: dict[str, Any]) -> str:
    rows = []
    for label, key in [
        ("最高产出补练", "trainingImpact"),
        ("考虑成本补练", "costAdjustedTrainingImpact"),
    ]:
        item = report.get(key) or {}
        rows.append(
            "<tr>"
            f"<td>{escape(label)}</td>"
            f"<td>{escape(str(item.get('status', '-')))}</td>"
            f"<td>{fmt(item.get('scoreDelta'))}</td>"
            f"<td>{fmt(item.get('upgradeCount'))}</td>"
            f"<td>{escape(str(item.get('message', '-')))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>口径</th><th>状态</th><th>分数差</th><th>补练数量</th><th>说明</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_baseline_section(baseline: dict[str, Any]) -> str:
    return (
        "<h2>手排表验算</h2>"
        "<div class=\"cards\">"
        + render_candidate_like_card("手排表", baseline)
        + "</div>"
    )


def render_candidate_like_card(title: str, item: dict[str, Any]) -> str:
    daily = item.get("dailyExpected", {})
    economics = item.get("validationEconomics", {})
    drone = economics.get("droneAccounting", {})
    return (
        "<div class=\"card\">"
        f"<h3>{escape(title)}</h3>"
        + small_table(
            [
                ("综合分", item.get("score")),
                ("龙门币毛", daily.get("lmdGross")),
                ("龙门币净", daily.get("lmdNet")),
                ("经验", daily.get("exp")),
                ("合成玉", daily.get("orundum")),
                ("办公室效率", daily.get("officeSpeed")),
                ("材料龙门币成本", (daily.get("materialCosts") or {}).get("4001")),
                ("无人机收入", resource_map(drone.get("income", {}))),
                ("无人机支出", resource_map(drone.get("costs", {}))),
            ]
        )
        + "</div>"
    )


def render_yituliu_table(cases: list[dict[str, Any]]) -> str:
    body = []
    for case in cases:
        expected = case.get("expectedDaily", {})
        economics = case.get("validationEconomics", {})
        drone = economics.get("droneAccounting", {})
        body.append(
            "<tr>"
            f"<td>{escape(str(case.get('title', case.get('id', ''))))}</td>"
            f"<td>{escape(str(case.get('mode', '-')))}</td>"
            f"<td>{escape(str(case.get('algorithmCoverage', '-')))}</td>"
            f"<td>{'通过' if case.get('passed') else '未通过'}</td>"
            f"<td>{fmt(expected.get('lmdGross'))}</td>"
            f"<td>{fmt(economics.get('lmdNet', expected.get('lmdNet')))}</td>"
            f"<td>{fmt((economics.get('materialCosts') or {}).get('4001'))}</td>"
            f"<td>{fmt(expected.get('exp'))}</td>"
            f"<td>{fmt(expected.get('orundum'))}</td>"
            f"<td>{fmt(expected.get('officeSpeed'))}</td>"
            f"<td>{resource_map(drone.get('income', {}))}</td>"
            f"<td>{resource_map(drone.get('costs', {}))}</td>"
            f"<td>{escape(str(case.get('source', '-')))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>案例</th><th>模式</th><th>覆盖口径</th><th>公式验算</th>"
        "<th>龙门币毛</th><th>龙门币净</th><th>材料龙门币成本</th>"
        "<th>经验</th><th>合成玉</th><th>办公室效率</th><th>无人机收入</th><th>无人机支出</th><th>来源</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def render_candidate_table(candidates: list[dict[str, Any]]) -> str:
    rows = []
    for candidate in candidates:
        daily = candidate.get("dailyExpected", {})
        economics = candidate.get("validationEconomics", {})
        drone = economics.get("droneAccounting", {})
        export_info = candidate.get("scheduleExport") or {}
        href = escape(str(export_info.get("relativePath") or export_info.get("path") or ""))
        power = candidate.get("powerStatus") or {}
        target_fit = candidate.get("targetFit") or {}
        rows.append(
            "<tr>"
            f"<td>{escape(str(candidate.get('id', '')))}</td>"
            f"<td><a class=\"download\" href=\"{href}\" download>导出排班 JSON</a></td>"
            f"<td>{escape(str(target_fit.get('fitLevel', '-')))}</td>"
            f"<td>{escape(str(target_fit.get('reason', '-')))}</td>"
            f"<td>{escape(str(candidate.get('candidateRole', '-')))}</td>"
            f"<td>{escape(str(candidate.get('dronePolicy', '-')))}</td>"
            f"<td>{escape(str(candidate.get('profileLabel', '')))}</td>"
            f"<td>{escape(str(candidate.get('layout', '')))}</td>"
            f"<td>{escape(str(candidate.get('mode', '')))}</td>"
            f"<td>{'是' if candidate.get('allowUpgrades') else '否'}</td>"
            f"<td>{fmt(candidate.get('score'))}</td>"
            f"<td>{fmt(candidate.get('baselineMargin'))}</td>"
            f"<td>{fmt(daily.get('lmdGross'))}</td>"
            f"<td>{fmt(daily.get('lmdNet'))}</td>"
            f"<td>{fmt(daily.get('exp'))}</td>"
            f"<td>{fmt(daily.get('orundum'))}</td>"
            f"<td>{fmt(daily.get('officeSpeed'))}</td>"
            f"<td>{fmt(daily.get('pureGoldDelta'))}</td>"
            f"<td>{fmt(daily.get('shardDelta'))}</td>"
            f"<td>{fmt(power.get('margin'))}</td>"
            f"<td>{escape(power_adjustment_summary(power))}</td>"
            f"<td>{fmt(daily.get('droneCount'))}</td>"
            f"<td>{resource_map(drone.get('income', {}))}</td>"
            f"<td>{resource_map(drone.get('costs', {}))}</td>"
            f"<td>{fmt(candidate.get('upgradeCount'))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>ID</th><th>导出</th><th>目标匹配</th><th>目标说明</th><th>候选类型</th><th>Drone Policy</th><th>评分口径</th><th>布局</th><th>模式</th><th>补练</th>"
        "<th>分数</th><th>基线差</th><th>龙门币毛</th><th>龙门币净</th><th>经验</th><th>合成玉</th><th>办公室效率</th>"
        "<th>赤金</th><th>碎片</th><th>电力余量</th><th>Power Adjustment</th><th>无人机/日</th><th>无人机收入</th><th>无人机支出</th><th>补练数</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_counterintuitive_diagnostics(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        experiment = item.get("forcedExperiment") or {}
        rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('title', item.get('id', ''))))}</td>"
            f"<td>{'已满足' if item.get('currentlySatisfied') else '未满足'}</td>"
            f"<td>{escape(str(item.get('modeledRule', '-')))}</td>"
            f"<td>{escape(str(experiment.get('status', '-')))}</td>"
            f"<td>{fmt(experiment.get('scoreDelta'))}</td>"
            f"<td>{resource_map(experiment.get('dailyExpectedDelta') or {})}</td>"
            f"<td>{escape(str(item.get('conclusion', '-')))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>案例</th><th>当前状态</th><th>建模情况</th><th>强制实验</th><th>分数差</th><th>资源差</th><th>结论</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_anomaly_table(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        experiment = item.get("forcedExperiment") or {}
        explanation = item.get("dependencyExplanation") or {}
        insertion_summary = item.get("insertionSearchSummary") or {}
        rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('operator', '-')))}</td>"
            f"<td>{escape(str(item.get('buffName', '-')))}</td>"
            f"<td>{escape(str(item.get('type', '-')))}</td>"
            f"<td>{escape(str(item.get('reason', '-')))}</td>"
            f"<td>{escape(str(explanation.get('summary', '-')))}</td>"
            f"<td>{fmt(item.get('solutionCount'))}</td>"
            f"<td>{aggregate_status_label(item)}</td>"
            f"<td>{fmt(item.get('priorityScore'))}</td>"
            f"<td>{escape(str(experiment.get('status', '-')))}</td>"
            f"<td>{fmt(experiment.get('scoreDelta'))}</td>"
            f"<td>{resource_map(insertion_summary.get('statusCounts') or {})}</td>"
            f"<td>{escape(str(item.get('conclusion', '-')))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>干员</th><th>技能</th><th>类型</th><th>可疑点</th><th>点名解释</th><th>涉及解数</th><th>当前状态</th><th>优先级</th><th>强制实验</th><th>分数差</th><th>插入搜索</th><th>结论</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_solution_diagnostics_summary(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        summary = item.get("solutionSummary") or {}
        forced_positive = 0
        named_explained = 0
        insertion_summary = (item.get("diagnosticInsertionSearch") or {}).get("summary") or {}
        for anomaly in item.get("anomalyCandidates", []):
            if anomaly.get("type") == "named_dependency" and anomaly.get("dependencyExplanation"):
                named_explained += 1
            experiment = anomaly.get("forcedExperiment") or {}
            if experiment.get("status") == "evaluated" and float(experiment.get("scoreDelta") or 0.0) > 0:
                forced_positive += 1
        rows.append(
            "<tr>"
            f"<td>{escape(str(summary.get('group', '-')))}</td>"
            f"<td>{escape(str(summary.get('profileLabel', summary.get('profile', '-'))))}</td>"
            f"<td>{escape(str(summary.get('layout', '-')))}</td>"
            f"<td>{escape(str(summary.get('mode', '-')))}</td>"
            f"<td>{fmt(summary.get('score'))}</td>"
            f"<td>{fmt(summary.get('upgradeCount'))}</td>"
            f"<td>{fmt(len(item.get('anomalyCandidates', [])))}</td>"
            f"<td>{fmt(forced_positive)}</td>"
            f"<td>{fmt(named_explained)}</td>"
            f"<td>{fmt(insertion_summary.get('acceptedCount'))}</td>"
            f"<td>{fmt(insertion_summary.get('evaluatedNotImprovingCount'))}</td>"
            f"<td>{fmt(insertion_summary.get('unplaceableCount'))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>分组</th><th>评分口径</th><th>布局</th><th>模式</th><th>分数</th><th>补练数</th><th>异常数</th><th>强制更优数</th><th>已解释点名数</th><th>接受插入</th><th>尝试不增益</th><th>无法放置</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_diagnostic_insertion_coverage(coverage: dict[str, Any]) -> str:
    if not coverage:
        return "<p class=\"note\">暂无诊断派生插入搜索记录。</p>"
    summary = coverage.get("summary") or {}
    rows = []
    for item in coverage.get("solutions", []):
        optimizer_summary = item.get("optimizerSummary") or {}
        rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('solutionId', '-')))}</td>"
            f"<td>{escape(str(item.get('group', '-')))}</td>"
            f"<td>{escape(str(item.get('profile', '-')))}</td>"
            f"<td>{escape(str(item.get('layout', '-')))}</td>"
            f"<td>{escape(str(item.get('mode', '-')))}</td>"
            f"<td>{fmt(item.get('namedDependencyAnomalies'))}</td>"
            f"<td>{fmt(item.get('matchedInsertionRecords'))}</td>"
            f"<td>{fmt(optimizer_summary.get('acceptedCount'))}</td>"
            f"<td>{fmt(optimizer_summary.get('evaluatedNotImprovingCount'))}</td>"
            f"<td>{fmt(optimizer_summary.get('unplaceableCount'))}</td>"
            f"<td>{resource_map(item.get('statusCounts') or {})}</td>"
            "</tr>"
        )
    return (
        "<div class=\"card\">"
        + small_table(
            [
                ("候选解数量", summary.get("solutions")),
                ("点名依赖异常", summary.get("namedDependencyAnomalies")),
                ("匹配到插入搜索记录", summary.get("matchedInsertionRecords")),
                ("已接受插入", summary.get("accepted")),
                ("曾接受后被置换", summary.get("displaced")),
                ("尝试但不增益", summary.get("evaluatedNotImproving")),
                ("无法放置", summary.get("unplaceable")),
                ("Diagnosed not searched", summary.get("notSearched")),
                ("诊断缺少匹配规格", summary.get("noSpecKey")),
                ("诊断有但无插入组", summary.get("missingInsertionGroup")),
            ]
        )
        + "</div>"
        "<div class=\"scroll\"><table><thead><tr>"
        "<th>ID</th><th>分组</th><th>口径</th><th>布局</th><th>模式</th><th>点名异常</th><th>匹配搜索</th><th>候选接受</th><th>候选不增益</th><th>候选无法放置</th><th>状态分布</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def aggregate_status_label(item: dict[str, Any]) -> str:
    solution_count = item.get("solutionCount")
    if solution_count:
        return (
            "全部满足"
            if item.get("currentlySatisfied")
            else f"部分未满足 ({fmt(item.get('unsatisfiedSolutionCount'))}/{fmt(solution_count)})"
        )
    return "已满足" if item.get("currentlySatisfied") else "未满足"


def small_table(rows: list[tuple[str, Any]]) -> str:
    return (
        "<table><tbody>"
        + "".join(
            f"<tr><td>{escape(label)}</td><td>{fmt(value)}</td></tr>"
            for label, value in rows
        )
        + "</tbody></table>"
    )


def resource_map(data: dict[str, Any]) -> str:
    if not data:
        return "-"
    return "; ".join(f"{escape(str(key))}: {fmt(value)}" for key, value in sorted(data.items()))


def fmt(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        if abs(float(value)) < 0.0005:
            value = 0
        return f"{float(value):,.3f}".rstrip("0").rstrip(".")
    return escape(str(value))
