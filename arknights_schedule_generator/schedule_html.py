from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="schedule-html",
        description="Render one exact generated schedule JSON as a standalone HTML report.",
    )
    parser.add_argument("schedule_json", help="Generated schedule JSON to render.")
    parser.add_argument("--output", required=True, help="HTML report path.")
    args = parser.parse_args(argv)
    write_schedule_html(Path(args.schedule_json), Path(args.output))
    print(f"Wrote schedule HTML report: {args.output}")
    return 0


def write_schedule_html(schedule_path: Path, output_path: Path) -> None:
    data = json.loads(schedule_path.read_text(encoding="utf-8"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_schedule_html(data, schedule_path), encoding="utf-8")


def render_schedule_html(data: dict[str, Any], source_path: Path) -> str:
    title = str(data.get("title") or "Schedule Report")
    daily = data.get("dailyExpected") or {}
    analysis = data.get("analysis") or {}
    policy = analysis.get("pureGoldBalancePolicy") or {}
    cache = analysis.get("cacheValidation") or {}
    local = analysis.get("localOptimalityAudit") or {}
    pool = analysis.get("candidatePoolAudit") or {}
    conflict = analysis.get("objectiveConflictAudit") or {}
    search = analysis.get("diagnosticInsertionSearch") or {}
    reminders = analysis.get("reminders") or []
    score_breakdown = data.get("scoreBreakdown") or {}
    shifts = data.get("shifts") or []
    drone_targets = daily.get("droneTargets") or []
    drone_by_shift: dict[str, list[dict[str, Any]]] = {}
    for target in drone_targets:
        drone_by_shift.setdefault(str(target.get("shift") or ""), []).append(target)

    body = [
        "<!doctype html>",
        "<html lang=\"zh-CN\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{escape(title)}</title>",
        "<style>",
        CSS,
        "</style>",
        "</head>",
        "<body>",
        "<main>",
        "<header class=\"hero\">",
        f"<p class=\"eyebrow\">Exact Schedule Report</p><h1>{escape(title)}</h1>",
        f"<p class=\"muted\">Rendered directly from <code>{escape(str(source_path))}</code>; no recommendation re-ranking is applied.</p>",
        "</header>",
        metric_grid(
            [
                ("Score", fmt(data.get("score"))),
                ("Plans", str(data.get("planTimes") or len(shifts))),
                ("LMD Gross", fmt(daily.get("lmdGross"))),
                ("LMD Net", fmt(daily.get("lmdNet"))),
                ("EXP", fmt(daily.get("exp"))),
                ("Orundum", fmt(daily.get("orundum"))),
                ("Pure Gold", fmt(daily.get("pureGoldDelta"))),
                ("Shard", fmt(daily.get("shardDelta"))),
                ("Drones Used", f"{fmt(daily.get('droneUsed'))} / {fmt(daily.get('droneCount'))}"),
            ]
        ),
        section(
            "Pure Gold Balance Policy",
            detail_table(
                [
                    ("Status", policy.get("status")),
                    ("Target / day", policy.get("targetPerDay")),
                    ("Tolerance / day", policy.get("tolerancePerDay")),
                    ("Repeat count", policy.get("repeatCount")),
                    ("Cycle repeated", policy.get("cycleRepeated")),
                    ("Final Pure Gold", policy.get("finalPureGoldDelta")),
                    ("Final delta from target", policy.get("finalDeltaFromTarget")),
                    ("External assumption / day", policy.get("externalPureGoldAssumptionPerDay")),
                    ("Reason", policy.get("reason")),
                ]
            ),
        ),
        section(
            "Audit Summary",
            detail_table(
                [
                    ("Cache status", cache.get("status")),
                    ("Optimizer model", cache.get("optimizerModelVersion")),
                    ("Remaining local positives", local.get("remainingPositiveCount")),
                    ("Accepted local replacements", local.get("acceptedCount")),
                    ("LMD-positive rejected", conflict.get("lmdPositiveRejectedCount")),
                    ("Candidate pool checks", len(pool.get("comboPoolChecks") or [])),
                ]
            ),
        ),
        section("Diagnostic Comparisons", diagnostic_table(search)),
        section("Score Breakdown", key_value_chips(score_breakdown)),
        section("Warnings", warning_list(reminders)),
        section("Schedule", render_shifts(shifts, drone_by_shift)),
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(body)


def metric_grid(items: list[tuple[str, str]]) -> str:
    cards = "\n".join(
        f"<div class=\"metric\"><span>{escape(label)}</span><strong>{escape(value)}</strong></div>"
        for label, value in items
    )
    return f"<section class=\"metrics\">{cards}</section>"


def section(title: str, content: str) -> str:
    return f"<section class=\"section\"><h2>{escape(title)}</h2>{content}</section>"


def detail_table(rows: list[tuple[str, Any]]) -> str:
    rendered = []
    for key, value in rows:
        if value in (None, "", []):
            continue
        rendered.append(
            f"<tr><th>{escape(key)}</th><td>{escape(fmt(value))}</td></tr>"
        )
    return "<table class=\"details\"><tbody>" + "\n".join(rendered) + "</tbody></table>"


def key_value_chips(data: dict[str, Any]) -> str:
    if not data:
        return "<p class=\"muted\">No score breakdown.</p>"
    return "<div class=\"chips\">" + "".join(
        f"<span><b>{escape(str(key))}</b>{escape(fmt(value))}</span>"
        for key, value in data.items()
    ) + "</div>"


def warning_list(items: list[Any]) -> str:
    if not items:
        return "<p class=\"muted\">No warnings.</p>"
    return "<ul class=\"warnings\">" + "".join(
        f"<li>{escape(str(item))}</li>" for item in items
    ) + "</ul>"


def diagnostic_table(search: dict[str, Any]) -> str:
    records: list[dict[str, Any]] = []
    for bucket in ("accepted", "skipped", "displaced"):
        for item in search.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            row["bucket"] = bucket
            records.append(row)
    if not records:
        return "<p class=\"muted\">No diagnostic comparison records.</p>"

    def rank(item: dict[str, Any]) -> tuple[int, float]:
        text = json.dumps(item, ensure_ascii=False)
        priority = 0 if any(name in text for name in ("蕾缪安", "能天使", "阿米娅")) else 1
        return priority, -abs(float(item.get("scoreDelta") or 0.0))

    records = sorted(records, key=rank)[:160]
    rows = []
    for item in records:
        operators = ", ".join(str(name) for name in item.get("operators") or [])
        rows.append(
            "<tr>"
            f"<td>{escape(str(item.get('bucket') or ''))}</td>"
            f"<td>{escape(str(item.get('status') or ''))}</td>"
            f"<td>{escape(str(item.get('shift') or ''))}</td>"
            f"<td>{escape(fmt(item.get('scoreDelta')))}</td>"
            f"<td>{escape(fmt(item.get('candidateScore')))}</td>"
            f"<td>{escape(operators)}</td>"
            f"<td>{escape(str(item.get('reason') or item.get('groupId') or ''))}</td>"
            "</tr>"
        )
    return (
        "<table class=\"rooms\"><thead><tr><th>Bucket</th><th>Status</th><th>Shift</th>"
        "<th>Score Delta</th><th>Candidate Score</th><th>Operators</th><th>Reason / Group</th>"
        "</tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )


def render_shifts(
    shifts: list[dict[str, Any]], drone_by_shift: dict[str, list[dict[str, Any]]]
) -> str:
    rendered = []
    for shift in shifts:
        name = str(shift.get("name") or "")
        rendered.append(
            f"<article class=\"shift\"><div class=\"shift-head\"><h3>{escape(name)}</h3>"
            f"<span>{escape(str(shift.get('start') or ''))} / {escape(fmt(shift.get('durationHours')))}h</span></div>"
        )
        rendered.append(room_table(shift.get("rooms") or []))
        drones = drone_by_shift.get(name, [])
        if drones:
            rendered.append("<h4>Drones</h4>")
            rendered.append(drone_table(drones))
        rendered.append("</article>")
    return "\n".join(rendered)


def room_table(rooms: list[dict[str, Any]]) -> str:
    rows = []
    for room in rooms:
        operators = ", ".join(
            str(operator.get("name") or "")
            for operator in room.get("operators") or []
        )
        rows.append(
            "<tr>"
            f"<td>{escape(str(room.get('type') or ''))}</td>"
            f"<td>{escape(str(room.get('id') or ''))}</td>"
            f"<td>{escape(str(room.get('targetLabel') or room.get('target') or ''))}</td>"
            f"<td>{escape(operators)}</td>"
            f"<td>{escape(fmt(room.get('score')))}</td>"
            "</tr>"
        )
    return (
        "<table class=\"rooms\"><thead><tr><th>Room</th><th>ID</th><th>Target</th>"
        "<th>Operators</th><th>Score</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )


def drone_table(targets: list[dict[str, Any]]) -> str:
    rows = []
    for target in targets:
        operators = ", ".join(str(name) for name in target.get("operators") or [])
        contribution = target.get("contribution") or {}
        rows.append(
            "<tr>"
            f"<td>{escape(str(target.get('policy') or ''))}</td>"
            f"<td>{escape(str(target.get('roomId') or ''))}</td>"
            f"<td>{escape(fmt(target.get('droneCount')))}</td>"
            f"<td>{escape(operators)}</td>"
            f"<td>{escape(', '.join(f'{k}: {fmt(v)}' for k, v in contribution.items()))}</td>"
            "</tr>"
        )
    return (
        "<table class=\"rooms\"><thead><tr><th>Policy</th><th>Room</th><th>Drones</th>"
        "<th>Operators</th><th>Contribution</th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table>"
    )


def fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


CSS = """
:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f4f6f8;
  color: #17202a;
}
body {
  margin: 0;
}
main {
  max-width: 1180px;
  margin: 0 auto;
  padding: 28px;
}
.hero {
  padding: 26px 0 18px;
}
.eyebrow {
  margin: 0 0 8px;
  color: #5b6b7d;
  font-size: 13px;
  text-transform: uppercase;
  letter-spacing: .08em;
}
h1, h2, h3, h4, p {
  margin-top: 0;
}
h1 {
  font-size: 32px;
  line-height: 1.18;
  margin-bottom: 10px;
}
h2 {
  font-size: 20px;
  margin-bottom: 14px;
}
h3 {
  font-size: 17px;
  margin: 0;
}
h4 {
  margin: 14px 0 8px;
}
code {
  background: #e7ebef;
  padding: 2px 5px;
  border-radius: 4px;
}
.muted {
  color: #5c6b7a;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-bottom: 18px;
}
.metric, .section, .shift {
  background: #fff;
  border: 1px solid #dce2e8;
  border-radius: 8px;
}
.metric {
  padding: 14px;
}
.metric span {
  display: block;
  color: #647386;
  font-size: 13px;
  margin-bottom: 6px;
}
.metric strong {
  font-size: 21px;
}
.section {
  padding: 18px;
  margin: 14px 0;
}
.details, .rooms {
  width: 100%;
  border-collapse: collapse;
}
.details th, .details td, .rooms th, .rooms td {
  border-bottom: 1px solid #e4e9ee;
  padding: 9px 8px;
  text-align: left;
  vertical-align: top;
}
.details th {
  width: 260px;
  color: #526172;
  font-weight: 600;
}
.rooms th {
  color: #526172;
  font-size: 13px;
}
.rooms td:nth-child(4) {
  min-width: 360px;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.chips span {
  display: inline-flex;
  gap: 8px;
  align-items: center;
  background: #eef3f7;
  border: 1px solid #d9e2ea;
  border-radius: 999px;
  padding: 7px 10px;
}
.warnings {
  margin-bottom: 0;
  padding-left: 20px;
}
.shift {
  padding: 14px;
  margin: 12px 0;
}
.shift-head {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  align-items: baseline;
  margin-bottom: 10px;
}
.shift-head span {
  color: #637284;
}
@media (max-width: 760px) {
  main {
    padding: 16px;
  }
  .rooms {
    display: block;
    overflow-x: auto;
  }
  .shift-head {
    display: block;
  }
}
"""


if __name__ == "__main__":
    raise SystemExit(main())
