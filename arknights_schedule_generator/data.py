from __future__ import annotations

import json
import re
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import (
    BaseSkill,
    RosterOperator,
    UpgradeRequirement,
    phase_to_int,
    rarity_to_int,
)


RAW_BASE_URL = (
    "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/"
    "master/zh_CN/gamedata/excel"
)
JSDELIVR_BASE_URL = (
    "https://cdn.jsdelivr.net/gh/Kengxxiao/ArknightsGameData@"
    "master/zh_CN/gamedata/excel"
)
REQUIRED_FILES = (
    "building_data.json",
    "character_table.json",
    "item_table.json",
    "data_version.txt",
)
DOWNLOAD_USER_AGENT = (
    "arknights-schedule-generator/0.1 "
    "(+https://github.com/ChineseCricket/arknights_scheduel_generator)"
)

# Keep an explicit ssl module reference so frozen builds include urllib's HTTPS
# support, not only the _ssl binary extension.
_HTTPS_SSL_CONTEXT_FACTORY = ssl.create_default_context


@dataclass(frozen=True)
class DataSource:
    name: str
    base_url: str


DEFAULT_DATA_SOURCES = (
    DataSource("jsDelivr GitHub CDN", JSDELIVR_BASE_URL),
    DataSource("GitHub raw", RAW_BASE_URL),
)


class DataDownloadError(RuntimeError):
    pass

COMPLEX_MARKERS = (
    "当与",
    "每个",
    "每有",
    "归零",
    "低于",
    "大于",
    "小于",
    "心情",
    "感知信息",
    "人间烟火",
    "巫术结晶",
    "乌萨斯特饮",
    "特别订单",
    "违约订单",
    "仓库容量",
)

ROOM_KEYWORDS = {
    "MANUFACTURE": ("生产力",),
    "TRADING": ("订单获取效率",),
    "POWER": ("无人机充能速度",),
    "MEETING": ("线索搜集速度",),
    "HIRE": ("人脉资源", "公开招募"),
    "CONTROL": ("制造站生产力", "生产力", "订单获取效率", "心情消耗"),
    "DORMITORY": ("恢复",),
}

FACTION_TAG_ALIASES = {
    "blacksteel": ("bs",),
    "rhine": ("rh",),
    "student": ("ussg",),
    "rainbow": ("R6",),
    "pinus": ("psk",),
    "sami": ("sm",),
    "reserve1": ("A1",),
    "action4": ("mh",),
    "kazimierz": ("knight",),
}

SPECIAL_FACTION_TAGS_BY_CHAR_ID = {
    "char_1036_fang2": ("A1",),
    "char_1048_orchd2": ("mh2",),
    "char_4215_buddy": ("mh2",),
}


def download_data(
    data_dir: Path,
    force: bool = False,
    sources: Sequence[DataSource] = DEFAULT_DATA_SOURCES,
) -> list[Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    targets = [data_dir / file_name for file_name in REQUIRED_FILES]
    if not force and all(target.exists() for target in targets):
        return targets

    source_index = 0
    with tempfile.TemporaryDirectory(prefix=".download-", dir=data_dir) as temp_dir:
        staging_dir = Path(temp_dir)
        for file_name in REQUIRED_FILES:
            data, source_index = download_file_bytes(file_name, sources, source_index)
            validate_downloaded_file(file_name, data)
            (staging_dir / file_name).write_bytes(data)
        for file_name, target in zip(REQUIRED_FILES, targets):
            (staging_dir / file_name).replace(target)
    return targets


def download_file_bytes(
    file_name: str,
    sources: Sequence[DataSource] = DEFAULT_DATA_SOURCES,
    preferred_index: int = 0,
) -> tuple[bytes, int]:
    if not sources:
        raise DataDownloadError("No game data download sources configured.")

    errors: list[str] = []
    source_count = len(sources)
    for offset in range(source_count):
        index = (preferred_index + offset) % source_count
        source = sources[index]
        url = f"{source.base_url.rstrip('/')}/{file_name}"
        request = Request(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "User-Agent": DOWNLOAD_USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                return response.read(), index
        except HTTPError as exc:
            errors.append(f"{source.name} returned HTTP {exc.code} for {url}")
        except URLError as exc:
            errors.append(f"{source.name} failed for {url}: {exc.reason}")
        except OSError as exc:
            errors.append(f"{source.name} failed for {url}: {exc}")

    detail = "; ".join(errors)
    raise DataDownloadError(f"Failed to download {file_name}. {detail}")


def validate_downloaded_file(file_name: str, data: bytes) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataDownloadError(f"Downloaded {file_name} is not valid UTF-8.") from exc
    if file_name.endswith(".json"):
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise DataDownloadError(f"Downloaded {file_name} is not valid JSON.") from exc
    elif file_name == "data_version.txt" and not text.strip():
        raise DataDownloadError("Downloaded data_version.txt is empty.")


@dataclass
class GameData:
    building: dict[str, Any]
    characters: dict[str, Any]
    items: dict[str, Any]
    data_version: str = "unknown"

    @classmethod
    def load(cls, data_dir: Path) -> "GameData":
        missing = [name for name in REQUIRED_FILES[:3] if not (data_dir / name).exists()]
        if missing:
            joined = ", ".join(missing)
            raise FileNotFoundError(
                f"缺少游戏数据文件: {joined}。请先运行 update-data 或传入 --auto-update。"
            )
        version_path = data_dir / "data_version.txt"
        data_version = (
            version_path.read_text(encoding="utf-8").strip()
            if version_path.exists()
            else "unknown"
        )
        return cls(
            building=json.loads((data_dir / "building_data.json").read_text(encoding="utf-8")),
            characters=json.loads((data_dir / "character_table.json").read_text(encoding="utf-8")),
            items=json.loads((data_dir / "item_table.json").read_text(encoding="utf-8")),
            data_version=data_version,
        )

    @property
    def char_id_by_name(self) -> dict[str, str]:
        result: dict[str, str] = {}
        names = {
            str(data.get("name"))
            for data in self.characters.values()
            if data.get("name")
        }
        for name in names:
            char_id = self.resolve_roster_char_id(name)
            if char_id:
                result[name] = char_id
        return result

    def resolve_roster_char_id(self, name: str) -> str | None:
        matches = [
            (char_id, data)
            for char_id, data in self.characters.items()
            if data.get("name") == name
        ]
        if not matches:
            return None

        def priority(item: tuple[str, dict[str, Any]]) -> tuple[int, int, int, str]:
            char_id, data = item
            profession = str(data.get("profession") or "")
            has_building = char_id in self.building.get("chars", {})
            is_real_char = char_id.startswith("char_")
            is_token_or_trap = profession in {"TOKEN", "TRAP"} or char_id.startswith(("token_", "trap_"))
            return (
                1 if has_building else 0,
                1 if is_real_char and not is_token_or_trap else 0,
                0 if is_token_or_trap else 1,
                char_id,
            )

        return max(matches, key=priority)[0]

    def char_name(self, char_id: str) -> str:
        return self.characters.get(char_id, {}).get("name", char_id)

    def char_rarity(self, char_id: str, roster: RosterOperator | None = None) -> int:
        if roster and roster.rarity:
            return roster.rarity
        return rarity_to_int(self.characters.get(char_id, {}).get("rarity"))

    def item_record(self, item_id: str) -> dict[str, Any]:
        item_id = str(item_id)
        if item_id in self.items and isinstance(self.items[item_id], dict):
            return self.items[item_id]
        for section in ("items", "expItems"):
            table = self.items.get(section, {})
            if isinstance(table, dict) and isinstance(table.get(item_id), dict):
                return table[item_id]
        return {}

    def material_name(self, item_id: str) -> str:
        return self.item_record(item_id).get("name", str(item_id))

    def material_weight(self, item_id: str) -> float:
        item = self.item_record(item_id)
        rarity = rarity_to_int(item.get("rarity"))
        return max(1, rarity + 1) * 100.0

    def phase_max_level(self, char_id: str, phase: int) -> int:
        phases = self.characters.get(char_id, {}).get("phases", [])
        if 0 <= phase < len(phases):
            return int(phases[phase].get("maxLevel") or 1)
        return 1

    def evolution_cost(self, char_id: str, target_phase: int) -> list[dict[str, Any]]:
        phases = self.characters.get(char_id, {}).get("phases", [])
        if not (0 <= target_phase < len(phases)):
            return []
        costs = phases[target_phase].get("evolveCost") or []
        result: list[dict[str, Any]] = []
        for cost in costs:
            item_id = str(cost.get("id"))
            result.append(
                {
                    "id": item_id,
                    "name": self.material_name(item_id),
                    "count": int(cost.get("count") or 0),
                    "type": cost.get("type"),
                }
            )
        return result

    def upgrade_requirement(
        self,
        char_id: str,
        roster: RosterOperator,
        target_elite: int,
        target_level: int,
    ) -> UpgradeRequirement | None:
        if roster.elite > target_elite:
            return None
        if roster.elite == target_elite and roster.level >= target_level:
            return None

        rarity = max(1, self.char_rarity(char_id, roster))
        materials: list[dict[str, Any]] = []
        level_steps = 0
        current_elite = roster.elite
        current_level = max(1, roster.level)

        for phase in range(current_elite + 1, target_elite + 1):
            level_steps += max(0, self.phase_max_level(char_id, phase - 1) - current_level)
            materials.extend(self.evolution_cost(char_id, phase))
            current_level = 1

        if current_elite == target_elite:
            level_steps += max(0, target_level - current_level)
        elif target_level > 1:
            level_steps += target_level - 1

        material_score = sum(
            float(mat["count"]) * self.material_weight(str(mat["id"])) for mat in materials
        )
        level_score = float(level_steps * rarity * 25)
        return UpgradeRequirement(
            char_id=char_id,
            name=roster.name,
            from_elite=roster.elite,
            from_level=roster.level,
            to_elite=target_elite,
            to_level=target_level,
            cost_score=level_score + material_score,
            materials=materials,
            note="cost_score 是用于优化排序的估算资源权重，精英化材料为游戏数据原始材料。",
        )

    def skills_for_roster_operator(
        self,
        roster: RosterOperator,
        allow_upgrades: bool = False,
    ) -> list[BaseSkill]:
        char_id = self.resolve_roster_char_id(roster.name)
        if not char_id:
            return []
        base_char = self.building.get("chars", {}).get(char_id)
        if not base_char:
            return []
        faction_tags = self.faction_tags_for_char(char_id)

        skills: list[BaseSkill] = []
        buffs = self.building.get("buffs", {})
        for group in base_char.get("buffChar", []):
            for buff_data in group.get("buffData", []):
                buff_id = buff_data.get("buffId")
                buff = buffs.get(buff_id)
                if not buff:
                    continue
                cond = buff_data.get("cond", {})
                cond_elite = phase_to_int(cond.get("phase"))
                cond_level = int(cond.get("level") or 1)
                unlocked = roster.elite > cond_elite or (
                    roster.elite == cond_elite and roster.level >= cond_level
                )
                upgrade = None
                if not unlocked:
                    if not allow_upgrades:
                        continue
                    upgrade = self.upgrade_requirement(char_id, roster, cond_elite, cond_level)
                    if upgrade is None:
                        continue
                description = strip_rich_text(buff.get("description", ""))
                room_type = buff.get("roomType") or ""
                skills.append(
                    BaseSkill(
                        char_id=char_id,
                        operator_name=roster.name,
                        room_type=room_type,
                        buff_id=buff_id,
                        buff_name=buff.get("buffName", buff_id),
                        description=description,
                        efficiency=float(buff.get("efficiency") or 0),
                        targets=tuple(buff.get("targets") or ()),
                        cond_elite=cond_elite,
                        cond_level=cond_level,
                        unlocked=unlocked,
                        complex_condition=is_complex(description),
                        parsed_score=parse_skill_score(room_type, description, buff.get("efficiency")),
                        upgrade=upgrade,
                        faction_tags=faction_tags,
                    )
                )
        return skills

    def faction_tags_for_char(self, char_id: str) -> tuple[str, ...]:
        character = self.characters.get(char_id, {})
        tags: set[str] = set()
        for field, text in faction_power_values(character):
            tags.add(text)
            tags.add(f"{field}:{text}")
            tags.update(FACTION_TAG_ALIASES.get(text, ()))
        if "支援机械" in (character.get("tagList") or []):
            tags.add("op")
        if character.get("isSpChar"):
            tags.add("sp")
        tags.update(SPECIAL_FACTION_TAGS_BY_CHAR_ID.get(char_id, ()))
        return tuple(sorted(tags))


def faction_power_values(character: dict[str, Any]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    add_faction_power_values(values, character)

    main_power = character.get("mainPower")
    if isinstance(main_power, dict):
        add_faction_power_values(values, main_power)

    sub_power = character.get("subPower") or []
    if isinstance(sub_power, list):
        for power in sub_power:
            if isinstance(power, dict):
                add_faction_power_values(values, power)

    return values


def add_faction_power_values(values: list[tuple[str, str]], power: dict[str, Any]) -> None:
    for field in ("nationId", "groupId", "teamId"):
        value = power.get(field)
        if value:
            values.append((field, str(value)))


def strip_rich_text(text: str) -> str:
    text = re.sub(r"<\$[^>]+>", "", text)
    text = re.sub(r"</?\$[^>]*>", "", text)
    text = re.sub(r"</?@[^>]*>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("</>", "")


def is_complex(description: str) -> bool:
    return any(marker in description for marker in COMPLEX_MARKERS)


def parse_skill_score(room_type: str, description: str, efficiency: Any) -> float:
    try:
        value = float(efficiency or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return value

    keywords = ROOM_KEYWORDS.get(room_type, ())
    if not keywords or not any(keyword in description for keyword in keywords):
        return 0.0

    percentages = [float(match) for match in re.findall(r"\+(\d+(?:\.\d+)?)%", description)]
    if percentages:
        return max(percentages)

    if room_type == "DORMITORY":
        recoveries = [
            float(match) for match in re.findall(r"恢复\+(\d+(?:\.\d+)?)", description)
        ]
        if recoveries:
            return max(recoveries) * 10.0

    return 0.0
