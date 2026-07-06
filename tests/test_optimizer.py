from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from arknights_schedule_generator import dependency_parser
from arknights_schedule_generator.data import GameData
from arknights_schedule_generator.exporter import find_conflicts
from arknights_schedule_generator.models import (
    BaseSkill,
    RoomAssignment,
    RosterOperator,
    ShiftPlan,
    UpgradeRequirement,
)
from arknights_schedule_generator.optimizer import (
    COMBO_CANDIDATE_POOL_LIMIT,
    RoomCombo,
    RoomSpec,
    ScheduleOptimizer,
    ShiftInsertionGroup,
    ShiftInsertionSpec,
    apply_layout_variant,
    enumerate_target_options,
    forced_target_options,
    insertion_objective_score,
    insertion_group_to_dict,
    normalize_forced_targets,
    parse_layout,
    room_output_score,
    target_selection_score,
)
from arknights_schedule_generator.production import ProductionReport, ProductionSimulator, ProductionVector


class OptimizerTest(unittest.TestCase):
    def test_lmd_trade_room_score_ignores_pure_gold_consumption(self) -> None:
        spec = RoomSpec("trading_1", "TRADING", "O_GOLD", 3)
        lower_lmd_lower_gold_use = ProductionVector(lmdGross=10000.0, pureGoldDelta=-2.0)
        higher_lmd_higher_gold_use = ProductionVector(lmdGross=11000.0, pureGoldDelta=-200.0)

        self.assertGreater(
            room_output_score(spec, higher_lmd_higher_gold_use),
            room_output_score(spec, lower_lmd_lower_gold_use),
        )

    def test_target_selection_inventory_penalty_ignores_pure_gold_delta(self) -> None:
        balanced_gold = production_report_for_selection(
            20000.0,
            1000.0,
            10.0,
            100.0,
            lmd_net=-1000.0,
            pure_gold_delta=0.0,
            shard_delta=0.0,
        )
        deep_gold_deficit = production_report_for_selection(
            20000.0,
            1000.0,
            10.0,
            100.0,
            lmd_net=-1000.0,
            pure_gold_delta=-100.0,
            shard_delta=0.0,
        )

        self.assertAlmostEqual(
            target_selection_score("balanced_orundum", balanced_gold),
            target_selection_score("balanced_orundum", deep_gold_deficit),
        )

    def test_target_options_preserve_permutations_for_variable_room_levels(self) -> None:
        layout = apply_layout_variant(parse_layout("342"), "342-guide-orundum")

        options = enumerate_target_options(layout, "max_orundum")
        trading_orders = {tuple(trading) for trading, _ in options}
        manufacture_orders = {tuple(manufacture) for _, manufacture in options}

        self.assertIn(("O_GOLD", "O_DIAMOND", "O_GOLD"), trading_orders)
        self.assertIn(("O_GOLD", "O_GOLD", "O_DIAMOND"), trading_orders)
        self.assertTrue(all("F_EXP" not in targets for targets in manufacture_orders))

    def test_forced_targets_validate_exact_room_counts(self) -> None:
        layout = parse_layout("252")

        trading, manufacture = normalize_forced_targets(
            layout,
            (
                ["O_GOLD", "O_GOLD"],
                ["F_EXP", "F_EXP", "F_GOLD", "F_GOLD", "F_GOLD"],
            ),
        )

        self.assertEqual(trading, ["O_GOLD", "O_GOLD"])
        self.assertEqual(manufacture.count("F_GOLD"), 3)
        with self.assertRaises(ValueError):
            normalize_forced_targets(layout, (["O_GOLD"], manufacture))

    def test_forced_target_options_prioritize_exp_on_high_slot_factory(self) -> None:
        layout = apply_layout_variant(parse_layout("252"), "252-11")

        options = forced_target_options(
            layout,
            (
                ["O_GOLD", "O_GOLD"],
                ["F_GOLD", "F_GOLD", "F_EXP", "F_EXP", "F_EXP"],
            ),
        )
        manufacture_orders = {tuple(manufacture) for _, manufacture in options}

        self.assertEqual(len(manufacture_orders), 1)
        self.assertIn(("F_EXP", "F_EXP", "F_EXP", "F_GOLD", "F_GOLD"), manufacture_orders)

    def test_cached_candidate_rejects_stale_optimizer_model_version(self) -> None:
        from arknights_schedule_generator.optimizer import OPTIMIZER_MODEL_VERSION
        from arknights_schedule_generator.recommender import load_cached_candidate_evaluation

        with TemporaryDirectory() as temp_dir:
            export_path = Path(temp_dir) / "candidate.json"
            export_path.write_text(
                json.dumps(
                    {
                        "analysis": {
                            "diagnosticInsertionSearch": {
                                "optimizerModelVersion": OPTIMIZER_MODEL_VERSION - 1,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            cached = load_cached_candidate_evaluation(
                export_path,
                candidate_id="stale",
                expected_layout_raw="243",
                allow_upgrades=False,
                profile="guide",
                profile_label="guide",
                baseline_score=None,
                output_dir=Path(temp_dir),
                expected_max_groups=64,
                expected_joint_candidate_limit=64,
                expected_optimizer_model_version=OPTIMIZER_MODEL_VERSION,
                expected_anchor_count=0,
            )

        self.assertIsNone(cached)

    def test_balanced_orundum_selection_penalizes_exp_oversupply(self) -> None:
        exp_heavy = production_report_for_selection(
            22076.0,
            31680.0,
            498.0,
            338.0,
            lmd_net=-56836.0,
            pure_gold_delta=-0.052,
            shard_delta=-0.44,
        )
        reference_shaped = production_report_for_selection(
            29301.0,
            15840.0,
            475.0,
            164.0,
            lmd_net=-48651.0,
            pure_gold_delta=19.558,
            shard_delta=1.2,
        )

        self.assertGreater(
            target_selection_score("balanced_orundum", reference_shaped),
            target_selection_score("balanced_orundum", exp_heavy),
        )

    def test_max_orundum_insertion_objective_prefers_lmd_when_orundum_is_unchanged(self) -> None:
        lower_lmd_higher_generic_score = production_report_for_selection(
            45450.0,
            0.0,
            480.0,
            260.0,
        )
        higher_lmd_lower_generic_score = production_report_for_selection(
            46000.0,
            0.0,
            480.0,
            250.0,
        )

        self.assertGreater(
            insertion_objective_score("max_orundum", higher_lmd_lower_generic_score),
            insertion_objective_score("max_orundum", lower_lmd_higher_generic_score),
        )
        self.assertLess(
            insertion_objective_score("normal", higher_lmd_lower_generic_score),
            insertion_objective_score("normal", lower_lmd_higher_generic_score),
        )

    def test_dormitory_fill_prefers_operator_anchor_names(self) -> None:
        roster = [
            RosterOperator("普通休息甲", True, 5, 1, 0),
            RosterOperator("普通休息乙", True, 5, 1, 0),
            RosterOperator("参考锚点", True, 5, 1, 0),
            RosterOperator("参考锚点靠后", True, 5, 1, 0),
        ]
        optimizer = ScheduleOptimizer(GameData({"rooms": {}}, {}, {}), roster)
        optimizer.operator_anchor_preference = {"参考锚点", "参考锚点靠后"}
        optimizer.operator_anchor_rank = {"参考锚点": 0, "参考锚点靠后": 1}

        dormitories = optimizer._assign_dormitories(
            [("dormitory_1", "DORMITORY", None, 1)],
            active_names=set(),
            rest_priority=["普通休息甲"],
        )

        self.assertEqual(dormitories[0].operators[0].operator_name, "参考锚点")

        visible_anchor_names = {"参考锚点"}
        next_dormitories = optimizer._assign_dormitories(
            [("dormitory_1", "DORMITORY", None, 1)],
            active_names=set(),
            rest_priority=["普通休息甲"],
            visible_anchor_names=visible_anchor_names,
        )

        self.assertEqual(next_dormitories[0].operators[0].operator_name, "参考锚点靠后")

    def test_dormitory_anchor_coverage_replaces_duplicate_anchor(self) -> None:
        roster = [
            RosterOperator("参考锚点A", True, 5, 1, 0),
            RosterOperator("参考锚点B", True, 5, 1, 0),
            RosterOperator("参考锚点C", True, 5, 1, 0),
        ]
        optimizer = ScheduleOptimizer(GameData({"rooms": {}}, {}, {}), roster)
        optimizer.operator_anchor_preference = {"参考锚点A", "参考锚点B", "参考锚点C"}
        optimizer.operator_anchor_rank = {"参考锚点A": 0, "参考锚点B": 1, "参考锚点C": 2}
        shifts = [
            ShiftPlan(
                "A",
                "08:00",
                12,
                [],
                [
                    RoomAssignment(
                        "dormitory_1",
                        "DORMITORY",
                        "DORMITORY",
                        None,
                        [
                            optimizer._dorm_candidate("参考锚点A").skill,
                            optimizer._dorm_candidate("参考锚点B").skill,
                        ],
                        0,
                    )
                ],
            ),
            ShiftPlan(
                "B",
                "20:00",
                12,
                [],
                [
                    RoomAssignment(
                        "dormitory_1",
                        "DORMITORY",
                        "DORMITORY",
                        None,
                        [optimizer._dorm_candidate("参考锚点A").skill],
                        0,
                    )
                ],
            ),
        ]

        improved = optimizer._improve_dormitory_anchor_coverage(shifts)
        visible = {
            skill.operator_name
            for shift in improved
            for dormitory in shift.dormitories
            for skill in dormitory.operators
        }

        self.assertEqual(visible, {"参考锚点A", "参考锚点B", "参考锚点C"})

    def test_generates_conflict_free_schedule_and_upgrades(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_minimal_data(data_dir)
            game_data = GameData.load(data_dir)

            roster = [
                RosterOperator("能天使", True, 6, 1, 0),
                RosterOperator("德克萨斯", True, 5, 1, 2),
                RosterOperator("空", True, 5, 1, 0),
                RosterOperator("史都华德", True, 3, 30, 0),
                RosterOperator("夜刀", True, 2, 30, 0),
                RosterOperator("Lancet-2", True, 1, 30, 0),
                RosterOperator("Castle-3", True, 1, 30, 0),
                RosterOperator("巡林者", True, 2, 30, 0),
                RosterOperator("杜林", True, 2, 30, 0),
                RosterOperator("安德切尔", True, 3, 30, 0),
                RosterOperator("米格鲁", True, 3, 30, 0),
                RosterOperator("克洛丝", True, 3, 30, 0),
                RosterOperator("芬", True, 3, 30, 0),
                RosterOperator("翎羽", True, 3, 30, 0),
                RosterOperator("香草", True, 3, 30, 0),
                RosterOperator("玫兰莎", True, 3, 30, 0),
            ]
            optimizer = ScheduleOptimizer(game_data, roster, allow_upgrades=True, upgrade_cost_weight=0)
            result = optimizer.optimize(parse_layout("333"), shift_count=2)

        self.assertFalse(find_conflicts(result))
        self.assertEqual(len(result.shifts), 2)
        selected = [
            skill
            for shift in result.shifts
            for room in shift.rooms
            for skill in room.operators
        ]
        self.assertTrue(any(skill.operator_name == "能天使" for skill in selected))

    def test_local_replacements_run_for_balanced_orundum_mode(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_minimal_data(data_dir)
            game_data = GameData.load(data_dir)

            roster = [
                RosterOperator("能天使", True, 6, 1, 0),
                RosterOperator("德克萨斯", True, 5, 1, 2),
                RosterOperator("空", True, 5, 1, 0),
                RosterOperator("史都华德", True, 3, 30, 0),
                RosterOperator("夜刀", True, 2, 30, 0),
                RosterOperator("Lancet-2", True, 1, 30, 0),
                RosterOperator("Castle-3", True, 1, 30, 0),
                RosterOperator("巡林者", True, 2, 30, 0),
                RosterOperator("杜林", True, 2, 30, 0),
                RosterOperator("安德切尔", True, 3, 30, 0),
                RosterOperator("米格鲁", True, 3, 30, 0),
                RosterOperator("克洛丝", True, 3, 30, 0),
                RosterOperator("芬", True, 3, 30, 0),
                RosterOperator("翎羽", True, 3, 30, 0),
                RosterOperator("香草", True, 3, 30, 0),
                RosterOperator("玫兰莎", True, 3, 30, 0),
            ]
            optimizer = ScheduleOptimizer(game_data, roster, allow_upgrades=True, upgrade_cost_weight=0)
            seen_modes: list[str] = []

            def fake_local_replacements(
                layout,
                shifts,
                simulator,
                *,
                mode: str,
                current_report: ProductionReport,
            ):
                del layout, simulator
                seen_modes.append(mode)
                return shifts, {
                    "source": "test_probe",
                    "acceptedCount": 0,
                    "remainingPositiveCount": 0,
                    "positiveNeighborhoods": [],
                    "objectiveConflictAudit": {
                        "lmdPositiveRejectedCount": 0,
                        "lmdPositiveRejected": [],
                    },
                }, current_report

            optimizer._improve_shifts_with_local_replacements = fake_local_replacements  # type: ignore[method-assign]

            result = optimizer.optimize(parse_layout("333"), mode="balanced-orundum", shift_count=2)

        self.assertEqual(seen_modes, ["balanced_orundum"])
        self.assertEqual(
            result.diagnostic_insertion_search["localOptimalityAudit"]["source"],
            "test_probe",
        )

    def test_optimizer_supports_mixed_shift_durations(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_minimal_data(data_dir)
            game_data = GameData.load(data_dir)

            roster = [
                RosterOperator("能天使", True, 6, 1, 0),
                RosterOperator("德克萨斯", True, 5, 1, 2),
                RosterOperator("空", True, 5, 1, 0),
                RosterOperator("史都华德", True, 3, 30, 0),
                RosterOperator("夜刀", True, 2, 30, 0),
                RosterOperator("Lancet-2", True, 1, 30, 0),
                RosterOperator("Castle-3", True, 1, 30, 0),
                RosterOperator("巡林者", True, 2, 30, 0),
                RosterOperator("杜林", True, 2, 30, 0),
                RosterOperator("安德切尔", True, 3, 30, 0),
                RosterOperator("米格鲁", True, 3, 30, 0),
                RosterOperator("克洛丝", True, 3, 30, 0),
                RosterOperator("芬", True, 3, 30, 0),
                RosterOperator("翎羽", True, 3, 30, 0),
                RosterOperator("香草", True, 3, 30, 0),
                RosterOperator("玫兰莎", True, 3, 30, 0),
            ]
            optimizer = ScheduleOptimizer(game_data, roster, allow_upgrades=True, upgrade_cost_weight=0)
            result = optimizer.optimize(
                parse_layout("333"),
                shift_count=3,
                shift_hours=8,
                shift_durations=[12, 6, 6],
            )

        self.assertEqual([shift.duration_hours for shift in result.shifts], [12.0, 6.0, 6.0])
        self.assertEqual([shift.start for shift in result.shifts], ["08:00", "20:00", "02:00"])
        self.assertFalse(find_conflicts(result))

    def test_optimizer_supports_single_daily_shift(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_minimal_data(data_dir)
            game_data = GameData.load(data_dir)

            roster = [
                RosterOperator("能天使", True, 6, 1, 0),
                RosterOperator("德克萨斯", True, 5, 1, 2),
                RosterOperator("空", True, 5, 1, 0),
                RosterOperator("史都华德", True, 3, 30, 0),
                RosterOperator("夜刀", True, 2, 30, 0),
                RosterOperator("Lancet-2", True, 1, 30, 0),
                RosterOperator("Castle-3", True, 1, 30, 0),
                RosterOperator("巡林者", True, 2, 30, 0),
                RosterOperator("杜林", True, 2, 30, 0),
                RosterOperator("安德切尔", True, 3, 30, 0),
                RosterOperator("米格鲁", True, 3, 30, 0),
                RosterOperator("克洛丝", True, 3, 30, 0),
                RosterOperator("芬", True, 3, 30, 0),
                RosterOperator("翎羽", True, 3, 30, 0),
                RosterOperator("香草", True, 3, 30, 0),
                RosterOperator("玫兰莎", True, 3, 30, 0),
            ]
            optimizer = ScheduleOptimizer(game_data, roster, allow_upgrades=True, upgrade_cost_weight=0)
            result = optimizer.optimize(parse_layout("333"), shift_count=1, shift_hours=24)

        self.assertEqual(len(result.shifts), 1)
        self.assertEqual(result.shifts[0].duration_hours, 24.0)
        self.assertEqual(result.shifts[0].start, "08:00")
        self.assertFalse(find_conflicts(result))

    def test_trade_room_selection_uses_combo_true_production(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_minimal_data(data_dir)
            add_trade_combo_data(data_dir)
            game_data = GameData.load(data_dir)

            roster = [
                RosterOperator("普通贸易甲", True, 5, 1, 0),
                RosterOperator("普通贸易乙", True, 5, 1, 0),
                RosterOperator("普通贸易丙", True, 5, 1, 0),
                RosterOperator("巫恋", True, 5, 1, 0),
                RosterOperator("龙舌兰", True, 5, 1, 0),
                RosterOperator("但书", True, 5, 1, 0),
            ]
            optimizer = ScheduleOptimizer(game_data, roster)
            assignment = optimizer._assign_room(
                spec=RoomSpec("trading_1", "TRADING", "O_GOLD", 3, 3, 3),
                excluded=set(),
                duration_hours=24,
            )

        selected_names = {skill.operator_name for skill in assignment.operators}
        self.assertIn("但书", selected_names)
        self.assertNotEqual(
            selected_names,
            {"普通贸易甲", "普通贸易乙", "普通贸易丙"},
        )

    def test_trade_combo_search_counts_same_room_effective_speed(self) -> None:
        skills = [
            optimizer_skill("能天使", "TRADING", "exu_trade", 40, description="进驻贸易站时，订单获取效率+40%"),
            optimizer_skill("德克萨斯", "TRADING", "texas_trade", 35, description="进驻贸易站时，订单获取效率+35%"),
            optimizer_skill("普通贸易", "TRADING", "backup_trade", 34, description="进驻贸易站时，订单获取效率+34%"),
            optimizer_skill(
                "蕾缪安",
                "TRADING",
                "lemuen_trade",
                20,
                description="进驻贸易站时，订单获取效率+20%；当与能天使在同一个贸易站时，订单获取效率额外+25%",
            ),
        ]
        optimizer = optimizer_with_skills(skills)

        assignment = optimizer._assign_room(
            spec=RoomSpec("trading_1", "TRADING", "O_GOLD", 3, 3, 3),
            excluded=set(),
            duration_hours=12,
        )

        selected_names = {skill.operator_name for skill in assignment.operators}
        self.assertEqual(selected_names, {"能天使", "德克萨斯", "蕾缪安"})

    def test_insertion_refills_forced_room_with_best_remaining_partner(self) -> None:
        skills = [
            optimizer_skill("能天使", "TRADING", "exu_trade", 40),
            optimizer_skill("德克萨斯", "TRADING", "texas_trade", 1),
            optimizer_skill("夜刀", "TRADING", "yato_trade", 50),
            optimizer_skill(
                "蕾缪安",
                "TRADING",
                "lemuen_trade",
                20,
                description="进驻贸易站时，订单获取效率+20%；当与能天使在同一个贸易站时，订单获取效率额外+25%",
            ),
        ]
        optimizer = optimizer_with_skills(skills)
        shift = shift_with_rooms(
            "A",
            [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[1], skills[0], skills[2]])],
        )
        group = ShiftInsertionGroup(
            "lemuen_exu",
            (
                ShiftInsertionSpec("TRADING", "O_GOLD", "蕾缪安"),
                ShiftInsertionSpec("TRADING", "O_GOLD", "能天使"),
            ),
        )

        candidate = optimizer._insert_group_into_shift(parse_layout("243"), [shift], 0, group)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        selected_names = {skill.operator_name for skill in candidate[0].rooms[0].operators}
        self.assertEqual(selected_names, {"蕾缪安", "能天使", "夜刀"})

    def test_faction_tags_are_loaded_from_character_table(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_minimal_data(data_dir)
            add_trade_combo_data(data_dir)
            game_data = GameData.load(data_dir)

            skills = game_data.skills_for_roster_operator(
                RosterOperator("摩根", True, 5, 1, 0)
            )

        self.assertTrue(skills)
        self.assertIn("glasgow", skills[0].faction_tags)

    def test_faction_tags_include_main_and_sub_power_sources(self) -> None:
        game_data = GameData(
            {"rooms": {}},
            {
                "char_multi": {
                    "name": "Multi",
                    "nationId": "lungmen",
                    "groupId": "penguin",
                    "teamId": None,
                    "mainPower": {
                        "nationId": "lungmen",
                        "groupId": "penguin",
                        "teamId": None,
                    },
                    "subPower": [
                        {"nationId": "siracusa", "groupId": None, "teamId": None},
                        {"nationId": "kazimierz", "groupId": None, "teamId": None},
                    ],
                }
            },
            {},
        )

        tags = game_data.faction_tags_for_char("char_multi")

        self.assertEqual(len(tags), len(set(tags)))
        self.assertEqual(tags, tuple(sorted(tags)))
        self.assertIn("penguin", tags)
        self.assertIn("groupId:penguin", tags)
        self.assertIn("siracusa", tags)
        self.assertIn("nationId:siracusa", tags)
        self.assertIn("kazimierz", tags)
        self.assertIn("knight", tags)

    def test_special_faction_tag_patches_still_apply(self) -> None:
        game_data = GameData(
            {"rooms": {}},
            {
                "char_1036_fang2": {"name": "Fang2"},
                "char_1048_orchd2": {"name": "Orchid2"},
                "char_4215_buddy": {"name": "Buddy"},
            },
            {},
        )

        self.assertIn("A1", game_data.faction_tags_for_char("char_1036_fang2"))
        self.assertIn("mh2", game_data.faction_tags_for_char("char_1048_orchd2"))
        self.assertIn("mh2", game_data.faction_tags_for_char("char_4215_buddy"))

    def test_sub_power_faction_tags_are_consumed_by_optimizer_partners(self) -> None:
        game_data = GameData(
            {
                "rooms": {},
                "buffs": {
                    "partner_trade": {
                        "buffId": "partner_trade",
                        "buffName": "partner_trade",
                        "roomType": "TRADING",
                        "description": "Trading post speed +10%",
                        "efficiency": 10,
                        "targets": [],
                    }
                },
                "chars": {
                    "char_partner": {
                        "charId": "char_partner",
                        "buffChar": [
                            {
                                "buffData": [
                                    {
                                        "buffId": "partner_trade",
                                        "cond": {"phase": "PHASE_0", "level": 1},
                                    }
                                ]
                            }
                        ],
                    }
                },
            },
            {
                "char_partner": {
                    "name": "Partner",
                    "rarity": "TIER_5",
                    "nationId": "lungmen",
                    "mainPower": {
                        "nationId": "lungmen",
                        "groupId": "penguin",
                        "teamId": None,
                    },
                    "subPower": [
                        {"nationId": "siracusa", "groupId": None, "teamId": None},
                    ],
                    "phases": [{"maxLevel": 50, "evolveCost": None}],
                }
            },
            {},
        )
        partner_skills = game_data.skills_for_roster_operator(
            RosterOperator("Partner", True, 5, 1, 0)
        )
        optimizer = optimizer_with_skills(
            [optimizer_skill("Trigger", "TRADING", "trigger_trade", 1), *partner_skills]
        )

        partners = optimizer._faction_partners(
            "siracusa",
            exclude="Trigger",
            room_type="TRADING",
            target="O_GOLD",
        )

        self.assertTrue(partner_skills)
        self.assertIn("siracusa", partner_skills[0].faction_tags)
        self.assertEqual(partners, ["Partner"])

    def test_combo_pool_keeps_glasgow_partner_closure(self) -> None:
        skills = [
            optimizer_skill(f"HighTrade{index}", "TRADING", f"high_trade_{index}", 100 - index)
            for index in range(40)
        ]
        skills.extend(
            [
                optimizer_skill(
                    "Vina",
                    "TRADING",
                    "vina_glasgow_policy",
                    5,
                    description="Trading skill gains extra value with a Glasgow partner.",
                ),
                optimizer_skill(
                    "Morgan",
                    "TRADING",
                    "morgan_trade",
                    4,
                    faction_tags=("glasgow",),
                ),
            ]
        )
        optimizer = optimizer_with_skills(skills)

        pool = optimizer._combo_candidate_pool("TRADING", "O_GOLD", set())

        names = {candidate.skill.operator_name for candidate in pool}
        self.assertIn("Vina", names)
        self.assertIn("Morgan", names)

    def test_combo_pool_keeps_special_trade_closure(self) -> None:
        skills = [
            optimizer_skill(f"HighTrade{index}", "TRADING", f"high_trade_{index}", 100 - index)
            for index in range(40)
        ]
        skills.extend(
            [
                optimizer_skill("Shamare", "TRADING", "shamare_whisper", 0),
                optimizer_skill("Tequila", "TRADING", "tequila_special", 0),
                optimizer_skill("Butushu", "TRADING", "butushu_claim", 0),
            ]
        )
        optimizer = optimizer_with_skills(skills)

        pool = optimizer._combo_candidate_pool("TRADING", "O_GOLD", set())

        names = {candidate.skill.operator_name for candidate in pool}
        self.assertIn("Shamare", names)
        self.assertIn("Tequila", names)
        self.assertIn("Butushu", names)

    def test_combo_pool_keeps_guide_special_trade_anchor_without_parsed_skill(self) -> None:
        skills = [
            optimizer_skill(f"HighTrade{index}", "TRADING", f"high_trade_{index}", 100 - index)
            for index in range(40)
        ]
        roster = [
            *(RosterOperator(skill.operator_name, True, 5, 1, 0) for skill in skills),
            RosterOperator("可露希尔", True, 5, 1, 0),
        ]
        optimizer = ScheduleOptimizer(GameData({"rooms": {}}, {}, {}), roster)
        optimizer.skills_by_operator = {
            skill.operator_name: [skill]
            for skill in skills
        }

        pool = optimizer._combo_candidate_pool("TRADING", "O_GOLD", set())

        names = {candidate.skill.operator_name for candidate in pool}
        self.assertIn("可露希尔", names)
        croissant = next(candidate.skill for candidate in pool if candidate.skill.operator_name == "可露希尔")
        self.assertIn("guide_special_trade_anchor", croissant.buff_id)

    def test_trade_room_selection_explores_guide_special_anchor_with_any_partners(self) -> None:
        skills = [
            optimizer_skill("TradeA", "TRADING", "trade_a", 40),
            optimizer_skill("TradeB", "TRADING", "trade_b", 30),
            optimizer_skill("TradeC", "TRADING", "trade_c", 20),
        ]
        roster = [
            *(RosterOperator(skill.operator_name, True, 5, 1, 0) for skill in skills),
            RosterOperator("可露希尔", True, 5, 1, 0),
        ]
        optimizer = ScheduleOptimizer(GameData({"rooms": {}}, {}, {}), roster)
        optimizer.skills_by_operator = {
            skill.operator_name: [skill]
            for skill in skills
        }

        assignment = optimizer._assign_room(
            spec=RoomSpec("trading_1", "TRADING", "O_GOLD", 3, 3, 3),
            excluded=set(),
            duration_hours=24,
        )

        selected_names = {skill.operator_name for skill in assignment.operators}
        self.assertIn("可露希尔", selected_names)
        self.assertTrue({"TradeA", "TradeB"}.issubset(selected_names))

    def test_joint_room_assignment_avoids_room_order_greedy_trap(self) -> None:
        shared = optimizer_skill("SharedBest", "TRADING", "shared", 100)
        first_alt = optimizer_skill("FirstAlt", "TRADING", "first_alt", 60)
        second_filler = optimizer_skill("SecondFiller", "TRADING", "second_filler", 10)
        optimizer = optimizer_with_skills([shared, first_alt, second_filler])
        first_spec = RoomSpec("trading_1", "TRADING", "O_GOLD", 1, 3, 1)
        second_spec = RoomSpec("trading_2", "TRADING", "O_GOLD", 1, 1, 1)

        def fake_room_combos(
            spec: RoomSpec,
            excluded: set[str],
            duration_hours: float,
        ) -> list[RoomCombo]:
            del excluded, duration_hours
            if spec.room_id == "trading_1":
                return [
                    RoomCombo((shared,), 100.0),
                    RoomCombo((first_alt,), 60.0),
                ]
            return [
                RoomCombo((shared,), 100.0),
                RoomCombo((second_filler,), 10.0),
            ]

        optimizer._room_combos = fake_room_combos  # type: ignore[method-assign]

        rooms = optimizer._joint_production_room_assignments(
            [first_spec, second_spec],
            set(),
            duration_hours=12,
        )

        self.assertEqual(
            [skill.operator_name for skill in rooms["trading_1"].operators],
            ["FirstAlt"],
        )
        self.assertEqual(
            [skill.operator_name for skill in rooms["trading_2"].operators],
            ["SharedBest"],
        )

    def test_joint_room_assignment_keeps_deeper_disjoint_alternatives(self) -> None:
        hub = optimizer_skill("Hub", "TRADING", "hub", 100)
        low = optimizer_skill("LowFallback", "TRADING", "low", 1)
        fillers = [
            optimizer_skill(f"Filler{index}", "TRADING", f"filler_{index}", 50 - index)
            for index in range(20)
        ]
        alt_a = optimizer_skill("AltA", "TRADING", "alt_a", 35)
        alt_b = optimizer_skill("AltB", "TRADING", "alt_b", 34)
        optimizer = optimizer_with_skills([hub, low, *fillers, alt_a, alt_b])
        first_spec = RoomSpec("trading_1", "TRADING", "O_GOLD", 2, 3, 2)
        second_spec = RoomSpec("trading_2", "TRADING", "O_GOLD", 1, 3, 1)

        def fake_room_combos(
            spec: RoomSpec,
            excluded: set[str],
            duration_hours: float,
        ) -> list[RoomCombo]:
            del excluded, duration_hours
            if spec.room_id == "trading_1":
                hub_heavy = [
                    RoomCombo((hub, filler), 100.0 - index)
                    for index, filler in enumerate(fillers)
                ]
                return [*hub_heavy, RoomCombo((alt_a, alt_b), 70.0)]
            return [
                RoomCombo((hub,), 100.0),
                RoomCombo((low,), 1.0),
            ]

        optimizer._room_combos = fake_room_combos  # type: ignore[method-assign]

        rooms = optimizer._joint_production_room_assignments(
            [first_spec, second_spec],
            set(),
            duration_hours=12,
        )

        self.assertEqual(
            [skill.operator_name for skill in rooms["trading_1"].operators],
            ["AltA", "AltB"],
        )
        self.assertEqual(
            [skill.operator_name for skill in rooms["trading_2"].operators],
            ["Hub"],
        )

    def test_combo_pool_keeps_shard_formula_specialists(self) -> None:
        skills = [
            optimizer_skill(f"HighFactory{index}", "MANUFACTURE", f"high_factory_{index}", 100 - index)
            for index in range(40)
        ]
        skills.append(
            optimizer_skill(
                "Eyja",
                "MANUFACTURE",
                "eyja_shard",
                35,
                targets=("F_DIAMOND",),
            )
        )
        optimizer = optimizer_with_skills(skills)

        pool = optimizer._combo_candidate_pool("MANUFACTURE", "F_DIAMOND", set())

        names = {candidate.skill.operator_name for candidate in pool}
        self.assertIn("Eyja", names)

    def test_combo_pool_keeps_low_static_conditional_variants_and_partners(self) -> None:
        skills = [
            optimizer_skill(f"FastTrade{index}", "TRADING", f"fast_trade_{index}", 100 - index)
            for index in range(35)
        ]
        exusiai = optimizer_skill("能天使", "TRADING", "exusiai_trade", 35)
        lemuen_alpha = optimizer_skill("蕾缪安", "TRADING", "trade_ord_spd&multiPar[000]", 20)
        lemuen_beta = optimizer_skill(
            "蕾缪安",
            "TRADING",
            "trade_ord_spd&multiPar[100]",
            13,
            description="当与能天使在同一个贸易站时，订单获取效率额外提升。",
        )
        optimizer = optimizer_with_skills([*skills, exusiai, lemuen_alpha, lemuen_beta])

        pool = optimizer._combo_candidate_pool("TRADING", "O_GOLD", set())

        variants = {(candidate.skill.operator_name, candidate.skill.buff_id) for candidate in pool}
        self.assertIn(("蕾缪安", "trade_ord_spd&multiPar[100]"), variants)
        self.assertIn(("能天使", "exusiai_trade"), variants)

    def test_combo_pool_keeps_guide_manufacture_anchors(self) -> None:
        skills = [
            optimizer_skill(f"HighFactory{index}", "MANUFACTURE", f"high_factory_{index}", 120 - index)
            for index in range(40)
        ]
        skills.extend(
            [
                optimizer_skill("Miss.Christine", "MANUFACTURE", "exp_a", 30),
                optimizer_skill("食铁兽", "MANUFACTURE", "exp_b", 35),
                optimizer_skill("弑君者", "MANUFACTURE", "exp_c", 35),
            ]
        )
        optimizer = optimizer_with_skills(skills)

        pool = optimizer._combo_candidate_pool("MANUFACTURE", "F_EXP", set())

        names = {candidate.skill.operator_name for candidate in pool}
        self.assertIn("Miss.Christine", names)
        self.assertIn("食铁兽", names)
        self.assertIn("弑君者", names)

    def test_combo_pool_keeps_deeper_protected_candidates_with_small_budget(self) -> None:
        high_unprotected = [
            optimizer_skill(f"HighFactory{index}", "MANUFACTURE", f"high_factory_{index}", 200 - index)
            for index in range(14)
        ]
        protected = [
            optimizer_skill(
                f"ProtectedFactory{index}",
                "MANUFACTURE",
                f"protected_factory_{index}",
                100 - index,
                description="当与其他协同干员在同一个制造站时，生产力额外提升。",
            )
            for index in range(18)
        ]
        optimizer = optimizer_with_skills([*high_unprotected, *protected])

        pool = optimizer._combo_candidate_pool("MANUFACTURE", "F_GOLD", set())

        names = {candidate.skill.operator_name for candidate in pool}
        self.assertLessEqual(len(pool), COMBO_CANDIDATE_POOL_LIMIT)
        self.assertIn("ProtectedFactory17", names)

    def test_combo_pool_prioritizes_best_variant_for_protected_operator(self) -> None:
        high_unprotected = [
            optimizer_skill(f"HighFactory{index}", "MANUFACTURE", f"high_factory_{index}", 200 - index)
            for index in range(14)
        ]
        protected_fillers = [
            optimizer_skill(
                f"ProtectedFactory{index}",
                "MANUFACTURE",
                f"protected_factory_{index}",
                100 - index,
                description="当与其他协同干员在同一个制造站时，生产力额外提升。",
            )
            for index in range(17)
        ]
        optimizer = optimizer_with_skills(
            [
                *high_unprotected,
                *protected_fillers,
                optimizer_skill("VariantFactory", "MANUFACTURE", "variant_best", 19),
                optimizer_skill(
                    "VariantFactory",
                    "MANUFACTURE",
                    "variant_combo_seed",
                    1,
                    description="进驻制造站时，仓库容量额外提升。",
                ),
            ]
        )

        pool = optimizer._combo_candidate_pool("MANUFACTURE", "F_GOLD", set())

        variants = {(candidate.skill.operator_name, candidate.skill.buff_id) for candidate in pool}
        self.assertLessEqual(len(pool), COMBO_CANDIDATE_POOL_LIMIT)
        self.assertIn(("VariantFactory", "variant_best"), variants)

    def test_manufacture_room_selection_uses_guide_lookup_score(self) -> None:
        skills = [
            optimizer_skill("FactoryA", "MANUFACTURE", "factory_a", 40),
            optimizer_skill("FactoryB", "MANUFACTURE", "factory_b", 40),
            optimizer_skill("FactoryC", "MANUFACTURE", "factory_c", 40),
            optimizer_skill("Miss.Christine", "MANUFACTURE", "exp_a", 30),
            optimizer_skill("食铁兽", "MANUFACTURE", "exp_b", 35),
            optimizer_skill("弑君者", "MANUFACTURE", "exp_c", 35),
        ]
        optimizer = optimizer_with_skills(skills)

        assignment = optimizer._assign_room(
            spec=RoomSpec("manufacture_1", "MANUFACTURE", "F_EXP", 3, 3, 3),
            excluded=set(),
            duration_hours=12,
        )

        selected_names = {skill.operator_name for skill in assignment.operators}
        self.assertEqual(selected_names, {"Miss.Christine", "食铁兽", "弑君者"})

    def test_shift_insertion_groups_are_derived_from_named_dependency_text(self) -> None:
        skills = [
            optimizer_skill(
                "PartnerMaker",
                "MANUFACTURE",
                "partner_maker",
                0,
                targets=("F_EXP",),
                description="进驻制造站时，若TradeBuddy在贸易站，则作战记录类配方的生产力+35%",
            ),
            optimizer_skill("TradeBuddy", "TRADING", "trade_buddy", 20),
        ]
        optimizer = optimizer_with_skills(skills)

        groups = optimizer._shift_insertion_groups()

        self.assertTrue(
            any(
                group_has_specs(
                    group,
                    ("MANUFACTURE", "F_EXP", "PartnerMaker"),
                    ("TRADING", "O_GOLD", "TradeBuddy"),
                )
                for group in groups
            )
        )

    def test_base_wide_dependency_is_not_forced_into_room_from_context_window(self) -> None:
        skills = [
            optimizer_skill(
                "TradeTrigger",
                "TRADING",
                "trade_trigger",
                0,
                description="进驻贸易站时，当伺夜在基建内时，订单获取效率+35%",
            ),
            optimizer_skill("伺夜", "TRADING", "vigil_trade", 20),
        ]
        optimizer = optimizer_with_skills(skills)

        groups = optimizer._shift_insertion_groups()

        self.assertFalse(
            any(
                group_has_specs(
                    group,
                    ("TRADING", "O_GOLD", "TradeTrigger"),
                    ("TRADING", "O_GOLD", "伺夜"),
                )
                for group in groups
            )
        )

    def test_shift_insertion_group_places_same_room_dependency_together(self) -> None:
        skills = [
            optimizer_skill("HighTradeA", "TRADING", "high_trade_a", 80),
            optimizer_skill("HighTradeB", "TRADING", "high_trade_b", 70),
            optimizer_skill("HighTradeC", "TRADING", "high_trade_c", 60),
            optimizer_skill(
                "Vina",
                "TRADING",
                "vina_partner",
                0,
                description="当与Morgan在同一个贸易站时，订单获取效率+90%",
            ),
            optimizer_skill("Morgan", "TRADING", "morgan_trade", 0),
        ]
        optimizer = optimizer_with_skills(skills)
        group = next(
            group
            for group in optimizer._shift_insertion_groups()
            if group_has_specs(group, ("TRADING", "O_GOLD", "Vina"), ("TRADING", "O_GOLD", "Morgan"))
        )
        shifts = [
            shift_with_rooms(
                "A",
                [
                    make_test_room(
                        "trading_1",
                        "TRADING",
                        "O_GOLD",
                        [
                            skills[0],
                            skills[1],
                            skills[2],
                        ],
                    )
                ],
            ),
            shift_with_rooms("B", [make_test_room("trading_1", "TRADING", "O_GOLD", [])]),
        ]

        candidate = optimizer._insert_group_into_shift(parse_layout("243"), shifts, 0, group)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        target_room = candidate[0].rooms[0]
        self.assertTrue(room_contains(target_room, "TRADING", "O_GOLD", "Vina"))
        self.assertTrue(room_contains(target_room, "TRADING", "O_GOLD", "Morgan"))

    def test_shift_insertion_does_not_overfill_single_slot_office(self) -> None:
        from arknights_schedule_generator.models import RoomAssignment

        skills = [
            optimizer_skill("OfficeForced", "HIRE", "office_forced", 40),
            optimizer_skill("OfficeExisting", "HIRE", "office_existing", 45),
        ]
        optimizer = optimizer_with_skills(skills)
        group = ShiftInsertionGroup(
            "office_dependency",
            (ShiftInsertionSpec("HIRE", None, "OfficeForced"),),
        )
        shifts = [
            shift_with_rooms(
                "A",
                [
                    RoomAssignment(
                        "hire_1",
                        "HIRE",
                        "HIRE",
                        None,
                        [skills[1]],
                        45,
                        room_level=3,
                    )
                ],
            ),
            shift_with_rooms("B", []),
        ]

        candidate = optimizer._insert_group_into_shift(parse_layout("243"), shifts, 0, group)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        office = candidate[0].rooms[0]
        self.assertEqual([skill.operator_name for skill in office.operators], ["OfficeForced"])

    def test_office_capacity_is_hard_limited_to_one(self) -> None:
        from arknights_schedule_generator.models import RoomAssignment

        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_minimal_data(data_dir)
            game_data = GameData.load(data_dir)
            optimizer = ScheduleOptimizer(game_data, [])

            specs = optimizer._room_specs(parse_layout("333"), "normal")

        office_spec = next(spec for spec in specs if spec.room_type == "HIRE")
        self.assertEqual(office_spec.capacity, 1)
        self.assertEqual(
            optimizer._assignment_capacity(
                RoomAssignment(
                    "hire_1",
                    "HIRE",
                    "HIRE",
                    None,
                    [],
                    0,
                    room_level=3,
                    slots=2,
                )
            ),
            1,
        )

    def test_control_insertion_allows_no_skill_partner(self) -> None:
        skills = [
            optimizer_skill("ControlAnchor", "CONTROL", "control_anchor", 10),
        ]
        roster = [
            RosterOperator("ControlAnchor", True, 5, 1, 0),
            RosterOperator("NoSkillPartner", True, 5, 1, 0),
        ]
        optimizer = ScheduleOptimizer(GameData({"rooms": {}}, {}, {}), roster)
        optimizer.skills_by_operator = {"ControlAnchor": skills}

        candidate = optimizer._skill_for_insertion(
            ShiftInsertionSpec("CONTROL", None, "NoSkillPartner")
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.skill.operator_name, "NoSkillPartner")
        self.assertEqual(candidate.skill.room_type, "CONTROL")
        self.assertEqual(candidate.score, 0.0)

    def test_cost_filtered_dependency_group_is_reported_as_unavailable(self) -> None:
        expensive_upgrade = UpgradeRequirement(
            char_id="char_Trigger",
            name="Trigger",
            from_elite=0,
            from_level=1,
            to_elite=2,
            to_level=1,
            cost_score=1000.0,
            materials=[],
            note="test",
        )
        skills = [
            optimizer_skill(
                "Trigger",
                "MANUFACTURE",
                "trigger_pair",
                10,
                targets=("F_EXP",),
                description="进驻制造站时，若Partner在贸易站，则作战记录类配方的生产力+35%",
                upgrade=expensive_upgrade,
            ),
            optimizer_skill("Partner", "TRADING", "partner_trade", 30),
        ]
        optimizer = optimizer_with_skills(skills)
        optimizer.upgrade_cost_weight = 1.0
        groups = optimizer._diagnostic_shift_insertion_groups()
        available = optimizer._available_shift_insertion_groups(groups)

        records = optimizer._unavailable_shift_insertion_records(groups, available)

        self.assertTrue(
            any(
                item["groupId"] == "diagnostic_named_dependency:Trigger:Partner"
                and item["status"] == "unavailable_required_skill"
                and item["missingSpecs"][0]["operator"] == "Trigger"
                for item in records
            )
        )

    def test_available_dependency_group_over_limit_is_reported_as_not_searched(self) -> None:
        skills: list[BaseSkill] = []
        for index in range(110):
            skills.append(
                optimizer_skill(
                    f"Trigger{index}",
                    "TRADING",
                    f"trigger_{index}",
                    index + 1,
                    description=f"当与Partner{index}在同一个贸易站时，订单获取效率额外+35%",
                )
            )
            skills.append(optimizer_skill(f"Partner{index}", "TRADING", f"partner_{index}", 1))
        optimizer = optimizer_with_skills(skills)
        groups = optimizer._diagnostic_shift_insertion_groups()
        available = optimizer._available_shift_insertion_groups(groups)

        records = optimizer._group_limit_insertion_records(groups, available)

        self.assertEqual(len(available), 64)
        self.assertTrue(records)
        self.assertTrue(
            all(item["status"] == "not_searched_group_limit" for item in records)
        )

    def test_same_room_group_is_not_satisfied_when_split_across_rooms(self) -> None:
        skills = [
            optimizer_skill("Vina", "TRADING", "vina_partner", 0),
            optimizer_skill("Morgan", "TRADING", "morgan_trade", 0),
        ]
        optimizer = optimizer_with_skills(skills)
        group = ShiftInsertionGroup(
            "same_room",
            (
                ShiftInsertionSpec("TRADING", "O_GOLD", "Vina"),
                ShiftInsertionSpec("TRADING", "O_GOLD", "Morgan"),
            ),
        )
        shift = shift_with_rooms(
            "A",
            [
                make_test_room("trading_1", "TRADING", "O_GOLD", [skills[0]]),
                make_test_room("trading_2", "TRADING", "O_GOLD", [skills[1]]),
            ],
        )

        self.assertFalse(optimizer._insertion_group_satisfied(shift, group))

    def test_shift_insertion_rejects_neighbor_boundary_conflict(self) -> None:
        skills = [
            optimizer_skill("Vina", "TRADING", "vina_partner", 0),
            optimizer_skill("Morgan", "TRADING", "morgan_trade", 0),
            optimizer_skill("Backup", "TRADING", "backup_trade", 10),
        ]
        optimizer = optimizer_with_skills(skills)
        group = ShiftInsertionGroup(
            "same_room",
            (
                ShiftInsertionSpec("TRADING", "O_GOLD", "Vina"),
                ShiftInsertionSpec("TRADING", "O_GOLD", "Morgan"),
            ),
        )
        shifts = [
            shift_with_rooms("A", [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[2]])]),
            shift_with_rooms("B", [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[0]])]),
        ]

        candidate = optimizer._insert_group_into_shift(parse_layout("243"), shifts, 0, group)

        self.assertIsNone(candidate)

    def test_best_insertion_attempt_can_relocate_boundary_blocking_operator(self) -> None:
        skills = [
            optimizer_skill("Vina", "TRADING", "vina_partner", 100),
            optimizer_skill("Morgan", "TRADING", "morgan_trade", 100),
            optimizer_skill("Backup", "TRADING", "backup_trade", 1),
            optimizer_skill("Relief", "TRADING", "relief_trade", 50),
        ]
        optimizer = optimizer_with_skills(skills)
        group = ShiftInsertionGroup(
            "same_room",
            (
                ShiftInsertionSpec("TRADING", "O_GOLD", "Vina"),
                ShiftInsertionSpec("TRADING", "O_GOLD", "Morgan"),
            ),
        )
        shifts = [
            shift_with_rooms("A", [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[2]])]),
            shift_with_rooms("B", [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[0]])]),
        ]
        simulator = ProductionSimulator(optimizer.game_data)
        current_report = simulator.evaluate(shifts)
        current_score = current_report.score

        result, candidate, candidate_report = optimizer._best_insertion_attempt(
            parse_layout("243"),
            shifts,
            simulator,
            group,
            current_score,
            current_report,
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertIsNotNone(candidate_report)
        self.assertIn(result["status"], {"improves", "evaluated_not_improving"})
        self.assertIn("dailyExpectedDelta", result)
        self.assertTrue(result["assignmentChanges"])
        paired_shift_indexes = [
            index
            for index, shift in enumerate(candidate)
            if any(
                room_contains(room, "TRADING", "O_GOLD", "Vina")
                and room_contains(room, "TRADING", "O_GOLD", "Morgan")
                for room in shift.rooms
            )
        ]
        self.assertEqual(len(paired_shift_indexes), 1)
        paired_index = paired_shift_indexes[0]
        for index, shift in enumerate(candidate):
            if index == paired_index:
                continue
            self.assertFalse(
                any(room_contains(room, "TRADING", "O_GOLD", "Vina") for room in shift.rooms)
            )
        self.assertTrue(optimizer._active_boundaries_are_valid(candidate))

    def test_boundary_relocation_refills_both_neighbors_without_reusing_target_names(self) -> None:
        skills = [
            optimizer_skill("Vina", "TRADING", "vina_partner", 100),
            optimizer_skill("Morgan", "TRADING", "morgan_trade", 100),
            optimizer_skill("Backup", "TRADING", "backup_trade", 1),
            optimizer_skill("LeftRelief", "TRADING", "left_relief", 50),
            optimizer_skill("RightRelief", "TRADING", "right_relief", 40),
        ]
        optimizer = optimizer_with_skills(skills)
        group = ShiftInsertionGroup(
            "same_room",
            (
                ShiftInsertionSpec("TRADING", "O_GOLD", "Vina"),
                ShiftInsertionSpec("TRADING", "O_GOLD", "Morgan"),
            ),
        )
        shifts = [
            shift_with_rooms("A", [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[0]])]),
            shift_with_rooms("B", [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[2]])]),
            shift_with_rooms("C", [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[1]])]),
        ]

        candidate = optimizer._insert_group_with_boundary_relocation(
            parse_layout("243"),
            shifts,
            1,
            group,
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertTrue(room_contains(candidate[1].rooms[0], "TRADING", "O_GOLD", "Vina"))
        self.assertTrue(room_contains(candidate[1].rooms[0], "TRADING", "O_GOLD", "Morgan"))
        for index in (0, 2):
            self.assertFalse(
                any(room_contains(room, "TRADING", "O_GOLD", "Vina") for room in candidate[index].rooms)
            )
            self.assertFalse(
                any(room_contains(room, "TRADING", "O_GOLD", "Morgan") for room in candidate[index].rooms)
            )
        self.assertTrue(optimizer._active_boundaries_are_valid(candidate))

    def test_boundary_relocation_refills_underfilled_control_room(self) -> None:
        skills = [
            optimizer_skill("Forced", "CONTROL", "forced_control", 10),
            optimizer_skill("Backup", "CONTROL", "backup_control", 9),
            *[
                optimizer_skill(f"ControlFiller{index}", "CONTROL", f"control_filler_{index}", 8 - index)
                for index in range(18)
            ],
        ]
        optimizer = optimizer_with_skills(skills)
        group = ShiftInsertionGroup(
            "control_dependency",
            (ShiftInsertionSpec("CONTROL", None, "Forced"),),
        )
        shifts = [
            shift_with_rooms(
                "A",
                [make_test_room("control_1", "CONTROL", None, skills[:5], room_level=5, slots=5)],
            ),
            shift_with_rooms(
                "B",
                [make_test_room("control_1", "CONTROL", None, [skills[1]], room_level=5, slots=5)],
            ),
            shift_with_rooms(
                "C",
                [make_test_room("control_1", "CONTROL", None, skills[5:10], room_level=5, slots=5)],
            ),
        ]

        candidate = optimizer._insert_group_with_boundary_relocation(
            parse_layout("243"),
            shifts,
            1,
            group,
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertFalse(room_contains(candidate[0].rooms[0], "CONTROL", None, "Forced"))
        self.assertEqual(len(candidate[0].rooms[0].operators), 5)
        self.assertTrue(room_contains(candidate[1].rooms[0], "CONTROL", None, "Forced"))
        self.assertEqual(len(candidate[1].rooms[0].operators), 5)
        self.assertTrue(optimizer._active_boundaries_are_valid(candidate))

    def test_guide_trade_anchor_insertion_prefers_low_level_lmd_room(self) -> None:
        croissant = optimizer_skill("可露希尔", "TRADING", "croissant", 30)
        high = optimizer_skill("HighTrade", "TRADING", "high_trade", 30)
        low = optimizer_skill("LowTrade", "TRADING", "low_trade", 30)
        optimizer = optimizer_with_skills([croissant, high, low])
        group = ShiftInsertionGroup(
            "diagnostic_guide_trade_anchor:可露希尔",
            (ShiftInsertionSpec("TRADING", "O_GOLD", "可露希尔"),),
        )
        shifts = [
            shift_with_rooms(
                "A",
                [make_test_room("trading_1", "TRADING", "O_DIAMOND", [croissant])],
            ),
            shift_with_rooms(
                "B",
                [
                    make_test_room("trading_1", "TRADING", "O_GOLD", [high], room_level=3),
                    make_test_room(
                        "trading_2",
                        "TRADING",
                        "O_GOLD",
                        [low],
                        room_level=1,
                        slots=1,
                    ),
                ],
            ),
        ]

        candidate = optimizer._insert_group_with_boundary_relocation(
            parse_layout("342"),
            shifts,
            1,
            group,
        )

        self.assertIsNone(candidate)

    def test_room_grouped_insertion_can_force_distinct_lmd_trade_rooms(self) -> None:
        tequila = optimizer_skill("龙舌兰", "TRADING", "tequila_special_order", 30)
        butushu = optimizer_skill("但书", "TRADING", "butushu_claim", 30)
        fast_a = optimizer_skill("FastA", "TRADING", "fast_a", 45)
        fast_b = optimizer_skill("FastB", "TRADING", "fast_b", 45)
        old_a = optimizer_skill("OldA", "TRADING", "old_a", 20)
        old_b = optimizer_skill("OldB", "TRADING", "old_b", 20)
        optimizer = optimizer_with_skills([tequila, butushu, fast_a, fast_b, old_a, old_b])
        group = ShiftInsertionGroup(
            "reference_trade_room_shape:test",
            (
                ShiftInsertionSpec("TRADING", "O_GOLD", "龙舌兰", room_group="tequila_room"),
                ShiftInsertionSpec("TRADING", "O_GOLD", "但书", room_group="butushu_room"),
            ),
        )
        same_room_shift = shift_with_rooms(
            "A",
            [make_test_room("trading_1", "TRADING", "O_GOLD", [tequila, butushu])],
        )
        self.assertFalse(optimizer._insertion_group_satisfied(same_room_shift, group))
        shifts = [
            shift_with_rooms(
                "A",
                [
                    make_test_room("trading_1", "TRADING", "O_GOLD", [old_a]),
                    make_test_room("trading_2", "TRADING", "O_GOLD", [old_b]),
                ],
            )
        ]

        candidate = optimizer._insert_group_into_shift(parse_layout("252"), shifts, 0, group)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        lmd_rooms = [room for room in candidate[0].rooms if room.room_type == "TRADING"]
        self.assertEqual(
            sum(room_contains(room, "TRADING", "O_GOLD", "龙舌兰") for room in lmd_rooms),
            1,
        )
        self.assertEqual(
            sum(room_contains(room, "TRADING", "O_GOLD", "但书") for room in lmd_rooms),
            1,
        )
        self.assertFalse(
            any(
                room_contains(room, "TRADING", "O_GOLD", "龙舌兰")
                and room_contains(room, "TRADING", "O_GOLD", "但书")
                for room in lmd_rooms
            )
        )

    def test_named_dependency_specs_use_shared_parser_for_cross_room(self) -> None:
        skill = optimizer_skill(
            "Leto",
            "MANUFACTURE",
            "leto_exp",
            0,
            targets=("F_EXP",),
            description="进驻制造站时，若Gummy在贸易站，则作战记录类配方的生产力+35%",
        )
        optimizer = optimizer_with_skills(
            [skill, optimizer_skill("Gummy", "TRADING", "gummy_trade", 45)]
        )

        optimizer_specs = optimizer._named_dependency_insertion_specs(skill, "Gummy")
        parser_specs = dependency_parser.force_specs_from_explanation(
            dependency_parser.explain_named_dependency(skill, "Gummy"),
            operator="Leto",
            partner="Gummy",
        )

        self.assertEqual(
            [(spec.room_type, spec.target, spec.operator_name) for spec in optimizer_specs],
            [
                (spec.room_type, spec.target, operator)
                for spec in parser_specs
                for operator in spec.operators
            ],
        )

    def test_named_dependency_specs_use_shared_parser_for_same_room(self) -> None:
        skill = optimizer_skill(
            "Vina",
            "TRADING",
            "vina_partner",
            0,
            description="进驻同一个贸易站时，若Morgan在场，则订单获取效率提升。",
        )
        optimizer = optimizer_with_skills(
            [skill, optimizer_skill("Morgan", "TRADING", "morgan_trade", 45)]
        )

        optimizer_specs = optimizer._named_dependency_insertion_specs(skill, "Morgan")
        parser_specs = dependency_parser.force_specs_from_explanation(
            dependency_parser.explain_named_dependency(skill, "Morgan"),
            operator="Vina",
            partner="Morgan",
        )

        self.assertEqual(
            [(spec.room_type, spec.target, spec.operator_name) for spec in optimizer_specs],
            [
                (spec.room_type, spec.target, operator)
                for spec in parser_specs
                for operator in spec.operators
            ],
        )

    def test_alias_dependency_uses_canonical_roster_operator(self) -> None:
        skills = [
            optimizer_skill(
                "Morgan",
                "TRADING",
                "morgan_vina_alias",
                0,
                description="进驻同一个贸易站时，若Vina Victoria在场，则订单获取效率提升。",
            ),
            optimizer_skill("Vina", "TRADING", "vina_trade", 5),
        ]
        optimizer = optimizer_with_skills(skills)

        groups = optimizer._diagnostic_shift_insertion_groups()

        self.assertTrue(
            any(
                group.id == "diagnostic_named_dependency:Morgan:Vina"
                and group_has_specs(
                    group,
                    ("TRADING", "O_GOLD", "Morgan"),
                    ("TRADING", "O_GOLD", "Vina"),
                )
                for group in groups
            )
        )

    def test_alias_dependency_does_not_match_absent_canonical_operator(self) -> None:
        skills = [
            optimizer_skill(
                "Morgan",
                "TRADING",
                "morgan_vina_alias",
                0,
                description="进驻同一个贸易站时，若Vina Victoria在场，则订单获取效率提升。",
            )
        ]
        optimizer = optimizer_with_skills(skills)

        groups = optimizer._diagnostic_shift_insertion_groups()

        self.assertFalse(any(group.id.startswith("diagnostic_named_dependency:Morgan:Vina") for group in groups))

    def test_cross_room_alias_dependency_matches_direct_name_specs(self) -> None:
        direct = optimizer_skill(
            "Leto",
            "MANUFACTURE",
            "leto_direct",
            0,
            targets=("F_EXP",),
            description="进驻制造站时，若Vina在贸易站，则作战记录类配方的生产力+35%",
        )
        alias = optimizer_skill(
            "Leto",
            "MANUFACTURE",
            "leto_alias",
            0,
            targets=("F_EXP",),
            description="进驻制造站时，若Vina Victoria在贸易站，则作战记录类配方的生产力+35%",
        )
        optimizer = optimizer_with_skills(
            [alias, optimizer_skill("Vina", "TRADING", "vina_trade", 5)]
        )

        self.assertEqual(
            optimizer._named_dependency_insertion_specs(alias, "Vina"),
            optimizer._named_dependency_insertion_specs(direct, "Vina"),
        )

    def test_alias_dependency_preserves_siege_as_canonical_roster_name(self) -> None:
        skills = [
            optimizer_skill(
                "Morgan",
                "TRADING",
                "morgan_siege_alias",
                0,
                description="进驻同一个贸易站时，若Vina Victoria在场，则订单获取效率提升。",
            ),
            optimizer_skill("Siege", "TRADING", "siege_trade", 5),
        ]
        optimizer = optimizer_with_skills(skills)

        groups = optimizer._diagnostic_shift_insertion_groups()

        self.assertTrue(
            any(
                group.id == "diagnostic_named_dependency:Morgan:Siege"
                and group_has_specs(
                    group,
                    ("TRADING", "O_GOLD", "Morgan"),
                    ("TRADING", "O_GOLD", "Siege"),
                )
                for group in groups
            )
        )

    def test_shift_local_search_inserts_leto_gummy_cross_room_pair(self) -> None:
        skills = [
            optimizer_skill(f"Trade{index}", "TRADING", f"trade_{index}", 80 - index)
            for index in range(5)
        ]
        skills.append(optimizer_skill("古米", "TRADING", "gummy_trade", 45))
        skills.extend(
            optimizer_skill(
                f"ExpWorker{index}",
                "MANUFACTURE",
                f"exp_worker_{index}",
                20 - index,
                targets=("F_EXP",),
            )
            for index in range(12)
        )
        skills.append(
            optimizer_skill(
                "烈夏",
                "MANUFACTURE",
                "患难拍档",
                0,
                targets=("F_EXP",),
                description="进驻制造站时，若古米在贸易站，则作战记录类配方的生产力+35%",
            )
        )
        skills.extend(
            optimizer_skill(
                f"GoldWorker{index}",
                "MANUFACTURE",
                f"gold_worker_{index}",
                25 - index,
                targets=("F_GOLD",),
            )
            for index in range(12)
        )
        skills.extend(
            optimizer_skill(f"Power{index}", "POWER", f"power_{index}", 10)
            for index in range(2)
        )
        optimizer = optimizer_with_skills(skills)
        layout = parse_layout("243")
        initial_shifts, _ = optimizer._build_shifts(
            layout,
            optimizer._room_specs(layout, "normal"),
            shift_count=2,
            shift_hours=12,
            shift_times=["08:00", "20:00"],
        )
        initial_score = ProductionSimulator(optimizer.game_data).evaluate(initial_shifts).score

        result = optimizer.optimize(layout, shift_count=2)

        self.assertFalse(find_conflicts(result))
        self.assertGreater(result.score, initial_score)
        self.assertTrue(shift_has_operator(result, "MANUFACTURE", "F_EXP", "烈夏"))
        self.assertTrue(shift_has_operator(result, "TRADING", "O_GOLD", "古米"))
        self.assertTrue(shift_has_pair(result, ("MANUFACTURE", "F_EXP", "烈夏"), ("TRADING", "O_GOLD", "古米")))
        search = result.diagnostic_insertion_search
        self.assertEqual(search["summary"]["acceptedCount"], 1)
        self.assertTrue(
            any(
                item["groupId"] == "diagnostic_named_dependency:烈夏:古米"
                and item["status"] == "accepted"
                and item["finalSatisfied"]
                and item["scoreDelta"] > 0
                for item in search["accepted"]
            )
        )

    def test_shift_local_search_inserts_silverash_pramanix_without_production_swap(self) -> None:
        skills = [
            optimizer_skill(f"Control{index}", "CONTROL", f"control_{index}", 30 - index)
            for index in range(10)
        ]
        skills.append(
            optimizer_skill(
                "凛御银灰",
                "CONTROL",
                "silverash_control",
                0,
                description="进驻控制中枢时，协助喀兰贸易事务",
            )
        )
        skills.append(
            optimizer_skill(
                "圣聆初雪",
                "HIRE",
                "雪境归心",
                35,
                description="进驻办公室时，人脉资源的联络速度+35%，如果凛御银灰进驻在控制中枢，则人脉资源的联络速度额外+10%",
            )
        )
        skills.append(
            optimizer_skill(
                "OfficeBackup",
                "HIRE",
                "office_backup",
                30,
                description="进驻办公室时，人脉资源的联络速度+30%",
            )
        )
        skills.extend(
            optimizer_skill(f"Trade{index}", "TRADING", f"trade_{index}", 50 - index)
            for index in range(12)
        )
        skills.extend(
            optimizer_skill(
                f"Factory{index}",
                "MANUFACTURE",
                f"factory_{index}",
                35 - index,
            )
            for index in range(24)
        )
        skills.extend(
            optimizer_skill(f"Power{index}", "POWER", f"power_{index}", 10)
            for index in range(2)
        )
        optimizer = optimizer_with_skills(skills)
        layout = parse_layout("243")
        initial_shifts, _ = optimizer._build_shifts(
            layout,
            optimizer._room_specs(layout, "normal"),
            shift_count=2,
            shift_hours=12,
            shift_times=["08:00", "20:00"],
        )
        initial_production_rooms = {
            shift.name: production_room_names(shift)
            for shift in initial_shifts
        }
        initial_score = ProductionSimulator(optimizer.game_data).evaluate(initial_shifts).score

        result = optimizer.optimize(layout, shift_count=2)

        self.assertFalse(find_conflicts(result))
        self.assertGreater(result.score, initial_score)
        target_shift = shift_with_pair(
            result,
            ("CONTROL", None, "凛御银灰"),
            ("HIRE", None, "圣聆初雪"),
        )
        self.assertIsNotNone(target_shift)
        assert target_shift is not None
        self.assertEqual(
            production_room_names(target_shift),
            initial_production_rooms[target_shift.name],
        )

    def test_dependency_group_generation_adds_compound_right_side_neighborhood(self) -> None:
        skills = [
            optimizer_skill(
                "维什戴尔",
                "CONTROL",
                "wisadel_control",
                0,
                description="进驻控制中枢时，当魔王进驻控制中枢时所有贸易站订单效率+7%；当伊内丝入驻会客室时，会客室线索搜集速度+5%",
            ),
            optimizer_skill("魔王", "CONTROL", "am_control", 0),
            optimizer_skill("伊内丝", "MEETING", "ines_meeting", 10),
        ]
        optimizer = optimizer_with_skills(skills)

        groups = optimizer._diagnostic_shift_insertion_groups()

        self.assertTrue(
            any(
                group.id.startswith("compound_dependency:")
                and group_has_specs(
                    group,
                    ("CONTROL", None, "维什戴尔"),
                    ("CONTROL", None, "魔王"),
                    ("MEETING", None, "伊内丝"),
                )
                for group in groups
            )
        )

    def test_shift_local_search_continues_past_two_accepted_insertions(self) -> None:
        skills = [
            optimizer_skill(f"High{index}", "CONTROL", f"high_{index}", 0)
            for index in range(3)
        ]
        optimizer = optimizer_with_skills(skills)
        groups = [
            ShiftInsertionGroup(
                f"force_high_{index}",
                (ShiftInsertionSpec("CONTROL", None, f"High{index}"),),
            )
            for index in range(3)
        ]
        optimizer._diagnostic_shift_insertion_groups = lambda: groups  # type: ignore[method-assign]
        from arknights_schedule_generator.models import ShiftPlan

        class FakeScore:
            score = 0.0

        class FakeSimulator:
            def evaluate(self, shifts):
                return FakeScore()

        def accepted_ids(shifts):
            return set(shifts[0].start.split("|")[1:])

        def fake_satisfied(shift, group):
            return group.id in accepted_ids([shift])

        def fake_best_attempt(
            layout,
            current_shifts,
            simulator,
            group,
            current_score,
            current_report,
            **kwargs,
        ):
            if group.id in accepted_ids(current_shifts):
                return (
                    {
                        **insertion_group_to_dict(group),
                        "status": "already_satisfied",
                        "currentScore": round(current_score, 3),
                    },
                    None,
                    None,
                )
            next_shift = ShiftPlan(
                "A",
                f"{current_shifts[0].start}|{group.id}",
                12,
                [],
                [],
            )
            return (
                {
                    **insertion_group_to_dict(group),
                    "status": "improves",
                    "shift": "A",
                    "currentScore": round(current_score, 3),
                    "candidateScore": round(current_score + 1.0, 3),
                    "scoreDelta": 1.0,
                },
                [next_shift],
                current_report,
            )

        optimizer._insertion_group_satisfied = fake_satisfied  # type: ignore[method-assign]
        optimizer._best_insertion_attempt = fake_best_attempt  # type: ignore[method-assign]
        shifts = [ShiftPlan("A", "base", 12, [], [])]

        improved, search = optimizer._improve_shifts_with_insertions(
            parse_layout("243"),
            shifts,
            FakeSimulator(),
        )

        self.assertEqual(search["summary"]["acceptedCount"], 3)
        self.assertFalse(
            any(
                item["status"] == "remaining_improvement_after_pass_limit"
                for item in search["skipped"]
            )
        )
        self.assertEqual(accepted_ids(improved), {group.id for group in groups})

    def test_shift_local_search_accepts_same_room_faction_dependency(self) -> None:
        skills = [
            optimizer_skill(f"Trade{index}", "TRADING", f"trade_{index}", 20 - index)
            for index in range(20)
        ]
        skills.extend(
            [
                optimizer_skill(
                    "Vina",
                    "TRADING",
                    "foreign_trade_policy",
                    0,
                    description="进驻贸易站时，外贸决议生效；若同站存在格拉斯哥帮干员，则订单获取效率额外提升。",
                ),
                optimizer_skill(
                    "Morgan",
                    "TRADING",
                    "morgan_trade",
                    5,
                    faction_tags=("glasgow",),
                ),
            ]
        )
        skills.extend(
            optimizer_skill(f"Factory{index}", "MANUFACTURE", f"factory_{index}", 35 - index)
            for index in range(30)
        )
        skills.extend(
            optimizer_skill(f"Power{index}", "POWER", f"power_{index}", 10)
            for index in range(2)
        )
        optimizer = optimizer_with_skills(skills)

        result = optimizer.optimize(parse_layout("243"), shift_count=2)

        self.assertFalse(find_conflicts(result))
        self.assertTrue(shift_has_pair(result, ("TRADING", "O_GOLD", "Vina"), ("TRADING", "O_GOLD", "Morgan")))
        search = result.diagnostic_insertion_search
        self.assertTrue(
            any(
                item["groupId"] == "diagnostic_faction_dependency:Vina:glasgow"
                and item["status"] == "accepted"
                and item["finalSatisfied"]
                and item["scoreDelta"] > 0
                for item in search["accepted"]
            )
        )

    def test_faction_dependency_allows_no_skill_partner_as_room_trigger(self) -> None:
        skills = [
            optimizer_skill("TradeA", "TRADING", "trade_a", 4),
            optimizer_skill("TradeB", "TRADING", "trade_b", 3),
            optimizer_skill(
                "Vina",
                "TRADING",
                "foreign_trade_policy",
                0,
                description="进驻贸易站时，外贸决议生效；若同站存在格拉斯哥帮干员，则订单获取效率额外提升。",
            ),
            optimizer_skill(
                "Morgan",
                "DORMITORY",
                "morgan_dorm",
                0,
                faction_tags=("glasgow",),
            ),
        ]
        optimizer = optimizer_with_skills(skills)

        groups = optimizer._faction_dependency_insertion_groups(skills[2])
        group = next(
            group
            for group in groups
            if group_has_specs(group, ("TRADING", "O_GOLD", "Morgan"))
        )
        trigger_spec = next(spec for spec in group.specs if spec.operator_name == "Vina")
        partner_spec = next(spec for spec in group.specs if spec.operator_name == "Morgan")
        trigger_candidate = optimizer._skill_for_insertion(trigger_spec)
        partner_candidate = optimizer._skill_for_insertion(partner_spec)
        shifts = [
            shift_with_rooms(
                "A",
                [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[0], skills[1]])],
            )
        ]

        candidate = optimizer._insert_group_into_shift(parse_layout("243"), shifts, 0, group)

        self.assertFalse(trigger_spec.allow_no_skill)
        self.assertIsNotNone(trigger_candidate)
        assert trigger_candidate is not None
        self.assertNotEqual(trigger_candidate.skill.buff_id, "none")
        self.assertTrue(partner_spec.allow_no_skill)
        self.assertIsNotNone(partner_candidate)
        assert partner_candidate is not None
        self.assertEqual(partner_candidate.skill.buff_id, "none")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertTrue(room_contains(candidate[0].rooms[0], "TRADING", "O_GOLD", "Vina"))
        self.assertTrue(room_contains(candidate[0].rooms[0], "TRADING", "O_GOLD", "Morgan"))

    def test_named_dependency_does_not_synthesize_non_control_partner(self) -> None:
        skills = [
            optimizer_skill(
                "Trigger",
                "TRADING",
                "trigger_dependency",
                0,
                description="进驻贸易站时，若Partner在同一个贸易站，则订单获取效率额外提升。",
            ),
            optimizer_skill("Partner", "DORMITORY", "partner_dorm", 0),
        ]
        optimizer = optimizer_with_skills(skills)

        group = next(
            group
            for group in optimizer._diagnostic_shift_insertion_groups()
            if group.id == "diagnostic_named_dependency:Trigger:Partner"
        )
        partner_spec = next(spec for spec in group.specs if spec.operator_name == "Partner")

        self.assertFalse(partner_spec.allow_no_skill)
        self.assertIsNone(optimizer._skill_for_insertion(partner_spec))

    def test_shift_local_search_reports_faction_dependency_not_improving(self) -> None:
        skills = [
            optimizer_skill(f"Trade{index}", "TRADING", f"trade_{index}", 300 - index)
            for index in range(20)
        ]
        skills.extend(
            [
                optimizer_skill(
                    "Vina",
                    "TRADING",
                    "foreign_trade_policy",
                    0,
                    description="进驻贸易站时，外贸决议生效；若同站存在格拉斯哥帮干员，则订单获取效率额外提升。",
                ),
                optimizer_skill(
                    "Morgan",
                    "TRADING",
                    "morgan_trade",
                    5,
                    faction_tags=("glasgow",),
                ),
            ]
        )
        skills.extend(
            optimizer_skill(f"Factory{index}", "MANUFACTURE", f"factory_{index}", 35 - index)
            for index in range(30)
        )
        skills.extend(
            optimizer_skill(f"Power{index}", "POWER", f"power_{index}", 10)
            for index in range(2)
        )
        optimizer = optimizer_with_skills(skills)

        result = optimizer.optimize(parse_layout("243"), shift_count=2)

        self.assertFalse(find_conflicts(result))
        records = [
            item
            for item in [
                *result.diagnostic_insertion_search["accepted"],
                *result.diagnostic_insertion_search["skipped"],
            ]
            if item["groupId"] == "diagnostic_faction_dependency:Vina:glasgow"
        ]
        self.assertTrue(records)
        self.assertTrue(
            all(
                item["status"] in {"accepted", "evaluated_not_improving", "unplaceable"}
                for item in records
            )
        )

    def test_faction_dependency_without_available_partner_is_not_searched(self) -> None:
        skills = [
            optimizer_skill(
                "Vina",
                "TRADING",
                "foreign_trade_policy",
                0,
                description="进驻贸易站时，外贸决议生效；若同站存在格拉斯哥帮干员，则订单获取效率额外提升。",
            )
        ]
        skills.extend(
            optimizer_skill(f"Trade{index}", "TRADING", f"trade_{index}", 20 - index)
            for index in range(20)
        )
        skills.append(optimizer_skill("Gummy", "TRADING", "gummy_trade", 10))
        skills.extend(
            optimizer_skill(f"Factory{index}", "MANUFACTURE", f"factory_{index}", 35 - index)
            for index in range(30)
        )
        skills.append(
            optimizer_skill(
                "Leto",
                "MANUFACTURE",
                "leto_exp",
                0,
                targets=("F_EXP",),
                description="进驻制造站时，若Gummy在贸易站，则作战记录类配方的生产力+35%",
            )
        )
        skills.extend(
            optimizer_skill(f"Power{index}", "POWER", f"power_{index}", 10)
            for index in range(2)
        )
        optimizer = optimizer_with_skills(skills)

        result = optimizer.optimize(parse_layout("243"), shift_count=2)

        self.assertTrue(
            any(
                item["groupId"] == "diagnostic_faction_dependency:Vina:glasgow"
                and item["status"] == "not_searched_no_faction_partner"
                for item in result.diagnostic_insertion_search["skipped"]
            )
        )
        self.assertGreater(result.diagnostic_insertion_search["summary"]["availableGroupCount"], 0)

    def test_faction_dependency_generates_alternate_partner_when_best_is_blocked(self) -> None:
        skills = [
            optimizer_skill(
                "Vina",
                "TRADING",
                "foreign_trade_policy",
                0,
                description="进驻贸易站时，外贸决议生效；若同站存在格拉斯哥帮干员，则订单获取效率额外提升。",
            ),
            optimizer_skill("Morgan", "TRADING", "morgan_trade", 50, faction_tags=("glasgow",)),
            optimizer_skill("Dagda", "TRADING", "dagda_trade", 5, faction_tags=("glasgow",)),
            optimizer_skill("Backup", "TRADING", "backup_trade", 10),
        ]
        optimizer = optimizer_with_skills(skills)
        groups = optimizer._faction_dependency_insertion_groups(skills[0])
        morgan_group = next(group for group in groups if group_has_specs(group, ("TRADING", "O_GOLD", "Morgan")))
        dagda_group = next(group for group in groups if group_has_specs(group, ("TRADING", "O_GOLD", "Dagda")))
        shifts = [
            shift_with_rooms("A", [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[3]])]),
            shift_with_rooms("B", [make_test_room("trading_1", "TRADING", "O_GOLD", [skills[1]])]),
        ]

        blocked = optimizer._insert_group_into_shift(parse_layout("243"), shifts, 0, morgan_group)
        alternate = optimizer._insert_group_into_shift(parse_layout("243"), shifts, 0, dagda_group)

        self.assertIsNone(blocked)
        self.assertIsNotNone(alternate)


def shift_has_operator(result, room_type: str, target: str | None, operator_name: str) -> bool:
    return any(
        room_contains(room, room_type, target, operator_name)
        for shift in result.shifts
        for room in shift.rooms
    )


def shift_has_pair(result, *specs: tuple[str, str | None, str]) -> bool:
    return shift_with_pair(result, *specs) is not None


def shift_with_pair(result, *specs: tuple[str, str | None, str]):
    for shift in result.shifts:
        if all(
            any(room_contains(room, room_type, target, operator_name) for room in shift.rooms)
            for room_type, target, operator_name in specs
        ):
            return shift
    return None


def room_contains(
    room,
    room_type: str,
    target: str | None,
    operator_name: str,
) -> bool:
    return (
        room.room_type == room_type
        and (target is None or room.target == target)
        and any(skill.operator_name == operator_name for skill in room.operators)
    )


def group_has_specs(group, *specs: tuple[str, str | None, str]) -> bool:
    actual = {
        (spec.room_type, spec.target, spec.operator_name)
        for spec in group.specs
    }
    return all(spec in actual for spec in specs)


def shift_with_rooms(name: str, rooms):
    from arknights_schedule_generator.models import ShiftPlan

    return ShiftPlan(name, "08:00", 12, rooms, [])


def make_test_room(
    room_id: str,
    room_type: str,
    target: str | None,
    operators,
    *,
    room_level: int = 3,
    slots: int = 3,
):
    from arknights_schedule_generator.models import RoomAssignment

    return RoomAssignment(
        room_id,
        room_type,
        room_type,
        target,
        operators,
        0,
        room_level=room_level,
        slots=slots,
    )


def production_room_names(shift) -> dict[str, tuple[str, ...]]:
    return {
        room.room_id: tuple(skill.operator_name for skill in room.operators)
        for room in shift.rooms
        if room.room_type in {"TRADING", "MANUFACTURE", "POWER"}
    }


def write_minimal_data(data_dir: Path) -> None:
    rooms = {
        "CONTROL": {"phases": [{"maxStationedNum": 5}]},
        "TRADING": {"phases": [{"maxStationedNum": 3}]},
        "MANUFACTURE": {"phases": [{"maxStationedNum": 3}]},
        "POWER": {"phases": [{"maxStationedNum": 1}]},
        "DORMITORY": {"phases": [{"maxStationedNum": 5}]},
        "MEETING": {"phases": [{"maxStationedNum": 2}]},
        "HIRE": {"phases": [{"maxStationedNum": 2}]},
    }
    building = {
        "rooms": rooms,
        "buffs": {
            "trade_fast": {
                "buffId": "trade_fast",
                "buffName": "订单分发",
                "roomType": "TRADING",
                "description": "进驻贸易站时，订单获取效率+30%",
                "efficiency": 30,
                "targets": [],
            },
            "trade_upgrade": {
                "buffId": "trade_upgrade",
                "buffName": "物流专家",
                "roomType": "TRADING",
                "description": "进驻贸易站时，订单获取效率+40%",
                "efficiency": 40,
                "targets": [],
            },
            "manu_exp": {
                "buffId": "manu_exp",
                "buffName": "录像生产",
                "roomType": "MANUFACTURE",
                "description": "进驻制造站时，作战记录类配方的生产力+30%",
                "efficiency": 30,
                "targets": ["F_EXP"],
            },
            "power": {
                "buffId": "power",
                "buffName": "备用能源",
                "roomType": "POWER",
                "description": "进驻发电站时，无人机充能速度+10%",
                "efficiency": 10,
                "targets": [],
            },
            "dorm": {
                "buffId": "dorm",
                "buffName": "独处",
                "roomType": "DORMITORY",
                "description": "进驻宿舍时，自身心情每小时恢复+0.7",
                "efficiency": 0,
                "targets": [],
            },
        },
        "chars": {
            "char_exu": {
                "charId": "char_exu",
                "buffChar": [
                    {"buffData": [{"buffId": "trade_upgrade", "cond": {"phase": "PHASE_1", "level": 1}}]}
                ],
            },
            "char_texas": {
                "charId": "char_texas",
                "buffChar": [
                    {"buffData": [{"buffId": "trade_fast", "cond": {"phase": "PHASE_0", "level": 1}}]}
                ],
            },
            "char_sora": {
                "charId": "char_sora",
                "buffChar": [
                    {"buffData": [{"buffId": "manu_exp", "cond": {"phase": "PHASE_0", "level": 1}}]}
                ],
            },
            "char_steward": {
                "charId": "char_steward",
                "buffChar": [
                    {"buffData": [{"buffId": "power", "cond": {"phase": "PHASE_0", "level": 1}}]}
                ],
            },
        },
    }
    characters = {}
    for char_id, name, rarity in [
        ("char_exu", "能天使", "TIER_6"),
        ("char_texas", "德克萨斯", "TIER_5"),
        ("char_sora", "空", "TIER_5"),
        ("char_steward", "史都华德", "TIER_3"),
    ]:
        characters[char_id] = {
            "name": name,
            "rarity": rarity,
            "phases": [
                {"maxLevel": 50, "evolveCost": None},
                {"maxLevel": 80, "evolveCost": [{"id": "3001", "count": 2, "type": "MATERIAL"}]},
            ],
        }
    for name in ["夜刀", "Lancet-2", "Castle-3", "巡林者", "杜林", "安德切尔", "米格鲁", "克洛丝", "芬", "翎羽", "香草", "玫兰莎"]:
        characters[f"char_{name}"] = {
            "name": name,
            "rarity": "TIER_3",
            "phases": [{"maxLevel": 40, "evolveCost": None}],
        }
    items = {"3001": {"name": "龙门币", "rarity": "TIER_1"}}

    (data_dir / "building_data.json").write_text(json.dumps(building), encoding="utf-8")
    (data_dir / "character_table.json").write_text(json.dumps(characters), encoding="utf-8")
    (data_dir / "item_table.json").write_text(json.dumps(items), encoding="utf-8")
    (data_dir / "data_version.txt").write_text("test", encoding="utf-8")


def add_trade_combo_data(data_dir: Path) -> None:
    building_path = data_dir / "building_data.json"
    characters_path = data_dir / "character_table.json"
    building = json.loads(building_path.read_text(encoding="utf-8"))
    characters = json.loads(characters_path.read_text(encoding="utf-8"))
    building["buffs"].update(
        {
            "normal_trade_a": {
                "buffId": "normal_trade_a",
                "buffName": "普通贸易甲",
                "roomType": "TRADING",
                "description": "进驻贸易站时，订单获取效率+20%",
                "efficiency": 20,
                "targets": [],
            },
            "normal_trade_b": {
                "buffId": "normal_trade_b",
                "buffName": "普通贸易乙",
                "roomType": "TRADING",
                "description": "进驻贸易站时，订单获取效率+15%",
                "efficiency": 15,
                "targets": [],
            },
            "normal_trade_c": {
                "buffId": "normal_trade_c",
                "buffName": "普通贸易丙",
                "roomType": "TRADING",
                "description": "进驻贸易站时，订单获取效率+10%",
                "efficiency": 10,
                "targets": [],
            },
            "shamare_whisper": {
                "buffId": "shamare_whisper",
                "buffName": "低语",
                "roomType": "TRADING",
                "description": "进驻贸易站时，当前贸易站内其他干员提供的订单获取效率全部归零，且每人为自身+45%订单获取效率",
                "efficiency": 0,
                "targets": [],
            },
            "tequila_special": {
                "buffId": "tequila_special",
                "buffName": "龙门商法",
                "roomType": "TRADING",
                "description": "进驻贸易站时，固定获取特别订单",
                "efficiency": 0,
                "targets": [],
            },
            "butushu_claim": {
                "buffId": "butushu_claim",
                "buffName": "违约索赔",
                "roomType": "TRADING",
                "description": "进驻贸易站时，如果下笔赤金订单是违约订单，则赤金交付数额外+2",
                "efficiency": 0,
                "targets": [],
            },
        }
    )
    for char_id, name, buff_id in [
        ("char_normal_trade_a", "普通贸易甲", "normal_trade_a"),
        ("char_normal_trade_b", "普通贸易乙", "normal_trade_b"),
        ("char_normal_trade_c", "普通贸易丙", "normal_trade_c"),
        ("char_shamare", "巫恋", "shamare_whisper"),
        ("char_tequila", "龙舌兰", "tequila_special"),
        ("char_butushu", "但书", "butushu_claim"),
        ("char_morgan", "摩根", "normal_trade_a"),
    ]:
        building["chars"][char_id] = {
            "charId": char_id,
            "buffChar": [{"buffData": [{"buffId": buff_id, "cond": {"phase": "PHASE_0", "level": 1}}]}],
        }
        characters[char_id] = {
            "name": name,
            "rarity": "TIER_5",
            "groupId": "glasgow" if name == "摩根" else None,
            "phases": [{"maxLevel": 50, "evolveCost": None}],
        }
    building_path.write_text(json.dumps(building), encoding="utf-8")
    characters_path.write_text(json.dumps(characters), encoding="utf-8")


def optimizer_with_skills(skills: list[BaseSkill]) -> ScheduleOptimizer:
    roster_names = dict.fromkeys(skill.operator_name for skill in skills)
    roster = [RosterOperator(name, True, 5, 1, 0) for name in roster_names]
    optimizer = ScheduleOptimizer(GameData({"rooms": {}}, {}, {}), roster)
    optimizer.skills_by_operator = {}
    for skill in skills:
        optimizer.skills_by_operator.setdefault(skill.operator_name, []).append(skill)
    return optimizer


def production_report_for_selection(
    lmd_gross: float,
    exp: float,
    orundum: float,
    score: float,
    *,
    lmd_net: float = 0.0,
    pure_gold_delta: float = 0.0,
    shard_delta: float = 0.0,
) -> ProductionReport:
    vector = ProductionVector(
        lmdGross=lmd_gross,
        lmdNet=lmd_net,
        exp=exp,
        orundum=orundum,
        pureGoldDelta=pure_gold_delta,
        shardDelta=shard_delta,
    )
    return ProductionReport(
        dailyExpected=vector,
        scoreBreakdown={"fixture": score},
        roomReports=[],
        unsupportedSkillEffects=[],
        assumptions=[],
    )


def optimizer_skill(
    name: str,
    room_type: str,
    buff_id: str,
    parsed_score: float,
    *,
    targets: tuple[str, ...] = (),
    faction_tags: tuple[str, ...] = (),
    description: str = "",
    upgrade: UpgradeRequirement | None = None,
) -> BaseSkill:
    return BaseSkill(
        char_id=f"char_{name}",
        operator_name=name,
        room_type=room_type,
        buff_id=buff_id,
        buff_name=buff_id,
        description=description,
        efficiency=parsed_score,
        targets=targets,
        cond_elite=0,
        cond_level=1,
        unlocked=True,
        complex_condition=False,
        parsed_score=parsed_score,
        upgrade=upgrade,
        faction_tags=faction_tags,
    )


if __name__ == "__main__":
    unittest.main()
