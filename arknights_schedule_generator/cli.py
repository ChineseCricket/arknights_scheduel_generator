from __future__ import annotations

import argparse
import json
from pathlib import Path

from .calibration import build_calibration_report
from .data import GameData, download_data
from .exporter import write_result_json
from .full_roster import write_full_roster_xlsx
from .optimizer import ScheduleOptimizer, apply_layout_variant, parse_layout
from .power import RIGHT_SIDE_PRESETS, apply_right_side_preset
from .production import (
    DEFAULT_MAX_DRONE_CYCLE_REPEATS,
    DEFAULT_PURE_GOLD_TARGET_PER_DAY,
    DEFAULT_PURE_GOLD_TOLERANCE,
    ProductionSimulator,
)
from .recommender import DEFAULT_LAYOUTS, DEFAULT_MODES, recommend_schedules
from .roster import load_roster_xlsx
from .schedule_import import load_yituliu_schedule


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "update-data":
        paths = download_data(Path(args.data_dir), force=args.force)
        print("已更新游戏数据:")
        for path in paths:
            print(f"- {path}")
        return 0
    if args.command == "generate":
        return generate(args)
    if args.command == "score":
        return score(args)
    if args.command == "recommend":
        return recommend(args)
    if args.command == "make-full-roster":
        return make_full_roster(args)
    if args.command == "calibrate":
        return calibrate(args)
    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ak-schedule",
        description="Generate and score Arknights base schedules from Yituliu exports.",
    )
    subparsers = parser.add_subparsers(dest="command")

    update = subparsers.add_parser("update-data", help="Download latest game data cache.")
    update.add_argument("--data-dir", default="data/cache", help="Directory for JSON data cache.")
    update.add_argument("--force", action="store_true", help="Overwrite existing cached data.")

    generate_parser = subparsers.add_parser("generate", help="Generate schedule JSON.")
    add_common_data_args(generate_parser)
    generate_parser.add_argument("--layout", default="243", help="Base layout, e.g. 243, 333, 342.")
    generate_parser.add_argument(
        "--layout-variant",
        default="default",
        help="Room level/slot preset. Use default/full, or 252-11 through 252-15 for 252 variants.",
    )
    add_right_side_arg(generate_parser)
    generate_parser.add_argument(
        "--mode",
        default="balanced-orundum",
        help="normal, balanced-orundum/搓玉, or max-orundum.",
    )
    generate_parser.add_argument(
        "--allow-upgrades",
        action="store_true",
        help="Allow schedules that require raising owned operators to unlock base skills.",
    )
    generate_parser.add_argument(
        "--upgrade-cost-weight",
        type=float,
        default=0.015,
        help="Penalty weight for estimated upgrade resource cost.",
    )
    generate_parser.add_argument("--shift-count", type=int, default=2)
    generate_parser.add_argument("--shift-hours", type=int, default=12)
    generate_parser.add_argument(
        "--shift-times",
        default=None,
        help="Comma-separated shift starts, e.g. 08:00,20:00.",
    )
    generate_parser.add_argument(
        "--shard-formula",
        choices=["rock", "device"],
        default="rock",
        help="Originium shard recipe: rock uses 固源岩, device uses 装置.",
    )
    add_drone_policy_arg(generate_parser, default="auto")
    add_pure_gold_balance_args(generate_parser)
    generate_parser.add_argument("--min-lmd-gross", type=float, default=0.0)
    generate_parser.add_argument("--min-exp", type=float, default=0.0)
    generate_parser.add_argument("--min-orundum", type=float, default=0.0)
    generate_parser.add_argument(
        "--profile-runtime",
        action="store_true",
        help="Attach optimizer runtime counters and cache statistics under analysis.runtimeProfile.",
    )
    generate_parser.add_argument("--output", default="outputs/schedule.json")

    score_parser = subparsers.add_parser("score", help="Score an existing Yituliu schedule JSON.")
    add_common_data_args(score_parser)
    score_parser.add_argument("--schedule", required=True, help="Path to Yituliu schedule JSON.")
    score_parser.add_argument(
        "--mode",
        default="balanced-orundum",
        help="Scoring preset label for metadata; production is inferred from room products.",
    )
    score_parser.add_argument(
        "--allow-upgrades",
        action="store_true",
        help="Include locked but reachable base skills while scoring imported schedule.",
    )
    score_parser.add_argument(
        "--upgrade-cost-weight",
        type=float,
        default=0.015,
        help="Penalty weight for locked skill scoring in imported schedule.",
    )
    score_parser.add_argument(
        "--shard-formula",
        choices=["rock", "device"],
        default="rock",
        help="Originium shard recipe for material/LMD costs.",
    )
    score_parser.add_argument(
        "--metric-profile",
        choices=["formula", "guide", "all"],
        default="guide",
        help="Scoring assumptions to attach to the report.",
    )
    add_drone_policy_arg(score_parser)
    add_pure_gold_balance_args(score_parser, include_cycle_repeats=False)
    score_parser.add_argument("--output", default=None, help="Optional JSON score report path.")

    recommend_parser = subparsers.add_parser(
        "recommend",
        help="Evaluate common layouts/modes and write best current/upgraded schedules.",
    )
    add_common_data_args(recommend_parser)
    recommend_parser.add_argument(
        "--baseline-schedule",
        default=None,
        help="Optional existing Yituliu schedule JSON that generated solutions must beat.",
    )
    recommend_parser.add_argument(
        "--layouts",
        default=",".join(DEFAULT_LAYOUTS),
        help="Comma-separated base layouts to evaluate, e.g. 243,252,342.",
    )
    recommend_parser.add_argument(
        "--modes",
        default=",".join(DEFAULT_MODES),
        help="Comma-separated modes to evaluate: normal, balanced-orundum, max-orundum.",
    )
    add_right_side_arg(recommend_parser)
    recommend_parser.add_argument("--shift-count", type=int, default=2)
    recommend_parser.add_argument(
        "--shift-counts",
        default=None,
        help="Comma-separated shift counts to compare for recommend, e.g. 2,3. Defaults to --shift-count.",
    )
    recommend_parser.add_argument("--shift-hours", type=int, default=12)
    recommend_parser.add_argument(
        "--shift-patterns",
        default=None,
        help="Comma-separated shift count/hour pairs, e.g. 2x12,3x8. Overrides --shift-counts/--shift-hours.",
    )
    recommend_parser.add_argument(
        "--shift-times",
        default=None,
        help="Comma-separated shift starts, e.g. 08:00,20:00.",
    )
    recommend_parser.add_argument(
        "--shard-formula",
        choices=["rock", "device"],
        default="rock",
        help="Originium shard recipe.",
    )
    add_drone_policy_arg(recommend_parser, default="auto")
    add_pure_gold_balance_args(recommend_parser)
    recommend_parser.add_argument(
        "--upgrade-cost-weight",
        type=float,
        default=0.015,
        help="Penalty weight for estimated upgrade resource cost.",
    )
    recommend_parser.add_argument(
        "--allow-upgrades",
        action="store_true",
        help="Also run upgrade-planning profiles. Defaults to current-roster scheduling only.",
    )
    recommend_parser.add_argument("--min-lmd-gross", type=float, default=0.0)
    recommend_parser.add_argument("--min-exp", type=float, default=0.0)
    recommend_parser.add_argument("--min-orundum", type=float, default=0.0)
    recommend_parser.add_argument(
        "--no-enforce-baseline",
        action="store_true",
        help="Write the report even if no candidate beats the baseline schedule.",
    )
    recommend_parser.add_argument(
        "--jobs",
        default="auto",
        help="Candidate worker count for recommendation runs. Use auto for min(cpu_count - 1, 8).",
    )
    recommend_parser.add_argument(
        "--cache-policy",
        choices=["auto", "refresh", "off"],
        default="auto",
        help="Reuse candidate schedule JSON when the input fingerprint matches; refresh recomputes and off disables reads.",
    )
    recommend_parser.add_argument(
        "--profile-runtime",
        action="store_true",
        help="Attach optimizer runtime counters and cache statistics under each generated schedule analysis.runtimeProfile.",
    )
    recommend_parser.add_argument("--output-dir", default="outputs/recommendation")

    calibrate_parser = subparsers.add_parser(
        "calibrate", help="Run production calibration examples and write a report."
    )
    calibrate_parser.add_argument("--data-dir", default="data/cache", help="Directory for game data JSON.")
    calibrate_parser.add_argument(
        "--auto-update",
        action="store_true",
        help="Download missing game data before running.",
    )
    calibrate_parser.add_argument(
        "--shard-formula",
        choices=["rock", "device"],
        default="rock",
        help="Originium shard recipe for material/LMD costs.",
    )
    calibrate_parser.add_argument(
        "--profile",
        choices=["formula", "guide", "all"],
        default="all",
        help="Calibration case profile to run.",
    )
    calibrate_parser.add_argument("--schedule", default=None, help="Optional Yituliu schedule JSON.")
    calibrate_parser.add_argument(
        "--guide-samples",
        default=None,
        help="Comma-separated Yituliu/guide schedule JSON files to score as external samples.",
    )
    calibrate_parser.add_argument("--roster", default=None, help="Optional roster xlsx for schedule scoring.")
    calibrate_parser.add_argument(
        "--allow-upgrades",
        action="store_true",
        help="Include locked but reachable base skills while scoring imported schedule.",
    )
    calibrate_parser.add_argument(
        "--upgrade-cost-weight",
        type=float,
        default=0.015,
        help="Penalty weight for locked skill scoring in imported schedule.",
    )
    calibrate_parser.add_argument(
        "--output",
        default="outputs/production_calibration_report.json",
        help="Calibration report JSON path.",
    )
    add_drone_policy_arg(calibrate_parser)

    full_roster_parser = subparsers.add_parser(
        "make-full-roster",
        help="Write a Yituliu-format maxed operator roster fixture for optimizer benchmarks.",
    )
    full_roster_parser.add_argument("--data-dir", default="data/cache", help="Directory for game data JSON.")
    full_roster_parser.add_argument(
        "--auto-update",
        action="store_true",
        help="Download missing game data before running.",
    )
    full_roster_parser.add_argument(
        "--output",
        default="outputs/fixtures/yituliu_full_roster_maxed.xlsx",
        help="Output .xlsx path.",
    )
    return parser


def add_common_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--roster", required=True, help="Path to 干员练度表.xlsx.")
    parser.add_argument("--data-dir", default="data/cache", help="Directory for game data JSON.")
    parser.add_argument(
        "--auto-update",
        action="store_true",
        help="Download missing game data before running.",
    )


def add_drone_policy_arg(parser: argparse.ArgumentParser, default: str = "none") -> None:
    parser.add_argument(
        "--drone-policy",
        choices=["none", "lmd-trade", "gold-factory", "shard-factory", "exp-factory", "auto"],
        default=default,
        help="Drone usage model. Calibration defaults to none to keep base production separate.",
    )


def add_pure_gold_balance_args(
    parser: argparse.ArgumentParser,
    *,
    include_cycle_repeats: bool = True,
) -> None:
    parser.add_argument(
        "--pure-gold-target",
        type=float,
        default=DEFAULT_PURE_GOLD_TARGET_PER_DAY,
        help="Auto-drone target for daily Pure Gold delta. Default assumes about 2 external Pure Gold/day.",
    )
    parser.add_argument(
        "--pure-gold-tolerance",
        type=float,
        default=DEFAULT_PURE_GOLD_TOLERANCE,
        help="Acceptable absolute deviation from --pure-gold-target for auto drone balancing.",
    )
    if include_cycle_repeats:
        parser.add_argument(
            "--max-drone-cycle-repeats",
            type=int,
            default=DEFAULT_MAX_DRONE_CYCLE_REPEATS,
            help="Maximum full schedule-cycle repeats used to improve auto-drone Pure Gold granularity.",
        )


def add_right_side_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--right-side",
        choices=RIGHT_SIDE_PRESETS,
        default="full",
        help="Right-side/base power preset: full maxes right-side facilities and dorms, tries factory-only power fitting first, then reports an explicit dorm fallback if factory-only is impossible; strict-full keeps all rooms maxed and skips infeasible layouts; guide uses max right-side with level-1 dorms; basic uses level-1 right-side/dorms; ignore disables power feasibility filtering.",
    )


def generate(args: argparse.Namespace) -> int:
    game_data = load_game_data(args)
    roster = load_roster_xlsx(Path(args.roster))
    layout = apply_right_side_preset(
        apply_layout_variant(parse_layout(args.layout), args.layout_variant),
        args.right_side,
    )
    optimizer = ScheduleOptimizer(
        game_data,
        roster,
        allow_upgrades=args.allow_upgrades,
        upgrade_cost_weight=args.upgrade_cost_weight,
        shard_formula=args.shard_formula,
    )
    result = optimizer.optimize(
        layout,
        mode=args.mode,
        shift_count=args.shift_count,
        shift_hours=args.shift_hours,
        shift_times=parse_shift_times(args.shift_times),
        drone_policy=args.drone_policy,
        min_lmd_gross=args.min_lmd_gross,
        min_exp=args.min_exp,
        min_orundum=args.min_orundum,
        pure_gold_target=args.pure_gold_target,
        pure_gold_tolerance=args.pure_gold_tolerance,
        max_drone_cycle_repeats=args.max_drone_cycle_repeats,
        profile_runtime=args.profile_runtime,
    )
    output = Path(args.output)
    write_result_json(output, result, game_data)
    print(f"已生成排班 JSON: {output}")
    print_summary(result.score, result.production_report.to_dict() if result.production_report else {})
    for warning in result.warnings:
        print(f"- {warning}")
    return 0


def score(args: argparse.Namespace) -> int:
    game_data = load_game_data(args)
    roster = load_roster_xlsx(Path(args.roster))
    imported = load_yituliu_schedule(
        Path(args.schedule),
        game_data,
        roster,
        allow_upgrades=args.allow_upgrades,
        upgrade_cost_weight=args.upgrade_cost_weight,
    )
    report = ProductionSimulator(
        game_data,
        shard_formula=args.shard_formula,
        drone_policy=args.drone_policy,
        calibration_profile=args.metric_profile,
        pure_gold_target=args.pure_gold_target,
        pure_gold_tolerance=args.pure_gold_tolerance,
    ).evaluate(imported.shifts)
    conflicts = conflicts_for_shifts(imported.shifts)
    output_data = {
        "schedule": str(Path(args.schedule)),
        "layout": imported.layout.label,
        "mode": args.mode,
        "score": report.score,
        "scoreBreakdown": report.to_dict()["scoreBreakdown"],
        "dailyExpected": report.to_dict()["dailyExpected"],
        "analysis": {
            "warnings": imported.warnings,
            "conflicts": conflicts,
            "unsupportedSkillEffects": report.to_dict()["unsupportedSkillEffects"],
            "assumptions": report.to_dict()["assumptions"],
            "calibrationProfile": report.to_dict()["calibrationProfile"],
            "sourceAssumptions": report.to_dict()["sourceAssumptions"],
            "guideComparison": report.to_dict()["guideComparison"],
            "roomReports": report.to_dict()["roomReports"],
        },
    }
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入评分报告: {output_path}")
    print_summary(report.score, report.to_dict())
    if conflicts:
        print("冲突:")
        for conflict in conflicts:
            print(f"- {conflict}")
    return 0


def recommend(args: argparse.Namespace) -> int:
    game_data = load_game_data(args)
    roster = load_roster_xlsx(Path(args.roster))
    report = recommend_schedules(
        game_data,
        roster,
        output_dir=Path(args.output_dir),
        baseline_schedule=Path(args.baseline_schedule) if args.baseline_schedule else None,
        layouts=parse_csv(args.layouts),
        modes=parse_csv(args.modes),
        shift_count=args.shift_count,
        shift_counts=parse_int_csv(args.shift_counts) if args.shift_counts else None,
        shift_hours=args.shift_hours,
        shift_patterns=parse_shift_patterns(args.shift_patterns),
        shift_times=parse_shift_times(args.shift_times),
        shard_formula=args.shard_formula,
        drone_policy=args.drone_policy,
        upgrade_cost_weight=args.upgrade_cost_weight,
        right_side=args.right_side,
        min_lmd_gross=args.min_lmd_gross,
        min_exp=args.min_exp,
        min_orundum=args.min_orundum,
        include_upgrades=args.allow_upgrades,
        pure_gold_target=args.pure_gold_target,
        pure_gold_tolerance=args.pure_gold_tolerance,
        max_drone_cycle_repeats=args.max_drone_cycle_repeats,
        jobs=args.jobs,
        cache_policy=args.cache_policy,
        profile_runtime=args.profile_runtime,
    )
    written = report["writtenFiles"]
    print(f"已写入推荐报告: {written['report']}")
    if "htmlReport" in written:
        print(f"HTML 阅读报告: {written['htmlReport']}")
    if "bestCurrentSchedule" in written:
        print(f"当前练度最佳排班: {written['bestCurrentSchedule']}")
    if "bestUpgradesSchedule" in written:
        print(f"补练最高产出排班: {written['bestUpgradesSchedule']}")
    if "bestUpgradesCostAdjustedSchedule" in written:
        print(f"考虑成本补练排班: {written['bestUpgradesCostAdjustedSchedule']}")
    if "bestTargetCompatibleCurrentSchedule" in written:
        print(f"当前练度目标匹配排班: {written['bestTargetCompatibleCurrentSchedule']}")
    if "bestTargetCompatibleUpgradesSchedule" in written:
        print(f"补练目标匹配排班: {written['bestTargetCompatibleUpgradesSchedule']}")
    if "bestTargetCompatibleCostAdjustedSchedule" in written:
        print(f"成本补练目标匹配排班: {written['bestTargetCompatibleCostAdjustedSchedule']}")
    if "upgradeRequirements" in written:
        print(f"最高产出补练干员列表: {written['upgradeRequirements']}")
    if "upgradeRequirementsCostAdjusted" in written:
        print(f"考虑成本补练干员列表: {written['upgradeRequirementsCostAdjusted']}")
    comparison = report.get("baselineComparison")
    if comparison:
        best = comparison["bestOverall"]
        print(
            f"手工基线分数 {comparison['baselineScore']:.3f}; "
            f"最佳候选 {best['score']:.3f}; 差值 {best['margin']:.3f}"
        )
        if not best["passes"] and not args.no_enforce_baseline:
            print("没有候选方案达到手工基线，已保留报告但返回失败状态。")
            return 2
    return 0


def make_full_roster(args: argparse.Namespace) -> int:
    game_data = load_game_data(args)
    output = Path(args.output)
    summary = write_full_roster_xlsx(output, game_data)
    print(f"已生成满练练度表: {output}")
    print(
        "满练表校验: "
        f"生成 {summary['generatedOperatorCount']} / "
        f"回读 {summary['loadedOperatorCount']} / "
        f"已招募 {summary['recruitedOperatorCount']} / "
        f"排除非普通干员 {summary['excludedNonCharOrTokenTrapCount']}"
    )
    return 0


def calibrate(args: argparse.Namespace) -> int:
    game_data = load_game_data(args)
    roster = load_roster_xlsx(Path(args.roster)) if args.roster else None
    report = build_calibration_report(
        game_data,
        shard_formula=args.shard_formula,
        schedule_path=Path(args.schedule) if args.schedule else None,
        guide_samples=parse_paths(args.guide_samples),
        roster=roster,
        allow_upgrades=args.allow_upgrades,
        upgrade_cost_weight=args.upgrade_cost_weight,
        profile=args.profile,
        drone_policy=args.drone_policy,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    print(f"已写入校准报告: {output_path}")
    print(
        f"校准用例: {summary['passed']}/{summary['caseCount']} 通过; "
        f"失败 {summary['failed']} 个。"
    )
    if summary["failedCases"]:
        print("失败用例:")
        for name in summary["failedCases"]:
            print(f"- {name}")
    return 0


def load_game_data(args: argparse.Namespace) -> GameData:
    data_dir = Path(args.data_dir)
    if args.auto_update:
        download_data(data_dir, force=False)
    return GameData.load(data_dir)


def parse_shift_times(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_int_csv(raw: str | None) -> list[int]:
    return [int(part) for part in parse_csv(raw)]


def parse_shift_patterns(raw: str | None) -> list[tuple[int, int]] | None:
    if not raw:
        return None
    patterns: list[tuple[int, int]] = []
    for part in parse_csv(raw):
        normalized = part.lower().replace("*", "x")
        if "x" not in normalized:
            raise ValueError("--shift-patterns entries must look like 2x12 or 3x8.")
        count_raw, hours_raw = normalized.split("x", 1)
        count = int(count_raw)
        hours = int(hours_raw)
        if count < 1 or hours <= 0:
            raise ValueError("--shift-patterns requires count >= 1 and positive hours.")
        patterns.append((count, hours))
    return patterns


def parse_paths(raw: str | None) -> list[Path] | None:
    if not raw:
        return None
    return [Path(part.strip()) for part in raw.split(",") if part.strip()]


def print_summary(score_value: float, report: dict[str, object]) -> None:
    daily = report.get("dailyExpected", {}) if report else {}
    print(f"综合评分: {score_value:.3f}")
    if isinstance(daily, dict):
        print(
            "每日预期: "
            f"龙门币毛 {daily.get('lmdGross', 0)} / "
            f"龙门币净 {daily.get('lmdNet', 0)} / "
            f"经验 {daily.get('exp', 0)} / "
            f"合成玉 {daily.get('orundum', 0)} / "
            f"赤金变化 {daily.get('pureGoldDelta', 0)} / "
            f"碎片变化 {daily.get('shardDelta', 0)}"
        )


def conflicts_for_shifts(shifts) -> list[str]:
    conflicts: list[str] = []
    for shift in shifts:
        seen: dict[str, str] = {}
        for room in [*shift.rooms, *shift.dormitories]:
            for skill in room.operators:
                previous = seen.get(skill.operator_name)
                if previous:
                    conflicts.append(f"{shift.name}/{skill.operator_name}: {previous} + {room.room_id}")
                else:
                    seen[skill.operator_name] = room.room_id
    return conflicts


if __name__ == "__main__":
    raise SystemExit(main())
