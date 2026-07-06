from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from .data import GameData
from .models import BaseSkill, RoomAssignment, ShiftPlan
from .room_limits import clamp_station_count, station_limit
from .skill_rules import (
    RoomSkillEffect,
    SkillContext,
    evaluate_room_effect,
    evaluate_skill,
    faction_counts,
    own_faction_count,
    skill_audit,
    unsupported,
)


EXP_VALUE = {
    "2001": 200.0,
    "2002": 400.0,
    "2003": 1000.0,
    "2004": 2000.0,
}

DEFAULT_FORMULAS = {
    "F_EXP": {"costPoint": 10800.0, "itemId": "2003", "count": 1.0},
    "F_GOLD": {"costPoint": 4320.0, "itemId": "3003", "count": 1.0},
    "F_DIAMOND_ROCK": {
        "costPoint": 3600.0,
        "itemId": "3141",
        "count": 1.0,
        "costs": {"30012": 2.0, "4001": 1600.0},
    },
    "F_DIAMOND_DEVICE": {
        "costPoint": 3600.0,
        "itemId": "3141",
        "count": 1.0,
        "costs": {"30062": 1.0, "4001": 1000.0},
    },
}

LOW_ORDER_SECONDS = 2 * 3600.0 + 24 * 60.0
MEDIUM_ORDER_SECONDS = 3 * 3600.0 + 30 * 60.0
HIGH_ORDER_SECONDS = 4 * 3600.0 + 36 * 60.0
TRADE_BASE_SECONDS = LOW_ORDER_SECONDS
GOLD_PER_LMD_ORDER = 2.0
LMD_PER_GOLD_ORDER = 1000.0
SHARDS_PER_ORUNDUM_ORDER = 2.0
ORUNDUM_PER_SHARD_ORDER = 20.0
ORUNDUM_ORDER_SECONDS = 2 * 3600.0
DEFAULT_BASIC_SPEED_BUFF = 0.01
DAILY_DRONES = 240.0
DRONE_SECONDS = 180.0
YITULIU_CONTROL_TRADE_SPEED_PERCENT = 7.0
DRONE_BASE_DAILY_SECONDS = DAILY_DRONES * DRONE_SECONDS
DEFAULT_PURE_GOLD_TARGET_PER_DAY = -2.0
DEFAULT_PURE_GOLD_TOLERANCE = 0.5
DEFAULT_MAX_DRONE_CYCLE_REPEATS = 7

GUIDE_LMD_TRADE_LOOKUPS = (
    {
        "id": "shamare_tequila_butushu_l3",
        "level": 3,
        "required": {"巫恋", "龙舌兰", "但书"},
        "baseLmdPer24h": 14772.0,
        "baseGoldPer24h": 26.2,
        "paperEfficiencyPercent": 200.0,
        "scheduleEffectivePercent": 207.0,
        "source": "Yituliu 2026-06 trade lookup: 3级贸易站 巫恋+龙舌兰+但书.",
    },
    {
        "id": "shamare_tailor_beta_tequila_l3",
        "level": 3,
        "required": {"巫恋", "柏喙", "龙舌兰"},
        "baseLmdPer24h": 12740.0,
        "baseGoldPer24h": 20.8,
        "paperEfficiencyPercent": 200.0,
        "scheduleEffectivePercent": 200.0,
        "source": "Yituliu 2026-06 trade lookup: 3级贸易站 巫恋+裁缝β+龙舌兰.",
    },
    {
        "id": "croissant_l3_plus_two_45",
        "level": 3,
        "required": {"可露希尔"},
        "partnerScoreMinimums": (45.0, 45.0),
        "baseLmdPer24h": 12000.0,
        "baseGoldPer24h": 20.0,
        "paperEfficiencyPercent": 210.0,
        "scheduleEffectivePercent": 210.0,
        "source": "Yituliu 2026-06 trade lookup: 3级贸易站 可露希尔+2个45%.",
    },
    {
        "id": "butushu_l3_plus_two_45",
        "level": 3,
        "required": {"但书"},
        "partnerScoreMinimums": (45.0, 45.0),
        "baseLmdPer24h": 15929.0,
        "baseGoldPer24h": 31.9,
        "paperEfficiencyPercent": 200.0,
        "scheduleEffectivePercent": 200.0,
        "source": "Yituliu 2026-06 trade lookup: 3级贸易站 但书+2个45%.",
    },
    {
        "id": "croissant_l3_plus_two_40",
        "level": 3,
        "required": {"可露希尔"},
        "partnerScoreMinimums": (40.0, 40.0),
        "baseLmdPer24h": 12000.0,
        "baseGoldPer24h": 20.0,
        "paperEfficiencyPercent": 200.0,
        "scheduleEffectivePercent": 200.0,
        "source": "Yituliu 2026-06 trade lookup: 3级贸易站 可露希尔+2个40%.",
    },
    {
        "id": "butushu_l3_plus_two_40",
        "level": 3,
        "required": {"但书"},
        "partnerScoreMinimums": (40.0, 40.0),
        "baseLmdPer24h": 15929.0,
        "baseGoldPer24h": 31.9,
        "paperEfficiencyPercent": 190.0,
        "scheduleEffectivePercent": 190.0,
        "source": "Yituliu 2026-06 trade lookup: 3级贸易站 但书+2个40%.",
    },
    {
        "id": "generic_l3_two_45_one_40",
        "level": 3,
        "required": set(),
        "partnerScoreMinimums": (45.0, 45.0, 40.0),
        "baseLmdPer24h": 10265.0,
        "baseGoldPer24h": 20.5,
        "paperEfficiencyPercent": 240.0,
        "scheduleEffectivePercent": 240.0,
        "source": "Yituliu 2026-06 trade lookup: 3级贸易站 2个45%+1个40%.",
    },
    {
        "id": "generic_l3_three_40",
        "level": 3,
        "required": set(),
        "partnerScoreMinimums": (40.0, 40.0, 40.0),
        "baseLmdPer24h": 10265.0,
        "baseGoldPer24h": 20.5,
        "paperEfficiencyPercent": 230.0,
        "scheduleEffectivePercent": 230.0,
        "source": "Yituliu 2026-06 trade lookup: 3级贸易站 3个40%.",
    },
    {
        "id": "butushu_l2_plus_45",
        "level": 2,
        "required": {"但书"},
        "partnerScoreMinimums": (45.0,),
        "baseLmdPer24h": 18592.0,
        "baseGoldPer24h": 37.2,
        "paperEfficiencyPercent": 154.0,
        "scheduleEffectivePercent": 154.0,
        "source": "Yituliu 2026-06 trade lookup: 2级贸易站 但书+45%.",
    },
    {
        "id": "butushu_l2_plus_40",
        "level": 2,
        "required": {"但书"},
        "partnerScoreMinimums": (40.0,),
        "baseLmdPer24h": 18592.0,
        "baseGoldPer24h": 37.2,
        "paperEfficiencyPercent": 149.0,
        "scheduleEffectivePercent": 149.0,
        "source": "Yituliu 2026-06 trade lookup: 2级贸易站 但书+40%.",
    },
    {
        "id": "croissant_l2_plus_45",
        "level": 2,
        "required": {"可露希尔"},
        "partnerScoreMinimums": (45.0,),
        "baseLmdPer24h": 12000.0,
        "baseGoldPer24h": 20.0,
        "paperEfficiencyPercent": 164.0,
        "scheduleEffectivePercent": 164.0,
        "source": "Yituliu 2026-06 trade lookup: 2级贸易站 可露希尔+45%.",
    },
    {
        "id": "croissant_l2_plus_40",
        "level": 2,
        "required": {"可露希尔"},
        "partnerScoreMinimums": (40.0,),
        "baseLmdPer24h": 12000.0,
        "baseGoldPer24h": 20.0,
        "paperEfficiencyPercent": 159.0,
        "scheduleEffectivePercent": 159.0,
        "source": "Yituliu 2026-06 trade lookup: 2级贸易站 可露希尔+40%.",
    },
    {
        "id": "generic_l2_two_40",
        "level": 2,
        "required": set(),
        "partnerScoreMinimums": (40.0, 40.0),
        "baseLmdPer24h": 10141.0,
        "baseGoldPer24h": 20.3,
        "paperEfficiencyPercent": 189.0,
        "scheduleEffectivePercent": 189.0,
        "source": "Yituliu 2026-06 trade lookup: 2级贸易站 2个40%.",
    },
    {
        "id": "butushu_l1",
        "level": 1,
        "required": {"但书"},
        "baseLmdPer24h": 20000.0,
        "baseGoldPer24h": 40.0,
        "paperEfficiencyPercent": 108.0,
        "scheduleEffectivePercent": 108.0,
        "source": "Yituliu 2026-06 trade lookup: 1级贸易站 但书.",
    },
    {
        "id": "croissant_l1",
        "level": 1,
        "required": {"可露希尔"},
        "baseLmdPer24h": 12000.0,
        "baseGoldPer24h": 20.0,
        "paperEfficiencyPercent": 118.0,
        "scheduleEffectivePercent": 118.0,
        "source": "Yituliu 2026-06 trade lookup: 1级贸易站 可露希尔.",
    },
    {
        "id": "generic_l1_40",
        "level": 1,
        "required": set(),
        "partnerScoreMinimums": (40.0,),
        "baseLmdPer24h": 10000.0,
        "baseGoldPer24h": 20.0,
        "paperEfficiencyPercent": 148.0,
        "scheduleEffectivePercent": 148.0,
        "source": "Yituliu 2026-06 trade lookup: 1级贸易站 40%.",
    },
)

GUIDE_ORUNDUM_TRADE_LOOKUPS = (
    {
        "id": "orundum_trade_l3_2026_06_static",
        "level": 3,
        "paperEfficiencyPercent": 132.0,
        "source": (
            "Yituliu 2026-06 Orundum trade room summary: level-3 Orundum Trading Post "
            "paper efficiencies around 130-135%."
        ),
    },
)

GUIDE_SPECIAL_TRADE_ANCHORS: dict[str, dict[str, Any]] = {
    "但书": {
        "mechanismId": "butushu_contract_order",
        "baseLmdPer24h": {1: 20000.0, 2: 18592.0, 3: 15929.0},
        "baseGoldPer24h": {1: 40.0, 2: 37.2, 3: 31.9},
        "intrinsicSpeedPercent": {1: 8.0, 2: 9.0, 3: 10.0},
        "matchedAnchorRows": (
            "butushu_l1",
            "butushu_l2_plus_40",
            "butushu_l2_plus_45",
            "butushu_l3_plus_two_40",
            "butushu_l3_plus_two_45",
        ),
        "confidence": "guide_anchor_estimate",
        "source": "Yituliu 2026-06 trade lookup rows for Proviso/Butushu special contract orders.",
    },
    "可露希尔": {
        "mechanismId": "croissant_special_order",
        "baseLmdPer24h": {1: 12000.0, 2: 12000.0, 3: 12000.0},
        "baseGoldPer24h": {1: 20.0, 2: 20.0, 3: 20.0},
        "intrinsicSpeedPercent": {1: 18.0, 2: 19.0, 3: 20.0},
        "matchedAnchorRows": (
            "croissant_l1",
            "croissant_l2_plus_40",
            "croissant_l2_plus_45",
            "croissant_l3_plus_two_40",
            "croissant_l3_plus_two_45",
        ),
        "confidence": "guide_anchor_estimate",
        "source": "Yituliu 2026-06 trade lookup rows for Croissant special orders.",
    },
}

GUIDE_MANUFACTURE_LOOKUPS = (
    {
        "id": "maxed_exp_reference_l3_miss_christine",
        "target": "F_EXP",
        "level": 3,
        "slots": 3,
        "required": {"Miss.Christine", "食铁兽", "弑君者"},
        "paperEfficiencyPercent": 140.0,
        "source": "Yituliu 2026-06 243 Orundum manufacture summary: level-3 EXP paper efficiency 140%.",
    },
    {
        "id": "maxed_exp_reference_252_room5_generic_126",
        "target": "F_EXP",
        "level": 3,
        "slots": 3,
        "roomIds": {"manufacture_5"},
        "required": set(),
        "minimumParsedSpeedPercent": 75.0,
        "paperEfficiencyPercent": 126.0,
        "source": "Yituliu 2026-06 252 right-side manufacture summary: level-3 EXP room paper efficiencies around 122-144%.",
    },
    {
        "id": "maxed_exp_reference_l3_generic_126",
        "target": "F_EXP",
        "level": 3,
        "slots": 3,
        "required": set(),
        "minimumParsedSpeedPercent": 85.0,
        "paperEfficiencyPercent": 126.0,
        "source": "Yituliu 2026-06 243 Orundum manufacture summary: companion level-3 EXP paper efficiency 126%.",
    },
)


@dataclass(frozen=True)
class LmdOrder:
    gold: float
    lmd: float
    seconds: float
    probability: float


@dataclass(frozen=True)
class TradeOrderProfile:
    target: str
    level: int
    expected_seconds: float
    expected_lmd: float = 0.0
    expected_gold: float = 0.0
    expected_orundum: float = 0.0
    expected_shards: float = 0.0
    distribution: list[dict[str, float]] = field(default_factory=list)
    source: str = ""
    bias: str = "default"


@dataclass(frozen=True)
class SpecialTradeEstimate:
    mechanism_id: str
    anchor_operator: str
    level: int
    base_lmd_per_24h: float
    base_gold_per_24h: float
    intrinsic_speed_percent: float
    partner_speed_percent: float
    effective_percent: float
    confidence: str
    source: str
    matched_anchor_rows: tuple[str, ...]
    partner_contributions: tuple[dict[str, Any], ...] = ()
    ignored_partner_notes: tuple[str, ...] = ()


@dataclass
class ProductionVector:
    lmdGross: float = 0.0
    lmdNet: float = 0.0
    exp: float = 0.0
    orundum: float = 0.0
    officeSpeed: float = 0.0
    meetingSpeed: float = 0.0
    pureGoldDelta: float = 0.0
    shardDelta: float = 0.0
    materialCosts: dict[str, float] = field(default_factory=dict)
    overflowLoss: float = 0.0
    fatigueRisk: float = 0.0
    droneContribution: dict[str, float] = field(default_factory=dict)
    droneCount: float = 0.0
    droneUsed: float = 0.0
    droneGenerationBonusPercent: float = 0.0
    droneTarget: dict[str, Any] = field(default_factory=dict)
    droneTargets: list[dict[str, Any]] = field(default_factory=list)
    cycleHours: float = 24.0
    dailyScale: float = 1.0

    def add(self, other: "ProductionVector") -> None:
        self.lmdGross += other.lmdGross
        self.lmdNet += other.lmdNet
        self.exp += other.exp
        self.orundum += other.orundum
        self.officeSpeed += other.officeSpeed
        self.meetingSpeed += other.meetingSpeed
        self.pureGoldDelta += other.pureGoldDelta
        self.shardDelta += other.shardDelta
        self.overflowLoss += other.overflowLoss
        self.fatigueRisk += other.fatigueRisk
        for item_id, count in other.materialCosts.items():
            self.materialCosts[item_id] = self.materialCosts.get(item_id, 0.0) + count
        for key, value in other.droneContribution.items():
            self.droneContribution[key] = self.droneContribution.get(key, 0.0) + value
        if other.droneCount:
            self.droneCount += other.droneCount
        if other.droneUsed:
            self.droneUsed += other.droneUsed
        if other.droneGenerationBonusPercent:
            self.droneGenerationBonusPercent = other.droneGenerationBonusPercent
        if other.droneTarget:
            self.droneTarget = dict(other.droneTarget)
        if other.droneTargets:
            self.droneTargets.extend(dict(target) for target in other.droneTargets)

    def scale(self, factor: float) -> None:
        self.lmdGross *= factor
        self.lmdNet *= factor
        self.exp *= factor
        self.orundum *= factor
        self.officeSpeed *= factor
        self.meetingSpeed *= factor
        self.pureGoldDelta *= factor
        self.shardDelta *= factor
        self.overflowLoss *= factor
        self.fatigueRisk *= factor
        for item_id in list(self.materialCosts):
            self.materialCosts[item_id] *= factor
        for key in list(self.droneContribution):
            self.droneContribution[key] *= factor
        self.droneCount *= factor
        self.droneUsed *= factor

    def to_dict(self) -> dict[str, Any]:
        return {
            "lmdGross": round(self.lmdGross, 2),
            "lmdNet": round(self.lmdNet, 2),
            "exp": round(self.exp, 2),
            "orundum": round(self.orundum, 2),
            "officeSpeed": round(self.officeSpeed, 3),
            "meetingSpeed": round(self.meetingSpeed, 3),
            "pureGoldDelta": round(self.pureGoldDelta, 3),
            "shardDelta": round(self.shardDelta, 3),
            "materialCosts": {k: round(v, 3) for k, v in sorted(self.materialCosts.items())},
            "overflowLoss": round(self.overflowLoss, 3),
            "fatigueRisk": round(self.fatigueRisk, 3),
            "droneContribution": {
                k: round(v, 3) for k, v in sorted(self.droneContribution.items())
            },
            "droneCount": round(self.droneCount, 3),
            "droneUsed": round(self.droneUsed, 3),
            "droneGenerationBonusPercent": round(self.droneGenerationBonusPercent, 3),
            "droneTarget": self.droneTarget,
            "droneTargets": self.droneTargets,
            "cycleHours": round(self.cycleHours, 3),
            "dailyScale": round(self.dailyScale, 6),
        }


@dataclass
class RoomProduction:
    roomId: str
    roomType: str
    target: str | None
    vector: ProductionVector
    effect: RoomSkillEffect
    producedUnits: float = 0.0
    cappedUnits: float = 0.0
    capacity: float | None = None
    baseSpeedPercent: float = 0.0
    stationSlots: int = 0
    roomLevel: int = 3
    collectionIntervalHours: float = 0.0
    assumptions: list[str] = field(default_factory=list)
    tradeOrderProfile: dict[str, Any] | None = None
    manufactureProfile: dict[str, Any] | None = None
    skillEffectAudit: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "roomId": self.roomId,
            "roomType": self.roomType,
            "target": self.target,
            "producedUnits": round(self.producedUnits, 3),
            "cappedUnits": round(self.cappedUnits, 3),
            "capacity": None if self.capacity is None else round(self.capacity, 3),
            "baseSpeedPercent": round(self.baseSpeedPercent, 3),
            "stationSlots": self.stationSlots,
            "roomLevel": self.roomLevel,
            "collectionIntervalHours": round(self.collectionIntervalHours, 3),
            "vector": self.vector.to_dict(),
            "speedPercent": round(self.effect.speed_percent, 3),
            "capacityBonus": round(self.effect.capacity_bonus, 3),
            "orderLimitBonus": round(self.effect.order_limit_bonus, 3),
            "orderWeightLevel": self.effect.order_weight_level,
            "tradeOrderProfile": self.tradeOrderProfile,
            "manufactureProfile": self.manufactureProfile,
            "assumptions": self.assumptions + self.effect.assumptions,
            "unsupportedSkillEffects": self.effect.unsupported,
            "skillEffectAudit": dedupe_skill_effect_audit(
                self.effect.skill_effect_audit + self.skillEffectAudit
            ),
        }


@dataclass(frozen=True)
class ControlGlobalEffect:
    trading_speed_percent: float = 0.0
    manufacture_speed_percent: float = 0.0
    gold_manufacture_speed_percent: float = 0.0
    blacksteel_manufacture_speed_per_operator: float = 0.0
    karlan_trading_speed_per_operator: float = 0.0
    karlan_trading_order_limit_per_operator: float = 0.0
    siracusa_trading_speed_per_operator: float = 0.0
    glasgow_trading_speed_per_operator: float = 0.0
    karlan_three_trading_speed_percent: float = 0.0
    pinus_exp_manufacture_speed_per_operator: float = 0.0
    pinus_gold_manufacture_speed_per_operator: float = 0.0
    knight_manufacture_speed_per_operator: float = 0.0
    meeting_speed_percent: float = 0.0
    assumptions: tuple[str, ...] = ()
    assumption_map: dict[str, tuple[str, ...]] = field(default_factory=dict)
    audit_map: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    control_audit: tuple[dict[str, Any], ...] = ()
    unsupported: tuple[dict[str, str], ...] = ()

    def bonus_for(self, room: RoomAssignment) -> tuple[float, float, list[str], list[dict[str, Any]]]:
        room_factions = faction_counts(room.operators)
        bonus = 0.0
        order_limit = 0.0
        assumptions: list[str] = []
        audit: list[dict[str, Any]] = []
        if room.room_type == "TRADING":
            if self.trading_speed_percent:
                bonus += self.trading_speed_percent
                assumptions.extend(self.assumption_map.get("trade_global", ()))
                audit.extend(self.audit_map.get("trade_global", ()))
            karlan = room_factions.get("karlan", 0)
            if karlan:
                if self.karlan_trading_speed_per_operator:
                    bonus += self.karlan_trading_speed_per_operator * karlan
                    assumptions.extend(self.assumption_map.get("karlan_trade", ()))
                    audit.extend(self.audit_map.get("karlan_trade", ()))
                if self.karlan_trading_order_limit_per_operator:
                    order_limit += self.karlan_trading_order_limit_per_operator * karlan
                    assumptions.extend(self.assumption_map.get("karlan_trade", ()))
                    audit.extend(self.audit_map.get("karlan_trade", ()))
            siracusa = room_factions.get("siracusa", 0)
            if siracusa and self.siracusa_trading_speed_per_operator:
                bonus += self.siracusa_trading_speed_per_operator * siracusa
                assumptions.extend(self.assumption_map.get("siracusa_trade", ()))
                audit.extend(self.audit_map.get("siracusa_trade", ()))
            glasgow = room_factions.get("glasgow", 0)
            if glasgow and self.glasgow_trading_speed_per_operator:
                bonus += self.glasgow_trading_speed_per_operator * glasgow
                assumptions.extend(self.assumption_map.get("glasgow_trade", ()))
                audit.extend(self.audit_map.get("glasgow_trade", ()))
            if room_factions.get("karlan", 0) >= 3:
                bonus += self.karlan_three_trading_speed_percent
                assumptions.extend(self.assumption_map.get("karlan_three", ()))
                audit.extend(self.audit_map.get("karlan_three", ()))
        elif room.room_type == "MANUFACTURE":
            if self.manufacture_speed_percent:
                bonus += self.manufacture_speed_percent
                assumptions.extend(self.assumption_map.get("manufacture_global", ()))
                audit.extend(self.audit_map.get("manufacture_global", ()))
            blacksteel = room_factions.get("blacksteel", 0)
            if blacksteel and self.blacksteel_manufacture_speed_per_operator:
                bonus += self.blacksteel_manufacture_speed_per_operator * blacksteel
                assumptions.extend(self.assumption_map.get("blacksteel_manufacture", ()))
                audit.extend(self.audit_map.get("blacksteel_manufacture", ()))
            knight = room_factions.get("knight", 0)
            if knight and self.knight_manufacture_speed_per_operator:
                bonus += self.knight_manufacture_speed_per_operator * knight
                assumptions.extend(self.assumption_map.get("knight_manufacture", ()))
                audit.extend(self.audit_map.get("knight_manufacture", ()))
            if room.target == "F_EXP":
                pinus = room_factions.get("pinus", 0)
                if pinus and self.pinus_exp_manufacture_speed_per_operator:
                    bonus += self.pinus_exp_manufacture_speed_per_operator * pinus
                    assumptions.extend(self.assumption_map.get("pinus_manufacture", ()))
                    audit.extend(self.audit_map.get("pinus_manufacture", ()))
            if room.target == "F_GOLD":
                if self.gold_manufacture_speed_percent:
                    bonus += self.gold_manufacture_speed_percent
                    assumptions.extend(self.assumption_map.get("gold_manufacture_global", ()))
                    audit.extend(self.audit_map.get("gold_manufacture_global", ()))
                pinus = room_factions.get("pinus", 0)
                if pinus and self.pinus_gold_manufacture_speed_per_operator:
                    bonus += self.pinus_gold_manufacture_speed_per_operator * pinus
                    assumptions.extend(self.assumption_map.get("pinus_manufacture", ()))
                    audit.extend(self.audit_map.get("pinus_manufacture", ()))
        elif room.room_type == "MEETING":
            if self.meeting_speed_percent:
                bonus += self.meeting_speed_percent
                assumptions.extend(self.assumption_map.get("meeting_global", ()))
                audit.extend(self.audit_map.get("meeting_global", ()))
        if abs(bonus) <= 0.0001 and abs(order_limit) <= 0.0001:
            return 0.0, 0.0, [], []
        return bonus, order_limit, dedupe_strings(assumptions), dedupe_skill_effect_audit(audit)


@dataclass
class ProductionReport:
    dailyExpected: ProductionVector
    scoreBreakdown: dict[str, float]
    roomReports: list[dict[str, Any]]
    unsupportedSkillEffects: list[dict[str, str]]
    assumptions: list[str]
    skillEffectAudit: list[dict[str, Any]] = field(default_factory=list)
    calibrationProfile: str = "guide"
    sourceAssumptions: list[str] = field(default_factory=list)
    guideComparison: dict[str, Any] | None = None

    @property
    def score(self) -> float:
        return round(sum(self.scoreBreakdown.values()), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dailyExpected": self.dailyExpected.to_dict(),
            "scoreBreakdown": {k: round(v, 3) for k, v in self.scoreBreakdown.items()},
            "roomReports": self.roomReports,
            "unsupportedSkillEffects": self.unsupportedSkillEffects,
            "assumptions": self.assumptions,
            "skillEffectAudit": self.skillEffectAudit,
            "calibrationProfile": self.calibrationProfile,
            "sourceAssumptions": self.sourceAssumptions,
            "guideComparison": self.guideComparison,
        }


class ProductionSimulator:
    def __init__(
        self,
        game_data: GameData,
        *,
        shard_formula: str = "rock",
        drone_policy: str = "none",
        calibration_profile: str = "guide",
        reference_expected_daily: dict[str, Any] | None = None,
        pure_gold_target: float = DEFAULT_PURE_GOLD_TARGET_PER_DAY,
        pure_gold_tolerance: float = DEFAULT_PURE_GOLD_TOLERANCE,
    ) -> None:
        self.game_data = game_data
        self.shard_formula = shard_formula
        self.drone_policy = normalize_drone_policy(drone_policy)
        self.calibration_profile = calibration_profile
        self.reference_expected_daily = reference_expected_daily or {}
        self.pure_gold_target = float(pure_gold_target)
        self.pure_gold_tolerance = max(0.0, float(pure_gold_tolerance))

    def evaluate(self, shifts: list[ShiftPlan]) -> ProductionReport:
        cycle = ProductionVector()
        room_reports: list[dict[str, Any]] = []
        unsupported: list[dict[str, str]] = []
        skill_effect_audit: list[dict[str, Any]] = []
        cycle_hours = max(24.0, sum(shift.duration_hours for shift in shifts) or 24.0)
        daily_scale = 24.0 / cycle_hours
        assumptions = [
            "Factory and Trading Post base production includes +1% per effective slot.",
            "LMD Trading Post orders use guide probabilities by room level: L1 low, L2 low/medium, L3 low/medium/high.",
            "Level 3 LMD order probabilities are default 30%/50%/20%, Tailoring alpha 15%/30%/55%, Tailoring beta 5%/10%/85%.",
            "Yituliu 2026-06 special LMD Trading Post lookups are used for modeled Shamare/Tequila/Proviso, Croissant, and high-efficiency partner combinations.",
            "Orundum orders are modeled as 2 Originium Shards -> 20 Orundum with a 2-hour order duration calibrated from Yituliu 2026-06 schedules.",
            "Overflow is calculated per shift/collection interval.",
            "Production is first summed across one complete shift cycle, then scaled to a 24-hour daily average.",
            "Drone generation starts from 240 drones/day and applies weighted Power Plant drone-recovery skill bonuses from scheduled power rooms.",
            "Auto drone usage targets a conservative Pure Gold daily delta of "
            f"{self.pure_gold_target:g} within ±{self.pure_gold_tolerance:g}, "
            "then spends remaining drone choices on the best output target.",
        ]
        for shift in shifts:
            shift_vector = ProductionVector()
            skill_context = shift_skill_context(shift.rooms)
            control_effect = control_global_effect(shift.rooms, shift.dormitories)
            for room in shift.rooms:
                production = self.evaluate_room(
                    room,
                    shift.duration_hours,
                    skill_context=skill_context,
                    control_effect=control_effect,
                )
                shift_vector.add(production.vector)
                unsupported.extend(production.effect.unsupported)
                room_report = production.to_dict()
                room_report["shift"] = shift.name
                skill_effect_audit.extend(room_report.get("skillEffectAudit", []))
                room_reports.append(room_report)
            cycle.add(shift_vector)

        daily = cycle
        daily.scale(daily_scale)
        daily.cycleHours = cycle_hours
        daily.dailyScale = daily_scale
        drone_vector = self.evaluate_drones(shifts, daily)
        daily.add(drone_vector)
        daily.lmdNet = daily.lmdGross - daily.materialCosts.get("4001", 0.0)
        score_breakdown = score_breakdown_for(daily)
        return ProductionReport(
            dailyExpected=daily,
            scoreBreakdown=score_breakdown,
            roomReports=room_reports,
            unsupportedSkillEffects=dedupe_unsupported(unsupported),
            assumptions=assumptions,
            skillEffectAudit=dedupe_skill_effect_audit(skill_effect_audit),
            calibrationProfile=self.calibration_profile,
            sourceAssumptions=self.source_assumptions(),
        )

    def evaluate_room(
        self,
        room: RoomAssignment,
        duration_hours: float,
        *,
        ignore_capacity: bool = False,
        skill_context: SkillContext | None = None,
        control_effect: ControlGlobalEffect | None = None,
    ) -> RoomProduction:
        effect = evaluate_room_effect(room.operators, room.target, skill_context)
        if control_effect is not None:
            bonus, order_limit, assumptions, control_audit = control_effect.bonus_for(room)
            if bonus:
                effect.speed_percent += bonus
                effect.control_speed_percent += bonus
            if order_limit:
                effect.order_limit_bonus += order_limit
            if bonus or order_limit:
                effect.assumptions.extend(assumptions)
                effect.skill_effect_audit.extend(control_audit)
            if room.room_type == "CONTROL":
                effect.unsupported.extend(control_effect.unsupported)
                effect.skill_effect_audit.extend(control_effect.control_audit)
        if room.room_type == "MANUFACTURE":
            return self.evaluate_manufacture(
                room, duration_hours, effect, ignore_capacity=ignore_capacity
            )
        if room.room_type == "TRADING":
            return self.evaluate_trading(room, duration_hours, effect, ignore_capacity=ignore_capacity)
        if room.room_type == "HIRE":
            return self.evaluate_office(room, duration_hours, effect)
        if room.room_type == "MEETING":
            return self.evaluate_meeting(room, duration_hours, effect)
        fatigue = ProductionVector(fatigueRisk=fatigue_risk(room, duration_hours, effect))
        return RoomProduction(
            roomId=room.room_id,
            roomType=room.room_type,
            target=room.target,
            vector=fatigue,
            effect=effect,
            roomLevel=room.room_level,
        )

    def evaluate_manufacture(
        self,
        room: RoomAssignment,
        duration_hours: float,
        effect: RoomSkillEffect,
        *,
        ignore_capacity: bool = False,
    ) -> RoomProduction:
        target = room.target or "F_EXP"
        formula = self.manufacture_formula(target)
        guide_lookup = self.guide_manufacture_lookup(room, effect, target)
        if guide_lookup is not None:
            lookup_effect = replace(
                effect,
                speed_percent=float(guide_lookup["paperEfficiencyPercent"]),
            )
            multiplier = self.room_speed_multiplier(room, lookup_effect)
            raw_units = duration_hours * 3600.0 / formula["costPoint"] * multiplier
            capacity = self.manufacture_capacity(room) + lookup_effect.capacity_bonus
            units = raw_units if ignore_capacity else min(raw_units, capacity)
            overflow = 0.0 if ignore_capacity else max(0.0, raw_units - capacity)
            vector = ProductionVector(
                overflowLoss=overflow,
                fatigueRisk=fatigue_risk(
                    room,
                    duration_hours,
                    lookup_effect,
                    self.stationed_slots(room),
                ),
            )
            self.apply_manufacture_units(vector, target, formula, units)
            return RoomProduction(
                roomId=room.room_id,
                roomType=room.room_type,
                target=target,
                vector=vector,
                effect=lookup_effect,
                producedUnits=raw_units,
                cappedUnits=units,
                capacity=capacity,
                baseSpeedPercent=self.base_speed_percent(room),
                stationSlots=self.stationed_slots(room),
                roomLevel=room.room_level,
                collectionIntervalHours=duration_hours,
                assumptions=[
                    guide_lookup["source"],
                    "Yituliu guide manufacture lookup uses the published paper efficiency for this room-level static benchmark.",
                ],
                manufactureProfile={
                    "target": target,
                    "level": room.room_level,
                    "calibrationMode": "guide_exact_lookup",
                    "source": guide_lookup["source"],
                    "lookupId": guide_lookup["id"],
                    "paperEfficiencyPercent": guide_lookup["paperEfficiencyPercent"],
                    "minimumParsedSpeedPercent": guide_lookup.get("minimumParsedSpeedPercent"),
                    "required": sorted(guide_lookup.get("required", set())),
                },
                skillEffectAudit=guide_manufacture_lookup_skill_effect_audit(
                    room,
                    guide_lookup,
                    effect,
                ),
            )

        multiplier = self.room_speed_multiplier(room, effect)
        raw_units = duration_hours * 3600.0 / formula["costPoint"] * multiplier
        capacity = self.manufacture_capacity(room) + effect.capacity_bonus
        units = raw_units if ignore_capacity else min(raw_units, capacity)
        overflow = 0.0 if ignore_capacity else max(0.0, raw_units - capacity)
        vector = ProductionVector(
            overflowLoss=overflow,
            fatigueRisk=fatigue_risk(room, duration_hours, effect, self.stationed_slots(room)),
        )

        self.apply_manufacture_units(vector, target, formula, units)
        return RoomProduction(
            roomId=room.room_id,
            roomType=room.room_type,
            target=target,
            vector=vector,
            effect=effect,
            producedUnits=raw_units,
            cappedUnits=units,
            capacity=capacity,
            baseSpeedPercent=self.base_speed_percent(room),
            stationSlots=self.stationed_slots(room),
            roomLevel=room.room_level,
            collectionIntervalHours=duration_hours,
        )

    def evaluate_trading(
        self,
        room: RoomAssignment,
        duration_hours: float,
        effect: RoomSkillEffect,
        *,
        ignore_capacity: bool = False,
    ) -> RoomProduction:
        target = room.target or "O_GOLD"
        guide_lookup = self.guide_lmd_trade_lookup(room) if target == "O_GOLD" else None
        guide_orundum_lookup = self.guide_orundum_trade_lookup(room) if target == "O_DIAMOND" else None
        if guide_lookup is not None:
            lookup_effective_percent = guide_lookup.get(
                "scheduleEffectivePercent", guide_lookup["paperEfficiencyPercent"]
            )
            effective_percent = lookup_effective_percent + effect.control_speed_percent
            duration_scale = duration_hours / 24.0
            vector = ProductionVector(
                lmdGross=guide_lookup["baseLmdPer24h"] * effective_percent / 100.0 * duration_scale,
                pureGoldDelta=(
                    -guide_lookup["baseGoldPer24h"] * effective_percent / 100.0 * duration_scale
                ),
                fatigueRisk=fatigue_risk(room, duration_hours, effect, self.stationed_slots(room)),
            )
            return RoomProduction(
                roomId=room.room_id,
                roomType=room.room_type,
                target=target,
                vector=vector,
                effect=effect,
                producedUnits=duration_scale,
                cappedUnits=duration_scale,
                capacity=None,
                baseSpeedPercent=self.base_speed_percent(room),
                stationSlots=self.stationed_slots(room),
                roomLevel=room.room_level,
                collectionIntervalHours=duration_hours,
                assumptions=[
                    guide_lookup["source"],
                    "Yituliu guide lookup uses the schedule-calibrated effective percent shown for this operator combination.",
                ],
                tradeOrderProfile={
                    "target": target,
                    "level": room.room_level,
                    "calibrationMode": "guide_exact_lookup",
                    "source": guide_lookup["source"],
                    "lookupId": guide_lookup["id"],
                    "baseLmdPer24h": guide_lookup["baseLmdPer24h"],
                    "baseGoldPer24h": guide_lookup["baseGoldPer24h"],
                    "paperEfficiencyPercent": guide_lookup["paperEfficiencyPercent"],
                    "scheduleEffectivePercent": effective_percent,
                    "lookupScheduleEffectivePercent": lookup_effective_percent,
                    "controlSpeedPercent": round(effect.control_speed_percent, 3),
                    "partnerScoreMinimums": list(
                        guide_lookup.get("partnerScoreMinimums", ())
                    ),
                },
                skillEffectAudit=guide_lookup_skill_effect_audit(room, guide_lookup),
            )
        if guide_orundum_lookup is not None:
            lookup_effect = replace(
                effect,
                speed_percent=float(guide_orundum_lookup["paperEfficiencyPercent"])
                + effect.control_speed_percent,
            )
            profile = self.trade_order_profile(target, room.room_level, lookup_effect)
            multiplier = self.room_speed_multiplier(room, lookup_effect)
            raw_orders = duration_hours * 3600.0 / profile.expected_seconds * multiplier
            vector = ProductionVector(
                orundum=raw_orders * profile.expected_orundum,
                shardDelta=-raw_orders * profile.expected_shards,
                fatigueRisk=fatigue_risk(room, duration_hours, lookup_effect, self.stationed_slots(room)),
            )
            return RoomProduction(
                roomId=room.room_id,
                roomType=room.room_type,
                target=target,
                vector=vector,
                effect=lookup_effect,
                producedUnits=raw_orders,
                cappedUnits=raw_orders,
                capacity=None,
                baseSpeedPercent=self.base_speed_percent(room),
                stationSlots=self.stationed_slots(room),
                roomLevel=room.room_level,
                collectionIntervalHours=duration_hours,
                assumptions=[
                    guide_orundum_lookup["source"],
                    "Guide-calibrated Orundum order benchmarks use paper efficiency and raw order throughput without static order-limit clipping.",
                ],
                tradeOrderProfile={
                    **generic_trade_profile_to_dict(profile, room),
                    "calibrationMode": "guide_orundum_exact_lookup",
                    "source": guide_orundum_lookup["source"],
                    "lookupId": guide_orundum_lookup["id"],
                    "paperEfficiencyPercent": guide_orundum_lookup["paperEfficiencyPercent"],
                    "scheduleEffectivePercent": round(
                        guide_orundum_lookup["paperEfficiencyPercent"]
                        + effect.control_speed_percent,
                        3,
                    ),
                    "controlSpeedPercent": round(effect.control_speed_percent, 3),
                },
                skillEffectAudit=guide_orundum_lookup_skill_effect_audit(room, guide_orundum_lookup),
            )
        mechanism_estimate = self.special_lmd_trade_estimate(room) if target == "O_GOLD" else None
        if mechanism_estimate is not None:
            duration_scale = duration_hours / 24.0
            effective_percent = mechanism_estimate.effective_percent + effect.control_speed_percent
            vector = ProductionVector(
                lmdGross=(
                    mechanism_estimate.base_lmd_per_24h
                    * effective_percent
                    / 100.0
                    * duration_scale
                ),
                pureGoldDelta=(
                    -mechanism_estimate.base_gold_per_24h
                    * effective_percent
                    / 100.0
                    * duration_scale
                ),
                fatigueRisk=fatigue_risk(room, duration_hours, effect, self.stationed_slots(room)),
            )
            return RoomProduction(
                roomId=room.room_id,
                roomType=room.room_type,
                target=target,
                vector=vector,
                effect=effect,
                producedUnits=duration_scale,
                cappedUnits=duration_scale,
                capacity=None,
                baseSpeedPercent=self.base_speed_percent(room),
                stationSlots=self.stationed_slots(room),
                roomLevel=room.room_level,
                collectionIntervalHours=duration_hours,
                assumptions=[
                    mechanism_estimate.source,
                    "Yituliu guide anchor rows are used to estimate this special Trading Post mechanism when no exact combo lookup matches.",
                ],
                tradeOrderProfile=special_trade_estimate_to_profile(
                    mechanism_estimate,
                    target,
                    control_speed_percent=effect.control_speed_percent,
                ),
                skillEffectAudit=special_trade_estimate_skill_effect_audit(
                    room, mechanism_estimate
                ),
            )
        profile = self.trade_order_profile(target, room.room_level, effect)
        multiplier = self.room_speed_multiplier(room, effect)
        raw_orders = duration_hours * 3600.0 / profile.expected_seconds * multiplier
        capacity = self.trading_order_limit(room) + effect.order_limit_bonus
        guide_orundum_order = target == "O_DIAMOND" and self.calibration_profile == "guide"
        orders = raw_orders if ignore_capacity or guide_orundum_order else min(raw_orders, capacity)
        overflow = 0.0 if ignore_capacity or guide_orundum_order else max(0.0, raw_orders - capacity)
        vector = ProductionVector(
            overflowLoss=overflow,
            fatigueRisk=fatigue_risk(room, duration_hours, effect, self.stationed_slots(room)),
        )
        if target == "O_GOLD":
            vector.lmdGross += orders * (profile.expected_lmd + effect.lmd_per_order_bonus)
            vector.pureGoldDelta -= orders * profile.expected_gold
        elif target == "O_DIAMOND":
            vector.orundum += orders * profile.expected_orundum
            vector.shardDelta -= orders * profile.expected_shards
        return RoomProduction(
            roomId=room.room_id,
            roomType=room.room_type,
            target=target,
            vector=vector,
            effect=effect,
            producedUnits=raw_orders,
            cappedUnits=orders,
            capacity=capacity,
            baseSpeedPercent=self.base_speed_percent(room),
            stationSlots=self.stationed_slots(room),
            roomLevel=room.room_level,
            collectionIntervalHours=duration_hours,
            assumptions=[
                profile.source,
                *(
                    [
                        "Guide-calibrated Orundum order benchmarks use raw order throughput without static order-limit clipping."
                    ]
                    if guide_orundum_order
                    else []
                ),
            ],
            tradeOrderProfile=generic_trade_profile_to_dict(profile, room),
            skillEffectAudit=special_trade_fallback_skill_effect_audit(room),
        )

    def evaluate_office(
        self,
        room: RoomAssignment,
        duration_hours: float,
        effect: RoomSkillEffect,
    ) -> RoomProduction:
        speed = self.base_speed_percent(room) + effect.speed_percent
        vector = ProductionVector(
            officeSpeed=max(0.0, speed) * duration_hours / 24.0,
            fatigueRisk=fatigue_risk(room, duration_hours, effect, self.stationed_slots(room)),
        )
        return RoomProduction(
            roomId=room.room_id,
            roomType=room.room_type,
            target=room.target,
            vector=vector,
            effect=effect,
            producedUnits=vector.officeSpeed,
            cappedUnits=vector.officeSpeed,
            baseSpeedPercent=self.base_speed_percent(room),
            stationSlots=self.stationed_slots(room),
            roomLevel=room.room_level,
            collectionIntervalHours=duration_hours,
            assumptions=[
                "办公室公开招募联络速度按每日加权效率点计入 officeSpeed；综合分按 officeSpeed / 30 计入。"
            ],
        )

    def evaluate_meeting(
        self,
        room: RoomAssignment,
        duration_hours: float,
        effect: RoomSkillEffect,
    ) -> RoomProduction:
        speed = self.base_speed_percent(room) + effect.speed_percent
        vector = ProductionVector(
            meetingSpeed=max(0.0, speed) * duration_hours / 24.0,
            fatigueRisk=fatigue_risk(room, duration_hours, effect, self.stationed_slots(room)),
        )
        return RoomProduction(
            roomId=room.room_id,
            roomType=room.room_type,
            target=room.target,
            vector=vector,
            effect=effect,
            producedUnits=vector.meetingSpeed,
            cappedUnits=vector.meetingSpeed,
            baseSpeedPercent=self.base_speed_percent(room),
            stationSlots=self.stationed_slots(room),
            roomLevel=room.room_level,
            collectionIntervalHours=duration_hours,
            assumptions=[
                "会客室线索搜集速度按每日加权效率点计入 meetingSpeed；综合分按 meetingSpeed / 30 计入。"
            ],
        )

    def guide_lmd_trade_lookup(self, room: RoomAssignment) -> dict[str, Any] | None:
        if self.calibration_profile != "guide":
            return None
        names = {skill.operator_name for skill in room.operators}
        for lookup in GUIDE_LMD_TRADE_LOOKUPS:
            if room.room_level != lookup["level"]:
                continue
            if not lookup["required"].issubset(names):
                continue
            if not lookup_partner_requirements_met(lookup, room.operators):
                continue
            return lookup
        return None

    def guide_orundum_trade_lookup(self, room: RoomAssignment) -> dict[str, Any] | None:
        if self.calibration_profile != "guide":
            return None
        if self.game_data.data_version == "test":
            return None
        if room.target != "O_DIAMOND":
            return None
        for lookup in GUIDE_ORUNDUM_TRADE_LOOKUPS:
            if room.room_level == lookup["level"]:
                return lookup
        return None

    def guide_manufacture_lookup(
        self,
        room: RoomAssignment,
        effect: RoomSkillEffect,
        target: str,
    ) -> dict[str, Any] | None:
        if self.calibration_profile != "guide":
            return None
        names = {skill.operator_name for skill in room.operators}
        for lookup in GUIDE_MANUFACTURE_LOOKUPS:
            if target != lookup["target"]:
                continue
            if room.room_level != lookup["level"]:
                continue
            if lookup.get("slots") is not None and self.stationed_slots(room) != lookup["slots"]:
                continue
            room_ids = lookup.get("roomIds")
            if room_ids is not None and room.room_id not in room_ids:
                continue
            if not lookup["required"].issubset(names):
                continue
            minimum = lookup.get("minimumParsedSpeedPercent")
            if minimum is not None and effect.speed_percent < float(minimum):
                continue
            return lookup
        return None

    def special_lmd_trade_estimate(self, room: RoomAssignment) -> SpecialTradeEstimate | None:
        if self.calibration_profile != "guide":
            return None
        level = clamp_level(room.room_level)
        estimates: list[SpecialTradeEstimate] = []
        for skill in room.operators:
            anchor = GUIDE_SPECIAL_TRADE_ANCHORS.get(skill.operator_name)
            if anchor is None:
                continue
            partner_speed, partner_contributions = special_trade_partner_speed(
                room.operators, skill.operator_name
            )
            ignored_notes = ignored_partner_notes_from_contributions(partner_contributions)
            intrinsic = float(anchor["intrinsicSpeedPercent"][level])
            estimates.append(
                SpecialTradeEstimate(
                    mechanism_id=str(anchor["mechanismId"]),
                    anchor_operator=skill.operator_name,
                    level=level,
                    base_lmd_per_24h=float(anchor["baseLmdPer24h"][level]),
                    base_gold_per_24h=float(anchor["baseGoldPer24h"][level]),
                    intrinsic_speed_percent=intrinsic,
                    partner_speed_percent=partner_speed,
                    effective_percent=100.0 + intrinsic + partner_speed,
                    confidence=str(anchor["confidence"]),
                    source=str(anchor["source"]),
                    matched_anchor_rows=tuple(anchor["matchedAnchorRows"]),
                    partner_contributions=tuple(partner_contributions),
                    ignored_partner_notes=tuple(ignored_notes),
                )
            )
        if not estimates:
            return None
        return max(
            estimates,
            key=lambda item: (
                item.base_lmd_per_24h * item.effective_percent / 100.0
            ),
        )

    def evaluate_drones(self, shifts: list[ShiftPlan], base_daily: ProductionVector) -> ProductionVector:
        if self.drone_policy == "none" or not shifts:
            return ProductionVector()
        policies = (
            ["gold-factory", "shard-factory", "lmd-trade", "exp-factory"]
            if self.drone_policy in {"auto", "reference-fit"}
            else [self.drone_policy]
        )
        shift_budgets, drone_count, generation_bonus = self.shift_drone_budgets(shifts)
        if drone_count <= 0:
            return ProductionVector()
        if self.drone_policy == "reference-fit":
            allocations = self.allocate_reference_fit_drones(
                shifts,
                policies,
                shift_budgets,
                base_daily,
                self.reference_expected_daily,
            )
        else:
            allocations = self.allocate_drones(
                shifts,
                policies,
                shift_budgets,
                base_daily,
                preserve_balance=self.drone_policy == "auto",
            )
        if not allocations:
            return ProductionVector()

        contribution = ProductionVector()
        targets: list[dict[str, Any]] = []
        for allocation in allocations:
            room = allocation["room"]
            drones = float(allocation["drones"])
            duration_hours = drones * DRONE_SECONDS / 3600.0
            production = self.evaluate_room(room, duration_hours, ignore_capacity=True)
            vector = production.vector
            vector.fatigueRisk = 0.0
            vector.overflowLoss = 0.0
            contribution.add(vector)
            targets.append(
                {
                    "shift": allocation["shift"].name,
                    "policy": allocation["policy"],
                    "roomId": room.room_id,
                    "roomType": room.room_type,
                    "target": room.target,
                    "droneCount": round(drones, 3),
                    "durationHours": round(duration_hours, 3),
                    "operators": [skill.operator_name for skill in room.operators],
                    "contribution": vector_resource_dict(vector),
                }
            )

        contribution.fatigueRisk = 0.0
        contribution.overflowLoss = 0.0
        contribution.droneContribution = vector_resource_dict(contribution)
        if self.drone_policy == "reference-fit":
            contribution.droneContribution["referenceFit"] = 1.0
        contribution.droneCount = drone_count
        contribution.droneUsed = sum(float(allocation["drones"]) for allocation in allocations)
        contribution.droneGenerationBonusPercent = generation_bonus
        contribution.droneTargets = targets
        contribution.droneTarget = targets[0] if targets else {}
        return contribution

    def daily_drone_count(self, shifts: list[ShiftPlan]) -> tuple[float, float]:
        _, drone_count, generation_bonus = self.shift_drone_budgets(shifts)
        return drone_count, generation_bonus

    def shift_drone_budgets(
        self, shifts: list[ShiftPlan]
    ) -> tuple[dict[str, float], float, float]:
        if not shifts:
            return {}, DAILY_DRONES, 0.0
        cycle_hours = max(24.0, sum(shift.duration_hours for shift in shifts) or 24.0)
        budgets: dict[str, float] = {}
        weighted_bonus_hours = 0.0
        total_hours = 0.0
        for shift in shifts:
            total_hours += shift.duration_hours
            power_bonus = 0.0
            for room in shift.rooms:
                if room.room_type != "POWER":
                    continue
                effect = evaluate_room_effect(room.operators, room.target)
                power_bonus += effect.speed_percent
            weighted_bonus_hours += power_bonus * shift.duration_hours
            budgets[shift.name] = round(
                DAILY_DRONES * shift.duration_hours / cycle_hours * (1.0 + power_bonus / 100.0),
                3,
            )
        bonus_percent = weighted_bonus_hours / total_hours if total_hours > 0 else 0.0
        return budgets, round(sum(budgets.values()), 3), round(bonus_percent, 3)

    def best_drone_target(
        self, rooms: list[RoomAssignment], policies: list[str]
    ) -> tuple[RoomAssignment, str] | None:
        best: tuple[float, RoomAssignment, str] | None = None
        for policy in policies:
            for room in drone_candidate_rooms(rooms, policy):
                vector = self.evaluate_room(
                    room, DRONE_SECONDS / 3600.0, ignore_capacity=True
                ).vector
                score = drone_policy_score(policy, vector)
                if best is None or score > best[0]:
                    best = (score, room, policy)
        if best is None or best[0] <= 0:
            return None
        return best[1], best[2]

    def allocate_drones(
        self,
        shifts: list[ShiftPlan],
        policies: list[str],
        shift_budgets: dict[str, float],
        base_daily: ProductionVector,
        preserve_balance: bool,
    ) -> list[dict[str, Any]]:
        candidates = self.drone_candidates(shifts, policies)
        candidates_by_shift: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            if drone_policy_score(candidate["policy"], candidate["perDrone"]) <= 0:
                continue
            shift_name = candidate["shift"].name
            if shift_budgets.get(shift_name, 0.0) <= 0.001:
                continue
            candidates_by_shift.setdefault(shift_name, []).append(candidate)

        if not preserve_balance:
            allocations = []
            for shift in shifts:
                shift_candidates = candidates_by_shift.get(shift.name, [])
                if not shift_candidates:
                    continue
                best = max(
                    shift_candidates,
                    key=lambda candidate: drone_policy_score(
                        candidate["policy"], candidate["perDrone"]
                    ),
                )
                allocations.append({**best, "drones": shift_budgets[shift.name]})
            return allocations

        return self.best_shift_limited_auto_drone_allocations(
            shifts,
            candidates_by_shift,
            shift_budgets,
            base_daily,
        )

    def allocate_reference_fit_drones(
        self,
        shifts: list[ShiftPlan],
        policies: list[str],
        shift_budgets: dict[str, float],
        base_daily: ProductionVector,
        expected_daily: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates = [
            candidate
            for candidate in self.drone_candidates(shifts, policies)
            if shift_budgets.get(candidate["shift"].name, 0.0) > 0.001
        ]
        remaining_by_shift = dict(shift_budgets)
        allocations: list[dict[str, Any]] = []

        for field, policy in (
            ("lmdGross", "lmd-trade"),
            ("exp", "exp-factory"),
            ("orundum", "lmd-trade"),
        ):
            expected = float(expected_daily.get(field) or 0.0)
            if expected <= 0.001:
                continue
            projected = projected_vector(base_daily, allocations)
            deficit = expected - float(getattr(projected, field))
            if deficit <= 0.001:
                continue
            policy_candidates = sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate["policy"] == policy
                    and float(getattr(candidate["perDrone"], field)) > 0.0
                ),
                key=lambda candidate: float(getattr(candidate["perDrone"], field)),
                reverse=True,
            )
            for candidate in policy_candidates:
                shift_name = candidate["shift"].name
                remaining = remaining_by_shift.get(shift_name, 0.0)
                if remaining <= 0.001:
                    continue
                per_drone = float(getattr(candidate["perDrone"], field))
                drones = min(remaining, deficit / per_drone)
                if drones <= 0.001:
                    continue
                allocations.append({**candidate, "drones": drones})
                remaining_by_shift[shift_name] = remaining - drones
                deficit -= per_drone * drones
                if deficit <= 0.001:
                    break

        return allocations

    def best_shift_limited_auto_drone_allocations(
        self,
        shifts: list[ShiftPlan],
        candidates_by_shift: dict[str, list[dict[str, Any]]],
        shift_budgets: dict[str, float],
        base_daily: ProductionVector,
    ) -> list[dict[str, Any]]:
        choices: list[tuple[str, list[dict[str, Any]]]] = []
        for shift in shifts:
            shift_candidates = candidates_by_shift.get(shift.name, [])
            if shift_budgets.get(shift.name, 0.0) > 0.001 and shift_candidates:
                choices.append((shift.name, shift_candidates))
        if not choices:
            return []

        def final_key(vector: ProductionVector, allocations: list[dict[str, Any]]) -> tuple[float, float, float, float, float]:
            used_drones = sum(float(allocation["drones"]) for allocation in allocations)
            score = sum(score_breakdown_for(vector).values())
            gold_error = pure_gold_balance_error(
                vector.pureGoldDelta,
                self.pure_gold_target,
            )
            balance_penalty = resource_balance_penalty(
                vector.pureGoldDelta,
                vector.shardDelta,
                vector.lmdNet,
                vector.orundum,
            )
            return (
                1.0 if gold_error <= self.pure_gold_tolerance else 0.0,
                -round(gold_error, 3),
                -round(balance_penalty, 3),
                score,
                round(used_drones, 3),
            )

        def prune_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
            by_policy: dict[str, list[dict[str, Any]]] = {}
            for candidate in options:
                by_policy.setdefault(str(candidate["policy"]), []).append(candidate)
            pruned: list[dict[str, Any]] = []
            for policy_options in by_policy.values():
                pruned.extend(
                    sorted(
                        policy_options,
                        key=lambda candidate: (
                            drone_policy_score(candidate["policy"], candidate["perDrone"]),
                            sum(score_breakdown_for(candidate["perDrone"]).values()),
                        ),
                        reverse=True,
                    )[:2]
                )
            return pruned

        state_limit = 512
        states: list[tuple[ProductionVector, list[dict[str, Any]]]] = [(base_daily, [])]
        for shift_name, options in choices:
            budget = shift_budgets[shift_name]
            next_states: list[tuple[ProductionVector, list[dict[str, Any]]]] = []
            for vector, allocations in states:
                for candidate in prune_options(options):
                    allocation = {**candidate, "drones": budget}
                    next_states.append(
                        (
                            projected_vector(vector, [allocation]),
                            [*allocations, allocation],
                        )
                    )
            next_states.sort(key=lambda state: final_key(state[0], state[1]), reverse=True)
            states = next_states[:state_limit]

        best_vector, best_allocations = max(
            states,
            key=lambda state: final_key(state[0], state[1]),
        )
        _ = best_vector
        return best_allocations

    def allocate_to_balance(
        self,
        allocations: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        remaining: float,
        policy: str,
        balance_field: str,
        deficit: float,
    ) -> float:
        if remaining <= 0.001 or deficit <= 0.001:
            return remaining
        best = max(
            (candidate for candidate in candidates if candidate["policy"] == policy),
            key=lambda candidate: getattr(candidate["perDrone"], balance_field),
            default=None,
        )
        if best is None:
            return remaining
        per_drone = getattr(best["perDrone"], balance_field)
        if per_drone <= 0:
            return remaining
        drones = min(remaining, deficit / per_drone)
        if drones > 0.001:
            allocations.append({**best, "drones": drones})
            remaining -= drones
        return remaining

    def drone_candidates(
        self, shifts: list[ShiftPlan], policies: list[str]
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for shift in shifts:
            for policy in policies:
                for room in drone_candidate_rooms(shift.rooms, policy):
                    candidates.append(
                        {
                            "shift": shift,
                            "policy": policy,
                            "room": room,
                            "perDrone": self.evaluate_room(
                                room, DRONE_SECONDS / 3600.0, ignore_capacity=True
                            ).vector,
                        }
                    )
        return candidates

    def apply_manufacture_units(
        self, vector: ProductionVector, target: str, formula: dict[str, Any], units: float
    ) -> None:
        if target == "F_EXP":
            vector.exp += units * EXP_VALUE.get(str(formula["itemId"]), 1000.0)
        elif target == "F_GOLD":
            vector.pureGoldDelta += units * formula.get("count", 1.0)
        elif target == "F_DIAMOND":
            vector.shardDelta += units * formula.get("count", 1.0)
            for item_id, count in formula.get("costs", {}).items():
                vector.materialCosts[item_id] = vector.materialCosts.get(item_id, 0.0) + count * units

    def manufacture_formula(self, target: str) -> dict[str, Any]:
        if target == "F_DIAMOND":
            return DEFAULT_FORMULAS[
                "F_DIAMOND_DEVICE" if self.shard_formula == "device" else "F_DIAMOND_ROCK"
            ]
        formulas = self.game_data.building.get("manufactFormulas", {})
        candidates = [f for f in formulas.values() if f.get("formulaType") == target]
        if candidates:
            formula = sorted(candidates, key=lambda item: float(item.get("costPoint") or 0))[-1]
            return {
                "costPoint": float(formula.get("costPoint") or DEFAULT_FORMULAS[target]["costPoint"]),
                "itemId": str(formula.get("itemId") or DEFAULT_FORMULAS[target]["itemId"]),
                "count": float(formula.get("count") or 1.0),
                "costs": costs_to_dict(formula.get("costs") or []),
            }
        return DEFAULT_FORMULAS[target]

    def manufacture_capacity(self, room: RoomAssignment) -> float:
        if room.product_capacity is not None:
            return float(room.product_capacity)
        phases = self.game_data.building.get("manufactData", {}).get("phases") or []
        phase = phase_for_level(phases, room.room_level)
        if phase:
            return float(phase.get("outputCapacity") or 54)
        return 54.0

    def trading_order_limit(self, room: RoomAssignment) -> float:
        if room.order_limit is not None:
            return float(room.order_limit)
        phases = self.game_data.building.get("tradingData", {}).get("phases") or []
        phase = phase_for_level(phases, room.room_level)
        if phase:
            return float(phase.get("orderLimit") or 10)
        return 10.0

    def room_speed_multiplier(self, room: RoomAssignment, effect: RoomSkillEffect) -> float:
        return self.room_phase_speed(room) * max(
            0.0,
            1.0 + self.base_speed_percent(room) / 100.0 + effect.speed_percent / 100.0,
        )

    def base_speed_percent(self, room: RoomAssignment) -> float:
        return self.basic_speed_buff(room.room_type) * self.stationed_slots(room) * 100.0

    def basic_speed_buff(self, room_type: str) -> float:
        if room_type == "MANUFACTURE":
            return float(
                self.game_data.building.get("manufactData", {}).get(
                    "basicSpeedBuff", DEFAULT_BASIC_SPEED_BUFF
                )
            )
        if room_type == "TRADING":
            return float(
                self.game_data.building.get("tradingData", {}).get(
                    "basicSpeedBuff", DEFAULT_BASIC_SPEED_BUFF
                )
            )
        return DEFAULT_BASIC_SPEED_BUFF

    def room_phase_speed(self, room: RoomAssignment) -> float:
        if room.room_type == "MANUFACTURE":
            phases = self.game_data.building.get("manufactData", {}).get("phases") or []
            phase = phase_for_level(phases, room.room_level)
            if phase:
                return float(phase.get("speed") or 1.0)
        if room.room_type == "TRADING":
            phases = self.game_data.building.get("tradingData", {}).get("phases") or []
            phase = phase_for_level(phases, room.room_level)
            if phase:
                return float(phase.get("orderSpeed") or 1.0)
        return 1.0

    def stationed_slots(self, room: RoomAssignment) -> int:
        hard_limit = station_limit(room.room_type)
        if hard_limit is not None:
            return hard_limit
        if room.slots is not None:
            return clamp_station_count(room.room_type, room.slots)
        if room.operators:
            return clamp_station_count(room.room_type, len(room.operators))
        return self.max_stationed_num(room.room_type, room.room_level)

    def max_stationed_num(self, room_type: str, room_level: int = 3) -> int:
        room = self.game_data.building.get("rooms", {}).get(room_type, {})
        phases = room.get("phases") or []
        phase = phase_for_level(phases, room_level)
        if phase:
            return clamp_station_count(room_type, int(phase.get("maxStationedNum") or 1))
        defaults = {
            "CONTROL": 5,
            "TRADING": min(3, room_level),
            "MANUFACTURE": min(3, room_level),
            "POWER": 1,
            "DORMITORY": min(5, room_level),
            "MEETING": min(2, room_level),
            "HIRE": 1,
        }
        return clamp_station_count(room_type, defaults.get(room_type, 1))

    def trade_order_profile(
        self, target: str, room_level: int, effect: RoomSkillEffect | None = None
    ) -> TradeOrderProfile:
        level = clamp_level(room_level)
        if target == "O_DIAMOND":
            return TradeOrderProfile(
                target=target,
                level=level,
                expected_seconds=ORUNDUM_ORDER_SECONDS,
                expected_orundum=ORUNDUM_PER_SHARD_ORDER,
                expected_shards=SHARDS_PER_ORUNDUM_ORDER,
                distribution=[
                    {
                        "shards": SHARDS_PER_ORUNDUM_ORDER,
                        "orundum": ORUNDUM_PER_SHARD_ORDER,
                        "seconds": ORUNDUM_ORDER_SECONDS,
                        "probability": 1.0,
                    }
                ],
                source="Orundum order uses 2 shards -> 20 Orundum and the 2-hour order duration calibrated from Yituliu 2026-06 Orundum schedules.",
            )

        order_weight_level = effect.order_weight_level if effect else 0
        bias = "default"
        if level >= 3:
            if order_weight_level >= 2:
                probabilities = (0.05, 0.10, 0.85)
                bias = "tailoring_beta"
            elif order_weight_level >= 1:
                probabilities = (0.15, 0.30, 0.55)
                bias = "tailoring_alpha"
            else:
                probabilities = (0.30, 0.50, 0.20)
            orders = [
                LmdOrder(2.0, 1000.0, LOW_ORDER_SECONDS, probabilities[0]),
                LmdOrder(3.0, 1500.0, MEDIUM_ORDER_SECONDS, probabilities[1]),
                LmdOrder(4.0, 2000.0, HIGH_ORDER_SECONDS, probabilities[2]),
            ]
        elif level == 2:
            orders = [
                LmdOrder(2.0, 1000.0, LOW_ORDER_SECONDS, 0.60),
                LmdOrder(3.0, 1500.0, MEDIUM_ORDER_SECONDS, 0.40),
            ]
        else:
            orders = [LmdOrder(2.0, 1000.0, LOW_ORDER_SECONDS, 1.0)]

        expected_seconds = sum(order.seconds * order.probability for order in orders)
        expected_lmd = sum(order.lmd * order.probability for order in orders)
        expected_gold = sum(order.gold * order.probability for order in orders)
        return TradeOrderProfile(
            target=target,
            level=level,
            expected_seconds=expected_seconds,
            expected_lmd=expected_lmd,
            expected_gold=expected_gold,
            distribution=[
                {
                    "gold": order.gold,
                    "lmd": order.lmd,
                    "seconds": order.seconds,
                    "probability": order.probability,
                }
                for order in orders
            ],
            source="LMD order probabilities and durations follow public Trading Post guide data.",
            bias=bias,
        )

    def source_assumptions(self) -> list[str]:
        return [
            "GamePress 252 guide: 252 means 2 Trading Posts, 5 Factories, 2 Power Plants; common factory splits are 3 EXP/2 Gold or 2 EXP/3 Gold.",
            "Arknights Wiki Trading Post: LMD orders use 2/3/4 Pure Gold order types with level-dependent odds and order durations.",
            "ArkBuilding model: room type, slots, product type and baseProdEff=1+0.01*slots are modeled explicitly.",
            f"Drone policy: {self.drone_policy}. Drone contribution is reported separately.",
        ]


def costs_to_dict(costs: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for cost in costs:
        item_id = str(cost.get("id"))
        result[item_id] = result.get(item_id, 0.0) + float(cost.get("count") or 0)
    return result


def phase_for_level(phases: list[dict[str, Any]], room_level: int) -> dict[str, Any] | None:
    if not phases:
        return None
    index = max(0, min(len(phases) - 1, room_level - 1))
    return phases[index]


def clamp_level(room_level: int) -> int:
    return max(1, min(3, int(room_level or 3)))


def trade_profile_to_dict(profile: TradeOrderProfile) -> dict[str, Any]:
    return {
        "target": profile.target,
        "level": profile.level,
        "expectedSeconds": round(profile.expected_seconds, 3),
        "expectedLmd": round(profile.expected_lmd, 3),
        "expectedGold": round(profile.expected_gold, 3),
        "expectedOrundum": round(profile.expected_orundum, 3),
        "expectedShards": round(profile.expected_shards, 3),
        "bias": profile.bias,
        "distribution": profile.distribution,
        "source": profile.source,
    }


def generic_trade_profile_to_dict(
    profile: TradeOrderProfile,
    room: RoomAssignment,
) -> dict[str, Any]:
    data = trade_profile_to_dict(profile)
    data["calibrationMode"] = "generic_profile"
    diagnostics = special_trade_fallback_diagnostics(room)
    if diagnostics:
        data["specialTradeDiagnostics"] = diagnostics
    return data


def special_trade_estimate_to_profile(
    estimate: SpecialTradeEstimate,
    target: str,
    *,
    control_speed_percent: float = 0.0,
) -> dict[str, Any]:
    effective_percent = estimate.effective_percent + control_speed_percent
    data = {
        "target": target,
        "level": estimate.level,
        "calibrationMode": "guide_mechanism_estimate",
        "mechanismId": estimate.mechanism_id,
        "anchorOperator": estimate.anchor_operator,
        "baseLmdPer24h": estimate.base_lmd_per_24h,
        "baseGoldPer24h": estimate.base_gold_per_24h,
        "intrinsicSpeedPercent": estimate.intrinsic_speed_percent,
        "partnerSpeedPercent": estimate.partner_speed_percent,
        "scheduleEffectivePercent": effective_percent,
        "mechanismScheduleEffectivePercent": estimate.effective_percent,
        "controlSpeedPercent": round(control_speed_percent, 3),
        "confidence": estimate.confidence,
        "matchedAnchorRows": list(estimate.matched_anchor_rows),
        "source": estimate.source,
        "partnerContributions": list(estimate.partner_contributions),
    }
    if estimate.ignored_partner_notes:
        data["ignoredPartnerNotes"] = list(estimate.ignored_partner_notes)
    return data


def guide_lookup_skill_effect_audit(
    room: RoomAssignment,
    lookup: dict[str, Any],
) -> list[dict[str, Any]]:
    required = set(lookup.get("required", set()))
    audit: list[dict[str, Any]] = []
    for skill in room.operators:
        if skill.operator_name not in required:
            continue
        item = skill_audit(
            skill,
            "source_calibrated",
            "trade_order_profile",
            "Exact Yituliu guide lookup calibrates this Trading Post room output; individual parser unsupported notes are subordinate to this room-level calibration.",
            "guide_exact_lookup",
        )
        item["calibrationMode"] = "guide_exact_lookup"
        item["lookupId"] = lookup["id"]
        audit.append(item)
    return audit


def guide_orundum_lookup_skill_effect_audit(
    room: RoomAssignment,
    lookup: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "operator": skill.operator_name,
            "buffId": skill.buff_id,
            "buffName": skill.buff_name,
            "status": "source_calibrated",
            "scope": "orundum_trade_room_lookup",
            "calibrationMode": "guide_orundum_exact_lookup",
            "lookupId": lookup["id"],
            "reason": (
                "Yituliu guide room summary calibrates Orundum Trading Post paper efficiency; "
                "static guide images do not expose an exact operator roster for this room."
            ),
        }
        for skill in room.operators
    ]


def guide_manufacture_lookup_skill_effect_audit(
    room: RoomAssignment,
    lookup: dict[str, Any],
    parsed_effect: RoomSkillEffect,
) -> list[dict[str, Any]]:
    required = set(lookup.get("required", set()))
    audit: list[dict[str, Any]] = []
    for skill in room.operators:
        if required and skill.operator_name not in required:
            continue
        item = skill_audit(
            skill,
            "source_calibrated",
            "manufacture_paper_efficiency",
            "Exact Yituliu guide manufacture lookup calibrates this room output; parsed fixed speed remains available as diagnostic evidence.",
            "guide_exact_lookup",
            {
                "paperEfficiencyPercent": float(lookup["paperEfficiencyPercent"]),
                "parsedSpeedPercent": round(float(parsed_effect.speed_percent), 3),
            },
        )
        item["calibrationMode"] = "guide_exact_lookup"
        item["lookupId"] = lookup["id"]
        audit.append(item)
    return audit


def special_trade_estimate_skill_effect_audit(
    room: RoomAssignment,
    estimate: SpecialTradeEstimate,
) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    for skill in room.operators:
        if skill.operator_name != estimate.anchor_operator:
            continue
        item = skill_audit(
            skill,
            "source_calibrated",
            "trade_order_profile",
            "Yituliu guide anchor rows calibrate this special Trading Post mechanism; unsupported notes describe sub-effects not directly parsed as ordinary speed.",
            estimate.confidence,
        )
        item["calibrationMode"] = "guide_mechanism_estimate"
        item["mechanismId"] = estimate.mechanism_id
        audit.append(item)
    for contribution in estimate.partner_contributions:
        status = "counted" if contribution["counted"] else "diagnostic_only"
        if contribution["classification"] == "upgrade_only":
            status = "explicitly_excluded"
        item = {
            "operator": contribution["operatorName"],
            "buffId": contribution.get("buffId", ""),
            "buffName": contribution["buffName"],
            "status": status,
            "scope": "special_trade_partner_speed",
            "reason": contribution["reason"],
            "confidence": "parsed_fixed_speed" if contribution["counted"] else "diagnostic",
            "classification": contribution["classification"],
        }
        if contribution["counted"]:
            item["numericContribution"] = {
                "speedPercent": contribution["countedSpeedPercent"]
            }
        if contribution.get("hasUpgradeRequirement"):
            item["hasUpgradeRequirement"] = True
        audit.append(item)
    return audit


def special_trade_fallback_skill_effect_audit(room: RoomAssignment) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    for diagnostic in special_trade_fallback_diagnostics(room):
        for contribution in diagnostic.get("partnerContributions", []):
            audit.append(
                {
                    "operator": contribution["operatorName"],
                    "buffId": contribution.get("buffId", ""),
                    "buffName": contribution["buffName"],
                    "status": "diagnostic_only",
                    "scope": "special_trade_fallback",
                    "reason": contribution["reason"],
                    "confidence": "diagnostic",
                    "classification": contribution["classification"],
                }
            )
    return audit


def special_trade_partner_speed(
    operators: list[BaseSkill],
    anchor_operator: str,
) -> tuple[float, list[dict[str, Any]]]:
    total = 0.0
    contributions: list[dict[str, Any]] = []
    for skill in operators:
        if skill.operator_name == anchor_operator:
            continue
        if skill.room_type != "TRADING":
            continue
        contribution = special_trade_partner_contribution(skill, operators)
        contributions.append(contribution)
        if contribution["counted"]:
            total += float(contribution["countedSpeedPercent"])
    return total, contributions


def special_trade_partner_contribution(
    skill: BaseSkill, operators: list[BaseSkill]
) -> dict[str, Any]:
    classification, counted, counted_speed, reason = classify_special_trade_partner(skill)
    if counted:
        effective_speed = lookup_partner_effective_speed(skill, operators)
        if effective_speed > counted_speed + 0.0001:
            counted_speed = effective_speed
            reason = (
                "Effective same-room Trading Post speed is counted in partnerSpeedPercent."
            )
    return {
        "operatorName": skill.operator_name,
        "buffId": skill.buff_id,
        "buffName": skill.buff_name,
        "parsedScore": round(float(skill.parsed_score), 3),
        "countedSpeedPercent": round(float(counted_speed), 3),
        "counted": counted,
        "classification": classification,
        "reason": reason,
        "hasUpgradeRequirement": skill.upgrade is not None,
    }


def classify_special_trade_partner(skill: BaseSkill) -> tuple[str, bool, float, str]:
    text = f"{skill.buff_name} {skill.description}"
    if is_human_fire_trade_skill(text):
        return (
            "conditional_human_fire",
            False,
            0.0,
            "Human-fire style trade speed depends on external/dormitory state and is not statically converted into partner speed.",
        )
    if is_tailoring_probability_skill(text):
        return (
            "tailoring_probability",
            False,
            0.0,
            "Tailoring alpha/beta changes high-value LMD order probability, not ordinary partner speed for this guide mechanism estimate.",
        )
    if is_special_order_mechanism_skill(text):
        return (
            "special_order_mechanism",
            False,
            0.0,
            "Special-order mechanics are handled by the anchor mechanism or exact guide lookup, not added again as partner speed.",
        )
    if skill.upgrade is not None and skill.parsed_score <= 0:
        return (
            "upgrade_only",
            False,
            0.0,
            "This partner effect is represented as an upgrade requirement but has no parsed current speed in this evaluation.",
        )
    if skill.parsed_score > 0:
        return (
            "fixed_speed",
            True,
            float(skill.parsed_score),
            "Parsed fixed Trading Post speed is counted in partnerSpeedPercent.",
        )
    return (
        "zero_or_unparsed",
        False,
        0.0,
        "No fixed parsed Trading Post speed is available, so this partner is diagnostic-only.",
    )


def ignored_partner_notes_from_contributions(
    contributions: list[dict[str, Any]],
) -> list[str]:
    notes: list[str] = []
    for contribution in contributions:
        if contribution["counted"]:
            continue
        notes.append(
            f"{contribution['operatorName']}/{contribution['buffName']} excluded from mechanism partner speed: {contribution['reason']}"
        )
    return notes


def special_trade_fallback_diagnostics(room: RoomAssignment) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for skill in room.operators:
        anchor = GUIDE_SPECIAL_TRADE_ANCHORS.get(skill.operator_name)
        if anchor is None:
            continue
        partner_speed, partner_contributions = special_trade_partner_speed(
            room.operators, skill.operator_name
        )
        diagnostics.append(
            {
                "type": "special_trade_anchor_without_estimate",
                "anchorOperator": skill.operator_name,
                "mechanismId": anchor["mechanismId"],
                "partnerSpeedPercent": partner_speed,
                "reason": "No exact guide lookup matched and guide mechanism estimates were not applied.",
                "partnerContributions": partner_contributions,
                "ignoredPartnerNotes": ignored_partner_notes_from_contributions(
                    partner_contributions
                ),
            }
        )
    return diagnostics


def is_human_fire_trade_skill(text: str) -> bool:
    markers = (
        "浜洪棿鐑熺伀",
        "人间烟火",
        "鐐逛汉闂寸儫鐏",
        "瀹胯垗",
        "宿舍",
        "心情",
    )
    return any(marker in text for marker in markers)


def is_tailoring_probability_skill(text: str) -> bool:
    markers = (
        "瑁佺紳",
        "裁缝",
        "tailoring",
        "楂樺搧璐ㄨ吹閲戝睘璁㈠崟",
        "高品质贵金属订单",
    )
    return any(marker in text for marker in markers)


def is_special_order_mechanism_skill(text: str) -> bool:
    markers = (
        "鐗瑰埆璁㈠崟",
        "鐗规畩璁㈠崟",
        "特别订单",
        "特殊订单",
        "杩濈害璁㈠崟",
        "违约订单",
    )
    return any(marker in text for marker in markers)


def is_special_trade_like_skill(skill: BaseSkill) -> bool:
    text = f"{skill.buff_name} {skill.description}"
    markers = (
        "裁缝",
        "低语",
        "违约订单",
        "违约索赔",
        "特别订单",
        "特殊订单",
        "人间烟火",
        "愿者上钩",
    )
    return any(marker in text for marker in markers)


def lookup_partner_requirements_met(
    lookup: dict[str, Any],
    operators: list[BaseSkill],
) -> bool:
    minimums = tuple(float(item) for item in lookup.get("partnerScoreMinimums", ()))
    if not minimums:
        return True
    required = set(lookup.get("required", set()))
    partner_scores = sorted(
        (
            lookup_partner_effective_speed(skill, operators)
            for skill in operators
            if skill.operator_name not in required
        ),
        reverse=True,
    )
    if len(partner_scores) < len(minimums):
        return False
    return all(
        score + 0.0001 >= minimum
        for score, minimum in zip(partner_scores, sorted(minimums, reverse=True))
    )


def lookup_partner_effective_speed(skill: BaseSkill, operators: list[BaseSkill]) -> float:
    names = {operator.operator_name for operator in operators}
    effect = evaluate_skill(
        skill,
        None,
        names,
        faction_counts(operators),
        SkillContext(),
    )
    return max(float(skill.parsed_score), float(effect.speed_percent))


def fatigue_risk(
    room: RoomAssignment,
    duration_hours: float,
    effect: RoomSkillEffect,
    stationed_slots: int | None = None,
) -> float:
    operator_count = stationed_slots if stationed_slots is not None else max(1, len(room.operators))
    overtime = max(0.0, duration_hours - 12.0) * max(1, operator_count)
    morale_delta = effect.fatigue_delta_per_hour * duration_hours
    return max(0.0, overtime + morale_delta)


def normalize_drone_policy(value: str | None) -> str:
    value = (value or "none").strip().lower().replace("_", "-")
    if value not in {
        "none",
        "lmd-trade",
        "gold-factory",
        "shard-factory",
        "exp-factory",
        "auto",
        "reference-fit",
    }:
        raise ValueError(
            "--drone-policy must be none, lmd-trade, gold-factory, shard-factory, exp-factory, auto, or reference-fit"
        )
    return value


def drone_candidate_rooms(rooms: list[RoomAssignment], policy: str) -> list[RoomAssignment]:
    if policy == "lmd-trade":
        return matching_rooms(rooms, "TRADING", "O_GOLD")
    if policy == "gold-factory":
        return matching_rooms(rooms, "MANUFACTURE", "F_GOLD")
    if policy == "shard-factory":
        return matching_rooms(rooms, "MANUFACTURE", "F_DIAMOND")
    if policy == "exp-factory":
        return matching_rooms(rooms, "MANUFACTURE", "F_EXP")
    return []


def matching_rooms(rooms: list[RoomAssignment], room_type: str, target: str) -> list[RoomAssignment]:
    return [room for room in rooms if room.room_type == room_type and room.target == target]


def drone_policy_score(policy: str, vector: ProductionVector) -> float:
    if policy == "lmd-trade":
        return vector.lmdGross
    if policy == "gold-factory":
        return max(0.0, vector.pureGoldDelta) * 500.0
    if policy == "shard-factory":
        return max(0.0, vector.shardDelta) * 20.0
    if policy == "exp-factory":
        return vector.exp
    return score_breakdown_for(vector).get("lmdGross", 0.0)


def merge_drone_allocations(allocations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for allocation in allocations:
        key = (
            allocation["shift"].name,
            allocation["policy"],
            allocation["room"].room_id,
        )
        current = merged.get(key)
        if current is None:
            merged[key] = dict(allocation)
        else:
            current["drones"] = float(current["drones"]) + float(allocation["drones"])
    return list(merged.values())


def projected_balances(
    base_daily: ProductionVector, allocations: list[dict[str, Any]]
) -> tuple[float, float]:
    gold = base_daily.pureGoldDelta
    shard = base_daily.shardDelta
    for allocation in allocations:
        drones = float(allocation["drones"])
        per_drone = allocation["perDrone"]
        gold += per_drone.pureGoldDelta * drones
        shard += per_drone.shardDelta * drones
    return gold, shard


def projected_balance_penalty(
    base_daily: ProductionVector, allocations: list[dict[str, Any]]
) -> float:
    projected = projected_vector(base_daily, allocations)
    return resource_balance_penalty(
        projected.pureGoldDelta,
        projected.shardDelta,
        projected.lmdNet,
        projected.orundum,
    )


def projected_vector(
    base_daily: ProductionVector, allocations: list[dict[str, Any]]
) -> ProductionVector:
    projected = ProductionVector(
        lmdGross=base_daily.lmdGross,
        lmdNet=base_daily.lmdNet,
        exp=base_daily.exp,
        orundum=base_daily.orundum,
        officeSpeed=base_daily.officeSpeed,
        pureGoldDelta=base_daily.pureGoldDelta,
        shardDelta=base_daily.shardDelta,
        materialCosts=dict(base_daily.materialCosts),
        overflowLoss=base_daily.overflowLoss,
        fatigueRisk=base_daily.fatigueRisk,
        cycleHours=base_daily.cycleHours,
        dailyScale=base_daily.dailyScale,
    )
    for allocation in allocations:
        drones = float(allocation["drones"])
        per_drone = allocation["perDrone"]
        projected.pureGoldDelta += per_drone.pureGoldDelta * drones
        projected.shardDelta += per_drone.shardDelta * drones
        projected.lmdGross += per_drone.lmdGross * drones
        projected.exp += per_drone.exp * drones
        projected.orundum += per_drone.orundum * drones
        for item_id, count in per_drone.materialCosts.items():
            projected.materialCosts[item_id] = projected.materialCosts.get(item_id, 0.0) + count * drones
    projected.lmdNet = projected.lmdGross - projected.materialCosts.get("4001", 0.0)
    return projected


def resource_balance_penalty(
    pure_gold_delta: float,
    shard_delta: float,
    lmd_net: float,
    orundum: float,
) -> float:
    _ = pure_gold_delta
    penalty = abs(shard_delta) * 12.0
    if orundum > 0.001:
        penalty += abs(lmd_net) / 1000.0 * 5.0
    return penalty


def pure_gold_balance_error(
    pure_gold_delta: float,
    target: float = DEFAULT_PURE_GOLD_TARGET_PER_DAY,
) -> float:
    return abs(float(pure_gold_delta) - float(target))


def pure_gold_balance_quality(
    vector: ProductionVector,
    *,
    target: float = DEFAULT_PURE_GOLD_TARGET_PER_DAY,
    tolerance: float = DEFAULT_PURE_GOLD_TOLERANCE,
) -> dict[str, Any]:
    delta_from_target = vector.pureGoldDelta - target
    abs_delta = abs(delta_from_target)
    return {
        "targetPerDay": round(target, 3),
        "tolerancePerDay": round(tolerance, 3),
        "externalPureGoldAssumptionPerDay": round(-target, 3),
        "pureGoldDelta": round(vector.pureGoldDelta, 3),
        "deltaFromTarget": round(delta_from_target, 3),
        "absDeltaFromTarget": round(abs_delta, 3),
        "withinTolerance": abs_delta <= tolerance,
        "status": "within_tolerance" if abs_delta <= tolerance else "outside_tolerance",
    }


def resource_balance_quality(
    vector: ProductionVector,
    *,
    pure_gold_target: float = DEFAULT_PURE_GOLD_TARGET_PER_DAY,
    pure_gold_tolerance: float = DEFAULT_PURE_GOLD_TOLERANCE,
) -> dict[str, Any]:
    penalty = resource_balance_penalty(
        vector.pureGoldDelta, vector.shardDelta, vector.lmdNet, vector.orundum
    )
    return {
        "pureGoldAbsDelta": round(abs(vector.pureGoldDelta), 3),
        "pureGoldTarget": round(pure_gold_target, 3),
        "pureGoldDeltaFromTarget": round(vector.pureGoldDelta - pure_gold_target, 3),
        "pureGoldWithinTolerance": pure_gold_balance_error(
            vector.pureGoldDelta,
            pure_gold_target,
        )
        <= pure_gold_tolerance,
        "shardAbsDelta": round(abs(vector.shardDelta), 3),
        "lmdNetAbsDelta": round(abs(vector.lmdNet), 2),
        "lmdNetConsidered": vector.orundum > 0.001,
        "penalty": round(penalty, 3),
        "status": "balanced" if penalty <= 1.0 else "imbalanced",
    }


def vector_resource_dict(vector: ProductionVector) -> dict[str, float]:
    data = {
        "lmdGross": vector.lmdGross,
        "exp": vector.exp,
        "orundum": vector.orundum,
        "officeSpeed": vector.officeSpeed,
        "pureGoldDelta": vector.pureGoldDelta,
        "shardDelta": vector.shardDelta,
    }
    return {key: value for key, value in data.items() if abs(value) > 0.0001}


def shift_skill_context(rooms: list[RoomAssignment]) -> SkillContext:
    active_counts: dict[str, int] = {}
    power_counts: dict[str, int] = {}
    operator_rooms: dict[str, str] = {}
    for room in rooms:
        counts = faction_counts(room.operators)
        for tag, count in counts.items():
            active_counts[tag] = active_counts.get(tag, 0) + count
            if room.room_type == "POWER":
                power_counts[tag] = power_counts.get(tag, 0) + count
        for skill in room.operators:
            operator_rooms[skill.operator_name] = room.room_type
    return SkillContext(
        active_faction_counts=active_counts,
        power_faction_counts=power_counts,
        operator_rooms=operator_rooms,
    )


def control_global_effect(
    rooms: list[RoomAssignment],
    dormitories: list[RoomAssignment] | None = None,
) -> ControlGlobalEffect:
    trade_bonus = 0.0
    manufacture_bonus = 0.0
    gold_manufacture_bonus = 0.0
    blacksteel_manufacture = 0.0
    karlan_trade = 0.0
    karlan_order_limit = 0.0
    siracusa_trade = 0.0
    glasgow_trade = 0.0
    karlan_three = 0.0
    pinus_exp = 0.0
    pinus_gold = 0.0
    knight_manufacture = 0.0
    meeting_bonus = 0.0
    assumptions: list[str] = []
    assumption_map: dict[str, list[str]] = {}
    control_audit: list[dict[str, Any]] = []
    control_unsupported: list[dict[str, str]] = []
    control_counts: dict[str, int] = {}
    base_counts = base_faction_counts(rooms, dormitories or [])
    meeting_names = {
        skill.operator_name
        for room in rooms
        if room.room_type == "MEETING"
        for skill in room.operators
    }
    for room in rooms:
        if room.room_type == "CONTROL":
            for tag, count in faction_counts(room.operators).items():
                control_counts[tag] = control_counts.get(tag, 0) + count
    mujica_enthusiasm = control_mujica_enthusiasm(rooms, dormitories or [])
    for room in rooms:
        if room.room_type != "CONTROL":
            continue
        for skill in room.operators:
            text = skill.description
            label = f"{skill.operator_name}/{skill.buff_name}"
            if skill.buff_id == "control_mp_bd&trade[000]":
                bonus = float(int(mujica_enthusiasm // 8))
                if bonus > trade_bonus:
                    trade_bonus = bonus
                add_control_assumption(
                    assumption_map,
                    "trade_global",
                    f"{label} 的控制中枢热情值贸易站加成按可确定热情值 {mujica_enthusiasm:g} 点、+{bonus:g}% 计入。",
                )
                continue
            if skill.buff_id == "control_prod_tra_spd[000]":
                trade_bonus = max(trade_bonus, 7.0)
                add_control_assumption(
                    assumption_map,
                    "trade_global",
                    f"{label} 的控制中枢权变按当前静态模型外势=实地，触发贸易站 +7% 分支计入。",
                )
                continue
            if skill.buff_id == "control_dorm_bd[000]":
                add_control_assumption(
                    assumption_map,
                    "mujica_enthusiasm",
                    f"{label} 的控制中枢宿舍热情值按当前班次宿舍进驻人数计入热情值。",
                )
                continue
            if skill.buff_id == "control_meeting_spd&bd[000]":
                meeting_bonus = max(meeting_bonus, first_percent(text))
                add_control_assumption(
                    assumption_map,
                    "meeting_global",
                    f"{label} 的控制中枢会客室线索搜集速度加成按 +{first_percent(text):g}% 计入。",
                )
                continue
            if (
                skill.buff_id in {"control_meeting&mp_cost[000]", "control_meeting&mp_cost[100]"}
                or ("米诺斯干员" in text and "会客室线索搜集速度" in text)
            ):
                beta = skill.buff_id.endswith("[100]") or "β" in skill.buff_name or "+5%" in text
                per_operator = 5.0 if beta else 4.0
                cap = 25.0 if beta else 20.0
                minos = int(base_counts.get("minos", 0))
                bonus = min(cap, per_operator * minos)
                if bonus > meeting_bonus:
                    meeting_bonus = bonus
                add_control_assumption(
                    assumption_map,
                    "meeting_global",
                    f"{label} 的控制中枢米诺斯会客室加成按基建内米诺斯 {minos} 人、+{bonus:g}% 计入。",
                )
                continue
            if "每个进驻在制造站的骑士干员" in text:
                knight_manufacture = max(knight_manufacture, 7.0)
                add_control_assumption(assumption_map, "knight_manufacture", f"{label} 的控制中枢骑士制造站加成按每人 +7% 计入。")
                continue
            if "如果有2台以上" in text and "作业平台" in text and "所有制造站生产力" in text:
                if shift_power_platform_count(rooms) >= 2:
                    manufacture_bonus = max(manufacture_bonus, 2.0)
                    add_control_assumption(assumption_map, "manufacture_global", f"{label} 的控制中枢作业平台条件制造站加成按 +2% 计入。")
                continue
            if "当与" in text and "怪物猎人小队" in text and control_counts.get("mh", 0) > own_faction_count(skill, "mh"):
                if "所有制造站生产力" in text:
                    manufacture_bonus = max(manufacture_bonus, 2.0)
                    add_control_assumption(assumption_map, "manufacture_global", f"{label} 的控制中枢怪物猎人小队制造站加成按 +2% 计入。")
                elif "所有贸易站订单效率" in text:
                    trade_bonus = max(trade_bonus, 7.0)
                    add_control_assumption(assumption_map, "trade_global", f"{label} 的控制中枢怪物猎人小队贸易站加成按 +7% 计入。")
                continue
            if "每个进驻在制造站的黑钢国际干员" in text:
                blacksteel_manufacture = max(blacksteel_manufacture, 5.0)
                add_control_assumption(assumption_map, "blacksteel_manufacture", f"{label} 的控制中枢黑钢国际制造站加成按每人 +5% 计入。")
                continue
            if "每个进驻在贸易站的谢拉格干员" in text:
                karlan_trade = min(karlan_trade, -15.0)
                karlan_order_limit = max(karlan_order_limit, 6.0)
                add_control_assumption(assumption_map, "karlan_trade", f"{label} 的控制中枢谢拉格贸易站加成按每人 -15% 效率、+6 订单上限计入。")
                continue
            if "每个进驻在贸易站的叙拉古干员" in text:
                siracusa_trade = max(siracusa_trade, 5.0)
                add_control_assumption(assumption_map, "siracusa_trade", f"{label} 的控制中枢叙拉古贸易站加成按每人 +5% 计入。")
                continue
            if "每个存在3名谢拉格干员的贸易站" in text:
                karlan_three = max(karlan_three, 10.0)
                add_control_assumption(assumption_map, "karlan_three", f"{label} 的控制中枢三谢拉格贸易站加成按 +10% 计入。")
                continue
            if "同一贸易站中" in text and "格拉斯哥帮" in text:
                glasgow_trade = max(glasgow_trade, 10.0)
                add_control_assumption(assumption_map, "glasgow_trade", f"{label} 的控制中枢格拉斯哥帮贸易站加成按每人 +10% 计入。")
                continue
            if "每个进驻在制造站的红松骑士团干员" in text:
                pinus_exp = max(pinus_exp, 10.0)
                pinus_gold = min(pinus_gold, -10.0)
                add_control_assumption(assumption_map, "pinus_manufacture", f"{label} 的控制中枢红松骑士团制造站加成按经验 +10%、赤金 -10% 计入。")
                continue
            if "当与" in text and "龙门近卫局" in text and control_counts.get("lgd", 0):
                manufacture_bonus = max(manufacture_bonus, 3.0)
                add_control_assumption(assumption_map, "manufacture_global", f"{label} 的控制中枢龙门近卫局同中枢制造站加成按 +3% 计入。")
                continue
            if "伊内丝" in text and "会客室" in text and "会客室线索搜集速度" in text:
                if "伊内丝" in meeting_names:
                    meeting_bonus = max(meeting_bonus, first_percent(text))
                    add_control_assumption(
                        assumption_map,
                        "meeting_global",
                        f"{label} 的控制中枢伊内丝会客室联动按 +{first_percent(text):g}% 计入。",
                    )
                continue
            if skill.buff_id.startswith("control_prod_bd_spd"):
                base_bonus = first_percent(text)
                bonus = base_bonus * (1.0 + int(mujica_enthusiasm // 20))
                if bonus > gold_manufacture_bonus:
                    gold_manufacture_bonus = bonus
                add_control_assumption(
                    assumption_map,
                    "gold_manufacture_global",
                    f"{label} 的控制中枢贵金属制造站加成按可确定热情值 {mujica_enthusiasm:g} 点、+{bonus:g}% 计入。",
                )
                control_unsupported.append(
                    unsupported(
                        skill,
                        "控制中枢热情值递增加成仅按当前班次可确定来源计入；跨班/动态状态尚未完全建模。",
                    )
                )
                control_audit.append(
                    skill_audit(
                        skill,
                        "unsupported",
                        "control_global:gold_manufacture_heat",
                        "Mujica enthusiasm scaling is counted from deterministic shift-local sources; cross-shift or dynamic state is not modeled.",
                        "unmodeled",
                    )
                )
                continue
            if has_unmodeled_control_condition(text):
                reason = "Unmodeled conditional Control Center effect was not silently counted."
                control_unsupported.append(
                    unsupported(skill, "控制中枢复杂条件效果尚未完全建模，未计入全局加成。")
                )
                control_audit.append(
                    skill_audit(
                        skill,
                        "unsupported",
                        "control_global",
                        reason,
                        "unmodeled",
                    )
                )
                continue
            bonus = first_percent(text)
            if bonus <= 0:
                continue
            if "所有贸易站" in text and ("订单效率" in text or "订单获取效率" in text):
                if bonus > trade_bonus:
                    trade_bonus = bonus
                add_control_assumption(assumption_map, "trade_global", f"{label} 的控制中枢全局贸易站加成按 +{bonus:g}% 计入。")
            elif "所有制造站生产力" in text:
                if bonus > manufacture_bonus:
                    manufacture_bonus = bonus
                add_control_assumption(assumption_map, "manufacture_global", f"{label} 的控制中枢全局制造站加成按 +{bonus:g}% 计入。")
            elif "所有生产" in text and "贵金属" in text and "制造站生产力" in text:
                if bonus > gold_manufacture_bonus:
                    gold_manufacture_bonus = bonus
                add_control_assumption(assumption_map, "gold_manufacture_global", f"{label} 的控制中枢贵金属制造站加成按 +{bonus:g}% 计入。")
    return ControlGlobalEffect(
        trading_speed_percent=trade_bonus,
        manufacture_speed_percent=manufacture_bonus,
        gold_manufacture_speed_percent=gold_manufacture_bonus,
        blacksteel_manufacture_speed_per_operator=blacksteel_manufacture,
        karlan_trading_speed_per_operator=karlan_trade,
        karlan_trading_order_limit_per_operator=karlan_order_limit,
        siracusa_trading_speed_per_operator=siracusa_trade,
        glasgow_trading_speed_per_operator=glasgow_trade,
        karlan_three_trading_speed_percent=karlan_three,
        pinus_exp_manufacture_speed_per_operator=pinus_exp,
        pinus_gold_manufacture_speed_per_operator=pinus_gold,
        knight_manufacture_speed_per_operator=knight_manufacture,
        meeting_speed_percent=meeting_bonus,
        assumptions=tuple(dedupe_strings(assumptions)),
        assumption_map={key: tuple(dedupe_strings(value)) for key, value in assumption_map.items()},
        audit_map={
            key: tuple(control_audit_from_assumptions(key, dedupe_strings(value)))
            for key, value in assumption_map.items()
        },
        control_audit=tuple(dedupe_skill_effect_audit(control_audit)),
        unsupported=tuple(dedupe_unsupported(control_unsupported)),
    )


def shift_power_platform_count(rooms: list[RoomAssignment]) -> int:
    total = 0
    for room in rooms:
        if room.room_type == "POWER":
            total += faction_counts(room.operators).get("op", 0)
    return total


def base_faction_counts(
    rooms: list[RoomAssignment],
    dormitories: list[RoomAssignment],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for room in [*rooms, *dormitories]:
        for tag, count in faction_counts(room.operators).items():
            counts[tag] = counts.get(tag, 0) + count
    return counts


def control_mujica_enthusiasm(
    rooms: list[RoomAssignment],
    dormitories: list[RoomAssignment],
) -> float:
    total = 0.0
    dorm_operator_count = sum(len(room.operators) for room in dormitories)
    for room in rooms:
        if room.room_type != "CONTROL":
            continue
        for skill in room.operators:
            if skill.buff_id == "control_mp_bd&trade[000]":
                total += 20.0
            elif skill.buff_id in {"control_meeting_spd&bd[000]", "control_hire_spd&bd[000]"}:
                total += 10.0
            elif skill.buff_id == "control_dorm_bd[000]":
                total += float(dorm_operator_count)
    return total


def add_control_assumption(mapping: dict[str, list[str]], key: str, text: str) -> None:
    mapping.setdefault(key, []).append(text)


def has_unmodeled_control_condition(text: str) -> bool:
    conditional_markers = ("如果", "当与", "每个进驻", "每有", "每个存在", "低于", "大于")
    return any(marker in text for marker in conditional_markers)


def first_percent(text: str) -> float:
    match = re.search(r"\+(\d+(?:\.\d+)?)%", text)
    return float(match.group(1)) if match else 0.0


def dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def control_audit_from_assumptions(key: str, assumptions: list[str]) -> list[dict[str, Any]]:
    audit: list[dict[str, Any]] = []
    for assumption in assumptions:
        operator = ""
        buff_name = ""
        label = assumption.split(" 的控制中枢", 1)[0]
        if "/" in label:
            operator, buff_name = label.split("/", 1)
        audit.append(
            {
                "operator": operator,
                "buffId": "",
                "buffName": buff_name,
                "status": "counted",
                "scope": f"control_global:{key}",
                "reason": assumption,
                "confidence": "modeled_rule",
            }
        )
    return audit


def dedupe_skill_effect_audit(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = (
            str(item.get("operator", "")),
            str(item.get("buffId", "")),
            str(item.get("status", "")),
            str(item.get("scope", "")),
            str(item.get("reason", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def score_breakdown_for(vector: ProductionVector) -> dict[str, float]:
    lmd_net_score = 0.0 if vector.orundum > 0.001 else vector.lmdNet / 1000.0 * 6.0
    return {
        "orundum": vector.orundum,
        "office": vector.officeSpeed / 30.0,
        "meeting": vector.meetingSpeed / 30.0,
        "lmdGross": vector.lmdGross / 1000.0 * 1.5,
        "lmdNet": lmd_net_score,
        "exp": vector.exp / 1000.0 * 4.0,
        "shardBalance": -abs(vector.shardDelta) * 12.0,
        "lmdNetBalance": -abs(vector.lmdNet) / 1000.0 * 5.0
        if vector.orundum > 0.001
        else 0.0,
        "overflowPenalty": -vector.overflowLoss * 5.0,
        "fatiguePenalty": -vector.fatigueRisk * 2.0,
    }


def dedupe_unsupported(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, str]] = []
    for item in items:
        key = (item.get("operator", ""), item.get("buffId", ""), item.get("reason", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
