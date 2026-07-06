from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import BaseSkill


@dataclass
class RoomSkillEffect:
    speed_percent: float = 0.0
    capacity_bonus: float = 0.0
    order_limit_bonus: float = 0.0
    fatigue_delta_per_hour: float = 0.0
    lmd_per_order_bonus: float = 0.0
    order_weight_level: int = 0
    control_speed_percent: float = 0.0
    fixed_special_order: str | None = None
    assumptions: list[str] = field(default_factory=list)
    unsupported: list[dict[str, str]] = field(default_factory=list)
    skill_effect_audit: list[dict[str, Any]] = field(default_factory=list)

    def add(self, other: "RoomSkillEffect") -> None:
        self.speed_percent += other.speed_percent
        self.capacity_bonus += other.capacity_bonus
        self.order_limit_bonus += other.order_limit_bonus
        self.fatigue_delta_per_hour += other.fatigue_delta_per_hour
        self.lmd_per_order_bonus += other.lmd_per_order_bonus
        self.order_weight_level = max(self.order_weight_level, other.order_weight_level)
        self.control_speed_percent += other.control_speed_percent
        self.assumptions.extend(other.assumptions)
        self.unsupported.extend(other.unsupported)
        self.skill_effect_audit.extend(other.skill_effect_audit)
        if other.fixed_special_order:
            self.fixed_special_order = other.fixed_special_order


@dataclass(frozen=True)
class SkillContext:
    active_faction_counts: dict[str, int] = field(default_factory=dict)
    power_faction_counts: dict[str, int] = field(default_factory=dict)
    operator_rooms: dict[str, str] = field(default_factory=dict)


def evaluate_room_effect(
    skills: list[BaseSkill],
    target: str | None,
    context: SkillContext | None = None,
) -> RoomSkillEffect:
    effect = RoomSkillEffect()
    names = {skill.operator_name for skill in skills}
    room_factions = faction_counts(skills)
    context = context or SkillContext()

    # Shamare-like stations zero out other operators and replace station speed with
    # her per-teammate value. This is only useful for trading.
    whisper = next(
        (
            skill
            for skill in skills
            if skill.room_type == "TRADING"
            and ("低语" in skill.buff_name or "其他干员提供的订单获取效率全部归零" in skill.description)
        ),
        None,
    )
    if whisper:
        speed = 45.0 * max(0, len(skills) - 1)
        effect.speed_percent += speed
        effect.assumptions.append("巫恋/低语按每名同站队友为站点贡献 45% 订单效率估算。")
        effect.skill_effect_audit.append(
            skill_audit(
                whisper,
                "counted",
                "room_speed",
                "Shamare-like station rule replaces partner speed with a per-teammate station estimate.",
                "modeled_rule",
                {"speedPercent": speed},
            )
        )
        for skill in skills:
            if skill is whisper:
                continue
            effect.add(evaluate_skill(skill, target, names, room_factions, context, include_speed=False))
    else:
        for skill in skills:
            effect.add(evaluate_skill(skill, target, names, room_factions, context))

    # Capacity conversion skills depend on the whole room, so handle them after
    # base capacity bonuses have been accumulated.
    if any("回收利用" in skill.buff_name for skill in skills) and effect.capacity_bonus:
        speed = effect.capacity_bonus * 2.0
        effect.speed_percent += speed
        effect.assumptions.append("红云/回收利用按全站仓库容量加成每格 +2% 生产力估算。")
        for skill in skills:
            if "回收利用" in skill.buff_name:
                effect.skill_effect_audit.append(
                    skill_audit(
                        skill,
                        "counted",
                        "capacity_conversion",
                        "Capacity conversion is counted from the room's total capacity bonus.",
                        "modeled_rule",
                        {"speedPercent": speed},
                    )
                )
    for skill in skills:
        if "大就是好" in skill.buff_name:
            own_bonus = capacity_bonus_from_text(skill.description)
            speed = own_bonus * (3.0 if own_bonus > 16 else 1.0)
            effect.speed_percent += speed
            effect.assumptions.append("泡泡/大就是好按自身仓库容量转换为生产力估算。")
            effect.skill_effect_audit.append(
                skill_audit(
                    skill,
                    "counted",
                    "capacity_conversion",
                    "Own capacity bonus is converted into production speed.",
                    "modeled_rule",
                    {"speedPercent": speed},
                )
            )
    return effect


def evaluate_skill(
    skill: BaseSkill,
    target: str | None,
    room_names: set[str],
    room_factions: dict[str, int],
    context: SkillContext,
    *,
    include_speed: bool = True,
) -> RoomSkillEffect:
    effect = RoomSkillEffect()
    description = skill.description
    if target and skill.targets and target not in skill.targets:
        effect.skill_effect_audit.append(
            skill_audit(
                skill,
                "explicitly_excluded",
                "target_filter",
                f"Skill targets {sorted(skill.targets)} do not match room target {target}.",
                "target_mismatch",
            )
        )
        return effect

    special = evaluate_faction_skill(
        skill,
        target,
        room_names,
        room_factions,
        context,
        include_speed=include_speed,
    )
    if special is not None:
        ensure_complex_skill_is_audited(skill, special)
        return special

    if include_speed and skill.room_type in {"MANUFACTURE", "TRADING", "POWER", "MEETING", "HIRE"}:
        effect.speed_percent += skill.parsed_score
        if abs(skill.parsed_score) > 0.0001:
            effect.skill_effect_audit.append(
                skill_audit(
                    skill,
                    "counted",
                    "room_speed",
                    "Parsed fixed room speed is counted in speedPercent.",
                    "parsed_fixed_speed",
                    {"speedPercent": float(skill.parsed_score)},
                )
            )
    if skill.room_type == "HIRE" and (
        "雪境归心" in description or "凛御银灰" in description
    ):
        if include_speed and context.operator_rooms.get("凛御银灰") == "CONTROL":
            effect.speed_percent += 10.0
            effect.assumptions.append(
                f"{skill.operator_name}/雪境归心按凛御银灰进驻控制中枢额外 +10% 计入。"
            )

    capacity_bonus = capacity_bonus_from_text(description)
    order_limit_bonus = order_limit_bonus_from_text(description)
    fatigue_delta = fatigue_delta_from_text(description)
    if (
        skill.room_type == "CONTROL"
        and "当与" in description
        and "阿米娅" in description
        and "一起进驻控制中枢" in description
    ):
        if "阿米娅" not in room_names:
            fatigue_delta = 0.0
            mark_unsupported(effect, skill, "阿米娅未同驻控制中枢，条件心情恢复未计入。")
        else:
            effect.assumptions.append(f"{skill.operator_name}/{skill.buff_name} 的阿米娅同驻心情恢复条件已满足。")
    if fatigue_delta:
        unmet_reason = conditional_morale_unmet_reason(
            skill,
            description,
            room_names,
            room_factions,
        )
        if unmet_reason:
            fatigue_delta = 0.0
            effect.skill_effect_audit.append(
                skill_audit(
                    skill,
                    "condition_unmet",
                    "fatigue",
                    unmet_reason,
                    "modeled_condition",
                )
            )
    order_weight_level = order_weight_level_from_skill(skill)
    effect.capacity_bonus += capacity_bonus
    effect.order_limit_bonus += order_limit_bonus
    effect.fatigue_delta_per_hour += fatigue_delta
    effect.order_weight_level = max(effect.order_weight_level, order_weight_level)
    if capacity_bonus:
        effect.skill_effect_audit.append(
            skill_audit(
                skill,
                "counted",
                "capacity",
                "Parsed capacity bonus is counted in room capacity.",
                "parsed_capacity",
                {"capacityBonus": capacity_bonus},
            )
        )
    if order_limit_bonus:
        effect.skill_effect_audit.append(
            skill_audit(
                skill,
                "counted",
                "order_limit",
                "Parsed order-limit bonus is counted in room order capacity.",
                "parsed_order_limit",
                {"orderLimitBonus": order_limit_bonus},
            )
        )
    if fatigue_delta:
        effect.skill_effect_audit.append(
            skill_audit(
                skill,
                "counted",
                "fatigue",
                "Parsed morale delta is counted in fatigue risk.",
                "parsed_fatigue",
                {"fatigueDeltaPerHour": fatigue_delta},
            )
        )
    if order_weight_level:
        effect.skill_effect_audit.append(
            skill_audit(
                skill,
                "counted",
                "order_probability",
                "Parsed order-quality bias is counted in the generic LMD order profile.",
                "parsed_order_weight",
                {"orderWeightLevel": float(order_weight_level)},
            )
        )

    if (
        include_speed
        and skill.room_type == "TRADING"
        and skill.buff_id == "trade_ord_spd&multiPar[100]"
        and "能天使" in room_names
    ):
        effect.speed_percent += 25.0
        effect.skill_effect_audit.append(
            skill_audit(
                skill,
                "counted",
                "same_room_condition",
                "Lemuen's Exusiai same-room Trading Post bonus is satisfied and counted.",
                "modeled_rule",
                {"speedPercent": 25.0},
            )
        )
        effect.assumptions.append("蕾缪安/订单分发按与能天使同贸易站额外 +25% 计入。")

    if "当与" in description and "在同一个贸易站" in description:
        if any(name in description for name in room_names):
            if include_speed:
                speed = max(skill.parsed_score, max_percent(description))
                effect.speed_percent += speed
                effect.skill_effect_audit.append(
                    skill_audit(
                        skill,
                        "counted",
                        "same_room_condition",
                        "Named same-room condition is satisfied and counted.",
                        "modeled_rule",
                        {"speedPercent": speed},
                    )
                )
            effect.assumptions.append(f"{skill.operator_name}/{skill.buff_name} 的同站条件已满足并计入。")
        else:
            mark_unsupported(effect, skill, "同站条件未满足，未计入条件收益。")
    if "违约订单" in description:
        mark_unsupported(
            effect,
            skill,
            "违约订单收益需要订单分布模型；当前仅标记，不直接改变产出。校准贸易档案可能会单独处理房间总产出。",
        )
        if "赤金交付数额外+2" in description:
            effect.lmd_per_order_bonus += 1000.0
            effect.assumptions.append("但书违约索赔按每单额外 2 赤金等价 1000 龙门币估算。")
            effect.skill_effect_audit.append(
                skill_audit(
                    skill,
                    "counted",
                    "lmd_order_bonus",
                    "Per-order LMD-equivalent bonus is counted where the text exposes a fixed value.",
                    "modeled_rule",
                    {"lmdPerOrderBonus": 1000.0},
                )
            )
    if "固定获取" in description and "特别订单" in description:
        effect.fixed_special_order = skill.operator_name
        mark_unsupported(effect, skill, "特别订单真实收益未在公开 building_data 中给出，按基础订单速度估算。")
    if "不受任何订单获取效率的影响" in description:
        effect.speed_percent = 0.0
        effect.assumptions.append("佩佩特别独占订单按不受订单效率影响处理。")
        effect.skill_effect_audit.append(
            skill_audit(
                skill,
                "explicitly_excluded",
                "room_speed",
                "Skill text states the order is not affected by order acquisition speed.",
                "modeled_exclusion",
            )
        )
    if should_mark_unsupported(skill):
        mark_unsupported(effect, skill, "复杂条件效果尚未完全建模。")
    ensure_complex_skill_is_audited(skill, effect)
    return effect


def evaluate_faction_skill(
    skill: BaseSkill,
    target: str | None,
    room_names: set[str],
    room_factions: dict[str, int],
    context: SkillContext,
    *,
    include_speed: bool,
) -> RoomSkillEffect | None:
    text = f"{skill.buff_name} {skill.description}"
    effect = RoomSkillEffect()
    if skill.room_type == "TRADING":
        if "帮派指南针" in text:
            if include_speed:
                glasgow = room_factions.get("glasgow", 0)
                effect.speed_percent += 20.0 * glasgow
                if any(name in room_names for name in {"推进之王", "维娜·维多利亚"}):
                    effect.speed_percent += 35.0
                effect.assumptions.append(
                    f"{skill.operator_name}/帮派指南针按同站格拉斯哥帮 {glasgow} 人计入。"
                )
            return effect
        if "同城加急单" in text:
            if include_speed:
                laterano = room_factions.get("laterano", 0)
                effect.speed_percent += 15.0 * laterano
                effect.assumptions.append(
                    f"{skill.operator_name}/同城加急单按同站拉特兰 {laterano} 人计入。"
                )
            return effect
        if "外贸决议" in text:
            if include_speed:
                effect.speed_percent += 30.0
                if room_factions.get("glasgow", 0) > 0:
                    effect.speed_percent += 10.0
                effect.assumptions.append(f"{skill.operator_name}/外贸决议按格拉斯哥帮同站条件计入。")
            return effect
        if "队长的自觉" in text:
            effect.order_limit_bonus += 3.0
            if include_speed:
                count = room_factions.get("mh2", 0)
                effect.speed_percent += 20.0 * count
                effect.assumptions.append(
                    f"{skill.operator_name}/队长的自觉按泡影国狩猎小队 {count} 人计入。"
                )
            return effect

    if skill.room_type == "MANUFACTURE":
        if "患难拍档" in text:
            if include_speed and target == "F_EXP" and context.operator_rooms.get("古米") == "TRADING":
                effect.speed_percent += 35.0
                effect.assumptions.append(f"{skill.operator_name}/患难拍档按古米进驻贸易站计入。")
            return effect
        if "重聚时光" in text:
            if include_speed:
                count = room_factions.get("A1", 0)
                effect.speed_percent += 10.0 * count
                effect.assumptions.append(
                    f"{skill.operator_name}/重聚时光按同站 A1 小队 {count} 人计入。"
                )
            return effect
        if "情同手足" in text:
            if include_speed and target == "F_EXP":
                effect.speed_percent += 30.0
                if room_factions.get("ussg", 0) > 0:
                    effect.speed_percent += 10.0
                effect.assumptions.append(f"{skill.operator_name}/情同手足按乌萨斯学生自治团同站条件计入。")
            return effect
        if "挑大梁" in text:
            if include_speed and target == "F_GOLD":
                count = min(context.active_faction_counts.get("blacksteel", 0), 3)
                effect.speed_percent += 2.0 * count
                effect.assumptions.append(f"{skill.operator_name}/挑大梁按全基建黑钢国际 {count} 人计入。")
            effect.fatigue_delta_per_hour -= 0.15
            return effect
        if "造价高昂" in text:
            if include_speed and target == "F_GOLD":
                count = min(context.active_faction_counts.get("rhine", 0), 5)
                effect.speed_percent += 3.0 * count
                effect.assumptions.append(f"{skill.operator_name}/造价高昂按全基建莱茵生命 {count} 人计入。")
            return effect
        if "机械精通" in text:
            if include_speed and target == "F_GOLD":
                per_platform = 10.0 if "β" in text else 5.0
                count = context.power_faction_counts.get("op", 0)
                effect.speed_percent += per_platform * count
                effect.assumptions.append(
                    f"{skill.operator_name}/机械精通按发电站作业平台 {count} 台计入。"
                )
            return effect

    if skill.room_type == "POWER":
        if "维护中" in text:
            if include_speed and context.power_faction_counts.get("laterano", 0) > own_faction_count(skill, "laterano"):
                effect.speed_percent += 5.0
                effect.assumptions.append(f"{skill.operator_name}/维护中按其他拉特兰发电站干员计入。")
            return effect
        if "鸡励机制" in text:
            if include_speed and context.power_faction_counts.get("op", 0) > own_faction_count(skill, "op"):
                effect.speed_percent += 5.0
                effect.assumptions.append(f"{skill.operator_name}/鸡励机制按其他作业平台发电站干员计入。")
            return effect
        if "生态科主任" in text:
            if include_speed:
                count = max(0, context.active_faction_counts.get("rhine", 0) - own_faction_count(skill, "rhine"))
                count = min(count, 5)
                effect.speed_percent += 10.0 + 3.0 * count
                effect.assumptions.append(f"{skill.operator_name}/生态科主任按额外莱茵生命 {count} 人计入。")
            return effect
    return None


def faction_counts(skills: list[BaseSkill]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for skill in skills:
        for tag in skill.faction_tags:
            counts[tag] = counts.get(tag, 0) + 1
    return counts


def own_faction_count(skill: BaseSkill, tag: str) -> int:
    return 1 if tag in skill.faction_tags else 0


def capacity_bonus_from_text(text: str) -> float:
    return sum(float(match) for match in re.findall(r"仓库容量上限\+(\d+(?:\.\d+)?)", text))


def order_limit_bonus_from_text(text: str) -> float:
    total = sum(float(match) for match in re.findall(r"订单上限\+(\d+(?:\.\d+)?)", text))
    # Pepe's "每级 +1 个订单上限" in a level-3 trading post.
    if "每级" in text and "订单上限" in text:
        total += 3.0
    return total


def order_weight_level_from_skill(skill: BaseSkill) -> int:
    text = f"{skill.buff_id} {skill.buff_name} {skill.description}"
    if "trade_ord_wt" not in text and "高品质贵金属订单" not in text:
        return 0
    if "β" in text or "提升当前贸易站高品质贵金属订单" in text and "小幅" not in text:
        return 2
    return 1


def fatigue_delta_from_text(text: str) -> float:
    total = 0.0
    patterns = (
        r"心情每小时(消耗|恢复)<[^>]*>([+-])(\d+(?:\.\d+)?)",
        r"心情每小时(消耗|恢复)([+-])(\d+(?:\.\d+)?)",
    )
    for pattern in patterns:
        for action, sign, value in re.findall(pattern, text):
            number = float(value)
            signed = number if sign == "+" else -number
            total += -signed if action == "恢复" else signed
    return total


def conditional_morale_unmet_reason(
    skill: BaseSkill,
    description: str,
    room_names: set[str],
    room_factions: dict[str, int],
) -> str | None:
    if "心情每小时" not in description or "当与" not in description:
        return None
    if "萨尔贡干员" in description and "进驻控制中枢一起工作" in description:
        if room_factions.get("sargon", 0) <= own_faction_count(skill, "sargon"):
            return "萨尔贡干员未同驻控制中枢，条件心情变化未计入。"
        return None
    partner = named_same_room_condition(description)
    if partner and partner not in room_names:
        return f"{partner}未同驻同一房间，条件心情变化未计入。"
    return None


def named_same_room_condition(description: str) -> str | None:
    patterns = (
        r"当与([^，；。]+?)在同一个",
        r"当与([^，；。]+?)一起进驻控制中枢",
        r"当与([^，；。]+?)进驻控制中枢一起工作",
        r"当与([^，；。]+?)进驻会客室一起工作",
    )
    for pattern in patterns:
        match = re.search(pattern, description)
        if match:
            return match.group(1).strip()
    return None


def max_percent(text: str) -> float:
    values = [float(match) for match in re.findall(r"\+(\d+(?:\.\d+)?)%", text)]
    return max(values, default=0.0)


def should_mark_unsupported(skill: BaseSkill) -> bool:
    text = skill.description
    markers = ("人间烟火", "感知信息", "巫术结晶", "乌萨斯特饮", "线索板", "更容易获得")
    return any(marker in text for marker in markers)


def unsupported(skill: BaseSkill, reason: str) -> dict[str, str]:
    return {
        "operator": skill.operator_name,
        "buffId": skill.buff_id,
        "buffName": skill.buff_name,
        "reason": reason,
    }


def skill_audit(
    skill: BaseSkill,
    status: str,
    scope: str,
    reason: str,
    confidence: str,
    numeric_contribution: dict[str, float] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "operator": skill.operator_name,
        "buffId": skill.buff_id,
        "buffName": skill.buff_name,
        "status": status,
        "scope": scope,
        "reason": reason,
        "confidence": confidence,
    }
    if numeric_contribution:
        item["numericContribution"] = {
            key: round(value, 3)
            for key, value in numeric_contribution.items()
            if abs(value) > 0.0001
        }
    if skill.upgrade is not None:
        item["hasUpgradeRequirement"] = True
    return item


def mark_unsupported(effect: RoomSkillEffect, skill: BaseSkill, reason: str) -> None:
    effect.unsupported.append(unsupported(skill, reason))
    effect.skill_effect_audit.append(
        skill_audit(skill, "unsupported", skill.room_type, reason, "unmodeled")
    )


def ensure_complex_skill_is_audited(skill: BaseSkill, effect: RoomSkillEffect) -> None:
    if effect.skill_effect_audit or effect.unsupported:
        return
    numeric = numeric_contribution_from_effect(effect)
    if numeric:
        effect.skill_effect_audit.append(
            skill_audit(
                skill,
                "counted",
                skill.room_type,
                "Modeled complex rule contributed numeric effect in this room evaluation.",
                "modeled_rule",
                numeric,
            )
        )
        return
    if not should_audit_unmodeled_complex_skill(skill):
        return
    effect.skill_effect_audit.append(
        skill_audit(
            skill,
            "diagnostic_only",
            skill.room_type,
            "Complex or upgrade-only skill produced no modeled numeric effect in this room evaluation.",
            "diagnostic",
        )
    )


def numeric_contribution_from_effect(effect: RoomSkillEffect) -> dict[str, float]:
    values = {
        "speedPercent": effect.speed_percent,
        "capacityBonus": effect.capacity_bonus,
        "orderLimitBonus": effect.order_limit_bonus,
        "fatigueDeltaPerHour": effect.fatigue_delta_per_hour,
        "lmdPerOrderBonus": effect.lmd_per_order_bonus,
        "orderWeightLevel": float(effect.order_weight_level),
    }
    return {key: value for key, value in values.items() if abs(value) > 0.0001}


def should_audit_unmodeled_complex_skill(skill: BaseSkill) -> bool:
    if skill.upgrade is not None:
        return True
    if not skill.complex_condition:
        return False
    text = f"{skill.buff_name} {skill.description}".lower()
    markers = (
        "if",
        "when",
        "condition",
        "upgrade-only",
        "当与",
        "如果",
        "每有",
        "每个",
        "同一",
        "宿舍",
        "特别订单",
        "违约订单",
        "人间烟火",
    )
    return any(marker in text for marker in markers)
