from __future__ import annotations


SINGLE_STATION_ROOM_TYPES = {"HIRE"}


def station_limit(room_type: str) -> int | None:
    if room_type in SINGLE_STATION_ROOM_TYPES:
        return 1
    return None


def clamp_station_count(room_type: str, count: int) -> int:
    value = max(0, int(count))
    limit = station_limit(room_type)
    if limit is None:
        return value
    return min(value, limit)
