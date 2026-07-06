from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PHASE_LABELS = {
    0: "未精英化",
    1: "精英1",
    2: "精英2",
}


def phase_to_int(value: str | int | None) -> int:
    if isinstance(value, int):
        return max(0, value)
    if not value:
        return 0
    mapping = {
        "PHASE_0": 0,
        "PHASE_1": 1,
        "PHASE_2": 2,
    }
    return mapping.get(str(value).upper(), 0)


def rarity_to_int(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    if not value:
        return 0
    text = str(value)
    if text.startswith("TIER_"):
        try:
            return int(text.rsplit("_", 1)[1])
        except ValueError:
            return 0
    try:
        return int(text)
    except ValueError:
        return 0


@dataclass(frozen=True)
class RosterOperator:
    name: str
    recruited: bool
    rarity: int
    level: int
    elite: int
    potential: int | None = None
    skill_level: int = 0
    masteries: tuple[int, int, int] = (0, 0, 0)
    modules: dict[str, int] = field(default_factory=dict)
    source_row: int | None = None

    @property
    def progression_label(self) -> str:
        return f"{PHASE_LABELS.get(self.elite, f'精英{self.elite}')} {self.level}级"


@dataclass(frozen=True)
class UpgradeRequirement:
    char_id: str
    name: str
    from_elite: int
    from_level: int
    to_elite: int
    to_level: int
    cost_score: float
    materials: list[dict[str, Any]]
    note: str

    @property
    def target_label(self) -> str:
        return f"{PHASE_LABELS.get(self.to_elite, f'精英{self.to_elite}')} {self.to_level}级"


@dataclass(frozen=True)
class BaseSkill:
    char_id: str
    operator_name: str
    room_type: str
    buff_id: str
    buff_name: str
    description: str
    efficiency: float
    targets: tuple[str, ...]
    cond_elite: int
    cond_level: int
    unlocked: bool
    complex_condition: bool
    parsed_score: float
    upgrade: UpgradeRequirement | None = None
    faction_tags: tuple[str, ...] = ()

    @property
    def condition_label(self) -> str:
        return f"{PHASE_LABELS.get(self.cond_elite, f'精英{self.cond_elite}')} {self.cond_level}级"


@dataclass(frozen=True)
class RoomAssignment:
    room_id: str
    room_type: str
    room_name: str
    target: str | None
    operators: list[BaseSkill]
    score: float
    room_level: int = 3
    slots: int | None = None
    product_capacity: int | None = None
    order_limit: int | None = None


@dataclass(frozen=True)
class ShiftPlan:
    name: str
    start: str
    duration_hours: float
    rooms: list[RoomAssignment]
    dormitories: list[RoomAssignment]


@dataclass(frozen=True)
class Layout:
    raw: str
    trading: int
    manufacture: int
    power: int
    dormitory: int = 4
    control: int = 1
    meeting: int = 1
    hire: int = 1
    training: int = 1
    workshop: int = 1
    trading_levels: tuple[int, ...] = ()
    manufacture_levels: tuple[int, ...] = ()
    manufacture_slots: tuple[int, ...] = ()
    power_levels: tuple[int, ...] = ()
    dormitory_levels: tuple[int, ...] = ()
    control_level: int = 5
    meeting_level: int = 3
    hire_level: int = 3
    training_level: int = 3
    workshop_level: int = 3
    right_side_preset: str = "full"
    power_adjustment: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.trading}{self.manufacture}{self.power}"
