from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import Workbook

from .data import GameData, rarity_to_int
from .models import RosterOperator
from .roster import load_roster_xlsx


SHEET_NAME = "干员练度表"
CORE_HEADERS = (
    "干员名称",
    "是否已招募",
    "星级",
    "等级",
    "精英化等级",
    "潜能等级",
    "通用技能等级",
    "1技能专精等级",
    "2技能专精等级",
    "3技能专精等级",
)
MODULE_HEADERS = ("χ分支模组", "γ分支模组", "Δ分支模组", "α分支模组")
HEADERS = (*CORE_HEADERS, *MODULE_HEADERS)


@dataclass(frozen=True)
class FullRosterRow:
    char_id: str
    name: str
    rarity: int
    elite: int
    level: int

    def to_xlsx_row(self) -> list[Any]:
        return [
            self.name,
            True,
            self.rarity,
            self.level,
            self.elite,
            6,
            7,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
        ]


def build_full_roster_rows(game_data: GameData) -> list[FullRosterRow]:
    rows: list[FullRosterRow] = []
    for char_id, character in game_data.characters.items():
        if not is_roster_operator_record(char_id, character):
            continue
        phases = character.get("phases") or []
        elite = len(phases) - 1
        level = int((phases[elite] or {}).get("maxLevel") or 1)
        rows.append(
            FullRosterRow(
                char_id=char_id,
                name=str(character["name"]),
                rarity=rarity_to_int(character.get("rarity")),
                elite=elite,
                level=level,
            )
        )
    return sorted(rows, key=lambda row: (row.rarity, row.char_id))


def is_roster_operator_record(char_id: str, character: dict[str, Any]) -> bool:
    if not char_id.startswith("char_"):
        return False
    if not character.get("name"):
        return False
    if not character.get("phases"):
        return False
    if character.get("profession") in {"TOKEN", "TRAP"}:
        return False
    return True


def write_full_roster_xlsx(path: Path, game_data: GameData) -> dict[str, Any]:
    rows = build_full_roster_rows(game_data)
    path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_NAME
    sheet.append(list(HEADERS))
    for row in rows:
        sheet.append(row.to_xlsx_row())
    workbook.save(path)
    workbook.close()

    loaded = load_roster_xlsx(path)
    validation = validate_loaded_roster(rows, loaded)
    summary = {
        "path": str(path),
        "sheetName": SHEET_NAME,
        "headerCount": len(HEADERS),
        "generatedOperatorCount": len(rows),
        "loadedOperatorCount": len(loaded),
        "recruitedOperatorCount": sum(1 for operator in loaded if operator.recruited),
        "excludedNonCharOrTokenTrapCount": excluded_record_count(game_data),
        "dataVersion": game_data.data_version,
        "validation": validation,
    }
    if not validation["passed"]:
        raise ValueError(f"Generated full roster failed validation: {validation}")
    return summary


def validate_loaded_roster(
    rows: list[FullRosterRow],
    loaded: list[RosterOperator],
) -> dict[str, Any]:
    expected_names = {row.name for row in rows}
    loaded_names = {operator.name for operator in loaded}
    missing = sorted(expected_names - loaded_names)
    unexpected = sorted(loaded_names - expected_names)
    not_maxed = [
        operator.name
        for operator in loaded
        if not operator.recruited
        or operator.potential != 6
        or operator.skill_level != 7
        or operator.masteries != (3, 3, 3)
        or any(value != 3 for value in operator.modules.values())
    ]
    return {
        "passed": not missing and not unexpected and not not_maxed,
        "missing": missing[:20],
        "unexpected": unexpected[:20],
        "notMaxed": not_maxed[:20],
    }


def excluded_record_count(game_data: GameData) -> int:
    return sum(
        1
        for char_id, character in game_data.characters.items()
        if character.get("name")
        and character.get("phases")
        and not is_roster_operator_record(char_id, character)
    )
