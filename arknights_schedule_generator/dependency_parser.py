from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import BaseSkill


OPERATOR_ALIASES: dict[str, tuple[str, ...]] = {
    "Vina": ("Vina Victoria", "Siege", "维娜·维多利亚", "推进之王"),
    "Siege": ("Vina", "Vina Victoria", "维娜·维多利亚", "推进之王"),
    "Vina Victoria": ("Vina", "Siege", "维娜·维多利亚", "推进之王"),
    "推进之王": ("维娜·维多利亚", "Siege", "Vina Victoria", "Vina"),
    "维娜·维多利亚": ("推进之王", "Siege", "Vina Victoria", "Vina"),
}


@dataclass(frozen=True)
class DependencySpec:
    room_type: str
    target: str | None
    operators: tuple[str, ...]


@dataclass(frozen=True)
class FactionDependency:
    room_type: str
    target: str | None
    faction_tag: str
    source: str
    confidence: str = "high"


def explain_named_dependency(skill: BaseSkill, partner: str) -> dict[str, Any]:
    text = f"{skill.buff_name} {skill.description}"
    trigger_target = first_skill_target(skill)
    same_room = same_room_dependency(text)
    if same_room:
        room_type, target = same_room
        return {
            "relation": "same_room",
            "summary": f"{skill.operator_name} 的技能文本点名 {partner}，关系解析为同一房间依赖。",
            "triggerRoomType": room_type,
            "partnerRoomType": room_type,
            "target": target or trigger_target,
            "confidence": "high",
        }

    partner_room = named_partner_room_type(text, partner)
    if partner_room:
        return {
            "relation": "cross_room",
            "summary": f"{skill.operator_name} 的技能文本点名 {partner}，关系解析为跨房间同班上场。",
            "triggerRoomType": skill.room_type,
            "partnerRoomType": partner_room,
            "triggerTarget": trigger_target,
            "partnerTarget": default_target_for_room(partner_room),
            "confidence": "medium",
        }

    return {
        "relation": "same_shift",
        "summary": f"{skill.operator_name} 的技能文本点名 {partner}，但未能确认具体房间关系；仅作为诊断提示。",
        "triggerRoomType": skill.room_type,
        "triggerTarget": trigger_target,
        "confidence": "low",
    }


def explain_same_room_faction_dependency(skill: BaseSkill) -> FactionDependency | None:
    if skill.room_type != "TRADING":
        return None
    text = f"{skill.buff_id} {skill.buff_name} {skill.description}".lower()
    if "帮派指南针" in text or "外贸决议" in text:
        return FactionDependency(
            room_type="TRADING",
            target="O_GOLD",
            faction_tag="glasgow",
            source="same_room_faction_dependency",
        )
    return None


def force_specs_from_explanation(
    explanation: dict[str, Any],
    *,
    operator: str,
    partner: str,
) -> list[DependencySpec]:
    relation = explanation.get("relation")
    if relation == "same_room":
        room_type = str(explanation.get("triggerRoomType") or "")
        if not room_type:
            return []
        return [DependencySpec(room_type, explanation.get("target"), (operator, partner))]
    if relation == "cross_room":
        trigger_room = str(explanation.get("triggerRoomType") or "")
        partner_room = str(explanation.get("partnerRoomType") or "")
        if not trigger_room or not partner_room:
            return []
        return [
            DependencySpec(trigger_room, explanation.get("triggerTarget"), (operator,)),
            DependencySpec(partner_room, explanation.get("partnerTarget"), (partner,)),
        ]
    return []


def mentioned_operator_names(text: str, roster_names: set[str]) -> list[str]:
    names = {
        name
        for name in roster_names
        if len(name) >= 2 and name in text
    }
    for canonical, aliases in OPERATOR_ALIASES.items():
        if canonical not in roster_names:
            continue
        if any(alias in text for alias in aliases):
            names.add(canonical)
    return sorted(
        names,
        key=len,
        reverse=True,
    )


def first_skill_target(skill: BaseSkill) -> str | None:
    return next(iter(skill.targets), None)


def same_room_dependency(text: str) -> tuple[str, str | None] | None:
    if "同一个贸易站" in text or "同一贸易站" in text:
        return ("TRADING", "O_GOLD")
    if "同一个制造站" in text or "同一制造站" in text:
        return ("MANUFACTURE", None)
    return None


def named_partner_room_type(text: str, partner: str) -> str | None:
    matched_name = first_text_name_for_operator(text, partner)
    if matched_name is None:
        return None
    partner_end = text.find(matched_name) + len(matched_name)
    window = text[partner_end:min(len(text), partner_end + 28)]
    if "基建内" in window:
        return None
    room_markers = (
        ("贸易站", "TRADING"),
        ("制造站", "MANUFACTURE"),
        ("发电站", "POWER"),
        ("控制中枢", "CONTROL"),
        ("中枢", "CONTROL"),
        ("办公室", "HIRE"),
        ("会客室", "MEETING"),
        ("宿舍", "DORMITORY"),
    )
    matches = [
        (window.find(marker), room_type)
        for marker, room_type in room_markers
        if marker in window
    ]
    if matches:
        return min(matches, key=lambda item: item[0])[1]
    return None


def first_text_name_for_operator(text: str, operator_name: str) -> str | None:
    names = (operator_name, *OPERATOR_ALIASES.get(operator_name, ()))
    matches = [name for name in names if name in text]
    if not matches:
        return None
    return min(matches, key=text.find)


def default_target_for_room(room_type: str) -> str | None:
    if room_type == "TRADING":
        return "O_GOLD"
    return None
