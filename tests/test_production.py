from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from dataclasses import replace
import json
import unittest

from arknights_schedule_generator.data import GameData
from arknights_schedule_generator.calibration import build_calibration_report
from arknights_schedule_generator.diagnostics import (
    DiagnosticContext,
    ForceSpec,
    build_recommendation_diagnostics,
    first_available_with_tag,
    force_specs_into_shift,
)
from arknights_schedule_generator.exporter import result_to_dict
from arknights_schedule_generator.full_roster import write_full_roster_xlsx
from arknights_schedule_generator.models import (
    BaseSkill,
    RoomAssignment,
    RosterOperator,
    ShiftPlan,
    UpgradeRequirement,
)
from arknights_schedule_generator.optimizer import (
    OptimizerResult,
    ScheduleOptimizer,
    apply_layout_variant,
    parse_layout,
    target_selection_score,
)
from arknights_schedule_generator.power import apply_right_side_preset, power_status
from arknights_schedule_generator.presets import preset_contract
from arknights_schedule_generator.production import (
    GUIDE_MANUFACTURE_LOOKUPS,
    GUIDE_LMD_TRADE_LOOKUPS,
    ProductionReport,
    ProductionSimulator,
    ProductionVector,
    fatigue_risk,
    resource_balance_quality,
    score_breakdown_for,
)
from arknights_schedule_generator.recommender import (
    DEFAULT_LAYOUTS,
    aggregate_anomaly_candidates,
    aggregate_diagnostic_insertion_coverage,
    best_candidate,
    best_reference_candidate,
    dependency_tradeoff_summary,
    guide_operator_anchor_preference,
    guide_field_comparison,
    guide_delta_gap_reason,
    operator_comparison,
    reference_drone_policies,
    reference_fit_seed_drone_policy,
    reference_resource_fit,
    reference_trade_insertion_groups,
    recommend_schedules,
    exported_schedule_capacity_valid,
    schedule_capacity_valid,
)
from arknights_schedule_generator.schedule_import import load_yituliu_schedule
from arknights_schedule_generator.skill_rules import (
    RoomSkillEffect,
    evaluate_room_effect,
    fatigue_delta_from_text,
)
from arknights_schedule_generator.web_app import ParsedForm, run_recommendation


class ProductionModelTest(unittest.TestCase):
    def test_default_layouts_exclude_invalid_324(self) -> None:
        self.assertNotIn("324", DEFAULT_LAYOUTS)
        self.assertIn("153", DEFAULT_LAYOUTS)

    def test_score_breakdown_does_not_penalize_pure_gold_inventory(self) -> None:
        breakdown = score_breakdown_for(ProductionVector(pureGoldDelta=-100.0))

        self.assertNotIn("pureGoldBalance", breakdown)
        self.assertEqual(sum(breakdown.values()), 0.0)

    def test_basic_manufacture_outputs_and_shard_costs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data)
            rooms = [
                room("manufacture_1", "MANUFACTURE", "F_GOLD"),
                room("manufacture_2", "MANUFACTURE", "F_EXP"),
                room("manufacture_3", "MANUFACTURE", "F_DIAMOND"),
            ]
            report = simulator.evaluate([ShiftPlan("A", "08:00", 12, rooms, [])])

        daily = report.dailyExpected
        self.assertAlmostEqual(daily.pureGoldDelta, 10.3)
        self.assertAlmostEqual(daily.exp, 4120.0)
        self.assertAlmostEqual(daily.shardDelta, 12.36)
        self.assertAlmostEqual(daily.materialCosts["30012"], 24.72)
        self.assertAlmostEqual(daily.materialCosts["4001"], 19776.0)

    def test_guide_manufacture_lookup_calibrates_exp_room_paper_efficiency(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            game_data.data_version = "guide-test"
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            exp_room = RoomAssignment(
                "manufacture_1",
                "MANUFACTURE",
                "MANUFACTURE",
                "F_EXP",
                [
                    skill(
                        "Miss.Christine",
                        "MANUFACTURE",
                        "exp_a",
                        "进驻制造站时，作战记录类配方的生产力+30%",
                        30,
                    ),
                    skill(
                        "食铁兽",
                        "MANUFACTURE",
                        "exp_b",
                        "进驻制造站时，作战记录类配方的生产力+35%",
                        35,
                    ),
                    skill(
                        "弑君者",
                        "MANUFACTURE",
                        "exp_c",
                        "进驻制造站时，作战记录类配方的生产力+35%",
                        35,
                    ),
                ],
                0,
                room_level=3,
                slots=3,
            )

            report = simulator.evaluate([ShiftPlan("A", "08:00", 12, [exp_room], [])])

        room_report = report.roomReports[0]
        lookup = GUIDE_MANUFACTURE_LOOKUPS[0]
        expected_exp = 12 * 3600 / 10800 * 1000 * (1 + (3 + 140) / 100)
        self.assertAlmostEqual(report.dailyExpected.exp, expected_exp)
        self.assertEqual(room_report["speedPercent"], lookup["paperEfficiencyPercent"])
        self.assertEqual(
            room_report["manufactureProfile"]["calibrationMode"],
            "guide_exact_lookup",
        )
        self.assertEqual(room_report["manufactureProfile"]["lookupId"], lookup["id"])

    def test_guide_generic_exp_manufacture_lookup_accepts_85_percent_room(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            exp_room = RoomAssignment(
                "manufacture_1",
                "MANUFACTURE",
                "MANUFACTURE",
                "F_EXP",
                [
                    skill("A", "MANUFACTURE", "exp_a", "作战记录生产力+30%", 30),
                    skill("B", "MANUFACTURE", "exp_b", "作战记录生产力+30%", 30),
                    skill("C", "MANUFACTURE", "exp_c", "作战记录生产力+25%", 25),
                ],
                0,
                room_level=3,
                slots=3,
            )

            report = simulator.evaluate([ShiftPlan("A", "08:00", 12, [exp_room], [])])

        room_report = report.roomReports[0]
        lookup = next(
            item
            for item in GUIDE_MANUFACTURE_LOOKUPS
            if item["id"] == "maxed_exp_reference_l3_generic_126"
        )
        expected_exp = 12 * 3600 / 10800 * 1000 * (1 + (3 + 126) / 100)
        self.assertAlmostEqual(report.dailyExpected.exp, expected_exp)
        self.assertEqual(room_report["manufactureProfile"]["lookupId"], lookup["id"])

    def test_guide_252_room5_exp_lookup_accepts_75_percent_room(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            room5 = RoomAssignment(
                "manufacture_5",
                "MANUFACTURE",
                "MANUFACTURE",
                "F_EXP",
                [
                    skill("A", "MANUFACTURE", "exp_a", "作战记录生产力+30%", 30),
                    skill("B", "MANUFACTURE", "exp_b", "作战记录生产力+25%", 25),
                    skill("C", "MANUFACTURE", "exp_c", "作战记录生产力+20%", 20),
                ],
                0,
                room_level=3,
                slots=3,
            )
            room4 = replace(room5, room_id="manufacture_4")

            room5_report = simulator.evaluate([ShiftPlan("A", "08:00", 12, [room5], [])])
            room4_report = simulator.evaluate([ShiftPlan("A", "08:00", 12, [room4], [])])

        expected_exp = 12 * 3600 / 10800 * 1000 * (1 + (3 + 126) / 100)
        self.assertAlmostEqual(room5_report.dailyExpected.exp, expected_exp)
        self.assertEqual(
            room5_report.roomReports[0]["manufactureProfile"]["lookupId"],
            "maxed_exp_reference_252_room5_generic_126",
        )
        self.assertIsNone(room4_report.roomReports[0]["manufactureProfile"])

    def test_guide_lmd_comparison_falls_back_to_base_when_auto_drones_overshoot(self) -> None:
        comparison = guide_field_comparison(
            {
                "lmdGross": 48939.48,
                "droneContribution": {"lmdGross": 19101.23},
            },
            {
                "lmdGross": 30600.0,
                "lmdGrossWithLmdExtra": 45300.0,
            },
            "lmdGross",
        )

        self.assertTrue(comparison["passed"])
        self.assertEqual(comparison["expectedField"], "lmdGross")
        self.assertEqual(comparison["modeledField"], "lmdGrossWithoutModeledDrones")

    def test_guide_lmd_extra_comparison_uses_wider_drone_budget_tolerance(self) -> None:
        comparison = guide_field_comparison(
            {
                "lmdGross": 42859.28,
                "droneContribution": {"lmdGross": 19101.23},
            },
            {
                "lmdGross": 30600.0,
                "lmdGrossWithLmdExtra": 45300.0,
            },
            "lmdGross",
        )

        self.assertTrue(comparison["passed"])
        self.assertEqual(comparison["expectedField"], "lmdGrossWithLmdExtra")
        self.assertEqual(comparison["modeledField"], "lmdGross")

    def test_guide_exp_comparison_falls_back_to_base_when_auto_drones_overshoot(self) -> None:
        comparison = guide_field_comparison(
            {
                "exp": 44933.0,
                "droneContribution": {"exp": 13053.0},
            },
            {
                "exp": 31700.0,
                "expExtraContribution": 5500.0,
            },
            "exp",
        )

        self.assertTrue(comparison["passed"])
        self.assertEqual(comparison["expectedField"], "exp")
        self.assertEqual(comparison["modeledField"], "expWithoutModeledDrones")

    def test_guide_exp_comparison_can_allow_simplified_reference_overproduction(self) -> None:
        comparison = guide_field_comparison(
            {"exp": 36640.0},
            {"exp": 33600.0, "allowOverproduction": ["exp"]},
            "exp",
        )

        self.assertTrue(comparison["passed"])
        self.assertEqual(comparison["passMode"], "overproduction_allowed")

    def test_static_lmd_base_comparison_allows_extra_split_rounding(self) -> None:
        comparison = guide_field_comparison(
            {
                "lmdGross": 48646.016,
                "droneContribution": {"lmdGross": 19101.0},
            },
            {
                "lmdGross": 30600.0,
                "lmdExtraContribution": 14700.0,
                "lmdGrossWithLmdExtra": 45300.0,
            },
            "lmdGross",
        )

        self.assertTrue(comparison["passed"])
        self.assertEqual(comparison["expectedField"], "lmdGross")
        self.assertEqual(comparison["modeledField"], "lmdGrossWithoutModeledDrones")

    def test_static_lmd_extra_comparison_falls_back_to_base_without_total_field(self) -> None:
        comparison = guide_field_comparison(
            {
                "lmdGross": 60586.26,
                "droneContribution": {"lmdGross": 8400.0},
            },
            {
                "lmdGross": 53100.0,
                "lmdExtraContribution": 12700.0,
            },
            "lmdGross",
        )

        self.assertTrue(comparison["passed"])
        self.assertEqual(comparison["expectedField"], "lmdGross")
        self.assertEqual(comparison["modeledField"], "lmdGrossWithoutModeledDrones")

    def test_office_speed_contributes_to_daily_expected_and_score(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data)
            office = RoomAssignment(
                "hire_1",
                "HIRE",
                "HIRE",
                None,
                [skill("办公室干员", "HIRE", "办公室", "进驻办公室时，人脉资源的联络速度+60%", 60)],
                0,
                room_level=3,
                slots=2,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 12, [office], [])])

        self.assertAlmostEqual(report.dailyExpected.officeSpeed, 30.5)
        self.assertAlmostEqual(report.scoreBreakdown["office"], 30.5 / 30.0)
        self.assertAlmostEqual(report.score, round(30.5 / 30.0, 3))
        self.assertEqual(report.roomReports[0]["stationSlots"], 1)

    def test_control_meeting_link_contributes_to_meeting_speed_and_score(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data)
            control = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "维什戴尔",
                        "CONTROL",
                        "萨卡兹魔王",
                        "进驻控制中枢时，当伊内丝入驻会客室时，会客室线索搜集速度+5%",
                        0,
                    )
                ],
                0,
                room_level=5,
                slots=5,
            )
            meeting = RoomAssignment(
                "meeting_1",
                "MEETING",
                "MEETING",
                None,
                [skill("伊内丝", "MEETING", "信使", "进驻会客室时，线索搜集速度提升10%", 10)],
                0,
                room_level=3,
                slots=2,
            )

            report = simulator.evaluate([ShiftPlan("A", "08:00", 12, [control, meeting], [])])

        self.assertAlmostEqual(report.dailyExpected.meetingSpeed, 8.5)
        self.assertAlmostEqual(report.scoreBreakdown["meeting"], 8.5 / 30.0)
        self.assertTrue(
            any(
                item["scope"] == "control_global:meeting_global"
                for item in report.roomReports[1]["skillEffectAudit"]
            )
        )

    def test_ferryman_control_bonus_counts_minos_and_guards_sargon_fatigue(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data)
            ferryman = skill(
                "摆渡人",
                "CONTROL",
                "英雄的骄傲·α",
                "进驻控制中枢时，基建内（不包含副手及活动室使用者）每有1名米诺斯干员，"
                "会客室线索搜集速度+4%（最多+20%，同种效果取最高）；"
                "当与萨尔贡干员进驻控制中枢一起工作时，自身心情每小时消耗+0.02",
                0,
                faction_tags=("minos",),
            )
            control = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    ferryman,
                    skill("米诺斯队友", "CONTROL", "测试", "进驻控制中枢时，测试", 0, faction_tags=("minos",)),
                ],
                0,
                room_level=5,
                slots=5,
            )
            meeting = RoomAssignment(
                "meeting_1",
                "MEETING",
                "MEETING",
                None,
                [skill("会客员", "MEETING", "会客", "进驻会客室时，线索搜集速度提升10%", 10)],
                0,
                room_level=3,
                slots=2,
            )

            report = simulator.evaluate([ShiftPlan("A", "08:00", 12, [control, meeting], [])])

        control_report = next(item for item in report.roomReports if item["roomType"] == "CONTROL")
        meeting_report = next(item for item in report.roomReports if item["roomType"] == "MEETING")
        self.assertFalse(control_report["unsupportedSkillEffects"])
        self.assertAlmostEqual(control_report["vector"]["fatigueRisk"], 0.0)
        self.assertTrue(
            any(
                item["operator"] == "摆渡人"
                and item["status"] == "condition_unmet"
                and item["scope"] == "fatigue"
                for item in control_report["skillEffectAudit"]
            )
        )
        self.assertAlmostEqual(report.dailyExpected.meetingSpeed, 10.0)
        self.assertTrue(
            any(
                item["operator"] == "摆渡人"
                and item["scope"] == "control_global:meeting_global"
                for item in meeting_report["skillEffectAudit"]
            )
        )

    def test_ferryman_sargon_condition_counts_fatigue_when_same_control_room(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data)
            control = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "摆渡人",
                        "CONTROL",
                        "英雄的骄傲·α",
                        "进驻控制中枢时，基建内（不包含副手及活动室使用者）每有1名米诺斯干员，"
                        "会客室线索搜集速度+4%（最多+20%，同种效果取最高）；"
                        "当与萨尔贡干员进驻控制中枢一起工作时，自身心情每小时消耗+0.02",
                        0,
                        faction_tags=("minos",),
                    ),
                    skill("萨尔贡队友", "CONTROL", "测试", "进驻控制中枢时，测试", 0, faction_tags=("sargon",)),
                ],
                0,
                room_level=5,
                slots=5,
            )

            report = simulator.evaluate([ShiftPlan("A", "08:00", 12, [control], [])])

        control_report = next(item for item in report.roomReports if item["roomType"] == "CONTROL")
        self.assertAlmostEqual(control_report["vector"]["fatigueRisk"], 0.24)
        self.assertTrue(
            any(
                item["operator"] == "摆渡人"
                and item["status"] == "counted"
                and item["scope"] == "fatigue"
                for item in control_report["skillEffectAudit"]
            )
        )

    def test_three_12h_shifts_are_averaged_over_full_cycle(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data)
            shifts = [
                ShiftPlan("A", "08:00", 12, [room("manufacture_1", "MANUFACTURE", "F_GOLD")], []),
                ShiftPlan("B", "20:00", 12, [room("manufacture_1", "MANUFACTURE", "F_GOLD")], []),
                ShiftPlan("C", "08:00", 12, [room("manufacture_1", "MANUFACTURE", "F_GOLD")], []),
            ]
            report = simulator.evaluate(shifts)

        self.assertAlmostEqual(report.dailyExpected.cycleHours, 36.0)
        self.assertAlmostEqual(report.dailyExpected.dailyScale, 2 / 3)
        self.assertAlmostEqual(report.dailyExpected.pureGoldDelta, 20.6)

    def test_auto_drones_are_shift_limited_and_fully_used(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, drone_policy="auto")
            gold_factory = RoomAssignment(
                "manufacture_1",
                "MANUFACTURE",
                "MANUFACTURE",
                "F_GOLD",
                [skill("赤金无人机", "MANUFACTURE", "赤金无人机", "进驻制造站时，生产力+1000%", 1000)],
                0,
                product_capacity=0,
            )
            shard_factory = RoomAssignment(
                "manufacture_2",
                "MANUFACTURE",
                "MANUFACTURE",
                "F_DIAMOND",
                [skill("碎片无人机", "MANUFACTURE", "碎片无人机", "进驻制造站时，生产力+1000%", 1000)],
                0,
                product_capacity=0,
            )
            shifts = [
                ShiftPlan("A", "08:00", 12, [room("trading_1", "TRADING", "O_GOLD"), gold_factory], []),
                ShiftPlan("B", "20:00", 12, [room("trading_2", "TRADING", "O_DIAMOND"), shard_factory], []),
            ]
            report = simulator.evaluate(shifts)

        policies_by_shift = {
            (target["shift"], target["policy"]) for target in report.dailyExpected.droneTargets
        }
        self.assertIn(("B", "shard-factory"), policies_by_shift)
        self.assertEqual(
            len({target["shift"] for target in report.dailyExpected.droneTargets}),
            len(report.dailyExpected.droneTargets),
        )
        self.assertAlmostEqual(report.dailyExpected.droneUsed, report.dailyExpected.droneCount)
        self.assertGreaterEqual(report.dailyExpected.shardDelta, -0.01)

    def test_auto_drones_target_conservative_negative_pure_gold_delta(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            shifts = [
                ShiftPlan(
                    "A",
                    "08:00",
                    24,
                    [
                        room("trading_1", "TRADING", "O_GOLD"),
                        room("manufacture_1", "MANUFACTURE", "F_GOLD"),
                    ],
                    [],
                )
            ]
            report = ProductionSimulator(game_data, drone_policy="auto").evaluate(shifts)

        self.assertEqual(report.dailyExpected.droneTargets[0]["policy"], "lmd-trade")
        self.assertLess(report.dailyExpected.pureGoldDelta, -2.0)

    def test_auto_drone_cycle_repeats_improve_pure_gold_granularity(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            shifts = [
                ShiftPlan(
                    "A",
                    "08:00",
                    24,
                    [
                        room("trading_1", "TRADING", "O_GOLD"),
                        room("manufacture_1", "MANUFACTURE", "F_GOLD"),
                    ],
                    [],
                )
            ]
            simulator = ProductionSimulator(game_data, drone_policy="auto")
            current_report = simulator.evaluate(shifts)
            optimizer = ScheduleOptimizer(game_data, [])

            expanded, report, policy = optimizer._apply_pure_gold_drone_cycle_repeats(
                shifts,
                simulator,
                current_report=current_report,
                drone_policy="auto",
                pure_gold_target=-2.0,
                pure_gold_tolerance=0.5,
                max_drone_cycle_repeats=7,
            )

        self.assertGreater(policy["repeatCount"], 1)
        self.assertEqual(len(expanded), policy["finalShiftCount"])
        self.assertEqual(policy["status"], "within_tolerance")
        self.assertLessEqual(abs(report.dailyExpected.pureGoldDelta - (-2.0)), 0.5)

    def test_trade_outputs_use_slot_base_and_interval_capacity(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data)
            trade_room = room("trading_1", "TRADING", "O_GOLD")
            twelve_hour = simulator.evaluate([ShiftPlan("A", "08:00", 12, [trade_room], [])])
            twenty_four_hour = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        profile = simulator.trade_order_profile("O_GOLD", 3)
        expected_orders_12h = 12 * 3600 / profile.expected_seconds * 1.03
        expected_orders_24h = 24 * 3600 / profile.expected_seconds * 1.03
        self.assertAlmostEqual(profile.expected_seconds, 12204.0)
        self.assertAlmostEqual(profile.expected_lmd, 1450.0)
        self.assertAlmostEqual(profile.expected_gold, 2.9)
        self.assertAlmostEqual(twelve_hour.dailyExpected.lmdGross, expected_orders_12h * 1450.0)
        self.assertAlmostEqual(twelve_hour.dailyExpected.pureGoldDelta, -expected_orders_12h * 2.9)
        self.assertAlmostEqual(twelve_hour.dailyExpected.overflowLoss, 0.0)
        self.assertAlmostEqual(twenty_four_hour.dailyExpected.lmdGross, expected_orders_24h * 1450.0)
        self.assertAlmostEqual(twenty_four_hour.dailyExpected.pureGoldDelta, -expected_orders_24h * 2.9)
        self.assertAlmostEqual(twenty_four_hour.dailyExpected.overflowLoss, 0.0)

    def test_yituliu_export_uses_one_drone_target_per_shift(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            shifts = [
                ShiftPlan(
                    "A",
                    "08:00",
                    12,
                    [
                        room("trading_1", "TRADING", "O_GOLD"),
                        RoomAssignment(
                            "manufacture_1",
                            "MANUFACTURE",
                            "MANUFACTURE",
                            "F_GOLD",
                            [
                                skill(
                                    "gold drone",
                                    "MANUFACTURE",
                                    "gold drone",
                                    "进驻制造站时，生产力+1000%",
                                    1000,
                                )
                            ],
                            0,
                            product_capacity=0,
                        ),
                        RoomAssignment(
                            "manufacture_2",
                            "MANUFACTURE",
                            "MANUFACTURE",
                            "F_EXP",
                            [
                                skill(
                                    "exp drone",
                                    "MANUFACTURE",
                                    "exp drone",
                                    "进驻制造站时，生产力+1000%",
                                    1000,
                                )
                            ],
                            0,
                            product_capacity=0,
                        ),
                    ],
                    [],
                )
            ]
            production_report = ProductionSimulator(game_data, drone_policy="auto").evaluate(shifts)
            result = OptimizerResult(
                layout=parse_layout("243"),
                mode="normal",
                shift_hours=12,
                shifts=shifts,
                score=production_report.score,
                warnings=[],
                production_report=production_report,
                drone_policy="auto",
            )
            payload = result_to_dict(result, game_data)

        drones = payload["plans"][0]["drones"]
        target_refs = {(target["room"], target["index"], target["policy"]) for target in drones["targets"]}
        self.assertTrue(drones["enable"])
        self.assertEqual(drones["room"], "manufacture")
        self.assertEqual(drones["index"], 2)
        self.assertIn(("manufacture", 2, "exp-factory"), target_refs)
        self.assertEqual(len(drones["targets"]), 1)
        self.assertAlmostEqual(
            drones["modeledDroneCount"],
            payload["dailyExpected"]["droneUsed"],
            places=3,
        )

    def test_balanced_orundum_generates_mixed_targets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator("德克萨斯", True, 5, 1, 0),
                RosterOperator("空", True, 5, 1, 0),
                RosterOperator("史都华德", True, 3, 30, 0),
                RosterOperator("夜刀", True, 2, 30, 0),
                RosterOperator("巡林者", True, 2, 30, 0),
                RosterOperator("杜林", True, 2, 30, 0),
            ]
            result = ScheduleOptimizer(game_data, roster).optimize(
                parse_layout("243"), mode="balanced-orundum"
            )

        targets = [room.target for room in result.shifts[0].rooms]
        self.assertIn("O_GOLD", targets)
        self.assertIn("O_DIAMOND", targets)
        self.assertIn("F_EXP", targets)
        self.assertIn("F_DIAMOND", targets)
        self.assertIn("dailyExpected", result.production_report.to_dict())
        self.assertIn("pureGoldDelta", result.production_report.to_dict()["dailyExpected"])
        self.assertLess(abs(result.production_report.dailyExpected.shardDelta), 5)

    def test_target_selection_score_uses_preset_contract_penalties(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator("德克萨斯", True, 5, 1, 0),
                RosterOperator("空", True, 5, 1, 0),
                RosterOperator("史都华德", True, 3, 30, 0),
                RosterOperator("夜刀", True, 2, 30, 0),
                RosterOperator("巡林者", True, 2, 30, 0),
                RosterOperator("杜林", True, 2, 30, 0),
            ]
            result = ScheduleOptimizer(game_data, roster).optimize(
                parse_layout("243"), mode="balanced-orundum"
            )

        report = result.production_report
        self.assertIsNotNone(report)
        assert report is not None
        contract = preset_contract("balanced-orundum")
        self.assertIn("inventory_balance", contract["targetSelectionPenalties"])
        self.assertIn("lmd_net_balance", contract["targetSelectionPenalties"])
        daily = report.to_dict()["dailyExpected"]
        balance = resource_balance_quality(report.dailyExpected)
        exp_support_ceiling = report.dailyExpected.lmdGross * 0.75
        exp_penalty = 0.0
        if report.dailyExpected.exp > exp_support_ceiling:
            exp_penalty = (report.dailyExpected.exp - exp_support_ceiling) / 60.0
        expected = (
            report.score
            - float(balance["penalty"])
            - abs(report.dailyExpected.lmdNet) / 100.0
            - exp_penalty
        )

        self.assertIn("lmdNet", daily)
        self.assertAlmostEqual(target_selection_score(result.mode, report), expected)

    def test_control_center_is_filled_with_no_skill_backfill(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]
            roster.extend(
                RosterOperator(f"中枢补位{i}", True, 0, 1, 0)
                for i in range(10)
            )
            result = ScheduleOptimizer(game_data, roster).optimize(
                parse_layout("243"), mode="normal"
            )

        for shift in result.shifts:
            control_rooms = [room for room in shift.rooms if room.room_type == "CONTROL"]
            self.assertEqual(len(control_rooms), 1)
            self.assertEqual(len(control_rooms[0].operators), 5)
            self.assertEqual(control_rooms[0].slots, 5)

    def test_schedule_capacity_valid_rejects_underfilled_control(self) -> None:
        result = OptimizerResult(
            layout=parse_layout("243"),
            mode="normal",
            shift_hours=12,
            shifts=[
                ShiftPlan(
                    "A",
                    "08:00",
                    12,
                    [
                        RoomAssignment(
                            "control_1",
                            "CONTROL",
                            "控制中枢",
                            None,
                            [skill("ControlA", "CONTROL", "control", "control", 10)],
                            10,
                            room_level=5,
                            slots=5,
                        )
                    ],
                    [],
                )
            ],
            score=0,
            warnings=[],
        )

        self.assertFalse(schedule_capacity_valid(result))

    def test_exported_schedule_capacity_valid_rejects_underfilled_control_cache(self) -> None:
        self.assertFalse(
            exported_schedule_capacity_valid(
                {
                    "shifts": [
                        {
                            "rooms": [
                                {
                                    "type": "CONTROL",
                                    "slots": 5,
                                    "operators": [{"name": "ControlA"}],
                                }
                            ]
                        }
                    ]
                }
            )
        )

    def test_import_yituliu_schedule_uses_schedule_type(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            schedule_path = Path(temp_dir) / "324搓玉.json"
            schedule_path.write_text(json.dumps(sample_schedule(), ensure_ascii=False), encoding="utf-8")
            game_data = GameData.load(data_dir)
            roster = [RosterOperator("德克萨斯", True, 5, 1, 0), RosterOperator("空", True, 5, 1, 0)]
            imported = load_yituliu_schedule(schedule_path, game_data, roster)
            report = ProductionSimulator(game_data).evaluate(imported.shifts)

        self.assertEqual(imported.layout.label, "243")
        self.assertEqual(len(imported.shifts), 3)
        self.assertGreater(report.dailyExpected.orundum, 0)
        self.assertGreater(report.dailyExpected.exp, 0)

    def test_import_yituliu_schedule_clamps_office_to_one_operator(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            schedule = {
                "scheduleType": {
                    "planTimes": 1,
                    "trading": 0,
                    "manufacture": 0,
                    "power": 0,
                    "dormitory": 0,
                },
                "plans": [
                    {
                        "name": "A",
                        "rooms": {
                            "hire": [
                                {
                                    "slots": 2,
                                    "operators": ["办公室甲", "办公室乙"],
                                }
                            ],
                        },
                    }
                ],
            }
            schedule_path = Path(temp_dir) / "office.json"
            schedule_path.write_text(json.dumps(schedule, ensure_ascii=False), encoding="utf-8")
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator("办公室甲", True, 5, 1, 0),
                RosterOperator("办公室乙", True, 5, 1, 0),
            ]

            imported = load_yituliu_schedule(schedule_path, game_data, roster)

        office = imported.shifts[0].rooms[0]
        self.assertEqual(office.slots, 1)
        self.assertEqual([skill.operator_name for skill in office.operators], ["办公室甲"])
        self.assertTrue(any("只允许 1 名干员" in warning for warning in imported.warnings))

    def test_forced_experiment_clamps_office_replacement_to_one_operator(self) -> None:
        game_data = GameData({"rooms": {}}, {}, {})
        forced = skill("OfficeForced", "HIRE", "forced", "进驻办公室时，人脉资源的联络速度+40%", 40)
        existing = skill("OfficeExisting", "HIRE", "existing", "进驻办公室时，人脉资源的联络速度+45%", 45)
        shifts = [
            ShiftPlan(
                "A",
                "08:00",
                12,
                [
                    RoomAssignment(
                        "hire_1",
                        "HIRE",
                        "HIRE",
                        None,
                        [existing],
                        45,
                        room_level=3,
                        slots=2,
                    )
                ],
                [],
            )
        ]
        result = OptimizerResult(
            layout=parse_layout("243"),
            mode="normal",
            shift_hours=12,
            shifts=shifts,
            score=0,
            warnings=[],
        )
        context = DiagnosticContext(
            game_data=game_data,
            roster=[],
            result=result,
            skills_by_name={"OfficeForced": [forced], "OfficeExisting": [existing]},
            shard_formula="rock",
        )

        forced_result = force_specs_into_shift(
            context,
            [ForceSpec("HIRE", None, ("OfficeForced",))],
            0,
        )

        self.assertIsNotNone(forced_result)
        assert forced_result is not None
        office = forced_result["shifts"][0].rooms[0]
        self.assertEqual([skill.operator_name for skill in office.operators], ["OfficeForced"])

    def test_recommendation_report_writes_best_schedules_and_upgrades(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            schedule_path = Path(temp_dir) / "manual.json"
            schedule_path.write_text(json.dumps(sample_schedule(), ensure_ascii=False), encoding="utf-8")
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                baseline_schedule=schedule_path,
                layouts=["243"],
                modes=["balanced-orundum"],
            )
            report_exists = (output_dir / "recommendation_report.json").exists()
            html_exists = (output_dir / "recommendation_report.html").exists()
            current_exists = (output_dir / "best_current_schedule.json").exists()
            upgrade_exists = (output_dir / "best_upgrades_schedule.json").exists()
            upgrade_cost_adjusted_exists = (
                output_dir / "best_upgrades_cost_adjusted_schedule.json"
            ).exists()
            target_current_exists = (
                output_dir / "best_target_compatible_current_schedule.json"
            ).exists()
            upgrade_list_exists = (output_dir / "upgrade_requirements.json").exists()
            upgrade_cost_adjusted_list_exists = (
                output_dir / "upgrade_requirements_cost_adjusted.json"
            ).exists()
            upgrade_xlsx_exists = (output_dir / "upgrade_requirements.xlsx").exists()
            upgrade_cost_adjusted_xlsx_exists = (
                output_dir / "upgrade_requirements_cost_adjusted.xlsx"
            ).exists()
            payload = json.loads(
                (output_dir / "best_current_schedule.json").read_text(encoding="utf-8")
            )
            candidate_export_path = Path(
                report["candidates"][0]["scheduleExport"]["path"]
            )
            candidate_export_exists = candidate_export_path.exists()
            candidate_payload = json.loads(candidate_export_path.read_text(encoding="utf-8"))
            html = (output_dir / "recommendation_report.html").read_text(encoding="utf-8")

        self.assertTrue(report["baselineComparison"]["bestOverall"]["passes"])
        self.assertEqual(report["trainingImpact"]["status"], "unused")
        self.assertTrue(report_exists)
        self.assertTrue(html_exists)
        self.assertTrue(current_exists)
        self.assertTrue(upgrade_exists)
        self.assertTrue(upgrade_cost_adjusted_exists)
        self.assertTrue(target_current_exists)
        self.assertTrue(upgrade_list_exists)
        self.assertTrue(upgrade_cost_adjusted_list_exists)
        self.assertTrue(upgrade_xlsx_exists)
        self.assertTrue(upgrade_cost_adjusted_xlsx_exists)
        self.assertIn("htmlReport", report["writtenFiles"])
        self.assertIn("upgradeRequirementsXlsx", report["writtenFiles"])
        self.assertIn("bestTargetCompatibleCurrentSchedule", report["writtenFiles"])
        self.assertIn("bestWithUpgradesCostAdjusted", report)
        self.assertIn("presetContracts", report)
        self.assertIn("recommendationIntent", report)
        self.assertIn("objectiveComparison", report)
        self.assertIn("bestTargetCompatibleCurrent", report)
        self.assertIn("targetFit", report["bestCurrent"])
        self.assertIn("targetFit", report["candidates"][0])
        self.assertEqual(report["presetContracts"]["balanced_orundum"]["id"], "balanced_orundum")
        self.assertEqual(
            report["presetContracts"]["balanced_orundum"]["lmdPolicy"],
            "lmd_net_balance_required",
        )
        self.assertIn(
            "lmd_net_balance",
            report["presetContracts"]["balanced_orundum"]["targetSelectionPenalties"],
        )
        self.assertEqual(report["presetContracts"]["normal"]["lmdPolicy"], "lmd_net_primary")
        self.assertEqual(report["presetContracts"]["max_orundum"]["drones"], "reported_separately")
        self.assertEqual(
            report["recommendationIntent"]["preferredContract"]["id"],
            "balanced_orundum",
        )
        self.assertEqual(report["bestCurrent"]["presetContract"]["id"], "balanced_orundum")
        self.assertIn("targetCounts", report["bestCurrent"])
        self.assertIn("contractStatus", report["bestCurrent"]["targetFit"])
        self.assertIn("contractChecks", report["bestCurrent"]["targetFit"])
        contract_checks = {
            item["id"]: item
            for item in report["bestCurrent"]["targetFit"]["contractChecks"]
        }
        self.assertIn("gold_factory", contract_checks)
        self.assertIn("shard_factory", contract_checks)
        self.assertEqual(contract_checks["shard_factory"]["metric"], "targetCounts.F_DIAMOND")
        self.assertEqual(report["recommendationIntent"]["preferredLayout"], "243")
        self.assertEqual(report["recommendationIntent"]["preferredMode"], "balanced_orundum")
        self.assertTrue(report["recommendationIntent"]["requiresOrundum"])
        self.assertEqual(report["bestTargetCompatibleCurrent"]["targetFit"]["fitLevel"], "exact")
        self.assertIn("yituliuCaseChecks", report)
        self.assertIn("manualScheduleCheck", report)
        self.assertIn("counterintuitiveDiagnostics", report)
        self.assertIn("anomalyCandidates", report)
        self.assertIn("diagnosticInsertionCoverage", report)
        self.assertIn("diagnosticInsertionSearch", report["bestCurrent"])
        self.assertIn("diagnosticInsertionSearch", report["diagnosticsBySolution"][0])
        self.assertIn(
            "diagnosticInsertionSearch",
            payload["analysis"],
        )
        self.assertIn(
            "diagnosticInsertionSearch",
            candidate_payload["analysis"],
        )
        self.assertTrue(report["yituliuCaseChecks"])
        self.assertEqual(len(report["counterintuitiveDiagnostics"]), 4)
        self.assertGreaterEqual(len(report["yituliuCaseChecks"]), 10)
        self.assertEqual(report["manualScheduleCheck"]["source"], str(schedule_path))
        self.assertIn("validationEconomics", report["manualScheduleCheck"])
        self.assertIn("lmdNet", report["manualScheduleCheck"]["validationEconomics"])
        self.assertIn(
            "droneAccounting",
            report["manualScheduleCheck"]["validationEconomics"],
        )
        self.assertIn("validationEconomics", report["yituliuCaseChecks"][0])
        self.assertIn("lmdNet", report["yituliuCaseChecks"][0]["validationEconomics"])
        self.assertIn(
            "droneAccounting",
            report["yituliuCaseChecks"][0]["validationEconomics"],
        )
        self.assertIn("balanceQuality", report["bestCurrent"])
        self.assertIn("balanceQuality", report["baseline"])
        self.assertIn("一图流案例验算", html)
        self.assertIn("手排表验算", html)
        self.assertIn("诊断派生插入搜索", html)
        self.assertIn("明日方舟基建排班推荐报告", html)
        self.assertIn("最高产出补练", html)
        self.assertIn("考虑成本补练", html)
        self.assertIn("目标匹配说明", html)
        self.assertIn("当前练度目标匹配最佳", html)
        self.assertIn("候选排行", html)
        self.assertIn("反常识诊断", html)
        self.assertIn("搓玉/不搓玉候选诊断", html)
        self.assertIn("大规模异常挖掘", html)
        self.assertIn("办公室效率", html)
        self.assertIn("diagnosticsBySolution", report)
        self.assertGreaterEqual(len(report["diagnosticsBySolution"]), 1)
        self.assertIn("龙门币净", html)
        self.assertIn("无人机收入", html)
        self.assertIn("无人机支出", html)
        self.assertIn("Drone Usage Plan", html)
        self.assertTrue(report["droneUsagePlan"])
        self.assertIn("candidateId", report["droneUsagePlan"][0])
        self.assertIn("plans", payload)
        self.assertTrue(payload["plans"][0]["drones"]["targets"])
        self.assertGreater(payload["plans"][0]["drones"]["modeledDroneCount"], 0)
        self.assertIn("dailyExpected", payload)
        self.assertEqual(payload["mode"]["contract"]["id"], "balanced_orundum")
        self.assertIn("author", payload)
        self.assertIn("title", payload)
        self.assertEqual(payload["planTimes"], "2班")
        self.assertEqual(payload["scheduleType"]["planTimes"], 2)
        self.assertIn("processing", payload["plans"][0]["rooms"])
        self.assertTrue(candidate_export_exists)
        self.assertIn("plans", candidate_payload)
        self.assertIn("candidate_schedules/", html)
        self.assertIn("referenceBenchmark", report)
        self.assertIn("导出排班 JSON", html)

    def test_runtime_profile_is_optional_and_exported_when_requested(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            plain = ScheduleOptimizer(game_data, roster).optimize(
                parse_layout("333"),
                mode="normal",
                shift_count=1,
                shift_hours=24,
            )
            profiled = ScheduleOptimizer(game_data, roster).optimize(
                parse_layout("333"),
                mode="normal",
                shift_count=1,
                shift_hours=24,
                profile_runtime=True,
            )
            plain_payload = result_to_dict(plain, game_data)
            profiled_payload = result_to_dict(profiled, game_data)

        self.assertEqual(plain.runtime_profile, {})
        self.assertNotIn("runtimeProfile", plain_payload["analysis"])
        self.assertIn("runtimeProfile", profiled_payload["analysis"])
        self.assertIn("cacheSizes", profiled_payload["analysis"]["runtimeProfile"])
        self.assertIn("counters", profiled_payload["analysis"]["runtimeProfile"])

    def test_recommendation_cache_key_rejects_stale_candidate(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            first = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["333"],
                modes=["normal"],
                shift_patterns=[(1, 24)],
                drone_policy="none",
                include_upgrades=False,
                cache_policy="refresh",
            )
            candidate_path = Path(first["bestCurrent"]["scheduleExport"]["path"])
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertIn(
                "candidateCacheKey",
                payload["analysis"]["cacheValidation"],
            )
            payload["score"] = 999999.0
            payload["analysis"]["diagnosticInsertionSearch"]["cacheValidation"][
                "candidateCacheKey"
            ] = "stale"
            payload["analysis"]["cacheValidation"]["candidateCacheKey"] = "stale"
            candidate_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            second = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["333"],
                modes=["normal"],
                shift_patterns=[(1, 24)],
                drone_policy="none",
                include_upgrades=False,
                cache_policy="auto",
            )
            refreshed_payload = json.loads(candidate_path.read_text(encoding="utf-8"))

        self.assertNotEqual(second["bestCurrent"]["score"], 999999.0)
        self.assertNotEqual(refreshed_payload["score"], 999999.0)
        self.assertNotEqual(
            refreshed_payload["analysis"]["cacheValidation"]["candidateCacheKey"],
            "stale",
        )

    def test_recommendation_jobs_are_deterministic_for_small_matrix(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            serial_dir = Path(temp_dir) / "serial"
            parallel_dir = Path(temp_dir) / "parallel"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            serial = recommend_schedules(
                game_data,
                roster,
                output_dir=serial_dir,
                layouts=["333"],
                modes=["normal"],
                shift_patterns=[(1, 24)],
                drone_policy="none",
                include_upgrades=False,
                cache_policy="refresh",
                jobs=1,
            )
            parallel = recommend_schedules(
                game_data,
                roster,
                output_dir=parallel_dir,
                layouts=["333"],
                modes=["normal"],
                shift_patterns=[(1, 24)],
                drone_policy="none",
                include_upgrades=False,
                cache_policy="refresh",
                jobs=2,
            )

        self.assertEqual(
            sorted(candidate["id"] for candidate in serial["candidates"]),
            sorted(candidate["id"] for candidate in parallel["candidates"]),
        )
        self.assertEqual(serial["bestCurrent"]["id"], parallel["bestCurrent"]["id"])
        self.assertAlmostEqual(serial["bestCurrent"]["score"], parallel["bestCurrent"]["score"])
        self.assertEqual(parallel["inputs"]["jobs"], 2)

    def test_recommendation_supports_shift_patterns_and_reference_benchmark(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["243"],
                modes=["normal"],
                shift_patterns=[(2, 12), (3, 8)],
                drone_policy="none",
            )
            written_report = json.loads(
                (output_dir / "recommendation_report.json").read_text(encoding="utf-8")
            )
            html = (output_dir / "recommendation_report.html").read_text(encoding="utf-8")

        candidate_patterns = {
            (candidate["shiftCount"], candidate["shiftHours"])
            for candidate in report["candidates"]
        }
        candidate_ids = {candidate["id"] for candidate in report["candidates"]}
        benchmark = report["referenceBenchmark"]

        self.assertIn((2, 12), candidate_patterns)
        self.assertIn((3, 8), candidate_patterns)
        self.assertTrue(any("_2x12_" in candidate_id for candidate_id in candidate_ids))
        self.assertTrue(any("_3x8_" in candidate_id for candidate_id in candidate_ids))
        self.assertEqual(
            report["inputs"]["shiftPatterns"],
            [{"shiftCount": 2, "shiftHours": 12}, {"shiftCount": 3, "shiftHours": 8}],
        )
        self.assertIn("hardGates", benchmark)
        self.assertIn("guideDeltas", benchmark)
        self.assertIn("gapSummary", benchmark)
        self.assertIn("advisoryGates", benchmark["hardGates"])
        self.assertNotIn("remainingImprovementZero", benchmark["hardGates"]["checks"])
        self.assertIn(
            "remainingImprovementZero",
            benchmark["hardGates"]["advisoryGates"]["checks"],
        )
        self.assertIn("outOfScope", benchmark["gapSummary"])
        self.assertIn("scopedReferenceCount", benchmark["gapSummary"])
        self.assertIn("shiftComparison", benchmark["guideDeltas"][0])
        self.assertIn("operatorComparison", benchmark["guideDeltas"][0])
        self.assertIn("roomTargetComparison", benchmark["guideDeltas"][0])
        self.assertIn("dependencyTradeoff", benchmark["guideDeltas"][0])
        static_detail_gaps = [
            item
            for item in benchmark["guideDeltas"]
            if (item.get("operatorComparison") or {}).get("status")
            in {
                "reference_operator_anchor_extraction_pending",
                "reference_room_target_extraction_pending",
            }
        ]
        self.assertEqual(static_detail_gaps, [])
        compared_static_refs = [
            item
            for item in benchmark["guideDeltas"]
            if (item.get("operatorComparison") or {}).get("status")
            == "reference_operator_anchor_compared"
            and (item.get("roomTargetComparison") or {}).get("status") == "compared"
        ]
        self.assertTrue(compared_static_refs)
        self.assertTrue(
            all(
                item.get("gapReason") == "reference_static_image_detail_unavailable"
                for item in static_detail_gaps
            )
        )
        self.assertTrue(
            all(
                (item.get("dependencyTradeoff") or {}).get("status") == "not_applicable"
                for item in static_detail_gaps
            )
        )
        self.assertIn("referenceBenchmark", written_report)
        self.assertIn("Tradeoff details", html)
        self.assertIn("Full Roster Search Benchmark", html)
        transcribed_243 = [
            item
            for item in benchmark["guideDeltas"]
            if item.get("referenceId") == "yituliu_2026_06_243_normal_2shift"
        ]
        self.assertTrue(transcribed_243)
        self.assertEqual(
            transcribed_243[0]["operatorComparison"]["status"],
            "reference_operator_anchor_compared",
        )
        self.assertEqual(
            transcribed_243[0]["roomTargetComparison"]["status"],
            "compared",
        )

    def test_best_reference_candidate_prefers_matching_room_targets(self) -> None:
        higher_score_wrong_targets = SimpleNamespace(
            id="252_normal_2x12_current",
            result=SimpleNamespace(score=1000.0),
            summary={
                "layout": "252",
                "mode": "normal",
                "shiftCount": 2,
                "targetCounts": {"O_GOLD": 2, "F_EXP": 3, "F_GOLD": 2},
            },
        )
        lower_score_matching_targets = SimpleNamespace(
            id="252_normal_2x12_current_ref_3gold",
            result=SimpleNamespace(score=900.0),
            summary={
                "layout": "252",
                "mode": "normal",
                "shiftCount": 2,
                "targetCounts": {"O_GOLD": 2, "F_EXP": 2, "F_GOLD": 3},
            },
        )
        reference = {
            "roomSummary": {
                "trading": [{"target": "O_GOLD"}, {"target": "O_GOLD"}],
                "manufacture": [
                    {"target": "F_EXP"},
                    {"target": "F_EXP"},
                    {"target": "F_GOLD"},
                    {"target": "F_GOLD"},
                    {"target": "F_GOLD"},
                ],
            }
        }

        selected = best_reference_candidate(
            [higher_score_wrong_targets, lower_score_matching_targets],
            layout="252",
            mode="normal",
            shift_count=2,
            reference_check=reference,
        )

        self.assertEqual(selected.id, "252_normal_2x12_current_ref_3gold")

    def test_best_reference_candidate_prefers_resource_closer_drone_variant(self) -> None:
        higher_score_farther = SimpleNamespace(
            id="243_normal_2x12_current",
            result=SimpleNamespace(score=1000.0),
            summary={
                "layout": "243",
                "mode": "normal",
                "shiftCount": 2,
                "targetCounts": {"O_GOLD": 2, "F_EXP": 2, "F_GOLD": 2},
                "dailyExpected": {"lmdGross": 50627.0, "exp": 45219.0},
            },
        )
        lower_score_closer = SimpleNamespace(
            id="243_normal_2x12_current_ref_drone_lmd_trade",
            result=SimpleNamespace(score=900.0),
            summary={
                "layout": "243",
                "mode": "normal",
                "shiftCount": 2,
                "targetCounts": {"O_GOLD": 2, "F_EXP": 2, "F_GOLD": 2},
                "dailyExpected": {"lmdGross": 62527.0, "exp": 36640.0},
            },
        )
        reference = {
            "expectedDaily": {"lmdGross": 57200.0, "exp": 35900.0, "expExtraContribution": 7100.0},
            "roomSummary": {
                "trading": [{"target": "O_GOLD"}, {"target": "O_GOLD"}],
                "manufacture": [
                    {"target": "F_EXP"},
                    {"target": "F_EXP"},
                    {"target": "F_GOLD"},
                    {"target": "F_GOLD"},
                ],
            },
        }

        selected = best_reference_candidate(
            [higher_score_farther, lower_score_closer],
            layout="243",
            mode="normal",
            shift_count=2,
            reference_check=reference,
        )

        self.assertEqual(selected.id, "243_normal_2x12_current_ref_drone_lmd_trade")
        self.assertGreater(
            reference_resource_fit(lower_score_closer, reference)[0],
            reference_resource_fit(higher_score_farther, reference)[0],
        )

    def test_scalar_best_prefers_primary_candidate_over_reference_drone_variant(self) -> None:
        primary = SimpleNamespace(
            id="primary_auto",
            profile="current",
            result=SimpleNamespace(score=100.0),
            summary={"candidateRole": "primary"},
        )
        reference_variant = SimpleNamespace(
            id="reference_lmd_trade",
            profile="current",
            result=SimpleNamespace(score=200.0),
            summary={"candidateRole": "reference_drone_variant"},
        )

        selected = best_candidate([primary, reference_variant], "current")

        self.assertEqual(selected.id, "primary_auto")

    def test_reference_drone_policies_follow_extra_labels(self) -> None:
        self.assertEqual(
            reference_drone_policies(
                {
                    "lmdExtraContribution": 100.0,
                    "expExtraContribution": 200.0,
                    "pureGoldExtraLmdValue": 300.0,
                }
            ),
            ["reference-fit", "lmd-trade", "exp-factory", "gold-factory"],
        )

        self.assertEqual(
            reference_fit_seed_drone_policy({"lmdExtraContribution": 100.0}),
            "lmd-trade",
        )

    def test_reference_fit_drone_policy_uses_partial_drones_to_hit_lmd_target(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            trade_room = room("trading_1", "TRADING", "O_GOLD")
            shifts = [ShiftPlan("A", "08:00", 24, [trade_room], [])]
            base = ProductionSimulator(game_data, drone_policy="none").evaluate(shifts)
            expected_lmd = base.dailyExpected.lmdGross + 1000.0
            report = ProductionSimulator(
                game_data,
                drone_policy="reference-fit",
                reference_expected_daily={"lmdGross": expected_lmd},
            ).evaluate(shifts)

        self.assertAlmostEqual(report.dailyExpected.lmdGross, expected_lmd, delta=1.0)
        self.assertEqual(report.dailyExpected.droneContribution["referenceFit"], 1.0)
        self.assertGreater(report.dailyExpected.droneUsed, 0.0)
        self.assertLess(report.dailyExpected.droneUsed, report.dailyExpected.droneCount)

    def test_reference_fit_comparison_uses_total_even_with_mixed_drone_resources(self) -> None:
        comparison = guide_field_comparison(
            {
                "lmdGross": 53100.0,
                "exp": 45600.0,
                "droneContribution": {"lmdGross": 5000.0, "exp": 960.0, "referenceFit": 1.0},
            },
            {"lmdGross": 53100.0, "exp": 45600.0},
            "exp",
        )

        self.assertEqual(comparison["modeledField"], "exp")
        self.assertTrue(comparison["passed"])

    def test_reference_trade_labels_create_distinct_room_shape_groups(self) -> None:
        groups = reference_trade_insertion_groups(
            {
                "id": "ref",
                "roomSummary": {
                    "trading": [
                        {"target": "O_GOLD", "label": "龙舌兰组"},
                        {"target": "O_GOLD", "label": "但书组"},
                    ]
                },
            }
        )

        self.assertEqual(len(groups), 1)
        specs = groups[0].specs
        self.assertEqual([spec.operator_name for spec in specs], ["龙舌兰", "但书"])
        self.assertEqual(len({spec.room_group for spec in specs}), 2)

    def test_operator_comparison_uses_static_image_anchors(self) -> None:
        candidate = SimpleNamespace(
            result=SimpleNamespace(
                shifts=[
                    ShiftPlan(
                        "A",
                        "08:00",
                        12,
                        [
                            RoomAssignment(
                                "trading_1",
                                "TRADING",
                                "TRADING",
                                "O_GOLD",
                                [
                                    skill("巫恋", "TRADING", "巫恋", "trade", 30),
                                    skill("龙舌兰", "TRADING", "龙舌兰", "trade", 30),
                                ],
                                0,
                            )
                        ],
                        [
                            RoomAssignment(
                                "dormitory_1",
                                "DORMITORY",
                                "DORMITORY",
                                None,
                                [skill("但书", "DORMITORY", "休息", "rest", 0)],
                                0,
                            )
                        ],
                    )
                ]
            )
        )

        comparison = operator_comparison(
            candidate,
            {"operatorAnchors": ["巫恋", "龙舌兰", "但书"]},
        )

        self.assertEqual(comparison["status"], "reference_operator_anchor_compared")
        self.assertEqual(comparison["referenceCompleteness"], "partial_anchors")
        self.assertEqual(comparison["anchorCount"], 3)
        self.assertEqual(comparison["matchedAnchorCount"], 3)
        self.assertAlmostEqual(comparison["anchorCoverage"], 1.0)
        self.assertEqual(comparison["missingAnchors"], [])
        self.assertEqual(comparison["missingAnchorAvailability"], "unknown")

    def test_operator_comparison_reports_missing_anchor_availability(self) -> None:
        candidate = SimpleNamespace(
            result=SimpleNamespace(
                shifts=[
                    ShiftPlan(
                        "A",
                        "08:00",
                        12,
                        [
                            RoomAssignment(
                                "trading_1",
                                "TRADING",
                                "TRADING",
                                "O_GOLD",
                                [skill("AnchorA", "TRADING", "AnchorA", "trade", 30)],
                                0,
                            )
                        ],
                        [],
                    )
                ]
            )
        )

        comparison = operator_comparison(
            candidate,
            {"operatorAnchors": ["AnchorA", "AnchorB", "AnchorC"]},
            {"AnchorA", "AnchorB"},
        )

        self.assertEqual(comparison["matchedAnchorCount"], 1)
        self.assertEqual(comparison["missingAnchors"], ["AnchorB", "AnchorC"])
        self.assertEqual(comparison["missingAvailableAnchors"], ["AnchorB"])
        self.assertEqual(comparison["missingUnavailableAnchors"], ["AnchorC"])
        self.assertEqual(comparison["missingAvailableAnchorCount"], 1)
        self.assertEqual(comparison["missingUnavailableAnchorCount"], 1)

    def test_guide_operator_anchor_preference_is_scoped_to_reference_pattern(self) -> None:
        anchors = guide_operator_anchor_preference("243", "balanced-orundum", 2, 12)

        self.assertIn("巫恋", anchors)
        self.assertIn("龙舌兰", anchors)
        normal_anchors = guide_operator_anchor_preference("243", "normal", 2, 12)
        self.assertIn("信仰搅拌机", normal_anchors)
        self.assertIn("可露希尔", normal_anchors)
        self.assertEqual(guide_operator_anchor_preference("243", "balanced-orundum", 3, 8), [])

    def test_guide_gap_reason_distinguishes_roster_dependency_tradeoff(self) -> None:
        candidate = SimpleNamespace(summary={"unsupportedSkillEffectCount": 2})
        reason = guide_delta_gap_reason(
            candidate,
            {
                "lmdGross": {
                    "passed": False,
                    "delta": -4000.0,
                },
                "orundum": {
                    "passed": True,
                    "delta": 0.0,
                },
            },
            {"status": "reference_operator_list_unavailable"},
            {
                "status": "compared",
                "passed": True,
                "matchRate": 1.0,
            },
        )

        self.assertEqual(reason, "roster_or_dependency_tradeoff")

    def test_dependency_tradeoff_summary_includes_current_room_operators(self) -> None:
        texas = skill("Texas", "TRADING", "Grudge", "same-room dependency", 65)
        exusiai = skill("Exusiai", "TRADING", "Logistics", "trade speed", 35)
        room_assignment = RoomAssignment(
            "trading_1",
            "TRADING",
            "TRADING",
            "O_GOLD",
            [texas, exusiai],
            100,
        )
        production_report = ProductionReport(
            ProductionVector(),
            {},
            [
                {
                    "shift": "A",
                    "roomId": "trading_1",
                    "skillEffectAudit": [
                        {
                            "operator": "Texas",
                            "buffName": "Grudge",
                            "status": "unsupported",
                            "reason": "same-room condition not satisfied; conditional income not counted.",
                        }
                    ],
                }
            ],
            [],
            [],
        )
        candidate = SimpleNamespace(
            result=SimpleNamespace(
                shifts=[ShiftPlan("A", "08:00", 12, [room_assignment], [])],
                production_report=production_report,
                diagnostic_insertion_search={
                    "accepted": [
                        {
                            "groupId": "diagnostic_named_dependency:Texas:Exusiai",
                            "operators": ["Texas", "Exusiai"],
                            "status": "accepted",
                            "shift": "A",
                            "scoreDelta": 12.5,
                            "dailyExpectedDelta": {"lmdGross": 500},
                            "assignmentChanges": [
                                {
                                    "shift": "A",
                                    "roomId": "trading_1",
                                    "roomType": "TRADING",
                                    "target": "O_GOLD",
                                    "before": ["Texas"],
                                    "after": ["Texas", "Exusiai"],
                                }
                            ],
                        }
                    ],
                    "skipped": [],
                },
            )
        )

        summary = dependency_tradeoff_summary(candidate)

        self.assertEqual(summary["status"], "has_tradeoffs")
        self.assertEqual(
            summary["unsatisfiedEffects"][0]["currentOperators"],
            ["Texas", "Exusiai"],
        )
        self.assertEqual(summary["searchedGroups"][0]["status"], "accepted")
        self.assertEqual(
            summary["searchedGroups"][0]["dailyExpectedDelta"],
            {"lmdGross": 500},
        )
        self.assertEqual(
            summary["searchedGroups"][0]["assignmentChanges"][0]["after"],
            ["Texas", "Exusiai"],
        )

    def test_recommendation_defaults_include_reference_shift_patterns(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["243"],
                modes=["normal"],
                drone_policy="none",
            )

        candidate_patterns = {
            (candidate["shiftCount"], candidate["shiftHours"])
            for candidate in report["candidates"]
        }
        self.assertIn((1, 24), candidate_patterns)
        self.assertIn((2, 12), candidate_patterns)
        self.assertIn((3, 8), candidate_patterns)
        self.assertIn((3, 12), candidate_patterns)
        self.assertEqual(
            report["inputs"]["shiftPatterns"],
            [
                {"shiftCount": 1, "shiftHours": 24},
                {"shiftCount": 2, "shiftHours": 12},
                {"shiftCount": 3, "shiftHours": 8},
                {"shiftCount": 3, "shiftHours": 12},
            ],
        )

    def test_recommendation_honors_explicit_shift_count_and_hours(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["243"],
                modes=["normal"],
                shift_count=3,
                shift_hours=8,
                drone_policy="none",
            )

        candidate_patterns = {
            (candidate["shiftCount"], candidate["shiftHours"])
            for candidate in report["candidates"]
        }
        self.assertEqual(candidate_patterns, {(3, 8)})
        self.assertEqual(report["bestCurrent"]["shiftCount"], 3)
        self.assertEqual(report["bestCurrent"]["shiftDurations"], [8.0, 8.0, 8.0])
        self.assertEqual(
            report["inputs"]["shiftPatterns"],
            [{"shiftCount": 3, "shiftHours": 8}],
        )

    def test_ui_recommendation_honors_explicit_shift_count_and_hours(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir)
            data_dir = root_dir / "data"
            output_dir = root_dir / "ui_out"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster_path = root_dir / "roster.xlsx"
            write_full_roster_xlsx(roster_path, game_data)

            payload = run_recommendation(
                ParsedForm(
                    fields={
                        "roster_path": ["roster.xlsx"],
                        "data_dir": ["data"],
                        "output_dir": ["ui_out"],
                        "layouts": ["243"],
                        "modes": ["normal"],
                        "shift_count": ["3"],
                        "shift_hours": ["8"],
                        "drone_policy": ["none"],
                        "right_side": ["full"],
                        "shard_formula": ["rock"],
                        "cache_policy": ["off"],
                        "jobs": ["1"],
                    },
                    files={},
                ),
                root_dir,
            )
            report = json.loads(
                Path(payload["files"]["report"]["path"]).read_text(encoding="utf-8")
            )
            best_schedule = json.loads(
                Path(payload["files"]["bestCurrentSchedule"]["path"]).read_text(
                    encoding="utf-8"
                )
            )

        candidate_patterns = {
            (candidate["shiftCount"], candidate["shiftHours"])
            for candidate in report["candidates"]
        }
        self.assertTrue(payload["ok"])
        self.assertEqual(candidate_patterns, {(3, 8)})
        self.assertEqual(report["bestCurrent"]["shiftCount"], 3)
        self.assertEqual(report["bestCurrent"]["shiftDurations"], [8.0, 8.0, 8.0])
        self.assertEqual(
            report["inputs"]["shiftPatterns"],
            [{"shiftCount": 3, "shiftHours": 8}],
        )
        self.assertEqual(len(best_schedule["shifts"]), 3)
        self.assertEqual(
            [shift["durationHours"] for shift in best_schedule["shifts"]],
            [8.0, 8.0, 8.0],
        )

    def test_diagnostic_insertion_coverage_deduplicates_same_search_record(self) -> None:
        diagnostics = [
            {
                "solutionSummary": {
                    "id": "solution_a",
                    "group": "balanced_orundum",
                    "profile": "current",
                    "layout": "243",
                    "mode": "balanced_orundum",
                    "score": 100,
                },
                "diagnosticInsertionSearch": {
                    "accepted": [
                        {
                            "groupId": "diagnostic_named_dependency:烈夏:古米",
                            "specKey": "MANUFACTURE:F_EXP:烈夏|TRADING:O_GOLD:古米",
                            "status": "accepted",
                            "scoreDelta": 3.5,
                        }
                    ],
                    "skipped": [],
                },
                "anomalyCandidates": [
                    {
                        "type": "named_cross_room_dependency",
                        "operator": "烈夏",
                        "expectedPartner": "古米",
                        "expectedRoomType": "MANUFACTURE/TRADING",
                        "expectedTarget": "F_EXP/O_GOLD",
                        "currentlySatisfied": False,
                        "priorityScore": 35,
                        "forcedExperiment": {"status": "evaluated", "scoreDelta": 2.0},
                    },
                    {
                        "type": "named_dependency",
                        "operator": "烈夏",
                        "expectedPartner": "古米",
                        "expectedRoomType": "MANUFACTURE",
                        "expectedTarget": ("F_EXP",),
                        "currentlySatisfied": False,
                        "priorityScore": 35,
                        "dependencyExplanation": {
                            "relation": "cross_room",
                            "triggerRoomType": "MANUFACTURE",
                            "partnerRoomType": "TRADING",
                            "triggerTarget": "F_EXP",
                            "partnerTarget": "O_GOLD",
                        },
                        "forcedExperiment": {"status": "evaluated", "scoreDelta": 2.0},
                    },
                    {
                        "type": "named_dependency",
                        "operator": "烈夏",
                        "expectedPartner": "古米",
                        "expectedRoomType": "MANUFACTURE",
                        "expectedTarget": ("F_EXP",),
                        "currentlySatisfied": False,
                        "priorityScore": 10,
                        "dependencyExplanation": {
                            "relation": "same_shift",
                            "triggerRoomType": "MANUFACTURE",
                            "triggerTarget": "F_EXP",
                            "confidence": "low",
                        },
                        "forcedExperiment": {"status": "not_run"},
                    },
                ],
            }
        ]

        coverage = aggregate_diagnostic_insertion_coverage(diagnostics)
        anomalies = aggregate_anomaly_candidates(diagnostics)

        self.assertEqual(coverage["summary"]["matchedInsertionRecords"], 1)
        self.assertEqual(coverage["summary"]["accepted"], 1)
        self.assertEqual(coverage["summary"]["noSpecKey"], 0)
        self.assertEqual(anomalies[0]["insertionSearchSummary"]["statusCounts"]["accepted"], 1)

    def test_diagnostic_insertion_coverage_matches_faction_group_without_spec_key(self) -> None:
        diagnostics = [
            {
                "solutionSummary": {
                    "id": "solution_a",
                    "group": "balanced_orundum",
                    "profile": "current",
                    "layout": "243",
                    "mode": "balanced_orundum",
                    "score": 100,
                },
                "diagnosticInsertionSearch": {
                    "accepted": [
                        {
                            "groupId": "diagnostic_faction_dependency:Vina:glasgow",
                            "specKey": "TRADING:O_GOLD:Morgan|TRADING:O_GOLD:Vina",
                            "status": "accepted",
                            "scoreDelta": 2.5,
                        }
                    ],
                    "skipped": [],
                },
                "anomalyCandidates": [
                    {
                        "type": "same_room_faction_dependency",
                        "operator": "Vina",
                        "expectedFaction": "glasgow",
                        "expectedPartner": "Morgan",
                        "currentlySatisfied": False,
                        "priorityScore": 40,
                    }
                ],
            }
        ]

        coverage = aggregate_diagnostic_insertion_coverage(diagnostics)
        anomalies = aggregate_anomaly_candidates(diagnostics)

        self.assertEqual(coverage["summary"]["matchedInsertionRecords"], 1)
        self.assertEqual(coverage["summary"]["accepted"], 1)
        self.assertEqual(anomalies[0]["insertionSearchSummary"]["statusCounts"]["accepted"], 1)

    def test_diagnostic_insertion_coverage_counts_not_searched_faction_dependency(self) -> None:
        diagnostics = [
            {
                "solutionSummary": {
                    "id": "solution_a",
                    "group": "balanced_orundum",
                    "profile": "current",
                    "layout": "243",
                    "mode": "balanced_orundum",
                    "score": 100,
                },
                "diagnosticInsertionSearch": {
                    "accepted": [],
                    "skipped": [
                        {
                            "groupId": "diagnostic_faction_dependency:Vina:glasgow",
                            "status": "not_searched_no_faction_partner",
                        }
                    ],
                },
                "anomalyCandidates": [
                    {
                        "type": "same_room_faction_dependency",
                        "operator": "Vina",
                        "expectedFaction": "glasgow",
                        "currentlySatisfied": False,
                        "priorityScore": 40,
                    }
                ],
            }
        ]

        coverage = aggregate_diagnostic_insertion_coverage(diagnostics)
        anomalies = aggregate_anomaly_candidates(diagnostics)

        self.assertEqual(coverage["summary"]["matchedInsertionRecords"], 0)
        self.assertEqual(coverage["summary"]["notSearched"], 1)
        self.assertEqual(
            anomalies[0]["insertionSearchSummary"]["statusCounts"]["not_searched_no_faction_partner"],
            1,
        )

    def test_faction_not_searched_matches_even_when_expected_partner_known(self) -> None:
        diagnostics = [
            {
                "solutionSummary": {
                    "id": "solution_a",
                    "group": "balanced_orundum",
                    "profile": "upgrades_cost_adjusted",
                    "layout": "243",
                    "mode": "balanced_orundum",
                    "score": 100,
                },
                "diagnosticInsertionSearch": {
                    "accepted": [],
                    "skipped": [
                        {
                            "groupId": "diagnostic_faction_dependency:Vina:glasgow",
                            "status": "not_searched_no_faction_partner",
                        }
                    ],
                },
                "anomalyCandidates": [
                    {
                        "type": "same_room_faction_dependency",
                        "operator": "Vina",
                        "expectedFaction": "glasgow",
                        "expectedPartner": "Morgan",
                        "currentlySatisfied": False,
                        "priorityScore": 40,
                    }
                ],
            }
        ]

        coverage = aggregate_diagnostic_insertion_coverage(diagnostics)
        anomalies = aggregate_anomaly_candidates(diagnostics)

        self.assertEqual(coverage["summary"]["missingInsertionGroup"], 0)
        self.assertEqual(coverage["summary"]["notSearched"], 1)
        self.assertEqual(
            anomalies[0]["insertionSearchSummary"]["statusCounts"]["not_searched_no_faction_partner"],
            1,
        )

    def test_faction_not_searched_fallback_does_not_mask_keyed_record(self) -> None:
        diagnostics = [
            {
                "solutionSummary": {
                    "id": "solution_a",
                    "group": "balanced_orundum",
                    "profile": "upgrades_cost_adjusted",
                    "layout": "243",
                    "mode": "balanced_orundum",
                    "score": 100,
                },
                "diagnosticInsertionSearch": {
                    "accepted": [],
                    "skipped": [
                        {
                            "groupId": "diagnostic_faction_dependency:Vina:glasgow",
                            "status": "not_searched_no_faction_partner",
                        },
                        {
                            "groupId": "diagnostic_faction_dependency:Vina:glasgow",
                            "specKey": "TRADING:O_GOLD:Morgan|TRADING:O_GOLD:Vina",
                            "status": "evaluated_not_improving",
                            "scoreDelta": -1.0,
                        },
                    ],
                },
                "anomalyCandidates": [
                    {
                        "type": "same_room_faction_dependency",
                        "operator": "Vina",
                        "expectedFaction": "glasgow",
                        "expectedPartner": "Morgan",
                        "currentlySatisfied": False,
                        "priorityScore": 40,
                    }
                ],
            }
        ]

        coverage = aggregate_diagnostic_insertion_coverage(diagnostics)
        anomalies = aggregate_anomaly_candidates(diagnostics)

        self.assertEqual(coverage["summary"]["matchedInsertionRecords"], 1)
        self.assertEqual(coverage["summary"]["notSearched"], 0)
        self.assertEqual(coverage["summary"]["evaluatedNotImproving"], 1)
        self.assertEqual(
            anomalies[0]["insertionSearchSummary"]["statusCounts"]["evaluated_not_improving"],
            1,
        )

    def test_faction_dependency_alternative_partner_counts_as_matched_search(self) -> None:
        diagnostics = [
            {
                "solutionSummary": {
                    "id": "solution_a",
                    "group": "balanced_orundum",
                    "profile": "current",
                    "layout": "243",
                    "mode": "balanced_orundum",
                    "score": 100,
                },
                "diagnosticInsertionSearch": {
                    "accepted": [],
                    "skipped": [
                        {
                            "groupId": "diagnostic_faction_dependency:Vina:glasgow",
                            "specKey": "TRADING:O_GOLD:Dagda|TRADING:O_GOLD:Vina",
                            "status": "evaluated_not_improving",
                            "scoreDelta": -1.0,
                        }
                    ],
                },
                "anomalyCandidates": [
                    {
                        "type": "same_room_faction_dependency",
                        "operator": "Vina",
                        "expectedFaction": "glasgow",
                        "expectedPartner": "Morgan",
                        "currentlySatisfied": False,
                        "priorityScore": 40,
                    }
                ],
            }
        ]

        coverage = aggregate_diagnostic_insertion_coverage(diagnostics)
        anomalies = aggregate_anomaly_candidates(diagnostics)

        self.assertEqual(coverage["summary"]["missingInsertionGroup"], 0)
        self.assertEqual(coverage["summary"]["matchedInsertionRecords"], 1)
        self.assertEqual(
            anomalies[0]["insertionSearchSummary"]["statusCounts"]["matched_alternative_spec"],
            1,
        )

    def test_low_confidence_same_shift_dependency_is_diagnostic_only(self) -> None:
        diagnostics = [
            {
                "solutionSummary": {
                    "id": "solution_a",
                    "group": "balanced_orundum",
                    "profile": "current",
                    "layout": "243",
                    "mode": "balanced_orundum",
                    "score": 100,
                },
                "diagnosticInsertionSearch": {"accepted": [], "skipped": []},
                "anomalyCandidates": [
                    {
                        "type": "named_dependency",
                        "operator": "Deepcolor",
                        "expectedPartner": "Ulpianus",
                        "dependencyExplanation": {
                            "relation": "same_shift",
                            "confidence": "low",
                        },
                        "currentlySatisfied": False,
                        "priorityScore": 10,
                    }
                ],
            }
        ]

        coverage = aggregate_diagnostic_insertion_coverage(diagnostics)
        anomalies = aggregate_anomaly_candidates(diagnostics)

        self.assertEqual(coverage["summary"]["missingInsertionGroup"], 0)
        self.assertEqual(coverage["summary"]["matchedInsertionRecords"], 0)
        self.assertEqual(anomalies[0]["insertionSearchSummary"]["statusCounts"], {})

    def test_unavailable_required_skill_is_not_counted_as_matched_search(self) -> None:
        diagnostics = [
            {
                "solutionSummary": {
                    "id": "solution_a",
                    "group": "balanced_orundum",
                    "profile": "upgrades_cost_adjusted",
                    "layout": "243",
                    "mode": "balanced_orundum",
                    "score": 100,
                },
                "diagnosticInsertionSearch": {
                    "accepted": [],
                    "skipped": [
                        {
                            "groupId": "diagnostic_named_dependency:Trigger:Partner",
                            "specKey": "MANUFACTURE:F_EXP:Trigger|TRADING:O_GOLD:Partner",
                            "status": "unavailable_required_skill",
                        },
                        {
                            "groupId": "diagnostic_named_dependency:Other:Partner",
                            "specKey": "TRADING:O_GOLD:Other|TRADING:O_GOLD:Partner",
                            "status": "not_searched_group_limit",
                        },
                    ],
                },
                "anomalyCandidates": [
                    {
                        "type": "named_dependency",
                        "operator": "Trigger",
                        "expectedPartner": "Partner",
                        "dependencyExplanation": {
                            "relation": "cross_room",
                            "triggerRoomType": "MANUFACTURE",
                            "partnerRoomType": "TRADING",
                            "triggerTarget": "F_EXP",
                            "partnerTarget": "O_GOLD",
                        },
                    },
                    {
                        "type": "named_dependency",
                        "operator": "Other",
                        "expectedPartner": "Partner",
                        "dependencyExplanation": {
                            "relation": "same_room",
                            "triggerRoomType": "TRADING",
                            "target": "O_GOLD",
                        },
                    },
                ],
            }
        ]

        coverage = aggregate_diagnostic_insertion_coverage(diagnostics)
        anomalies = aggregate_anomaly_candidates(diagnostics)

        self.assertEqual(coverage["summary"]["matchedInsertionRecords"], 0)
        self.assertEqual(coverage["summary"]["notSearched"], 2)
        self.assertEqual(coverage["summary"]["unavailableRequiredSkill"], 1)
        self.assertEqual(coverage["summary"]["groupLimitNotSearched"], 1)
        summaries = {
            item["operator"]: item["insertionSearchSummary"]
            for item in anomalies
        }
        self.assertEqual(summaries["Trigger"]["matchedSolutionCount"], 0)
        self.assertEqual(summaries["Other"]["matchedSolutionCount"], 0)

    def test_diagnostic_insertion_coverage_uses_faction_spec_key_when_partner_known(self) -> None:
        diagnostics = [
            {
                "solutionSummary": {
                    "id": "solution_a",
                    "group": "balanced_orundum",
                    "profile": "current",
                    "layout": "243",
                    "mode": "balanced_orundum",
                    "score": 100,
                },
                "diagnosticInsertionSearch": {
                    "accepted": [
                        {
                            "groupId": "diagnostic_faction_dependency:Vina:glasgow",
                            "specKey": "TRADING:O_GOLD:Morgan|TRADING:O_GOLD:Vina",
                            "status": "accepted",
                            "scoreDelta": 2.5,
                        }
                    ],
                    "skipped": [
                        {
                            "groupId": "diagnostic_faction_dependency:Vina:glasgow",
                            "specKey": "TRADING:O_GOLD:Dagda|TRADING:O_GOLD:Vina",
                            "status": "evaluated_not_improving",
                            "scoreDelta": -1.0,
                        }
                    ],
                },
                "anomalyCandidates": [
                    {
                        "type": "same_room_faction_dependency",
                        "operator": "Vina",
                        "expectedFaction": "glasgow",
                        "expectedPartner": "Dagda",
                        "currentlySatisfied": False,
                        "priorityScore": 40,
                    }
                ],
            }
        ]

        coverage = aggregate_diagnostic_insertion_coverage(diagnostics)
        anomalies = aggregate_anomaly_candidates(diagnostics)

        self.assertEqual(coverage["summary"]["accepted"], 0)
        self.assertEqual(coverage["summary"]["evaluatedNotImproving"], 1)
        self.assertEqual(
            anomalies[0]["insertionSearchSummary"]["statusCounts"]["evaluated_not_improving"],
            1,
        )

    def test_first_available_with_tag_uses_utf8_preferred_names_and_excludes_self(self) -> None:
        context = DiagnosticContext(
            game_data=GameData({"rooms": {}}, {}, {}),
            roster=[],
            result=OptimizerResult(parse_layout("243"), "normal", 12, [], 0.0, []),
            skills_by_name={
                "维娜·维多利亚": [
                    skill("维娜·维多利亚", "TRADING", "测试", "占位", 0, faction_tags=("glasgow",))
                ],
                "摩根": [
                    skill("摩根", "TRADING", "测试", "占位", 0, faction_tags=("glasgow",))
                ],
            },
            shard_formula="rock",
        )

        partner = first_available_with_tag(
            context,
            "glasgow",
            preferred=("摩根", "推进之王"),
            exclude="维娜·维多利亚",
        )
        fallback = first_available_with_tag(
            context,
            "glasgow",
            preferred=("维娜·维多利亚",),
            exclude="维娜·维多利亚",
        )

        self.assertEqual(partner, "摩根")
        self.assertEqual(fallback, "摩根")

    def test_recommendation_separates_scalar_best_from_target_compatible_best(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            schedule_path = Path(temp_dir) / "manual.json"
            schedule_path.write_text(json.dumps(sample_schedule(), ensure_ascii=False), encoding="utf-8")
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                baseline_schedule=schedule_path,
                layouts=["333", "243"],
                modes=["normal", "balanced-orundum"],
            )

        self.assertGreater(
            report["bestCurrent"]["score"],
            report["bestTargetCompatibleCurrent"]["score"],
        )
        self.assertEqual(report["bestCurrent"]["targetFit"]["fitLevel"], "off_target")
        self.assertEqual(report["bestCurrent"]["dailyExpected"]["orundum"], 0.0)
        self.assertEqual(report["bestTargetCompatibleCurrent"]["layout"], "243")
        self.assertEqual(report["bestTargetCompatibleCurrent"]["mode"], "balanced_orundum")
        self.assertGreater(report["bestTargetCompatibleCurrent"]["dailyExpected"]["orundum"], 0.0)
        self.assertEqual(
            report["objectiveComparison"]["profiles"][0]["targetCandidateId"],
            report["bestTargetCompatibleCurrent"]["id"],
        )

    def test_normal_contract_keeps_exp_chain_for_333(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["333"],
                modes=["normal"],
            )

        contract = report["presetContracts"]["normal"]
        self.assertNotIn("minimums", contract["targetSelectionPenalties"])
        self.assertGreaterEqual(report["bestCurrent"]["targetCounts"]["F_EXP"], 1)
        self.assertNotEqual(report["bestCurrent"]["targetFit"]["contractStatus"], "violated")
        self.assertIsNotNone(report["bestTargetCompatibleCurrent"])
        self.assertEqual(
            report["bestTargetCompatibleCurrent"]["id"],
            report["bestCurrent"]["id"],
        )

    def test_recommendation_keeps_raw_upgrade_ceiling_separate_from_cost_penalty(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            add_locked_trade_upgrade(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
                if name != "补练贸易"
            ]
            roster.append(RosterOperator("补练贸易", True, 3, 1, 0))

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["243"],
                modes=["normal"],
                upgrade_cost_weight=0.015,
            )

        self.assertGreater(report["bestWithUpgrades"]["upgradeCount"], 0)
        self.assertGreater(report["trainingImpact"]["scoreDelta"], 0)
        self.assertIn("bestWithUpgradesCostAdjusted", report)
        self.assertIn("costAdjustedTrainingImpact", report)

    def test_diagnostics_discovers_named_and_formula_anomalies(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            add_diagnostic_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator("烈夏", True, 5, 1, 2),
                RosterOperator("古米", True, 4, 1, 0),
                RosterOperator("艾雅法拉", True, 6, 1, 0),
            ]
            skills = {
                operator.name: game_data.skills_for_roster_operator(
                    operator,
                    allow_upgrades=True,
                )
                for operator in roster
            }
            shifts = [
                ShiftPlan(
                    "A",
                    "08:00",
                    24,
                    [
                        RoomAssignment(
                            "manufacture_1",
                            "MANUFACTURE",
                            "MANUFACTURE",
                            "F_EXP",
                            [skills["烈夏"][0]],
                            0,
                            room_level=3,
                            slots=3,
                        ),
                        RoomAssignment(
                            "manufacture_2",
                            "MANUFACTURE",
                            "MANUFACTURE",
                            "F_DIAMOND",
                            [],
                            0,
                            room_level=3,
                            slots=3,
                        ),
                        RoomAssignment(
                            "trading_1",
                            "TRADING",
                            "TRADING",
                            "O_GOLD",
                            [],
                            0,
                            room_level=3,
                            slots=3,
                        ),
                    ],
                    [],
                )
            ]
            production_report = ProductionSimulator(game_data).evaluate(shifts)
            result = OptimizerResult(
                layout=parse_layout("333"),
                mode="normal",
                shift_hours=24,
                shifts=shifts,
                score=production_report.score,
                warnings=[],
                production_report=production_report,
            )

            diagnostics = build_recommendation_diagnostics(
                game_data,
                roster,
                result,
                shard_formula="rock",
            )

        anomaly_types = {item["type"] for item in diagnostics["anomalyCandidates"]}
        self.assertIn("named_cross_room_dependency", anomaly_types)
        self.assertIn("formula_specialist_not_in_target_room", anomaly_types)
        named = [
            item
            for item in diagnostics["anomalyCandidates"]
            if item["type"] == "named_dependency" and item.get("expectedPartner") == "古米"
        ]
        self.assertTrue(named)
        self.assertEqual(named[0]["dependencyExplanation"]["relation"], "cross_room")
        self.assertEqual(named[0]["forcedExperiment"]["status"], "evaluated")
        self.assertTrue(
            any(
                item.get("forcedExperiment", {}).get("status") == "evaluated"
                for item in diagnostics["anomalyCandidates"]
            )
        )

    def test_current_profile_diagnostics_ignore_locked_upgrade_only_dependencies(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            building_path = data_dir / "building_data.json"
            character_path = data_dir / "character_table.json"
            building = json.loads(building_path.read_text(encoding="utf-8"))
            characters = json.loads(character_path.read_text(encoding="utf-8"))
            building["buffs"]["locked_pair"] = {
                "buffId": "locked_pair",
                "buffName": "locked_pair",
                "roomType": "MANUFACTURE",
                "description": "进驻制造站时，当与PartnerOp在同一个制造站时，贵金属类配方的生产力+15%",
                "efficiency": 15,
                "targets": ["F_GOLD"],
            }
            building["chars"]["char_locked"] = {
                "charId": "char_locked",
                "buffChar": [
                    {"buffData": [{"buffId": "locked_pair", "cond": {"phase": "PHASE_1", "level": 1}}]}
                ],
            }
            characters["char_locked"] = {
                "name": "LockedOp",
                "rarity": "TIER_5",
                "phases": [
                    {"maxLevel": 50, "evolveCost": None},
                    {"maxLevel": 80, "evolveCost": [{"id": "30012", "count": 1, "type": "MATERIAL"}]},
                ],
            }
            characters["char_partner"] = {
                "name": "PartnerOp",
                "rarity": "TIER_5",
                "phases": [{"maxLevel": 50, "evolveCost": None}],
            }
            building_path.write_text(json.dumps(building, ensure_ascii=False), encoding="utf-8")
            character_path.write_text(json.dumps(characters, ensure_ascii=False), encoding="utf-8")
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator("LockedOp", True, 5, 1, 0),
                RosterOperator("PartnerOp", True, 5, 1, 0),
            ]
            result = OptimizerResult(
                layout=parse_layout("243"),
                mode="normal",
                shift_hours=12,
                shifts=[],
                score=0,
                warnings=[],
            )

            current = build_recommendation_diagnostics(
                game_data,
                roster,
                result,
                shard_formula="rock",
                profile="current",
            )
            upgrades = build_recommendation_diagnostics(
                game_data,
                roster,
                result,
                shard_formula="rock",
                profile="upgrades_raw",
            )

        current_ops = {item.get("operator") for item in current["anomalyCandidates"]}
        upgrade_ops = {item.get("operator") for item in upgrades["anomalyCandidates"]}
        self.assertNotIn("LockedOp", current_ops)
        self.assertIn("LockedOp", upgrade_ops)

    def test_recommendation_skips_strict_right_full_layout_when_power_is_insufficient(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["342", "243"],
                modes=["normal"],
                right_side="strict-full",
            )

        skipped = {item.get("layout"): item.get("reason", "") for item in report["skipped"]}
        candidate_layouts = {candidate["layout"] for candidate in report["candidates"]}
        self.assertIn("342", skipped)
        self.assertIn("Insufficient base power", skipped["342"])
        self.assertNotIn("342", candidate_layouts)
        self.assertIn("243", candidate_layouts)
        self.assertEqual(report["bestCurrent"]["layout"], "243")
        self.assertEqual(report["bestCurrent"]["powerStatus"]["margin"], 0)

    def test_right_full_lowers_factories_until_power_is_feasible(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            building_path = data_dir / "building_data.json"
            building = json.loads(building_path.read_text(encoding="utf-8"))
            building["rooms"]["DORMITORY"]["phases"][4]["electricity"] = -30
            building_path.write_text(json.dumps(building, ensure_ascii=False), encoding="utf-8")
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["342", "243"],
                modes=["normal"],
                right_side="full",
                drone_policy="none",
            )
            html = (output_dir / "recommendation_report.html").read_text(encoding="utf-8")

        candidate = next(item for item in report["candidates"] if item["layout"] == "342")
        adjustment = candidate["powerStatus"]["powerAdjustment"]
        self.assertTrue(candidate["powerStatus"]["feasible"])
        self.assertEqual(adjustment["status"], "applied")
        self.assertEqual(adjustment["reason"], "right_full_power_fit")
        self.assertTrue(adjustment["manufactureOnlyFeasible"])
        self.assertLess(min(adjustment["manufactureLevels"]), 3)
        self.assertLess(sum(adjustment["manufactureSlots"]), 12)
        self.assertEqual(adjustment["dormitoryLevels"], adjustment["originalDormitoryLevels"])
        self.assertFalse(any(step["roomType"] == "DORMITORY" for step in adjustment["steps"]))
        self.assertIn("Power Adjustment", html)
        self.assertIn("right_full_power_fit", html)
        self.assertIn("factory-only", html)

    def test_342_guide_orundum_variant_is_power_feasible_for_guide_benchmark(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            layout = apply_right_side_preset(
                apply_layout_variant(parse_layout("342"), "342-guide-orundum"),
                "guide",
            )
            status = power_status(game_data, layout)

        self.assertTrue(status.feasible)
        self.assertEqual(layout.trading_levels, (3, 3, 1))
        self.assertEqual(layout.manufacture_levels, (3, 2, 2, 3))
        self.assertEqual(layout.dormitory_levels, (1, 1, 1, 1))
        self.assertEqual(status.margin, 0)

    def test_recommendation_can_compare_342_guide_orundum_reference(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["342-guide-orundum"],
                modes=["max-orundum"],
                shift_patterns=[(2, 12)],
                right_side="guide",
                drone_policy="none",
            )

        candidate_layouts = {candidate["layout"] for candidate in report["candidates"]}
        candidate_modes = {candidate["mode"] for candidate in report["candidates"]}
        guide_delta = next(
            item
            for item in report["referenceBenchmark"]["guideDeltas"]
            if item.get("referenceId") == "yituliu_2026_06_342_orundum_2shift"
        )

        self.assertIn("342", candidate_layouts)
        self.assertIn("max_orundum", candidate_modes)
        self.assertNotEqual(guide_delta["status"], "missing_candidate")
        self.assertEqual(guide_delta["mode"], "max_orundum")

    def test_recommendation_auto_expands_342_max_orundum_guide_variant(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            output_dir = Path(temp_dir) / "recommendation"
            data_dir.mkdir()
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            roster = [
                RosterOperator(name, True, 3, 30, 0)
                for name in game_data.char_id_by_name
            ]

            report = recommend_schedules(
                game_data,
                roster,
                output_dir=output_dir,
                layouts=["342"],
                modes=["max-orundum"],
                shift_patterns=[(2, 12)],
                right_side="guide",
                drone_policy="none",
            )

        candidate_ids = {candidate["id"] for candidate in report["candidates"]}
        raw_layouts = {candidate["layoutRaw"] for candidate in report["candidates"]}
        self.assertIn("342-guide-orundum", raw_layouts)
        self.assertTrue(any(candidate_id.startswith("342_guide_orundum_") for candidate_id in candidate_ids))

    def test_calibration_report_examples_pass(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            report = build_calibration_report(game_data)

        self.assertTrue(report["summary"]["allPassed"])
        self.assertEqual(report["tradeModel"]["status"], "guide_calibrated")
        cases = {case["name"]: case for case in report["cases"]}
        self.assertAlmostEqual(cases["factory_gold_24h"]["expected"]["pureGoldDelta"], 20.6)
        self.assertTrue(cases["guide_252_3exp_2gold"]["guideAssertion"]["checks"]["goldNearlyBalanced"])
        self.assertIn("roomReports", cases["guide_243_balanced_orundum"])
        self.assertAlmostEqual(
            report["communityReferences"]["yituliuOrundumCalculator"]["modelAlignment"][
                "rockRecipeLmdCostPerPullWan"
            ],
            9.6,
        )
        schedule_images = report["communityReferences"]["yituliuScheduleImages202606"]
        self.assertEqual(schedule_images["sourceVideo"]["bvid"], "BV19jVZ69Evp")
        asset_check = schedule_images["assetCatalogCheck"]
        self.assertEqual(report["sourceProvenance"]["yituliuScheduleAssetCatalog"], asset_check)
        self.assertEqual(asset_check["status"], "matched")
        self.assertTrue(asset_check["containsVideoBvid"])
        self.assertTrue(asset_check["containsVersionDate"])
        self.assertGreaterEqual(asset_check["imageEntryCount"], 20)
        self.assertTrue(
            asset_check["staticImageChecks"]["yituliu_2026_06_243_orundum_2shift"]
        )
        self.assertTrue(
            asset_check["staticImageChecks"]["yituliu_2026_06_342_orundum_2shift"]
        )
        self.assertTrue(asset_check["dynamic342OrundumCandidates"])
        image_names = {image["name"] for image in schedule_images["images"]}
        self.assertIn("243 搓玉 一天两换", image_names)
        self.assertIn("右满 342 搓玉 一天两换", image_names)
        targets = {target["id"]: target for target in report["guideCalibrationTargets"]}
        summary = report["guideTargetSummary"]
        yield_cases = {case["id"]: case for case in report["guideYieldValidationCases"]}
        yield_summary = report["guideYieldValidationSummary"]
        self.assertGreaterEqual(yield_summary["caseCount"], 10)
        self.assertTrue(yield_summary["allPassed"])
        self.assertGreaterEqual(yield_summary["orundumCaseCount"], 2)
        self.assertGreaterEqual(yield_summary["nonOrundumCaseCount"], 8)
        self.assertEqual(yield_summary["strictFormulaCaseCount"], 2)
        self.assertGreaterEqual(yield_summary["guideLabelCalibratedCaseCount"], 8)
        self.assertEqual(yield_summary["pendingRoomSummaryCaseCount"], 0)
        self.assertEqual(yield_summary["pendingRoomSummaryCaseIds"], [])
        self.assertEqual(
            yield_cases["yituliu_2026_06_243_normal_3shift"]["algorithmCoverage"],
            "guide_label_calibrated",
        )
        self.assertEqual(
            yield_cases["yituliu_2026_06_243_normal_3shift"]["expectedDaily"][
                "lmdGross"
            ],
            57200,
        )
        visual = yield_cases["yituliu_2026_06_243_normal_3shift"]["visualExtraction"]
        self.assertEqual(visual["status"], "operator_room_anchors_transcribed")
        self.assertIn("2026-06-28_1.webp", visual["localImagePath"])
        self.assertIn(
            "诗怀雅",
            yield_cases["yituliu_2026_06_243_normal_3shift"]["operatorAnchors"],
        )
        self.assertEqual(
            len(yield_cases["yituliu_2026_06_243_normal_3shift"]["roomSummary"]["trading"]),
            2,
        )
        self.assertEqual(
            len(
                yield_cases["yituliu_2026_06_243_normal_3shift"]["roomSummary"][
                    "manufacture"
                ]
            ),
            4,
        )
        visual_243_1shift = yield_cases["yituliu_2026_06_243_normal_1shift"][
            "visualExtraction"
        ]
        self.assertEqual(visual_243_1shift["status"], "operator_room_anchors_transcribed")
        self.assertIn("2026-06-28_4.webp", visual_243_1shift["localImagePath"])
        self.assertEqual(yield_cases["yituliu_2026_06_243_normal_1shift"]["shiftHours"], [24])
        self.assertIn(
            "可露希尔",
            yield_cases["yituliu_2026_06_243_normal_1shift"]["operatorAnchors"],
        )
        self.assertEqual(
            len(yield_cases["yituliu_2026_06_243_normal_1shift"]["roomSummary"]["trading"]),
            2,
        )
        self.assertEqual(
            len(
                yield_cases["yituliu_2026_06_243_normal_1shift"]["roomSummary"][
                    "manufacture"
                ]
            ),
            4,
        )
        visual_153_3shift = yield_cases["yituliu_2026_06_153_normal_3shift"][
            "visualExtraction"
        ]
        self.assertEqual(visual_153_3shift["status"], "operator_room_anchors_transcribed")
        self.assertIn("2026-06-28_5.webp", visual_153_3shift["localImagePath"])
        self.assertEqual(
            yield_cases["yituliu_2026_06_153_normal_3shift"]["shiftHours"],
            [17, 3.5, 3.5],
        )
        self.assertEqual(
            len(yield_cases["yituliu_2026_06_153_normal_3shift"]["roomSummary"]["trading"]),
            1,
        )
        self.assertEqual(
            len(
                yield_cases["yituliu_2026_06_153_normal_3shift"]["roomSummary"][
                    "manufacture"
                ]
            ),
            5,
        )
        visual_153_2shift = yield_cases["yituliu_2026_06_153_normal_2shift"][
            "visualExtraction"
        ]
        self.assertEqual(visual_153_2shift["status"], "operator_room_anchors_transcribed")
        self.assertIn("2026-06-28_6.webp", visual_153_2shift["localImagePath"])
        self.assertEqual(
            yield_cases["yituliu_2026_06_153_normal_2shift"]["shiftHours"],
            [12, 12],
        )
        self.assertEqual(
            len(yield_cases["yituliu_2026_06_153_normal_2shift"]["roomSummary"]["trading"]),
            1,
        )
        self.assertEqual(
            len(
                yield_cases["yituliu_2026_06_153_normal_2shift"]["roomSummary"][
                    "manufacture"
                ]
            ),
            5,
        )
        visual_243_simplified = yield_cases["yituliu_2026_06_243_simplified_2shift"][
            "visualExtraction"
        ]
        self.assertEqual(visual_243_simplified["status"], "operator_room_anchors_transcribed")
        self.assertIn("2026-06-28_3.webp", visual_243_simplified["localImagePath"])
        self.assertIn(
            "龙舌兰",
            yield_cases["yituliu_2026_06_243_simplified_2shift"]["operatorAnchors"],
        )
        self.assertEqual(
            len(yield_cases["yituliu_2026_06_243_simplified_2shift"]["roomSummary"]["trading"]),
            2,
        )
        self.assertEqual(
            len(
                yield_cases["yituliu_2026_06_243_simplified_2shift"]["roomSummary"][
                    "manufacture"
                ]
            ),
            4,
        )
        visual_252_full = yield_cases["yituliu_2026_06_252_full_2gold_3shift"][
            "visualExtraction"
        ]
        self.assertEqual(visual_252_full["status"], "operator_room_anchors_transcribed")
        self.assertIn("2026-06-28_7.webp", visual_252_full["localImagePath"])
        self.assertIn(
            "乌尔比安",
            yield_cases["yituliu_2026_06_252_full_2gold_3shift"]["operatorAnchors"],
        )
        self.assertEqual(
            len(yield_cases["yituliu_2026_06_252_full_2gold_3shift"]["roomSummary"]["trading"]),
            2,
        )
        self.assertEqual(
            len(
                yield_cases["yituliu_2026_06_252_full_2gold_3shift"]["roomSummary"][
                    "manufacture"
                ]
            ),
            5,
        )
        visual_252 = yield_cases["yituliu_2026_06_252_right_2gold_2shift"]["visualExtraction"]
        self.assertEqual(visual_252["status"], "operator_room_anchors_transcribed")
        self.assertIn("2026-06-28_8.webp", visual_252["localImagePath"])
        self.assertIn(
            "龙舌兰",
            yield_cases["yituliu_2026_06_252_right_2gold_2shift"]["operatorAnchors"],
        )
        self.assertEqual(
            len(yield_cases["yituliu_2026_06_252_right_2gold_2shift"]["roomSummary"]["trading"]),
            2,
        )
        self.assertEqual(
            len(
                yield_cases["yituliu_2026_06_252_right_2gold_2shift"]["roomSummary"][
                    "manufacture"
                ]
            ),
            5,
        )
        visual_252_three_gold = yield_cases["yituliu_2026_06_252_right_3gold_2shift"][
            "visualExtraction"
        ]
        self.assertEqual(visual_252_three_gold["status"], "operator_room_anchors_transcribed")
        self.assertIn("2026-06-28_9.webp", visual_252_three_gold["localImagePath"])
        self.assertIn(
            "多萝西",
            yield_cases["yituliu_2026_06_252_right_3gold_2shift"]["operatorAnchors"],
        )
        self.assertEqual(
            len(yield_cases["yituliu_2026_06_252_right_3gold_2shift"]["roomSummary"]["trading"]),
            2,
        )
        self.assertEqual(
            len(
                yield_cases["yituliu_2026_06_252_right_3gold_2shift"]["roomSummary"][
                    "manufacture"
                ]
            ),
            5,
        )
        self.assertEqual(
            yield_cases["yituliu_2026_06_342_orundum_2shift"]["expectedDaily"][
                "orundum"
            ],
            578,
        )
        self.assertEqual(summary["targetCount"], 3)
        self.assertEqual(summary["staticFormulaTargets"], 2)
        self.assertEqual(summary["staticFormulaMatched"], 2)
        self.assertTrue(summary["allStaticFormulaMatched"])
        self.assertTrue(summary["allYieldLabelsMatched"])
        self.assertEqual(summary["unresolvedClassification"]["yieldBlocking"], [])
        self.assertIn(
            "purple trade extra contribution exact drone budget",
            summary["unresolvedClassification"]["mechanismOnly"],
        )
        self.assertIn(
            "green rotation metric exact formula",
            summary["unresolvedClassification"]["nonYieldMetric"],
        )
        self.assertEqual(
            summary["pendingMowerSimulation"],
            [],
        )
        self.assertEqual(
            summary["guideCalibratedMowerTargets"],
            ["yituliu_2026_06_342_orundum_dynamic_mower"],
        )
        mower_target_id = "yituliu_2026_06_342_orundum_dynamic_mower"
        mower_plan_check = targets[mower_target_id]["formulaCheck"]["mowerPlanCheck"]
        if mower_plan_check["status"] == "matched":
            expected_matched_mower_plans = [mower_target_id]
            expected_dynamic_status = "matched_guide_calibrated"
            expected_benchmarked_mower_plans = [mower_target_id]
        else:
            self.assertEqual(mower_plan_check["status"], "missing")
            expected_matched_mower_plans = []
            expected_dynamic_status = "pending_plan_binding"
            expected_benchmarked_mower_plans = []
        self.assertEqual(summary["matchedMowerPlans"], expected_matched_mower_plans)
        resource_calibration = summary["resourceYieldCalibration"]
        self.assertEqual(resource_calibration["staticImageResourceLabels"]["status"], "matched")
        self.assertEqual(resource_calibration["staticImageResourceLabels"]["matched"], 2)
        self.assertIn(
            "orundum",
            resource_calibration["staticImageResourceLabels"]["modeledResourceFields"],
        )
        self.assertEqual(
            resource_calibration["dynamicMowerResourceLabels"]["status"],
            expected_dynamic_status,
        )
        self.assertEqual(
            resource_calibration["dynamicMowerResourceLabels"]["matchedPlanIds"],
            expected_matched_mower_plans,
        )
        self.assertEqual(
            resource_calibration["dynamicMowerResourceLabels"]["benchmarkedPlanIds"],
            expected_benchmarked_mower_plans,
        )
        self.assertEqual(
            resource_calibration["dynamicMowerResourceLabels"]["guideCalibratedPlanIds"],
            ["yituliu_2026_06_342_orundum_dynamic_mower"],
        )
        self.assertIn(
            "lmdGross",
            resource_calibration["dynamicMowerResourceLabels"]["modeledResourceFields"],
        )
        semantics = report["guideLabelSemantics"]
        self.assertEqual(
            semantics["purpleExtraIcon"]["status"],
            "identified_as_extra_acceleration_contribution",
        )
        self.assertEqual(
            semantics["purpleExtraIcon"]["aggregation"],
            "resource_specific_optional_not_simultaneous",
        )
        self.assertEqual(
            semantics["greenMetricIcon"]["status"],
            "confirmed_not_daily_resource_output_but_exact_formula_unresolved",
        )
        self.assertEqual(
            semantics["greenMetricIcon"]["productionCalibrationStatus"],
            "excluded_from_resource_yield_matching",
        )
        self.assertIn(
            "outputs/yituliu_green_metric_crop_2.png",
            semantics["greenMetricIcon"]["visualEvidence"]["localCrops"],
        )
        self.assertIn(
            "outputs/yituliu_2026_06_243_top_metrics.png",
            semantics["purpleExtraIcon"]["visualEvidence"]["localCrops"],
        )
        self.assertEqual(
            semantics["greenMetricIcon"]["unresolvedKey"],
            "green rotation metric exact formula",
        )
        self.assertGreaterEqual(len(semantics["greenMetricIcon"]["observedValues"]), 5)
        self.assertNotIn("purple extra contribution", summary["unresolved"])
        self.assertIn("purple trade extra contribution exact drone budget", summary["unresolved"])
        self.assertIn("green rotation metric exact formula", summary["unresolved"])
        self.assertNotIn("green 0.843 metric", summary["unresolved"])
        self.assertNotIn("green ratio icon", summary["unresolved"])
        self.assertNotIn("exact control/common-bonus split", summary["unresolved"])
        self.assertEqual(
            targets["yituliu_2026_06_243_orundum_2shift"]["expectedDaily"]["lmdGross"],
            30600,
        )
        self.assertIn(
            "2026-06-28/11.webp",
            targets["yituliu_2026_06_243_orundum_2shift"]["source"],
        )
        self.assertIn("巫恋", targets["yituliu_2026_06_243_orundum_2shift"]["operatorAnchors"])
        self.assertEqual(
            targets["yituliu_2026_06_243_orundum_2shift"]["expectedDaily"]["lmdGrossWithExtra"],
            45300,
        )
        self.assertEqual(
            targets["yituliu_2026_06_243_orundum_2shift"]["expectedDaily"][
                "lmdGrossWithLmdExtra"
            ],
            45300,
        )
        self.assertEqual(
            targets["yituliu_2026_06_342_orundum_2shift"]["expectedDaily"]["lmdGross"],
            47000,
        )
        self.assertIn(
            "2026-06-28/10.webp",
            targets["yituliu_2026_06_342_orundum_2shift"]["source"],
        )
        self.assertIn("可露希尔", targets["yituliu_2026_06_342_orundum_2shift"]["operatorAnchors"])
        self.assertEqual(
            targets["yituliu_2026_06_342_orundum_2shift"]["expectedDaily"]["lmdGrossWithExtra"],
            60700,
        )
        self.assertEqual(
            targets["yituliu_2026_06_342_orundum_2shift"]["expectedDaily"][
                "lmdGrossWithLmdExtra"
            ],
            60700,
        )
        self.assertEqual(
            targets["yituliu_2026_06_342_orundum_dynamic_mower"]["expectedDaily"]["lmdGross"],
            77000,
        )
        self.assertEqual(
            targets["yituliu_2026_06_342_orundum_dynamic_mower"]["expectedDaily"]["orundum"],
            540,
        )
        self.assertEqual(
            targets["yituliu_2026_06_342_orundum_dynamic_mower"]["machineReadableSchedule"][
                "scheduleId"
            ],
            1775555941084837,
        )
        mower_check = targets["yituliu_2026_06_342_orundum_dynamic_mower"]["formulaCheck"][
            "mowerPlanCheck"
        ]
        if mower_check["status"] == "matched":
            self.assertEqual(mower_check["actual"]["layout"], {"trading": 3, "manufacture": 4, "power": 2})
            self.assertEqual(mower_check["actual"]["tradingProducts"], {"lmd": 2, "orundum": 1})
            self.assertEqual(mower_check["actual"]["manufactureProducts"], {"gold": 3, "orirock": 1})
            self.assertIn("黑键", mower_check["actual"]["roomOperators"]["room_1_1"])
            self.assertGreaterEqual(mower_check["actual"]["dynamicReplacementRoomCount"], 7)
            self.assertTrue(mower_check["dynamicRuleSummary"]["hasRefreshTradingRule"])
            self.assertTrue(mower_check["dynamicRuleSummary"]["hasExhaustRequireRule"])
            self.assertIn("refresh_trading", mower_check["dynamicRuleSummary"]["fields"])
            self.assertEqual(
                mower_check["guideBenchmark"]["dailyExpectedFromImage"]["lmdGross"],
                77000,
            )
            self.assertEqual(
                mower_check["guideBenchmark"]["status"],
                "guide_label_bound_not_simulated",
            )
        else:
            self.assertEqual(mower_check["status"], "missing")
            self.assertIn("localRawPath", mower_check)
        self.assertEqual(
            targets["yituliu_2026_06_342_orundum_dynamic_mower"]["formulaCheck"]["status"],
            "guide_calibrated_mower",
        )
        self.assertEqual(
            targets["yituliu_2026_06_342_orundum_dynamic_mower"]["formulaCheck"][
                "allModeledComparisonsPass"
            ],
            mower_check["status"] == "matched",
        )
        self.assertEqual(
            targets["yituliu_2026_06_342_orundum_dynamic_mower"]["formulaCheck"][
                "comparisons"
            ]["lmdGross"]["modeled"],
            77000,
        )
        self.assertEqual(
            targets["yituliu_2026_06_243_orundum_2shift"]["expectedDaily"]["orundum"],
            582,
        )
        self.assertAlmostEqual(
            targets["yituliu_2026_06_243_orundum_2shift"]["rotationMetric"]["observed"],
            0.843,
        )
        self.assertTrue(
            targets["yituliu_2026_06_243_orundum_2shift"]["formulaCheck"]["comparisons"][
                "lmdGross"
            ]["passed"]
        )
        self.assertTrue(
            targets["yituliu_2026_06_243_orundum_2shift"]["formulaCheck"]["comparisons"][
                "orundum"
            ]["passed"]
        )
        self.assertTrue(
            targets["yituliu_2026_06_342_orundum_2shift"]["formulaCheck"]["comparisons"][
                "lmdGross"
            ]["passed"]
        )
        self.assertTrue(
            targets["yituliu_2026_06_342_orundum_2shift"]["formulaCheck"]["comparisons"][
                "pureGoldGrossLmdValue"
            ]["passed"]
        )
        self.assertTrue(
            targets["yituliu_2026_06_342_orundum_2shift"]["formulaCheck"][
                "allModeledComparisonsPass"
            ]
        )
        self.assertTrue(
            targets["yituliu_2026_06_342_orundum_2shift"]["formulaCheck"]["comparisons"][
                "orundum"
            ]["passed"]
        )
        extra_243 = targets["yituliu_2026_06_243_orundum_2shift"]["extraContributionCheck"]
        self.assertTrue(extra_243["checks"]["expExtraContribution"]["passed"])
        self.assertTrue(extra_243["checks"]["pureGoldExtraLmdValue"]["passed"])
        self.assertTrue(extra_243["checks"]["lmdExtraContribution"]["passed"])
        self.assertEqual(
            extra_243["checks"]["lmdExtraContribution"]["model"],
            "best_single_lmd_trade_240_drones",
        )
        self.assertEqual(
            extra_243["checks"]["lmdExtraContribution"]["bestSingleTradeWork24Lmd"],
            29543.0,
        )
        extra_342 = targets["yituliu_2026_06_342_orundum_2shift"]["extraContributionCheck"]
        self.assertEqual(extra_342["checks"]["lmdExtraContribution"]["status"], "inferred")
        self.assertGreater(extra_342["checks"]["lmdExtraContribution"]["inferredDrones"], 250)
        allocation = extra_342["checks"]["lmdExtraContribution"]["candidateAllocation"]
        self.assertEqual(allocation["status"], "plausible_unconfirmed")
        self.assertEqual(allocation["totalDrones"], 289)
        self.assertAlmostEqual(allocation["modeled"], 13700, delta=1)
        self.assertEqual(
            [item["work24Lmd"] for item in allocation["allocation"]],
            [25479.0, 21600.0],
        )
        self.assertIn("green rotation metric exact formula", extra_243["unresolved"])
        supporting = {
            item["id"]: item
            for item in report["communityReferences"]["yituliu202606SupportingLabelEvidence"]
        }
        self.assertEqual(
            supporting["yituliu_2026_06_243_normal_1shift"]["topLabels"][
                "greenRotationMetric"
            ],
            "0.792",
        )
        self.assertEqual(
            supporting["yituliu_2026_06_243_normal_2shift"]["topLabels"][
                "greenRotationMetric"
            ],
            "0.843",
        )
        self.assertEqual(
            supporting["yituliu_2026_06_342_orundum_dynamic_mower"]["topLabels"][
                "blueLmdTradeIcon"
            ],
            "77.0k",
        )
        older_reference = report["communityReferences"]["olderNonOrundumMonthlyYieldReference"]
        self.assertGreater(older_reference["rows"][0]["lmdGrossPerDay"], 40000)
        self.assertIn("currentModelOutputCaveat", report["communityReferences"])

    def test_trade_order_profiles_include_guide_distributions(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data)

        self.assertAlmostEqual(simulator.trade_order_profile("O_GOLD", 1).expected_lmd, 1000.0)
        self.assertAlmostEqual(simulator.trade_order_profile("O_GOLD", 2).expected_gold, 2.4)
        self.assertEqual(
            [order["probability"] for order in simulator.trade_order_profile("O_GOLD", 3).distribution],
            [0.3, 0.5, 0.2],
        )
        self.assertAlmostEqual(simulator.trade_order_profile("O_DIAMOND", 3).expected_seconds, 7200.0)
        alpha = simulator.trade_order_profile("O_GOLD", 3, effect_with_order_weight(1))
        beta = simulator.trade_order_profile("O_GOLD", 3, effect_with_order_weight(2))
        self.assertEqual(alpha.bias, "tailoring_alpha")
        self.assertEqual(beta.bias, "tailoring_beta")
        self.assertGreater(beta.expected_lmd, alpha.expected_lmd)

    def test_yituliu_orundum_trade_lookup_uses_static_guide_efficiency(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            game_data.data_version = "guide-test"
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_DIAMOND",
                [
                    skill("A", "TRADING", "order", "订单获取效率+30%", 30),
                    skill("B", "TRADING", "order", "订单获取效率+30%", 30),
                    skill("C", "TRADING", "order", "订单获取效率+30%", 30),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        room_report = report.roomReports[0]
        self.assertAlmostEqual(report.dailyExpected.orundum, 240.0 * 2.35)
        self.assertAlmostEqual(report.dailyExpected.shardDelta, -24.0 * 2.35)
        self.assertEqual(
            room_report["tradeOrderProfile"]["calibrationMode"],
            "guide_orundum_exact_lookup",
        )
        self.assertEqual(room_report["tradeOrderProfile"]["paperEfficiencyPercent"], 132.0)

    def test_yituliu_special_trade_lookup_for_shamare_tequila_butushu(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("巫恋", "TRADING", "低语", "订单获取效率归零"),
                    skill("龙舌兰", "TRADING", "龙门商法", "特殊订单"),
                    skill("但书", "TRADING", "违约索赔", "违约订单"),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        self.assertAlmostEqual(report.dailyExpected.lmdGross, 14772.0 * 2.07)
        self.assertAlmostEqual(report.dailyExpected.pureGoldDelta, -26.2 * 2.07)
        self.assertEqual(report.roomReports[0]["tradeOrderProfile"]["lookupId"], "shamare_tequila_butushu_l3")
        self.assertEqual(
            report.roomReports[0]["tradeOrderProfile"]["calibrationMode"],
            "guide_exact_lookup",
        )

    def test_yituliu_special_trade_lookup_adds_control_speed_bonus(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "Wang",
                        "CONTROL",
                        "control_prod_tra_spd[000]",
                        "进驻控制中枢时，若外势大于等于实地，所有贸易站订单效率+7%；若实地大于外势，所有制造站生产力+2%",
                        7,
                    )
                ],
                0,
                room_level=5,
                slots=5,
            )
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("巫恋", "TRADING", "低语", "订单获取效率归零"),
                    skill("龙舌兰", "TRADING", "龙门商法", "特别订单"),
                    skill("但书", "TRADING", "违约索赔", "违约订单"),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room], [])]
            )

        profile = report.roomReports[1]["tradeOrderProfile"]
        self.assertEqual(profile["lookupScheduleEffectivePercent"], 207.0)
        self.assertEqual(profile["controlSpeedPercent"], 7.0)
        self.assertEqual(profile["scheduleEffectivePercent"], 214.0)
        self.assertAlmostEqual(report.dailyExpected.lmdGross, 14772.0 * 2.14)

    def test_yituliu_special_trade_lookup_uses_per_combo_effective_percent(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            lookup = next(
                item
                for item in GUIDE_LMD_TRADE_LOOKUPS
                if item["id"] == "shamare_tailor_beta_tequila_l3"
            )
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill(name, "TRADING", "guide", "guide-calibrated lookup")
                    for name in sorted(lookup["required"])
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        self.assertAlmostEqual(report.dailyExpected.lmdGross, 12740.0 * 2.0)
        self.assertAlmostEqual(report.dailyExpected.pureGoldDelta, -20.8 * 2.0)
        self.assertEqual(
            report.roomReports[0]["tradeOrderProfile"]["scheduleEffectivePercent"], 200.0
        )

    def test_lmd_trade_lookup_threshold_uses_same_room_effective_speed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill(
                        "蕾缪安",
                        "TRADING",
                        "相伴",
                        "进驻贸易站时，订单获取效率+20%；当与能天使在同一个贸易站时，订单获取效率额外+25%",
                        20,
                    ),
                    skill("能天使", "TRADING", "物流专家", "进驻贸易站时，订单获取效率+40%", 40),
                    skill("德克萨斯", "TRADING", "物流专家", "进驻贸易站时，订单获取效率+40%", 40),
                ],
                0,
                room_level=3,
                slots=3,
            )

            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        self.assertAlmostEqual(report.dailyExpected.lmdGross, 10265.0 * 2.3)
        self.assertEqual(report.roomReports[0]["tradeOrderProfile"]["lookupId"], "generic_l3_three_40")

    def test_yituliu_special_trade_lookup_for_butushu_high_efficiency_partners(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("但书", "TRADING", "违约索赔", "违约订单"),
                    skill("古米", "TRADING", "高效贸易", "进驻贸易站时，订单获取效率+45%", 45),
                    skill("柏喙", "TRADING", "裁缝", "进驻贸易站时，订单获取效率+45%", 45),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        self.assertAlmostEqual(report.dailyExpected.lmdGross, 15929.0 * 2.0)
        self.assertAlmostEqual(report.dailyExpected.pureGoldDelta, -31.9 * 2.0)
        profile = report.roomReports[0]["tradeOrderProfile"]
        self.assertEqual(profile["lookupId"], "butushu_l3_plus_two_45")
        self.assertEqual(profile["partnerScoreMinimums"], [45.0, 45.0])

    def test_special_trade_mechanism_estimates_butushu_without_fixed_combo(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("但书", "TRADING", "违约索赔·β", "进驻贸易站时，如果下笔赤金订单是违约订单，则赤金交付数额外+2"),
                    skill("巫恋", "TRADING", "裁缝·α", "进驻贸易站时，小幅提升当前贸易站高品质贵金属订单的出现概率"),
                    skill("古米", "TRADING", "交际", "进驻贸易站时，订单获取效率+30%", 30),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        profile = report.roomReports[0]["tradeOrderProfile"]
        self.assertEqual(profile["calibrationMode"], "guide_mechanism_estimate")
        self.assertEqual(profile["mechanismId"], "butushu_contract_order")
        self.assertEqual(profile["anchorOperator"], "但书")
        self.assertAlmostEqual(profile["intrinsicSpeedPercent"], 10.0)
        self.assertAlmostEqual(profile["partnerSpeedPercent"], 30.0)
        self.assertAlmostEqual(profile["scheduleEffectivePercent"], 140.0)
        contributions = contributions_by_operator(profile)
        self.assertEqual(contributions["古米"]["classification"], "fixed_speed")
        self.assertTrue(contributions["古米"]["counted"])
        self.assertAlmostEqual(contributions["古米"]["countedSpeedPercent"], 30.0)
        self.assertEqual(contributions["巫恋"]["classification"], "tailoring_probability")
        self.assertFalse(contributions["巫恋"]["counted"])
        self.assertIn("reason", contributions["巫恋"])
        audit = audit_by_operator(report.roomReports[0])
        self.assertEqual(audit["但书"]["status"], "source_calibrated")
        self.assertEqual(audit["古米"]["status"], "counted")
        self.assertEqual(audit["巫恋"]["status"], "diagnostic_only")
        self.assertAlmostEqual(report.dailyExpected.lmdGross, 15929.0 * 1.4)
        self.assertAlmostEqual(report.dailyExpected.pureGoldDelta, -31.9 * 1.4)

    def test_special_trade_mechanism_estimate_adds_control_speed_bonus(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "Wang",
                        "CONTROL",
                        "control_prod_tra_spd[000]",
                        "进驻控制中枢时，若外势大于等于实地，所有贸易站订单效率+7%；若实地大于外势，所有制造站生产力+2%",
                        7,
                    )
                ],
                0,
                room_level=5,
                slots=5,
            )
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("但书", "TRADING", "违约索赔·β", "进驻贸易站时，如果下笔赤金订单是违约订单，则赤金交付数额外+2"),
                    skill("巫恋", "TRADING", "裁缝·α", "进驻贸易站时，小幅提升当前贸易站高品质贵金属订单的出现概率"),
                    skill("古米", "TRADING", "交际", "进驻贸易站时，订单获取效率+30%", 30),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room], [])]
            )

        profile = report.roomReports[1]["tradeOrderProfile"]
        self.assertEqual(profile["mechanismScheduleEffectivePercent"], 140.0)
        self.assertEqual(profile["controlSpeedPercent"], 7.0)
        self.assertEqual(profile["scheduleEffectivePercent"], 147.0)
        self.assertAlmostEqual(report.dailyExpected.lmdGross, 15929.0 * 1.47)

    def test_special_trade_mechanism_estimates_butushu_tailoring_only_partner(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("巫恋", "TRADING", "裁缝·α", "进驻贸易站时，小幅提升当前贸易站高品质贵金属订单的出现概率"),
                    skill("但书", "TRADING", "违约索赔·β", "进驻贸易站时，如果下笔赤金订单是违约订单，则赤金交付数额外+2"),
                    skill("柏喙", "TRADING", "裁缝·α", "进驻贸易站时，小幅提升当前贸易站高品质贵金属订单的出现概率"),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        profile = report.roomReports[0]["tradeOrderProfile"]
        self.assertEqual(profile["calibrationMode"], "guide_mechanism_estimate")
        self.assertEqual(profile["anchorOperator"], "但书")
        self.assertAlmostEqual(profile["partnerSpeedPercent"], 0.0)
        self.assertAlmostEqual(profile["scheduleEffectivePercent"], 110.0)
        contributions = contributions_by_operator(profile)
        self.assertEqual(contributions["巫恋"]["classification"], "tailoring_probability")
        self.assertEqual(contributions["柏喙"]["classification"], "tailoring_probability")
        self.assertFalse(contributions["巫恋"]["counted"])
        self.assertFalse(contributions["柏喙"]["counted"])
        self.assertGreater(len(profile["ignoredPartnerNotes"]), 0)

    def test_yituliu_special_trade_lookup_for_croissant_waai_fu_vigil(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("可露希尔", "TRADING", "特殊订单", "固定获取特别订单"),
                    skill("乌有", "TRADING", "高效贸易", "进驻贸易站时，订单获取效率+45%", 45),
                    skill("伺夜", "TRADING", "高效贸易", "进驻贸易站时，订单获取效率+45%", 45),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        self.assertAlmostEqual(report.dailyExpected.lmdGross, 12000.0 * 2.1)
        self.assertAlmostEqual(report.dailyExpected.pureGoldDelta, -20.0 * 2.1)
        profile = report.roomReports[0]["tradeOrderProfile"]
        self.assertEqual(profile["lookupId"], "croissant_l3_plus_two_45")
        self.assertEqual(profile["partnerScoreMinimums"], [45.0, 45.0])
        self.assertNotIn("partnerContributions", profile)

    def test_special_trade_mechanism_estimates_croissant_without_fixed_combo(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("可露希尔", "TRADING", "特殊订单", "固定获取特别订单"),
                    skill("乌有", "TRADING", "愿者上钩", "宿舍内每有1名干员则人间烟火+1，同时每有1点人间烟火，则订单获取效率+1%"),
                    skill("伺夜", "TRADING", "新城贸易", "进驻贸易站时，订单获取效率+25%，会客室每级额外提供5%获取效率", 25),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        profile = report.roomReports[0]["tradeOrderProfile"]
        self.assertEqual(profile["calibrationMode"], "guide_mechanism_estimate")
        self.assertEqual(profile["mechanismId"], "croissant_special_order")
        self.assertEqual(profile["anchorOperator"], "可露希尔")
        self.assertAlmostEqual(profile["intrinsicSpeedPercent"], 20.0)
        self.assertAlmostEqual(profile["partnerSpeedPercent"], 25.0)
        self.assertAlmostEqual(profile["scheduleEffectivePercent"], 145.0)
        contributions = contributions_by_operator(profile)
        self.assertEqual(contributions["乌有"]["classification"], "conditional_human_fire")
        self.assertFalse(contributions["乌有"]["counted"])
        self.assertAlmostEqual(contributions["乌有"]["countedSpeedPercent"], 0.0)
        self.assertIn("Human-fire", contributions["乌有"]["reason"])
        self.assertEqual(contributions["伺夜"]["classification"], "fixed_speed")
        self.assertTrue(contributions["伺夜"]["counted"])
        self.assertAlmostEqual(contributions["伺夜"]["countedSpeedPercent"], 25.0)
        self.assertAlmostEqual(report.dailyExpected.lmdGross, 12000.0 * 1.45)
        self.assertAlmostEqual(report.dailyExpected.pureGoldDelta, -20.0 * 1.45)

    def test_special_trade_mechanism_uses_same_room_effective_partner_speed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("但书", "TRADING", "违约索赔", "固定获取违约订单"),
                    skill(
                        "蕾缪安",
                        "TRADING",
                        "相伴",
                        "进驻贸易站时，订单获取效率+20%；当与能天使在同一个贸易站时，订单获取效率额外+25%",
                        20,
                    ),
                    skill("能天使", "TRADING", "物流专家", "进驻贸易站时，订单获取效率+35%", 35),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        profile = report.roomReports[0]["tradeOrderProfile"]
        self.assertEqual(profile["calibrationMode"], "guide_mechanism_estimate")
        self.assertEqual(profile["mechanismId"], "butushu_contract_order")
        self.assertAlmostEqual(profile["partnerSpeedPercent"], 80.0)
        self.assertAlmostEqual(profile["scheduleEffectivePercent"], 190.0)
        contributions = contributions_by_operator(profile)
        self.assertAlmostEqual(contributions["蕾缪安"]["countedSpeedPercent"], 45.0)
        self.assertAlmostEqual(report.dailyExpected.lmdGross, 15929.0 * 1.9)

    def test_special_trade_mechanism_marks_upgrade_partner_diagnostics(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="guide")
            upgrade = UpgradeRequirement(
                char_id="char_upgrade_partner",
                name="Upgrade Partner",
                from_elite=0,
                from_level=1,
                to_elite=2,
                to_level=1,
                cost_score=100.0,
                materials=[],
                note="test upgrade",
            )
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("但书", "TRADING", "违约索赔·β", "违约订单"),
                    skill(
                        "Upgrade Partner",
                        "TRADING",
                        "Upgrade Trade",
                        "Trading Post speed +35%",
                        35,
                        upgrade=upgrade,
                    ),
                    skill(
                        "Locked Diagnostic",
                        "TRADING",
                        "Locked Trade",
                        "Upgrade-only effect with no parsed speed",
                        0,
                        upgrade=upgrade,
                    ),
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        profile = report.roomReports[0]["tradeOrderProfile"]
        self.assertAlmostEqual(profile["partnerSpeedPercent"], 35.0)
        contributions = contributions_by_operator(profile)
        self.assertEqual(contributions["Upgrade Partner"]["classification"], "fixed_speed")
        self.assertTrue(contributions["Upgrade Partner"]["counted"])
        self.assertTrue(contributions["Upgrade Partner"]["hasUpgradeRequirement"])
        self.assertEqual(contributions["Locked Diagnostic"]["classification"], "upgrade_only")
        self.assertFalse(contributions["Locked Diagnostic"]["counted"])
        self.assertTrue(contributions["Locked Diagnostic"]["hasUpgradeRequirement"])
        audit = audit_by_operator(report.roomReports[0])
        self.assertEqual(audit["Upgrade Partner"]["status"], "counted")
        self.assertEqual(audit["Locked Diagnostic"]["status"], "explicitly_excluded")
        self.assertTrue(audit["Locked Diagnostic"]["hasUpgradeRequirement"])

    def test_drone_lmd_trade_matches_yituliu_243_extra_label_formula(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, drone_policy="lmd-trade")
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    BaseSkill(
                        "char_drone_trade",
                        "一图流贸易",
                        "TRADING",
                        "drone_trade",
                        "一图流无人机贸易验算",
                        "进驻贸易站时，订单获取效率+184.8%",
                        184.7896,
                        (),
                        0,
                        1,
                        True,
                        False,
                        184.7896,
                    )
                ],
                0,
                room_level=3,
                slots=3,
            )
            report = simulator.evaluate([ShiftPlan("A", "08:00", 24, [trade_room], [])])

        # Yituliu 2026-06 243 Orundum image: 30.6k + 14.7k LMD.
        # The stored calibration explains the purple 14.7k as 240 drones
        # applied to the best single LMD trading post with 29,543 LMD/24h.
        self.assertAlmostEqual(report.dailyExpected.droneCount, 240.0)
        self.assertAlmostEqual(
            report.dailyExpected.droneContribution["lmdGross"],
            14771.5,
            delta=1.0,
        )
        self.assertEqual(report.dailyExpected.droneTarget["roomId"], "trading_1")

    def test_power_station_skills_increase_daily_drone_generation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, drone_policy="lmd-trade")
            trade_room = room("trading_1", "TRADING", "O_GOLD")
            power_room = RoomAssignment(
                "power_1",
                "POWER",
                "POWER",
                None,
                [
                    BaseSkill(
                        "char_power",
                        "发电验算",
                        "POWER",
                        "power_test",
                        "发电站无人机验算",
                        "进驻发电站时，无人机充能速度+20%",
                        20,
                        (),
                        0,
                        1,
                        True,
                        False,
                        20,
                    )
                ],
                0,
            )

            report = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [trade_room, power_room], [])]
            )

        self.assertAlmostEqual(report.dailyExpected.droneCount, 288.0)
        self.assertAlmostEqual(report.dailyExpected.droneGenerationBonusPercent, 20.0)
        self.assertGreater(report.dailyExpected.droneContribution["lmdGross"], 0)

    def test_downloaded_dynamic_mower_sample_is_machine_readable(self) -> None:
        sample_path = Path("outputs/yituliu_2026_06_342_orundum_dynamic_mower.json")
        if not sample_path.exists():
            self.skipTest("Dynamic Mower sample has not been downloaded in this workspace.")
        payload = json.loads(sample_path.read_text(encoding="utf-8"))
        schedule = payload["data"]["schedule"]

        self.assertEqual(schedule["id"], 1775555941084837)
        self.assertIn("plan1", schedule)
        self.assertIn("conf", schedule)
        self.assertEqual(schedule["plan1"]["room_1_1"]["product"], "orundum")
        self.assertEqual(schedule["plan1"]["room_3_3"]["product"], "orirock")

    def test_shard_lmd_cost_matches_yituliu_per_pull_units(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            rock = ProductionSimulator(game_data, shard_formula="rock")
            device = ProductionSimulator(game_data, shard_formula="device")

        rock_formula = rock.manufacture_formula("F_DIAMOND")
        device_formula = device.manufacture_formula("F_DIAMOND")
        shards_per_pull = 60
        self.assertAlmostEqual(rock_formula["costs"]["4001"] * shards_per_pull / 10000, 9.6)
        self.assertAlmostEqual(device_formula["costs"]["4001"] * shards_per_pull / 10000, 6.0)

    def test_special_skill_rules_flag_and_estimate(self) -> None:
        shamare = skill("巫恋", "TRADING", "低语", "进驻贸易站时，当前贸易站内其他干员提供的订单获取效率全部归零，且每人为自身+45%订单获取效率")
        partner = skill("柏喙", "TRADING", "裁缝", "进驻贸易站时，订单获取效率+20%")
        butushu = skill("但书", "TRADING", "违约索赔·β", "进驻贸易站时，如果下笔赤金订单是违约订单，则赤金交付数额外+2")
        effect = evaluate_room_effect([shamare, partner, butushu], "O_GOLD")

        self.assertEqual(effect.speed_percent, 90.0)
        self.assertEqual(effect.lmd_per_order_bonus, 1000.0)
        self.assertTrue(effect.assumptions)

    def test_control_morale_condition_requires_amiya_same_control_room(self) -> None:
        mujica = skill(
            "魔王",
            "CONTROL",
            "魔王",
            "进驻控制中枢时，当与阿米娅一起进驻控制中枢时，心情每小时恢复+0.05",
        )
        amiya = skill("阿米娅", "CONTROL", "合作协议", "进驻控制中枢时，所有贸易站订单效率+7%", 7)

        without_amiya = evaluate_room_effect([mujica], None)
        with_amiya = evaluate_room_effect([mujica, amiya], None)

        self.assertEqual(without_amiya.fatigue_delta_per_hour, 0.0)
        self.assertTrue(without_amiya.unsupported)
        self.assertAlmostEqual(with_amiya.fatigue_delta_per_hour, -0.05)
        self.assertFalse(with_amiya.unsupported)

    def test_named_same_room_morale_condition_requires_partner(self) -> None:
        partner_skill = skill(
            "同站条件员",
            "TRADING",
            "默契",
            "当与能天使在同一个贸易站时，心情每小时消耗-0.3",
        )
        exusiai = skill("能天使", "TRADING", "贸易", "进驻贸易站时，订单获取效率+20%", 20)

        without_partner = evaluate_room_effect([partner_skill], "O_GOLD")
        with_partner = evaluate_room_effect([partner_skill, exusiai], "O_GOLD")

        self.assertEqual(without_partner.fatigue_delta_per_hour, 0.0)
        self.assertTrue(
            any(item["status"] == "condition_unmet" for item in without_partner.skill_effect_audit)
        )
        self.assertAlmostEqual(with_partner.fatigue_delta_per_hour, -0.3)

    def test_morale_delta_parses_consumption_and_recovery_signs(self) -> None:
        self.assertAlmostEqual(fatigue_delta_from_text("心情每小时恢复+0.05"), -0.05)
        self.assertAlmostEqual(fatigue_delta_from_text("心情每小时消耗+0.5"), 0.5)
        self.assertAlmostEqual(fatigue_delta_from_text("心情每小时消耗-0.1"), -0.1)
        self.assertAlmostEqual(fatigue_delta_from_text("心情每小时恢复-0.1"), 0.1)

    def test_morale_delta_parses_rich_text_tags(self) -> None:
        self.assertAlmostEqual(
            fatigue_delta_from_text("心情每小时消耗<@cc.vdown>+0.5</>"),
            0.5,
        )
        self.assertAlmostEqual(
            fatigue_delta_from_text("心情每小时恢复<@cc.vup>+0.05</>"),
            -0.05,
        )

    def test_fatigue_risk_clamps_recovery_below_zero(self) -> None:
        control_room = RoomAssignment(
            "control_1",
            "CONTROL",
            "CONTROL",
            None,
            [skill("恢复员", "CONTROL", "恢复", "心情每小时恢复+2")],
            0,
        )

        risk = fatigue_risk(
            control_room,
            12.0,
            RoomSkillEffect(fatigue_delta_per_hour=-2.0),
        )

        self.assertEqual(risk, 0.0)

    def test_formula_profile_counts_non_speed_partner_effects_under_shamare(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="formula")
            shamare = skill("巫恋", "TRADING", "低语", "进驻贸易站时，当前贸易站内其他干员提供的订单获取效率全部归零，且每人为自身+45%订单获取效率")
            filler = skill("占位", "TRADING", "协助", "进驻贸易站时，订单获取效率+20%", 20)
            butushu = skill("但书", "TRADING", "违约索赔·β", "进驻贸易站时，如果下笔赤金订单是违约订单，则赤金交付数额外+2")
            with_butushu = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [shamare, filler, butushu],
                0,
                room_level=3,
                slots=3,
            )
            without_butushu = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [shamare, filler, skill("占位2", "TRADING", "协助", "进驻贸易站时，订单获取效率+20%", 20)],
                0,
                room_level=3,
                slots=3,
            )

            report_with = simulator.evaluate([ShiftPlan("A", "08:00", 24, [with_butushu], [])])
            report_without = simulator.evaluate([ShiftPlan("A", "08:00", 24, [without_butushu], [])])

        self.assertGreater(report_with.dailyExpected.lmdGross, report_without.dailyExpected.lmdGross)

    def test_control_center_global_trade_bonus_applies_to_trading_posts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="formula")
            trade_room = room("trading_1", "TRADING", "O_GOLD")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "阿米娅",
                        "CONTROL",
                        "合作协议",
                        "进驻控制中枢时，所有贸易站订单效率+7%（同种效果取最高）",
                        7,
                    )
                ],
                0,
                room_level=5,
                slots=5,
            )

            with_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room], [])]
            )
            without_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [trade_room], [])]
            )

        self.assertGreater(with_control.dailyExpected.lmdGross, without_control.dailyExpected.lmdGross)
        self.assertTrue(
            any(
                "控制中枢全局贸易站加成" in assumption
                for report in with_control.roomReports
                for assumption in report["assumptions"]
            )
        )
        trade_reports = [item for item in with_control.roomReports if item["roomType"] == "TRADING"]
        self.assertTrue(
            any(
                item["status"] == "counted" and item["scope"] == "control_global:trade_global"
                for report in trade_reports
                for item in report["skillEffectAudit"]
            )
        )

    def test_control_center_mujica_fixed_gold_manufacture_bonus_is_counted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="formula")
            gold_room = room("manufacture_1", "MANUFACTURE", "F_GOLD")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "Sakiko",
                        "CONTROL",
                        "control_prod_bd_spd[010]",
                        "进驻控制中枢时，所有生产贵金属类配方的制造站生产力+1%，每有20点热情值，所有生产贵金属类配方的制造站生产力+1%",
                        1,
                    )
                ],
                0,
                room_level=5,
                slots=5,
            )

            with_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, gold_room], [])]
            )
            without_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [gold_room], [])]
            )

        self.assertGreater(with_control.dailyExpected.pureGoldDelta, without_control.dailyExpected.pureGoldDelta)
        manufacture_report = next(
            item for item in with_control.roomReports if item["roomType"] == "MANUFACTURE"
        )
        self.assertTrue(
            any(
                item["status"] == "counted"
                and item["scope"] == "control_global:gold_manufacture_global"
                for item in manufacture_report["skillEffectAudit"]
            )
        )
        control_report = next(item for item in with_control.roomReports if item["roomType"] == "CONTROL")
        self.assertTrue(control_report["unsupportedSkillEffects"])

    def test_control_center_mujica_trade_bonus_uses_deterministic_enthusiasm(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="formula")
            trade_room = room("trading_1", "TRADING", "O_GOLD")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "Mutsumi",
                        "CONTROL",
                        "control_mp_bd&trade[000]",
                        "进驻控制中枢时，热情值+20；每有8点热情值，所有贸易站订单效率+1%",
                        1,
                    ),
                    skill(
                        "Umiri",
                        "CONTROL",
                        "control_hire_spd&bd[000]",
                        "进驻控制中枢时，热情值+10；人脉资源的联络速度+10%",
                        10,
                    ),
                    skill(
                        "Wakaba",
                        "CONTROL",
                        "control_meeting_spd&bd[000]",
                        "进驻控制中枢时，热情值+10；会客室线索搜集速度提升+5%",
                        10,
                    ),
                ],
                0,
                room_level=5,
                slots=5,
            )

            with_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room], [])]
            )
            without_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [trade_room], [])]
            )

        self.assertGreater(with_control.dailyExpected.lmdGross, without_control.dailyExpected.lmdGross)
        trade_report = next(item for item in with_control.roomReports if item["roomType"] == "TRADING")
        self.assertTrue(
            any(
                item["status"] == "counted"
                and item["scope"] == "control_global:trade_global"
                and "热情值" in item["reason"]
                for item in trade_report["skillEffectAudit"]
            )
        )

    def test_control_center_mujica_enthusiasm_counts_shift_dormitories(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="formula")
            trade_room = room("trading_1", "TRADING", "O_GOLD")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "Mutsumi",
                        "CONTROL",
                        "control_mp_bd&trade[000]",
                        "进驻控制中枢时，热情值+20；每有8点热情值，所有贸易站订单效率+1%",
                        1,
                    ),
                    skill(
                        "Uika",
                        "CONTROL",
                        "control_dorm_bd[000]",
                        "进驻控制中枢时，宿舍内每有1名干员，热情值+1",
                        0,
                    ),
                ],
                0,
                room_level=5,
                slots=5,
            )
            dormitory = RoomAssignment(
                "dormitory_1",
                "DORMITORY",
                "DORMITORY",
                None,
                [skill(f"Rest{i}", "DORMITORY", "rest", "", 0) for i in range(5)],
                0,
                room_level=5,
                slots=5,
            )

            with_dorm = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room], [dormitory])]
            )
            without_dorm = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room], [])]
            )

        self.assertGreater(with_dorm.dailyExpected.lmdGross, without_dorm.dailyExpected.lmdGross)

    def test_control_center_wang_default_branch_counts_trade_bonus(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="formula")
            trade_room = room("trading_1", "TRADING", "O_GOLD")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "Wang",
                        "CONTROL",
                        "control_prod_tra_spd[000]",
                        "进驻控制中枢时，若外势大于等于实地，所有贸易站订单效率+7%；若实地大于外势，所有制造站生产力+2%",
                        7,
                    )
                ],
                0,
                room_level=5,
                slots=5,
            )

            with_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room], [])]
            )
            without_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [trade_room], [])]
            )

        self.assertGreater(with_control.dailyExpected.lmdGross, without_control.dailyExpected.lmdGross)
        control_report = next(item for item in with_control.roomReports if item["roomType"] == "CONTROL")
        self.assertFalse(control_report["unsupportedSkillEffects"])
        trade_report = next(item for item in with_control.roomReports if item["roomType"] == "TRADING")
        self.assertTrue(
            any(
                item["status"] == "counted"
                and item["scope"] == "control_global:trade_global"
                and "权变" in item["reason"]
                for item in trade_report["skillEffectAudit"]
            )
        )

    def test_unmodeled_control_condition_is_audited_as_unsupported(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="formula")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "条件中枢",
                        "CONTROL",
                        "未知条件",
                        "进驻控制中枢时，如果测试条件满足，则所有贸易站订单效率+9%",
                        9,
                    )
                ],
                0,
                room_level=5,
                slots=5,
            )
            trade_room = room("trading_1", "TRADING", "O_GOLD")
            report = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room], [])]
            )

        control_report = next(item for item in report.roomReports if item["roomType"] == "CONTROL")
        audit = audit_by_operator(control_report)
        self.assertEqual(audit["条件中枢"]["status"], "unsupported")
        self.assertTrue(control_report["unsupportedSkillEffects"])
        trade_report = next(item for item in report.roomReports if item["roomType"] == "TRADING")
        self.assertFalse(
            any(item["scope"] == "control_global:trade_global" for item in trade_report["skillEffectAudit"])
        )

    def test_same_room_faction_trade_bonus_is_formula_scored(self) -> None:
        morgan = skill(
            "摩根",
            "TRADING",
            "帮派指南针",
            "进驻贸易站时，同个贸易站中每有1名格拉斯哥帮干员，当前贸易站订单获取效率+20%；当与推进之王在同一个贸易站时，订单获取效率额外+35%",
            35,
            faction_tags=("glasgow",),
        )
        siege = skill(
            "推进之王",
            "TRADING",
            "无匹配基建技能",
            "用于测试格拉斯哥帮阵营计数。",
            0,
            faction_tags=("glasgow",),
        )
        effect = evaluate_room_effect([morgan, siege], "O_GOLD")

        self.assertEqual(effect.speed_percent, 75.0)
        self.assertTrue(any("格拉斯哥帮 2 人" in item for item in effect.assumptions))
        self.assertEqual(effect.skill_effect_audit[0]["status"], "counted")

    def test_zero_score_conditional_skill_is_diagnostic_only(self) -> None:
        conditional = skill(
            "条件伙伴",
            "TRADING",
            "测试条件",
            "Trading Post speed +35% if an external condition is satisfied",
            0,
        )
        effect = evaluate_room_effect([conditional], "O_GOLD")

        self.assertEqual(effect.speed_percent, 0.0)
        self.assertEqual(effect.skill_effect_audit[0]["status"], "diagnostic_only")

    def test_global_faction_counts_affect_room_skill_formula(self) -> None:
        nasti = skill(
            "娜斯提",
            "MANUFACTURE",
            "造价高昂",
            "进驻制造站时，基建内（不包含副手及活动室使用者）每有1名莱茵生命干员（最多5名），贵金属类配方的生产力+3%",
            3,
            faction_tags=("rhine",),
        )
        from arknights_schedule_generator.skill_rules import SkillContext

        effect = evaluate_room_effect(
            [nasti],
            "F_GOLD",
            SkillContext(active_faction_counts={"rhine": 2}),
        )

        self.assertEqual(effect.speed_percent, 6.0)
        self.assertTrue(any("莱茵生命 2 人" in item for item in effect.assumptions))

    def test_control_center_faction_trade_bonus_counts_room_factions(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="formula")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "戴菲恩",
                        "CONTROL",
                        "运筹好手",
                        "进驻控制中枢时，同一贸易站中，每有1名格拉斯哥帮干员，订单获取效率+10%",
                        10,
                    )
                ],
                0,
                room_level=5,
                slots=5,
            )
            trade_room = RoomAssignment(
                "trading_1",
                "TRADING",
                "TRADING",
                "O_GOLD",
                [
                    skill("摩根", "TRADING", "测试", "进驻贸易站时，订单获取效率+0%", 0, faction_tags=("glasgow",)),
                    skill("推进之王", "TRADING", "测试", "进驻贸易站时，订单获取效率+0%", 0, faction_tags=("glasgow",)),
                ],
                0,
                room_level=3,
                slots=3,
            )

            with_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room], [])]
            )
            without_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [trade_room], [])]
            )

        self.assertGreater(with_control.dailyExpected.lmdGross, without_control.dailyExpected.lmdGross)
        trade_reports = [item for item in with_control.roomReports if item["roomType"] == "TRADING"]
        self.assertTrue(any(report["speedPercent"] >= 20 for report in trade_reports))

    def test_second_batch_same_room_faction_production_rules(self) -> None:
        fang = skill(
            "历阵锐枪芬",
            "MANUFACTURE",
            "重聚时光",
            "进驻制造站时，当前制造站内每个A1小队干员为自身+10%的生产力",
            10,
            faction_tags=("A1",),
        )
        kroos = skill("克洛丝", "MANUFACTURE", "测试", "A1 占位", 0, faction_tags=("A1",))
        effect = evaluate_room_effect([fang, kroos], "F_EXP")
        self.assertEqual(effect.speed_percent, 20.0)

        orchid = skill(
            "焰狐龙梓兰",
            "TRADING",
            "队长的自觉",
            "进驻贸易站时，订单上限+3，且每有一个泡影国狩猎小队干员进驻贸易站，订单获取效率+20%",
            20,
            faction_tags=("mh2",),
        )
        buddy = skill("罗德岛隐秘队", "TRADING", "测试", "泡影国狩猎小队占位", 0, faction_tags=("mh2",))
        trade_effect = evaluate_room_effect([orchid, buddy], "O_GOLD")
        self.assertEqual(trade_effect.speed_percent, 40.0)
        self.assertEqual(trade_effect.order_limit_bonus, 3.0)

    def test_second_batch_cross_room_power_platform_rules(self) -> None:
        from arknights_schedule_generator.skill_rules import SkillContext

        alanna = skill(
            "阿兰娜",
            "MANUFACTURE",
            "机械精通·β",
            "进驻制造站时，每有1台作业平台进驻发电站，贵金属类配方的生产力+10%",
            10,
        )
        effect = evaluate_room_effect(
            [alanna],
            "F_GOLD",
            SkillContext(power_faction_counts={"op": 2}),
        )
        self.assertEqual(effect.speed_percent, 20.0)

        gallus = skill(
            "GALLUS²",
            "POWER",
            "鸡励机制",
            "进驻发电站时，如果其他作业平台进驻在发电站，则无人机充能速度+5%",
            5,
            faction_tags=("op",),
        )
        power_effect = evaluate_room_effect(
            [gallus],
            None,
            SkillContext(power_faction_counts={"op": 2}),
        )
        self.assertEqual(power_effect.speed_percent, 5.0)

    def test_second_batch_control_center_faction_production_rules(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_data(data_dir)
            game_data = GameData.load(data_dir)
            simulator = ProductionSimulator(game_data, calibration_profile="formula")
            control_room = RoomAssignment(
                "control_1",
                "CONTROL",
                "CONTROL",
                None,
                [
                    skill(
                        "火龙S黑角",
                        "CONTROL",
                        "秘传交涉术",
                        "当与怪物猎人小队干员进驻控制中枢一起工作时，所有贸易站订单效率+7%（同种效果取最高）",
                        7,
                        faction_tags=("mh",),
                    ),
                    skill("麒麟R夜刀", "CONTROL", "测试", "怪物猎人小队占位", 0, faction_tags=("mh",)),
                    skill(
                        "薇薇安娜",
                        "CONTROL",
                        "烛骑士微光",
                        "进驻控制中枢时，每个进驻在制造站的骑士干员生产力+7%",
                        7,
                    ),
                ],
                0,
                room_level=5,
                slots=5,
            )
            trade_room = room("trading_1", "TRADING", "O_GOLD")
            manufacture_room = RoomAssignment(
                "manufacture_1",
                "MANUFACTURE",
                "MANUFACTURE",
                "F_EXP",
                [skill("正义骑士号", "MANUFACTURE", "测试", "骑士占位", 0, faction_tags=("knight",))],
                0,
                room_level=3,
                slots=3,
            )
            with_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [control_room, trade_room, manufacture_room], [])]
            )
            without_control = simulator.evaluate(
                [ShiftPlan("A", "08:00", 24, [trade_room, manufacture_room], [])]
            )

        self.assertGreater(with_control.dailyExpected.lmdGross, without_control.dailyExpected.lmdGross)
        self.assertGreater(with_control.dailyExpected.exp, without_control.dailyExpected.exp)


def room(room_id: str, room_type: str, target: str | None) -> RoomAssignment:
    return RoomAssignment(room_id, room_type, room_type, target, [], 0)


def effect_with_order_weight(level: int) -> RoomSkillEffect:
    return RoomSkillEffect(order_weight_level=level)


def contributions_by_operator(profile: dict) -> dict[str, dict]:
    return {
        contribution["operatorName"]: contribution
        for contribution in profile["partnerContributions"]
    }


def audit_by_operator(report: dict) -> dict[str, dict]:
    return {item["operator"]: item for item in report["skillEffectAudit"]}


def skill(
    name: str,
    room_type: str,
    buff_name: str,
    description: str,
    parsed_score: float = 0,
    faction_tags: tuple[str, ...] = (),
    upgrade: UpgradeRequirement | None = None,
) -> BaseSkill:
    return BaseSkill(
        char_id=f"char_{name}",
        operator_name=name,
        room_type=room_type,
        buff_id=buff_name,
        buff_name=buff_name,
        description=description,
        efficiency=parsed_score,
        targets=(),
        cond_elite=0,
        cond_level=1,
        unlocked=True,
        complex_condition=True,
        parsed_score=parsed_score,
        upgrade=upgrade,
        faction_tags=faction_tags,
    )


def write_data(data_dir: Path) -> None:
    rooms = {
        "CONTROL": {"phases": [{"maxStationedNum": 5, "electricity": 0}]},
        "TRADING": {"phases": [{"maxStationedNum": 1, "electricity": -10}, {"maxStationedNum": 2, "electricity": -30}, {"maxStationedNum": 3, "electricity": -60}]},
        "MANUFACTURE": {"phases": [{"maxStationedNum": 1, "electricity": -10}, {"maxStationedNum": 2, "electricity": -30}, {"maxStationedNum": 3, "electricity": -60}]},
        "POWER": {"phases": [{"maxStationedNum": 1, "electricity": 60}, {"maxStationedNum": 1, "electricity": 130}, {"maxStationedNum": 1, "electricity": 270}]},
        "DORMITORY": {"phases": [{"maxStationedNum": 5, "electricity": -10}, {"maxStationedNum": 5, "electricity": -20}, {"maxStationedNum": 5, "electricity": -30}, {"maxStationedNum": 5, "electricity": -45}, {"maxStationedNum": 5, "electricity": -65}]},
        "MEETING": {"phases": [{"maxStationedNum": 2, "electricity": -10}, {"maxStationedNum": 2, "electricity": -30}, {"maxStationedNum": 2, "electricity": -60}]},
        "HIRE": {"phases": [{"maxStationedNum": 2, "electricity": -10}, {"maxStationedNum": 2, "electricity": -30}, {"maxStationedNum": 2, "electricity": -60}]},
        "TRAINING": {"phases": [{"maxStationedNum": 2, "electricity": -10}, {"maxStationedNum": 2, "electricity": -30}, {"maxStationedNum": 2, "electricity": -60}]},
        "WORKSHOP": {"phases": [{"maxStationedNum": 1, "electricity": -10}, {"maxStationedNum": 1, "electricity": -10}, {"maxStationedNum": 1, "electricity": -10}]},
    }
    building = {
        "rooms": rooms,
        "manufactData": {"phases": [{"outputCapacity": 54}]},
        "tradingData": {"phases": [{"orderLimit": 10}]},
        "manufactFormulas": {
            "3": {"formulaId": "3", "itemId": "2003", "count": 1, "costPoint": 10800, "formulaType": "F_EXP", "costs": []},
            "4": {"formulaId": "4", "itemId": "3003", "count": 1, "costPoint": 4320, "formulaType": "F_GOLD", "costs": []},
        },
        "buffs": {
            "trade": {"buffId": "trade", "buffName": "贸易", "roomType": "TRADING", "description": "进驻贸易站时，订单获取效率+30%", "efficiency": 30, "targets": []},
            "manu": {"buffId": "manu", "buffName": "制造", "roomType": "MANUFACTURE", "description": "进驻制造站时，生产力+30%", "efficiency": 30, "targets": []},
            "power": {"buffId": "power", "buffName": "发电", "roomType": "POWER", "description": "进驻发电站时，无人机充能速度+10%", "efficiency": 10, "targets": []},
        },
        "chars": {
            "char_texas": {"charId": "char_texas", "buffChar": [{"buffData": [{"buffId": "trade", "cond": {"phase": "PHASE_0", "level": 1}}]}]},
            "char_sora": {"charId": "char_sora", "buffChar": [{"buffData": [{"buffId": "manu", "cond": {"phase": "PHASE_0", "level": 1}}]}]},
            "char_steward": {"charId": "char_steward", "buffChar": [{"buffData": [{"buffId": "power", "cond": {"phase": "PHASE_0", "level": 1}}]}]},
        },
    }
    characters = {
        "char_texas": {"name": "德克萨斯", "rarity": "TIER_5", "phases": [{"maxLevel": 50, "evolveCost": None}]},
        "char_sora": {"name": "空", "rarity": "TIER_5", "phases": [{"maxLevel": 50, "evolveCost": None}]},
        "char_steward": {"name": "史都华德", "rarity": "TIER_3", "phases": [{"maxLevel": 40, "evolveCost": None}]},
    }
    items = {"items": {"30012": {"name": "固源岩", "rarity": "TIER_2"}, "4001": {"name": "龙门币", "rarity": "TIER_4"}}}
    (data_dir / "building_data.json").write_text(json.dumps(building), encoding="utf-8")
    (data_dir / "character_table.json").write_text(json.dumps(characters), encoding="utf-8")
    (data_dir / "item_table.json").write_text(json.dumps(items), encoding="utf-8")
    (data_dir / "data_version.txt").write_text("test", encoding="utf-8")


def add_diagnostic_data(data_dir: Path) -> None:
    building_path = data_dir / "building_data.json"
    character_path = data_dir / "character_table.json"
    building = json.loads(building_path.read_text(encoding="utf-8"))
    characters = json.loads(character_path.read_text(encoding="utf-8"))
    building["buffs"].update(
        {
            "leto_partner": {
                "buffId": "leto_partner",
                "buffName": "患难拍档",
                "roomType": "MANUFACTURE",
                "description": "进驻制造站时，若古米在贸易站，则作战记录类配方的生产力+35%",
                "efficiency": 0,
                "targets": ["F_EXP"],
            },
            "gummy_trade": {
                "buffId": "gummy_trade",
                "buffName": "交际",
                "roomType": "TRADING",
                "description": "进驻贸易站时，订单获取效率+30%",
                "efficiency": 30,
                "targets": [],
            },
            "eyja_shard": {
                "buffId": "eyja_shard",
                "buffName": "火山学家",
                "roomType": "MANUFACTURE",
                "description": "进驻制造站时，源石类配方的生产力+35%",
                "efficiency": 35,
                "targets": ["F_DIAMOND"],
            },
        }
    )
    for char_id, name, buff_id in [
        ("char_leto", "烈夏", "leto_partner"),
        ("char_gummy", "古米", "gummy_trade"),
        ("char_eyja", "艾雅法拉", "eyja_shard"),
    ]:
        building["chars"][char_id] = {
            "charId": char_id,
            "buffChar": [
                {"buffData": [{"buffId": buff_id, "cond": {"phase": "PHASE_0", "level": 1}}]}
            ],
        }
        characters[char_id] = {
            "name": name,
            "rarity": "TIER_5",
            "phases": [{"maxLevel": 80, "evolveCost": None}],
        }
    building_path.write_text(json.dumps(building, ensure_ascii=False), encoding="utf-8")
    character_path.write_text(json.dumps(characters, ensure_ascii=False), encoding="utf-8")


def add_locked_trade_upgrade(data_dir: Path) -> None:
    building_path = data_dir / "building_data.json"
    character_path = data_dir / "character_table.json"
    building = json.loads(building_path.read_text(encoding="utf-8"))
    characters = json.loads(character_path.read_text(encoding="utf-8"))
    building["buffs"]["locked_trade_boost"] = {
        "buffId": "locked_trade_boost",
        "buffName": "补练贸易测试",
        "roomType": "TRADING",
        "description": "进驻贸易站时，订单获取效率+120%",
        "efficiency": 120,
        "targets": [],
    }
    building["chars"]["char_locked_trade"] = {
        "charId": "char_locked_trade",
        "buffChar": [
            {
                "buffData": [
                    {"buffId": "locked_trade_boost", "cond": {"phase": "PHASE_1", "level": 1}}
                ]
            }
        ],
    }
    characters["char_locked_trade"] = {
        "name": "补练贸易",
        "rarity": "TIER_3",
        "phases": [
            {"maxLevel": 40, "evolveCost": None},
            {"maxLevel": 55, "evolveCost": None},
        ],
    }
    building_path.write_text(json.dumps(building, ensure_ascii=False), encoding="utf-8")
    character_path.write_text(json.dumps(characters, ensure_ascii=False), encoding="utf-8")


def sample_schedule() -> dict:
    return {
        "scheduleType": {"planTimes": 3, "trading": 2, "manufacture": 4, "power": 3, "dormitory": 4},
        "plans": [
            {
                "name": "第1班",
                "rooms": {
                    "trading": [{"product": "Orundum", "operators": ["德克萨斯"]}],
                    "manufacture": [
                        {"product": "Battle Record", "operators": ["空"]},
                        {"product": "Originium Shard", "operators": ["空"]},
                    ],
                    "power": [],
                    "dormitory": [],
                },
            },
            {"name": "第2班", "rooms": {"trading": [], "manufacture": [], "power": [], "dormitory": []}},
            {"name": "第3班", "rooms": {"trading": [], "manufacture": [], "power": [], "dormitory": []}},
        ],
    }


if __name__ == "__main__":
    unittest.main()
