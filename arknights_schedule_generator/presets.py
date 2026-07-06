from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Layout


MODE_ALIASES = {
    "normal": "normal",
    "gold": "normal",
    "不搓玉": "normal",
    "balanced-orundum": "balanced_orundum",
    "balanced_orundum": "balanced_orundum",
    "orundum": "balanced_orundum",
    "搓玉": "balanced_orundum",
    "max-orundum": "max_orundum",
    "max_orundum": "max_orundum",
    "极限搓玉": "max_orundum",
}

TARGET_LABELS = {
    "F_EXP": "作战记录",
    "F_GOLD": "赤金",
    "F_DIAMOND": "源石碎片",
    "O_GOLD": "赤金订单",
    "O_DIAMOND": "源石订单",
}


@dataclass(frozen=True)
class ModePreset:
    id: str
    label: str
    min_lmd_trading: int
    min_exp_factories: int
    min_shard_factories: int
    prefer_orundum: bool
    required_resource_chains: tuple[str, ...]
    primary_metrics: tuple[str, ...]
    lmd_policy: str
    inventory_policy: str
    drone_policy: str
    target_selection_penalties: tuple[str, ...]


PRESETS = {
    "normal": ModePreset(
        id="normal",
        label="不搓玉",
        min_lmd_trading=1,
        min_exp_factories=1,
        min_shard_factories=0,
        prefer_orundum=False,
        required_resource_chains=("lmd_trading", "gold_factory", "exp_factory"),
        primary_metrics=("lmdNet", "exp"),
        lmd_policy="lmd_net_primary",
        inventory_policy="prefer_stable_gold_no_shard_requirement",
        drone_policy="reported_separately",
        target_selection_penalties=("inventory_balance",),
    ),
    "balanced_orundum": ModePreset(
        id="balanced_orundum",
        label="平衡搓玉",
        min_lmd_trading=1,
        min_exp_factories=1,
        min_shard_factories=1,
        prefer_orundum=True,
        required_resource_chains=(
            "lmd_trading",
            "gold_factory",
            "exp_factory",
            "shard_factory",
            "orundum_trading",
        ),
        primary_metrics=("orundum", "lmdGross", "exp"),
        lmd_policy="lmd_net_balance_required",
        inventory_policy="prefer_near_zero_gold_shard_and_lmd_net_delta",
        drone_policy="reported_separately",
        target_selection_penalties=("inventory_balance", "lmd_net_balance"),
    ),
    "max_orundum": ModePreset(
        id="max_orundum",
        label="极限搓玉",
        min_lmd_trading=1,
        min_exp_factories=0,
        min_shard_factories=1,
        prefer_orundum=True,
        required_resource_chains=("lmd_trading", "gold_factory", "shard_factory", "orundum_trading"),
        primary_metrics=("orundum", "lmdGross"),
        lmd_policy="lmd_net_balance_penalized_not_primary",
        inventory_policy="prefer_stable_gold_shard_and_lmd_net_delta",
        drone_policy="reported_separately",
        target_selection_penalties=("inventory_balance", "lmd_net_balance"),
    ),
}


def normalize_mode(mode: str) -> str:
    key = MODE_ALIASES.get(mode.strip().lower())
    if not key:
        raise ValueError(
            "--mode 只支持 normal/不搓玉、balanced-orundum/搓玉 或 max-orundum/极限搓玉。"
        )
    return key


def mode_preset(mode: str) -> ModePreset:
    return PRESETS[normalize_mode(mode)]


def preset_contract(mode: str) -> dict[str, Any]:
    preset = mode_preset(mode)
    return {
        "id": preset.id,
        "label": preset.label,
        "requiredResourceChains": list(preset.required_resource_chains),
        "minimumRooms": {
            "lmdTrading": preset.min_lmd_trading,
            "expFactories": preset.min_exp_factories,
            "shardFactories": preset.min_shard_factories,
        },
        "primaryMetrics": list(preset.primary_metrics),
        "lmdPolicy": preset.lmd_policy,
        "inventoryPolicy": preset.inventory_policy,
        "drones": preset.drone_policy,
        "targetSelectionPenalties": list(preset.target_selection_penalties),
    }


def preset_contracts() -> dict[str, dict[str, Any]]:
    return {mode: preset_contract(mode) for mode in PRESETS}


def target_label(target: str | None) -> str | None:
    if target is None:
        return None
    return TARGET_LABELS.get(target, target)


def trading_targets(layout: Layout, mode: str) -> list[str]:
    preset = mode_preset(mode)
    lmd_count = min(layout.trading, max(1, preset.min_lmd_trading))
    if not preset.prefer_orundum:
        return ["O_GOLD"] * layout.trading
    targets = ["O_GOLD"] * lmd_count
    targets.extend(["O_DIAMOND"] * (layout.trading - lmd_count))
    return targets


def manufacture_targets(layout: Layout, mode: str) -> list[str]:
    preset = mode_preset(mode)
    if not preset.prefer_orundum:
        available_for_gold = max(0, layout.manufacture - preset.min_exp_factories)
        gold_count = min(layout.trading, available_for_gold)
        return ["F_GOLD"] * gold_count + ["F_EXP"] * (layout.manufacture - gold_count)

    targets: list[str] = []
    targets.extend(["F_EXP"] * min(layout.manufacture, preset.min_exp_factories))
    remaining = layout.manufacture - len(targets)
    targets.extend(["F_DIAMOND"] * min(remaining, preset.min_shard_factories))
    remaining = layout.manufacture - len(targets)
    gold_needed = min(remaining, max(1, preset.min_lmd_trading))
    targets.extend(["F_GOLD"] * gold_needed)
    remaining = layout.manufacture - len(targets)
    targets.extend(["F_DIAMOND"] * remaining)
    return targets
