from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from . import dependency_parser
from .data import GameData
from .models import BaseSkill, RoomAssignment, RosterOperator, ShiftPlan
from .optimizer import OptimizerResult, ROOM_NAMES
from .production import ProductionSimulator
from .room_limits import clamp_station_count


@dataclass(frozen=True)
class ForceSpec:
    room_type: str
    target: str | None
    operators: tuple[str, ...]


def build_recommendation_diagnostics(
    game_data: GameData,
    roster: Iterable[RosterOperator],
    result: OptimizerResult,
    *,
    shard_formula: str,
    max_anomalies: int = 30,
    solution_id: str | None = None,
    solution_group: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    roster_list = [operator for operator in roster if operator.recruited]
    allow_upgrades = profile != "current" if profile is not None else True
    skills_by_name = {
        operator.name: game_data.skills_for_roster_operator(
            operator,
            allow_upgrades=allow_upgrades,
        )
        for operator in roster_list
    }
    context = DiagnosticContext(
        game_data=game_data,
        roster=roster_list,
        result=result,
        skills_by_name=skills_by_name,
        shard_formula=shard_formula,
    )
    explicit = explicit_case_diagnostics(context)
    anomalies = discover_anomaly_candidates(context, max_anomalies=max_anomalies)
    return {
        "targetCandidate": {
            "id": solution_id,
            "group": solution_group,
            "profile": profile,
            "layout": result.layout.label,
            "mode": result.mode,
            "score": result.score,
            "shiftCount": len(result.shifts),
        },
        "gpuRecommendation": {
            "needed": False,
            "reason": "当前瓶颈是规则建模、组合搜索和可解释诊断；CPU 枚举、缓存、局部搜索或 CP-SAT 更适合。",
        },
        "counterintuitiveDiagnostics": explicit,
        "anomalyCandidates": anomalies,
    }


@dataclass
class DiagnosticContext:
    game_data: GameData
    roster: list[RosterOperator]
    result: OptimizerResult
    skills_by_name: dict[str, list[BaseSkill]]
    shard_formula: str


def explicit_case_diagnostics(context: DiagnosticContext) -> list[dict[str, Any]]:
    return [
        vina_glasgow_case(context),
        leto_gummy_case(context),
        silverash_snow_case(context),
        eyja_shard_case(context),
    ]


def vina_glasgow_case(context: DiagnosticContext) -> dict[str, Any]:
    partner = first_available_with_tag(context, "glasgow", preferred=("摩根", "推进之王"))
    experiment = forced_experiment(
        context,
        [ForceSpec("TRADING", "O_GOLD", tuple(name for name in ("维娜·维多利亚", partner) if name))],
    )
    current = same_room_with_faction(context, "维娜·维多利亚", "TRADING", "glasgow")
    return diagnostic_entry(
        "vina_glasgow_trade",
        "维娜·维多利亚是否应与格拉斯哥帮同贸易站",
        context,
        ["维娜·维多利亚", partner] if partner else ["维娜·维多利亚"],
        "TRADING",
        "O_GOLD",
        current,
        "已建模外贸决议·β：贸易站内存在格拉斯哥帮时额外 +10%。",
        experiment,
    )


def leto_gummy_case(context: DiagnosticContext) -> dict[str, Any]:
    current = operator_in_room_type(context, "烈夏", "MANUFACTURE") and operator_in_room_type(
        context, "古米", "TRADING"
    )
    experiment = forced_experiment(
        context,
        [
            ForceSpec("MANUFACTURE", "F_EXP", ("烈夏",)),
            ForceSpec("TRADING", "O_GOLD", ("古米",)),
        ],
    )
    return diagnostic_entry(
        "leto_gummy_cross_room",
        "烈夏制造站是否应搭配古米贸易站",
        context,
        ["烈夏", "古米"],
        "MANUFACTURE/TRADING",
        "F_EXP/O_GOLD",
        current,
        "已建模患难拍档：烈夏在经验制造站且古米在贸易站时 +35%。",
        experiment,
    )


def silverash_snow_case(context: DiagnosticContext) -> dict[str, Any]:
    current = operator_in_room_type(context, "凛御银灰", "CONTROL") and operator_in_room_type(
        context, "圣聆初雪", "HIRE"
    )
    experiment = forced_experiment(
        context,
        [
            ForceSpec("CONTROL", None, ("凛御银灰",)),
            ForceSpec("HIRE", None, ("圣聆初雪",)),
        ],
    )
    return diagnostic_entry(
        "silverash_snow_office",
        "凛御银灰中枢时是否应使用圣聆初雪办公室",
        context,
        ["凛御银灰", "圣聆初雪"],
        "CONTROL/HIRE",
        None,
        current,
        "圣聆初雪联动是办公室公开招募联络速度；officeSpeed 已进入综合评分，但仍与主产出资源分开展示。",
        experiment,
        scoring_scope="office_efficiency",
    )


def eyja_shard_case(context: DiagnosticContext) -> dict[str, Any]:
    current = operator_in_room_target(context, "艾雅法拉", "MANUFACTURE", "F_DIAMOND")
    experiment = forced_experiment(
        context,
        [ForceSpec("MANUFACTURE", "F_DIAMOND", ("艾雅法拉",))],
    )
    return diagnostic_entry(
        "eyja_shard_factory",
        "艾雅法拉是否应进入源石碎片制造站",
        context,
        ["艾雅法拉"],
        "MANUFACTURE",
        "F_DIAMOND",
        current,
        "火山学家目标为 F_DIAMOND，已能进入源石碎片候选；若未选中通常来自全局资源平衡或替换损失。",
        experiment,
    )


def diagnostic_entry(
    case_id: str,
    title: str,
    context: DiagnosticContext,
    operators: list[str | None],
    room_type: str,
    target: str | None,
    currently_satisfied: bool,
    modeled_rule: str,
    experiment: dict[str, Any],
    *,
    scoring_scope: str = "main_production",
) -> dict[str, Any]:
    names = [name for name in operators if name]
    return {
        "id": case_id,
        "title": title,
        "operators": [
            {
                "name": name,
                "available": name in context.skills_by_name,
                "locations": operator_locations(context.result, name),
                "skills": skill_summaries(context.skills_by_name.get(name, [])),
            }
            for name in names
        ],
        "expectedRoomType": room_type,
        "expectedTarget": target,
        "currentlySatisfied": currently_satisfied,
        "modeledRule": modeled_rule,
        "scoringScope": scoring_scope,
        "forcedExperiment": experiment,
        "conclusion": conclusion_for(currently_satisfied, experiment, scoring_scope),
    }


def discover_anomaly_candidates(
    context: DiagnosticContext,
    *,
    max_anomalies: int,
) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    names = {operator.name for operator in context.roster}
    for operator in context.roster:
        for skill in context.skills_by_name.get(operator.name, []):
            anomalies.extend(anomalies_for_skill(context, skill, names))

    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in anomalies:
        key = (item["type"], item["operator"], str(item.get("expectedRoomType")))
        previous = deduped.get(key)
        if previous is None or item["priorityScore"] > previous["priorityScore"]:
            deduped[key] = item

    ordered = sorted(
        deduped.values(),
        key=lambda item: (
            item.get("isRelevantToCurrentBest", False),
            item.get("available", False),
            item["priorityScore"],
        ),
        reverse=True,
    )
    for item in ordered[:12]:
        item["forcedExperiment"] = anomaly_experiment(context, item)
        item["conclusion"] = conclusion_for(
            item.get("currentlySatisfied", False),
            item.get("forcedExperiment", {}),
            item.get("scoringScope", "main_production"),
        )
    return ordered[:max_anomalies]


def anomalies_for_skill(
    context: DiagnosticContext,
    skill: BaseSkill,
    roster_names: set[str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    text = f"{skill.buff_name} {skill.description}"
    priority = max(skill.parsed_score, max_percent(text))
    if skill.room_type == "MANUFACTURE" and "F_DIAMOND" in skill.targets and priority >= 30:
        satisfied = operator_in_room_target(context, skill.operator_name, "MANUFACTURE", "F_DIAMOND")
        items.append(
            anomaly(
                "formula_specialist_not_in_target_room",
                skill,
                "高源石碎片制造技能未进入源石碎片制造站",
                "MANUFACTURE",
                "F_DIAMOND",
                priority,
                satisfied,
            )
        )
    if skill.room_type == "MANUFACTURE" and "古米" in text and "贸易站" in text:
        satisfied = operator_in_room_type(context, skill.operator_name, "MANUFACTURE") and operator_in_room_type(
            context, "古米", "TRADING"
        )
        item = anomaly(
            "named_cross_room_dependency",
            skill,
            "点名跨房间依赖未满足",
            "MANUFACTURE/TRADING",
            "F_EXP/O_GOLD",
            priority,
            satisfied,
        )
        item["expectedPartner"] = "古米"
        items.append(item)
    if skill.room_type == "TRADING" and "格拉斯哥帮" in text:
        satisfied = same_room_with_faction(context, skill.operator_name, "TRADING", "glasgow")
        item = anomaly(
            "same_room_faction_dependency",
            skill,
            "贸易站阵营同站依赖未满足",
            "TRADING",
            "O_GOLD",
            max(priority, 40.0),
            satisfied,
        )
        item["expectedFaction"] = "glasgow"
        partner = first_available_with_tag(
            context,
            "glasgow",
            preferred=("摩根", "推进之王"),
            exclude=skill.operator_name,
        )
        if partner:
            item["expectedPartner"] = partner
        items.append(item)
    for partner in mentioned_operator_names(text, roster_names):
        if partner == skill.operator_name:
            continue
        if skill.room_type in {"TRADING", "MANUFACTURE", "CONTROL", "HIRE"}:
            explanation = explain_named_dependency(skill, partner)
            item = anomaly(
                "named_dependency",
                skill,
                "技能文本点名依赖已解析为可解释依赖。",
                skill.room_type,
                tuple(skill.targets) if skill.targets else None,
                priority,
                dependency_currently_satisfied(context, skill.operator_name, partner, explanation),
            )
            item["expectedPartner"] = partner
            item["dependencyExplanation"] = explanation
            items.append(item)
    return [item for item in items if not item["currentlySatisfied"] or item["priorityScore"] >= 30]


def anomaly(
    anomaly_type: str,
    skill: BaseSkill,
    reason: str,
    room_type: str,
    target: Any,
    priority: float,
    satisfied: bool,
) -> dict[str, Any]:
    return {
        "type": anomaly_type,
        "operator": skill.operator_name,
        "buffId": skill.buff_id,
        "buffName": skill.buff_name,
        "roomType": skill.room_type,
        "skillDescription": skill.description,
        "reason": reason,
        "expectedRoomType": room_type,
        "expectedTarget": target,
        "currentlySatisfied": satisfied,
        "available": True,
        "priorityScore": round(float(priority), 3),
        "scoringScope": "office_efficiency" if skill.room_type == "HIRE" else "main_production",
    }


def anomaly_experiment(context: DiagnosticContext, item: dict[str, Any]) -> dict[str, Any]:
    operator = item.get("operator")
    if item["type"] == "formula_specialist_not_in_target_room":
        return forced_experiment(context, [ForceSpec("MANUFACTURE", "F_DIAMOND", (operator,))])
    if item["type"] == "named_cross_room_dependency" and item.get("expectedPartner") == "古米":
        return forced_experiment(
            context,
            [
                ForceSpec("MANUFACTURE", "F_EXP", (operator,)),
                ForceSpec("TRADING", "O_GOLD", ("古米",)),
            ],
        )
    if item["type"] == "same_room_faction_dependency" and item.get("expectedFaction"):
        partner = first_available_with_tag(context, str(item["expectedFaction"]), preferred=("摩根", "推进之王"))
        return forced_experiment(
            context,
            [ForceSpec("TRADING", "O_GOLD", tuple(name for name in (operator, partner) if name))],
        )
    if item["type"] == "named_dependency":
        specs = force_specs_from_explanation(
            item.get("dependencyExplanation") or {},
            operator=str(operator),
            partner=str(item.get("expectedPartner") or ""),
        )
        if specs:
            return forced_experiment(context, specs)
        return {
            "status": "not_run",
            "reason": "点名依赖已解释，但文本没有给出足够明确的房间关系，暂不强制实验。",
        }
    return {"status": "not_run", "reason": "该异常类型暂未配置强制实验。"}


def explain_named_dependency(skill: BaseSkill, partner: str) -> dict[str, Any]:
    return dependency_parser.explain_named_dependency(skill, partner)


def force_specs_from_explanation(
    explanation: dict[str, Any],
    *,
    operator: str,
    partner: str,
) -> list[ForceSpec]:
    return [
        ForceSpec(spec.room_type, spec.target, spec.operators)
        for spec in dependency_parser.force_specs_from_explanation(
            explanation,
            operator=operator,
            partner=partner,
        )
    ]


def dependency_currently_satisfied(
    context: DiagnosticContext,
    operator: str,
    partner: str,
    explanation: dict[str, Any],
) -> bool:
    relation = explanation.get("relation")
    if relation == "same_room":
        room_type = str(explanation.get("triggerRoomType") or "")
        target = explanation.get("target")
        for shift in context.result.shifts:
            for room in shift.rooms:
                if room.room_type != room_type:
                    continue
                if target and room.target != target:
                    continue
                names = {skill.operator_name for skill in room.operators}
                if operator in names and partner in names:
                    return True
        return False
    if relation == "cross_room":
        for shift in context.result.shifts:
            trigger_ok = False
            partner_ok = False
            for room in shift.rooms:
                names = {skill.operator_name for skill in room.operators}
                if operator in names and room.room_type == explanation.get("triggerRoomType"):
                    target = explanation.get("triggerTarget")
                    trigger_ok = not target or room.target == target
                if partner in names and room.room_type == explanation.get("partnerRoomType"):
                    target = explanation.get("partnerTarget")
                    partner_ok = not target or room.target == target
            if trigger_ok and partner_ok:
                return True
        return False
    return partner_in_any_shift(context, operator, partner)


def named_partner_room_type(text: str, partner: str) -> str | None:
    return dependency_parser.named_partner_room_type(text, partner)


def first_target(skill: BaseSkill) -> str | None:
    return dependency_parser.first_skill_target(skill)


def default_target_for_room(room_type: str) -> str | None:
    return dependency_parser.default_target_for_room(room_type)


def forced_experiment(context: DiagnosticContext, specs: list[ForceSpec]) -> dict[str, Any]:
    missing = [name for spec in specs for name in spec.operators if name not in context.skills_by_name]
    if missing:
        return {"status": "unavailable", "reason": f"干员不在练度表或未拥有: {', '.join(missing)}"}

    simulator = ProductionSimulator(
        context.game_data,
        shard_formula=context.shard_formula,
        drone_policy=context.result.drone_policy,
        calibration_profile="guide",
    )
    best: dict[str, Any] | None = None
    for shift_index in range(len(context.result.shifts)):
        forced = force_specs_into_shift(context, specs, shift_index)
        if forced is None:
            continue
        report = simulator.evaluate(forced["shifts"])
        delta = round(report.score - context.result.score, 3)
        candidate = {
            "status": "evaluated",
            "shift": context.result.shifts[shift_index].name,
            "currentScore": context.result.score,
            "forcedScore": report.score,
            "scoreDelta": delta,
            "dailyExpectedDelta": vector_delta(
                context.result.production_report.dailyExpected.to_dict()
                if context.result.production_report
                else {},
                report.dailyExpected.to_dict(),
            ),
            "replacements": forced["replacements"],
        }
        if best is None or candidate["scoreDelta"] > best["scoreDelta"]:
            best = candidate
    return best or {"status": "unavailable", "reason": "当前排班没有可放置该强制组合的目标房间。"}


def force_specs_into_shift(
    context: DiagnosticContext,
    specs: list[ForceSpec],
    shift_index: int,
) -> dict[str, Any] | None:
    forced_names = {name for spec in specs for name in spec.operators}
    new_shifts: list[ShiftPlan] = []
    replacements: list[dict[str, Any]] = []
    applied = [False] * len(specs)
    for index, shift in enumerate(context.result.shifts):
        if index != shift_index:
            new_shifts.append(shift)
            continue
        new_rooms: list[RoomAssignment] = []
        for room in shift.rooms:
            spec_index = next(
                (
                    idx
                    for idx, spec in enumerate(specs)
                    if not applied[idx]
                    and spec.room_type == room.room_type
                    and (spec.target is None or spec.target == room.target)
                ),
                None,
            )
            if spec_index is None:
                operators = [skill for skill in room.operators if skill.operator_name not in forced_names]
                new_rooms.append(copy_room(room, operators))
                continue
            spec = specs[spec_index]
            applied[spec_index] = True
            forced_skills = [
                best_skill_for(context, name, room.room_type, room.target)
                for name in spec.operators
            ]
            if any(skill is None for skill in forced_skills):
                return None
            forced_list = [skill for skill in forced_skills if skill is not None]
            raw_capacity = room.slots if room.slots is not None else max(
                len(room.operators), len(forced_list)
            )
            capacity = clamp_station_count(room.room_type, raw_capacity)
            if len(forced_list) > capacity:
                return None
            keep = [
                skill
                for skill in room.operators
                if skill.operator_name not in forced_names
                and skill.operator_name not in {forced.operator_name for forced in forced_list}
            ]
            operators = [*forced_list, *keep][:capacity]
            replacements.append(
                {
                    "roomId": room.room_id,
                    "roomType": room.room_type,
                    "target": room.target,
                    "before": [skill.operator_name for skill in room.operators],
                    "after": [skill.operator_name for skill in operators],
                }
            )
            new_rooms.append(copy_room(room, operators))
        new_shifts.append(
            ShiftPlan(
                name=shift.name,
                start=shift.start,
                duration_hours=shift.duration_hours,
                rooms=new_rooms,
                dormitories=shift.dormitories,
            )
        )
    if not all(applied):
        return None
    return {"shifts": new_shifts, "replacements": replacements}


def copy_room(room: RoomAssignment, operators: list[BaseSkill]) -> RoomAssignment:
    return RoomAssignment(
        room_id=room.room_id,
        room_type=room.room_type,
        room_name=room.room_name,
        target=room.target,
        operators=operators,
        score=0.0,
        room_level=room.room_level,
        slots=room.slots,
        product_capacity=room.product_capacity,
        order_limit=room.order_limit,
    )


def best_skill_for(
    context: DiagnosticContext,
    name: str,
    room_type: str,
    target: str | None,
) -> BaseSkill | None:
    matches = []
    for skill in context.skills_by_name.get(name, []):
        if skill.room_type != room_type:
            continue
        if target and skill.targets and target not in skill.targets:
            continue
        matches.append(skill)
    if not matches:
        return None
    return max(matches, key=lambda skill: skill.parsed_score)


def first_available_with_tag(
    context: DiagnosticContext,
    tag: str,
    *,
    preferred: tuple[str, ...] = (),
    exclude: str | None = None,
) -> str | None:
    for name in preferred:
        if name == exclude:
            continue
        if any(tag in skill.faction_tags for skill in context.skills_by_name.get(name, [])):
            return name
    for name, skills in context.skills_by_name.items():
        if name == exclude:
            continue
        if any(tag in skill.faction_tags for skill in skills):
            return name
    return None


def operator_locations(result: OptimizerResult, name: str) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    for shift in result.shifts:
        for room in [*shift.rooms, *shift.dormitories]:
            for skill in room.operators:
                if skill.operator_name == name:
                    locations.append(
                        {
                            "shift": shift.name,
                            "roomId": room.room_id,
                            "roomType": room.room_type,
                            "target": room.target,
                            "buffName": skill.buff_name,
                        }
                    )
    return locations


def operator_in_room_type(context: DiagnosticContext, name: str, room_type: str) -> bool:
    return any(item["roomType"] == room_type for item in operator_locations(context.result, name))


def operator_in_room_target(
    context: DiagnosticContext,
    name: str,
    room_type: str,
    target: str,
) -> bool:
    return any(
        item["roomType"] == room_type and item["target"] == target
        for item in operator_locations(context.result, name)
    )


def same_room_with_faction(
    context: DiagnosticContext,
    name: str,
    room_type: str,
    tag: str,
) -> bool:
    for shift in context.result.shifts:
        for room in shift.rooms:
            if room.room_type != room_type:
                continue
            names = {skill.operator_name for skill in room.operators}
            if name not in names:
                continue
            if any(skill.operator_name != name and tag in skill.faction_tags for skill in room.operators):
                return True
    return False


def partner_in_any_shift(context: DiagnosticContext, first: str, second: str) -> bool:
    for shift in context.result.shifts:
        active = {
            skill.operator_name
            for room in [*shift.rooms, *shift.dormitories]
            for skill in room.operators
        }
        if first in active and second in active:
            return True
    return False


def skill_summaries(skills: list[BaseSkill]) -> list[dict[str, Any]]:
    return [
        {
            "roomType": skill.room_type,
            "buffId": skill.buff_id,
            "buffName": skill.buff_name,
            "targets": list(skill.targets),
            "unlocked": skill.unlocked,
            "condition": skill.condition_label,
            "parsedScore": skill.parsed_score,
            "factionTags": list(skill.faction_tags),
            "hasUpgradeRequirement": skill.upgrade is not None,
        }
        for skill in skills
    ]


def mentioned_operator_names(text: str, roster_names: set[str]) -> list[str]:
    return dependency_parser.mentioned_operator_names(text, roster_names)


def max_percent(text: str) -> float:
    values = [float(match) for match in re.findall(r"\+(\d+(?:\.\d+)?)%", text)]
    return max(values, default=0.0)


def vector_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float]:
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


def conclusion_for(
    satisfied: bool,
    experiment: dict[str, Any],
    scoring_scope: str,
) -> str:
    if satisfied:
        return "当前最优解已经满足该直觉组合。"
    if scoring_scope == "auxiliary_facility":
        return "该收益不进入主生产评分，建议作为辅助设施诊断单独查看。"
    if experiment.get("status") != "evaluated":
        return f"无法完成强制实验：{experiment.get('reason', '未知原因')}"
    delta = float(experiment.get("scoreDelta") or 0.0)
    if delta > 0.01:
        return "强制组合优于当前方案，疑似存在规则缺失或搜索漏解。"
    if delta < -0.01:
        return "强制组合低于当前方案，当前算法认为该直觉组合不优。"
    return "强制组合与当前方案近似持平，可按人工偏好选择。"
