from __future__ import annotations

import argparse
import json
import mimetypes
import traceback
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy_default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .cli import parse_csv, parse_int_csv, parse_shift_patterns, parse_shift_times
from .data import GameData, download_data
from .power import RIGHT_SIDE_PRESETS
from .production import (
    DEFAULT_MAX_DRONE_CYCLE_REPEATS,
    DEFAULT_PURE_GOLD_TARGET_PER_DAY,
    DEFAULT_PURE_GOLD_TOLERANCE,
)
from .recommender import DEFAULT_LAYOUTS, DEFAULT_MODES, recommend_schedules
from .roster import load_roster_xlsx


DRONE_POLICIES = ("none", "lmd-trade", "gold-factory", "shard-factory", "exp-factory", "auto")
SHARD_FORMULAS = ("rock", "device")
CACHE_POLICIES = ("auto", "refresh", "off")


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class ParsedForm:
    fields: dict[str, list[str]]
    files: dict[str, UploadedFile]

    def text(self, name: str, default: str = "") -> str:
        values = self.fields.get(name)
        if not values:
            return default
        return values[-1].strip()

    def values(self, name: str) -> list[str]:
        return [value.strip() for value in self.fields.get(name, []) if value.strip()]

    def checked(self, name: str) -> bool:
        value = self.text(name)
        return bool(value) and value.lower() not in {"0", "false", "off", "no"}


class ScheduleUIServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], root_dir: Path):
        super().__init__(server_address, ScheduleUIHandler)
        self.root_dir = root_dir.resolve()


class ScheduleUIHandler(BaseHTTPRequestHandler):
    server: ScheduleUIServer

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return
        if parsed.path == "/api/defaults":
            self.send_json(HTTPStatus.OK, default_payload(self.server.root_dir))
            return
        if parsed.path.startswith("/files/"):
            self.serve_workspace_file(parsed)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/api/recommend":
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})
            return
        try:
            form = parse_request_form(self)
            payload = run_recommendation(form, self.server.root_dir)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - surfaced to the local UI.
            self.send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            return
        self.send_json(HTTPStatus.OK, payload)

    def serve_workspace_file(self, parsed: urllib.parse.SplitResult) -> None:
        raw = urllib.parse.unquote(parsed.path.removeprefix("/files/"))
        target = (self.server.root_dir / raw).resolve()
        if not target.is_relative_to(self.server.root_dir):
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "File is outside the workspace."})
            return
        if not target.is_file():
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "File not found."})
            return

        query = urllib.parse.parse_qs(parsed.query)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if query.get("download"):
            self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_print(f"[{timestamp}] {self.address_string()} {fmt % args}")


def parse_request_form(handler: BaseHTTPRequestHandler) -> ParsedForm:
    content_length = int(handler.headers.get("Content-Length", "0") or 0)
    content_type = handler.headers.get("Content-Type", "")
    body = handler.rfile.read(content_length)
    if content_type.startswith("multipart/form-data"):
        return parse_multipart_form(content_type, body)
    if content_type.startswith("application/x-www-form-urlencoded"):
        parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return ParsedForm({key: [str(item) for item in value] for key, value in parsed.items()}, {})
    raise ValueError("Unsupported form content type.")


def parse_multipart_form(content_type: str, body: bytes) -> ParsedForm:
    headers = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8")
    message = BytesParser(policy=email_policy_default).parsebytes(headers + body)
    fields: dict[str, list[str]] = {}
    files: dict[str, UploadedFile] = {}
    if not message.is_multipart():
        raise ValueError("Malformed multipart form.")
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            if not payload:
                continue
            files[str(name)] = UploadedFile(
                filename=filename,
                content_type=part.get_content_type(),
                data=payload,
            )
            continue
        charset = part.get_content_charset() or "utf-8"
        fields.setdefault(str(name), []).append(payload.decode(charset, errors="replace"))
    return ParsedForm(fields, files)


def run_recommendation(form: ParsedForm, root_dir: Path) -> dict[str, Any]:
    output_dir = resolve_user_path(root_dir, form.text("output_dir"), "outputs/ui_recommendation")
    output_dir.mkdir(parents=True, exist_ok=True)

    roster_path = uploaded_or_path(
        form,
        "roster_file",
        "roster_path",
        output_dir,
        root_dir,
        default_roster_path(root_dir),
    )
    if not roster_path or not roster_path.is_file():
        raise ValueError("请上传或填写有效的干员练度表 .xlsx。")

    baseline_schedule = uploaded_or_path(
        form,
        "baseline_file",
        "baseline_schedule",
        output_dir,
        root_dir,
        "",
        required=False,
    )
    data_dir = resolve_user_path(root_dir, form.text("data_dir"), "data/cache")
    if form.checked("auto_update"):
        download_data(data_dir, force=False)

    layouts = selected_layouts(form)
    modes = form.values("modes") or list(DEFAULT_MODES)
    shift_counts = parse_int_csv(form.text("shift_counts")) if form.text("shift_counts") else None
    shift_patterns = parse_shift_patterns(form.text("shift_patterns")) if form.text("shift_patterns") else None

    game_data = GameData.load(data_dir)
    roster = load_roster_xlsx(roster_path)
    report = recommend_schedules(
        game_data,
        roster,
        output_dir=output_dir,
        baseline_schedule=baseline_schedule,
        layouts=layouts,
        modes=modes,
        shift_count=int_field(form, "shift_count", 2),
        shift_counts=shift_counts,
        shift_hours=int_field(form, "shift_hours", 12),
        shift_patterns=shift_patterns,
        shift_times=parse_shift_times(form.text("shift_times") or None),
        shard_formula=choice_field(form, "shard_formula", SHARD_FORMULAS, "rock"),
        drone_policy=choice_field(form, "drone_policy", DRONE_POLICIES, "auto"),
        upgrade_cost_weight=float_field(form, "upgrade_cost_weight", 0.015),
        right_side=choice_field(form, "right_side", RIGHT_SIDE_PRESETS, "full"),
        min_lmd_gross=float_field(form, "min_lmd_gross", 0.0),
        min_exp=float_field(form, "min_exp", 0.0),
        min_orundum=float_field(form, "min_orundum", 0.0),
        include_upgrades=form.checked("allow_upgrades"),
        pure_gold_target=float_field(form, "pure_gold_target", DEFAULT_PURE_GOLD_TARGET_PER_DAY),
        pure_gold_tolerance=float_field(form, "pure_gold_tolerance", DEFAULT_PURE_GOLD_TOLERANCE),
        max_drone_cycle_repeats=int_field(form, "max_drone_cycle_repeats", DEFAULT_MAX_DRONE_CYCLE_REPEATS),
        jobs=form.text("jobs", "auto") or "auto",
        cache_policy=choice_field(form, "cache_policy", CACHE_POLICIES, "auto"),
        profile_runtime=form.checked("profile_runtime"),
    )
    return response_payload(report, root_dir, form.checked("no_enforce_baseline"))


def selected_layouts(form: ParsedForm) -> list[str]:
    layouts = form.values("layouts")
    custom = parse_csv(form.text("layouts_custom")) if form.text("layouts_custom") else []
    result = [*layouts, *custom]
    return result or list(DEFAULT_LAYOUTS)


def uploaded_or_path(
    form: ParsedForm,
    file_field: str,
    path_field: str,
    output_dir: Path,
    root_dir: Path,
    default_path: str,
    *,
    required: bool = True,
) -> Path | None:
    upload = form.files.get(file_field)
    if upload:
        return save_uploaded_file(output_dir, upload, path_field)
    raw_path = form.text(path_field, default_path).strip()
    if not raw_path:
        if required:
            raise ValueError(f"Missing required path: {path_field}.")
        return None
    return resolve_user_path(root_dir, raw_path)


def save_uploaded_file(output_dir: Path, upload: UploadedFile, prefix: str) -> Path:
    upload_dir = output_dir / "_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = safe_filename(upload.filename, f"{prefix}.bin")
    path = upload_dir / f"{stamp}_{uuid.uuid4().hex[:8]}_{filename}"
    path.write_bytes(upload.data)
    return path


def safe_filename(filename: str, fallback: str) -> str:
    name = Path(filename or fallback).name.replace("\x00", "").strip() or fallback
    allowed = []
    for char in name:
        if char.isalnum() or char in "._- ()[]":
            allowed.append(char)
        else:
            allowed.append("_")
    return "".join(allowed)[:160] or fallback


def resolve_user_path(root_dir: Path, raw_path: str, default: str | None = None) -> Path:
    text = (raw_path or default or "").strip()
    if not text:
        raise ValueError("Path cannot be empty.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = root_dir / path
    return path.resolve()


def default_roster_path(root_dir: Path) -> str:
    fixture = root_dir / "examples" / "fixtures" / "yituliu_full_roster_maxed.xlsx"
    return str(fixture.relative_to(root_dir)) if fixture.exists() else ""


def int_field(form: ParsedForm, name: str, default: int) -> int:
    text = form.text(name)
    return int(text) if text else default


def float_field(form: ParsedForm, name: str, default: float) -> float:
    text = form.text(name)
    return float(text) if text else default


def choice_field(form: ParsedForm, name: str, choices: tuple[str, ...], default: str) -> str:
    value = form.text(name, default) or default
    if value not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(choices)}.")
    return value


def response_payload(report: dict[str, Any], root_dir: Path, no_enforce_baseline: bool) -> dict[str, Any]:
    files = {
        key: link_for_path(root_dir, path)
        for key, path in (report.get("writtenFiles") or {}).items()
    }
    baseline = report.get("baselineComparison") or {}
    best = baseline.get("bestOverall") or {}
    baseline_warning = ""
    if baseline and not no_enforce_baseline and not best.get("passes", True):
        baseline_warning = "最佳候选没有超过手工基线；报告已生成，请按报告中的基线对比判断。"

    return {
        "ok": True,
        "message": baseline_warning or "推荐报告已生成。",
        "generatedAt": report.get("generatedAt"),
        "reportUrl": (files.get("htmlReport") or {}).get("url"),
        "files": files,
        "best": {
            "current": compact_candidate(report.get("bestCurrent")),
            "upgrades": compact_candidate(report.get("bestWithUpgrades")),
            "costAdjusted": compact_candidate(report.get("bestWithUpgradesCostAdjusted")),
            "targetCurrent": compact_candidate(report.get("bestTargetCompatibleCurrent")),
            "targetUpgrades": compact_candidate(report.get("bestTargetCompatibleWithUpgrades")),
            "targetCostAdjusted": compact_candidate(report.get("bestTargetCompatibleCostAdjusted")),
        },
        "baselineComparison": baseline,
    }


def compact_candidate(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not candidate:
        return None
    daily = candidate.get("dailyExpected") or {}
    target_fit = candidate.get("targetFit") or {}
    return {
        "id": candidate.get("id"),
        "score": candidate.get("score"),
        "layout": candidate.get("layout"),
        "mode": candidate.get("mode"),
        "profileLabel": candidate.get("profileLabel"),
        "candidateRole": candidate.get("candidateRole"),
        "allowUpgrades": candidate.get("allowUpgrades"),
        "upgradeCount": candidate.get("upgradeCount"),
        "targetFit": target_fit.get("fitLevel"),
        "dailyExpected": {
            "lmdGross": daily.get("lmdGross"),
            "lmdNet": daily.get("lmdNet"),
            "exp": daily.get("exp"),
            "orundum": daily.get("orundum"),
            "pureGoldDelta": daily.get("pureGoldDelta"),
            "shardDelta": daily.get("shardDelta"),
            "droneUsed": daily.get("droneUsed"),
        },
    }


def link_for_path(root_dir: Path, raw_path: str | Path) -> dict[str, str | None]:
    path = Path(raw_path)
    if not path.is_absolute():
        path = root_dir / path
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root_dir.resolve()).as_posix()
    except ValueError:
        return {"path": str(resolved), "url": None, "downloadUrl": None}
    url = "/files/" + urllib.parse.quote(relative, safe="/")
    return {"path": str(resolved), "url": url, "downloadUrl": f"{url}?download=1"}


def default_payload(root_dir: Path) -> dict[str, Any]:
    return {
        "root": str(root_dir),
        "paths": {
            "roster": default_roster_path(root_dir),
            "dataDir": "data/cache",
            "outputDir": "outputs/ui_recommendation",
        },
        "layouts": list(DEFAULT_LAYOUTS),
        "modes": list(DEFAULT_MODES),
        "rightSidePresets": list(RIGHT_SIDE_PRESETS),
        "dronePolicies": list(DRONE_POLICIES),
        "shardFormulas": list(SHARD_FORMULAS),
        "cachePolicies": list(CACHE_POLICIES),
        "values": {
            "shiftCount": 2,
            "shiftHours": 12,
            "dronePolicy": "auto",
            "rightSide": "full",
            "shardFormula": "rock",
            "cachePolicy": "auto",
            "jobs": "auto",
            "upgradeCostWeight": 0.015,
            "pureGoldTarget": DEFAULT_PURE_GOLD_TARGET_PER_DAY,
            "pureGoldTolerance": DEFAULT_PURE_GOLD_TOLERANCE,
            "maxDroneCycleRepeats": DEFAULT_MAX_DRONE_CYCLE_REPEATS,
            "minLmdGross": 0.0,
            "minExp": 0.0,
            "minOrundum": 0.0,
        },
    }


def safe_print(message: str) -> None:
    try:
        print(message)
    except (AttributeError, OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ak-schedule-ui",
        description="Run the local web UI for Arknights schedule recommendations.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", default=".", help="Workspace root for relative paths and file serving.")
    args = parser.parse_args(argv)

    root_dir = Path(args.root).resolve()
    server = ScheduleUIServer((args.host, args.port), root_dir)
    host, port = server.server_address
    safe_print(f"Arknights schedule UI: http://{host}:{port}/")
    safe_print(f"Workspace root: {root_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        safe_print("\nStopping UI server.")
    finally:
        server.server_close()
    return 0


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>明日方舟基建排班 UI</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f9;
      --panel: #ffffff;
      --panel-2: #eef3f6;
      --ink: #18222b;
      --muted: #607080;
      --line: #d9e2e8;
      --accent: #0f6f72;
      --accent-2: #254f8f;
      --danger: #a13b32;
      --shadow: 0 10px 26px rgba(24, 34, 43, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    .topbar {
      max-width: 1280px;
      margin: 0 auto;
      padding: 18px 22px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    main {
      max-width: 1280px;
      margin: 0 auto;
      padding: 20px 22px 32px;
      display: grid;
      grid-template-columns: minmax(360px, 470px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }
    form, .result-panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    form {
      padding: 16px;
    }
    fieldset {
      border: 0;
      border-top: 1px solid var(--line);
      margin: 0;
      padding: 16px 0 4px;
    }
    fieldset:first-child {
      border-top: 0;
      padding-top: 0;
    }
    legend {
      padding: 0 0 10px;
      font-weight: 700;
      color: var(--ink);
    }
    label {
      display: grid;
      gap: 6px;
      margin-bottom: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    input, select, button {
      font: inherit;
    }
    input[type="text"], input[type="number"], input[type="file"], select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      color: var(--ink);
      background: #fff;
    }
    input[type="file"] {
      padding: 6px 8px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .checks {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }
    .check {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 34px;
      margin: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px 9px;
      color: var(--ink);
      background: var(--panel-2);
      font-size: 13px;
    }
    .check input {
      width: 16px;
      height: 16px;
      margin: 0;
      accent-color: var(--accent);
    }
    .actions {
      display: flex;
      gap: 10px;
      align-items: center;
      border-top: 1px solid var(--line);
      padding-top: 14px;
      margin-top: 10px;
    }
    button, .button {
      min-height: 38px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 8px 12px;
      background: var(--accent);
      color: #fff;
      text-decoration: none;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      font-weight: 650;
    }
    button.secondary, .button.secondary {
      color: var(--accent-2);
      background: #fff;
      border-color: #9db4d4;
    }
    button:disabled {
      opacity: 0.65;
      cursor: wait;
    }
    .result-panel {
      min-height: 720px;
      padding: 16px;
    }
    .status {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 11px;
      background: #fafcfd;
      color: var(--muted);
      margin-bottom: 14px;
    }
    .status.error {
      color: var(--danger);
      border-color: #e2aaa5;
      background: #fff8f7;
      white-space: pre-wrap;
    }
    .links, .cards {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }
    .metric-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
      flex: 1 1 230px;
      min-width: 220px;
    }
    .metric-card h3 {
      font-size: 15px;
      margin: 0 0 8px;
    }
    .metric-card dl {
      display: grid;
      grid-template-columns: 92px 1fr;
      gap: 5px 10px;
      margin: 0;
      font-size: 13px;
    }
    .metric-card dt {
      color: var(--muted);
    }
    .metric-card dd {
      margin: 0;
      text-align: right;
      overflow-wrap: anywhere;
    }
    iframe {
      width: 100%;
      height: 720px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    .hidden { display: none; }
    @media (max-width: 980px) {
      main {
        grid-template-columns: 1fr;
      }
      .result-panel {
        min-height: 420px;
      }
    }
    @media (max-width: 560px) {
      .topbar, main {
        padding-left: 14px;
        padding-right: 14px;
      }
      .grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <h1>明日方舟基建排班 UI</h1>
      <span id="rootLabel"></span>
    </div>
  </header>
  <main>
    <form id="recommendForm">
      <fieldset>
        <legend>文件</legend>
        <label>干员练度表路径
          <input name="roster_path" type="text">
        </label>
        <label>上传干员练度表
          <input name="roster_file" type="file" accept=".xlsx,.xlsm,.xltx,.xltm">
        </label>
        <label>游戏数据目录
          <input name="data_dir" type="text">
        </label>
        <label class="check">
          <input name="auto_update" type="checkbox" value="1">
          缺失时自动更新游戏数据
        </label>
        <label>输出目录
          <input name="output_dir" type="text">
        </label>
        <label>手工基线排班 JSON
          <input name="baseline_schedule" type="text">
        </label>
        <label>上传基线排班 JSON
          <input name="baseline_file" type="file" accept=".json">
        </label>
      </fieldset>

      <fieldset>
        <legend>推荐范围</legend>
        <div id="layoutChecks" class="checks"></div>
        <label>补充布局
          <input name="layouts_custom" type="text" placeholder="243,252-11">
        </label>
        <div id="modeChecks" class="checks"></div>
        <div class="grid">
          <label>右侧设施预设
            <select name="right_side"></select>
          </label>
          <label>无人机策略
            <select name="drone_policy"></select>
          </label>
          <label>碎片配方
            <select name="shard_formula"></select>
          </label>
          <label>候选缓存
            <select name="cache_policy"></select>
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>班次</legend>
        <div class="grid">
          <label>班次数
            <input name="shift_count" type="number" min="1" step="1">
          </label>
          <label>每班小时
            <input name="shift_hours" type="number" min="1" step="1">
          </label>
          <label>班次数列表
            <input name="shift_counts" type="text" placeholder="2,3">
          </label>
          <label>班次模式
            <input name="shift_patterns" type="text" placeholder="2x12,3x8">
          </label>
        </div>
        <label>换班时间
          <input name="shift_times" type="text" placeholder="08:00,20:00">
        </label>
      </fieldset>

      <fieldset>
        <legend>约束</legend>
        <div class="grid">
          <label>补练成本权重
            <input name="upgrade_cost_weight" type="number" step="0.001">
          </label>
          <label>任务并发
            <input name="jobs" type="text">
          </label>
          <label>赤金目标/日
            <input name="pure_gold_target" type="number" step="0.01">
          </label>
          <label>赤金容差/日
            <input name="pure_gold_tolerance" type="number" step="0.01">
          </label>
          <label>无人机循环上限
            <input name="max_drone_cycle_repeats" type="number" min="1" step="1">
          </label>
          <label>最低龙门币毛收入
            <input name="min_lmd_gross" type="number" step="1">
          </label>
          <label>最低经验
            <input name="min_exp" type="number" step="1">
          </label>
          <label>最低合成玉
            <input name="min_orundum" type="number" step="1">
          </label>
        </div>
        <label class="check">
          <input name="allow_upgrades" type="checkbox" value="1">
          允许补练规划
        </label>
        <label class="check">
          <input name="profile_runtime" type="checkbox" value="1">
          记录运行画像
        </label>
        <label class="check">
          <input name="no_enforce_baseline" type="checkbox" value="1">
          不强制超过基线
        </label>
      </fieldset>

      <div class="actions">
        <button id="runButton" type="submit">生成推荐报告</button>
        <button class="secondary" id="resetButton" type="button">恢复默认</button>
      </div>
    </form>

    <section class="result-panel">
      <div id="status" class="status">等待生成。</div>
      <div id="links" class="links"></div>
      <div id="cards" class="cards"></div>
      <iframe id="reportFrame" class="hidden" title="推荐报告"></iframe>
    </section>
  </main>
  <script>
    let defaults = null;
    const form = document.getElementById("recommendForm");
    const statusBox = document.getElementById("status");
    const linksBox = document.getElementById("links");
    const cardsBox = document.getElementById("cards");
    const frame = document.getElementById("reportFrame");
    const runButton = document.getElementById("runButton");

    async function loadDefaults() {
      const response = await fetch("/api/defaults");
      defaults = await response.json();
      document.getElementById("rootLabel").textContent = defaults.root;
      applyDefaults();
    }

    function applyDefaults() {
      if (!defaults) return;
      form.roster_path.value = defaults.paths.roster || "";
      form.data_dir.value = defaults.paths.dataDir;
      form.output_dir.value = defaults.paths.outputDir;
      form.baseline_schedule.value = "";
      form.layouts_custom.value = "";
      form.shift_counts.value = "";
      form.shift_patterns.value = "";
      form.shift_times.value = "";
      form.shift_count.value = defaults.values.shiftCount;
      form.shift_hours.value = defaults.values.shiftHours;
      form.jobs.value = defaults.values.jobs;
      form.upgrade_cost_weight.value = defaults.values.upgradeCostWeight;
      form.pure_gold_target.value = defaults.values.pureGoldTarget;
      form.pure_gold_tolerance.value = defaults.values.pureGoldTolerance;
      form.max_drone_cycle_repeats.value = defaults.values.maxDroneCycleRepeats;
      form.min_lmd_gross.value = defaults.values.minLmdGross;
      form.min_exp.value = defaults.values.minExp;
      form.min_orundum.value = defaults.values.minOrundum;
      form.auto_update.checked = false;
      form.allow_upgrades.checked = false;
      form.profile_runtime.checked = false;
      form.no_enforce_baseline.checked = false;
      fillSelect(form.right_side, defaults.rightSidePresets, defaults.values.rightSide);
      fillSelect(form.drone_policy, defaults.dronePolicies, defaults.values.dronePolicy);
      fillSelect(form.shard_formula, defaults.shardFormulas, defaults.values.shardFormula);
      fillSelect(form.cache_policy, defaults.cachePolicies, defaults.values.cachePolicy);
      fillChecks(document.getElementById("layoutChecks"), "layouts", defaults.layouts, defaults.layouts);
      fillChecks(document.getElementById("modeChecks"), "modes", defaults.modes, defaults.modes);
    }

    function fillSelect(select, options, selected) {
      select.innerHTML = "";
      for (const item of options) {
        const option = document.createElement("option");
        option.value = item;
        option.textContent = item;
        option.selected = item === selected;
        select.appendChild(option);
      }
    }

    function fillChecks(container, name, options, selected) {
      container.innerHTML = "";
      const selectedSet = new Set(selected);
      for (const item of options) {
        const label = document.createElement("label");
        label.className = "check";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = name;
        input.value = item;
        input.checked = selectedSet.has(item);
        label.appendChild(input);
        label.appendChild(document.createTextNode(item));
        container.appendChild(label);
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      runButton.disabled = true;
      setStatus("正在生成推荐报告，候选较多时会花一些时间。");
      linksBox.innerHTML = "";
      cardsBox.innerHTML = "";
      frame.classList.add("hidden");
      try {
        const response = await fetch("/api/recommend", {
          method: "POST",
          body: new FormData(form),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error(payload.error || "生成失败。");
        }
        renderResult(payload);
      } catch (error) {
        setStatus(error.message, true);
      } finally {
        runButton.disabled = false;
      }
    });

    document.getElementById("resetButton").addEventListener("click", applyDefaults);

    function setStatus(message, error = false) {
      statusBox.textContent = message;
      statusBox.classList.toggle("error", error);
    }

    function renderResult(payload) {
      setStatus(payload.message || "推荐报告已生成。");
      const files = payload.files || {};
      const linkDefs = [
        ["打开推荐报告", files.htmlReport && files.htmlReport.url, false],
        ["下载推荐报告 JSON", files.report && files.report.downloadUrl, true],
        ["下载补练表 XLSX", files.upgradeRequirementsXlsx && files.upgradeRequirementsXlsx.downloadUrl, true],
        ["下载成本补练表 XLSX", files.upgradeRequirementsCostAdjustedXlsx && files.upgradeRequirementsCostAdjustedXlsx.downloadUrl, true],
        ["下载当前最佳排班", files.bestCurrentSchedule && files.bestCurrentSchedule.downloadUrl, true],
      ];
      linksBox.innerHTML = "";
      for (const [label, url, secondary] of linkDefs) {
        if (!url) continue;
        const link = document.createElement("a");
        link.className = "button" + (secondary ? " secondary" : "");
        link.href = url;
        link.textContent = label;
        if (label.startsWith("打开")) link.target = "_blank";
        linksBox.appendChild(link);
      }
      cardsBox.innerHTML = "";
      addCandidateCard("当前练度最佳", payload.best.current);
      addCandidateCard("补练最高产出", payload.best.upgrades);
      addCandidateCard("成本调整补练", payload.best.costAdjusted);
      addCandidateCard("目标匹配当前", payload.best.targetCurrent);
      if (payload.reportUrl) {
        frame.src = payload.reportUrl;
        frame.classList.remove("hidden");
      }
    }

    function addCandidateCard(title, candidate) {
      if (!candidate) return;
      const daily = candidate.dailyExpected || {};
      const card = document.createElement("article");
      card.className = "metric-card";
      card.innerHTML = `
        <h3>${escapeHtml(title)}</h3>
        <dl>
          <dt>ID</dt><dd>${escapeHtml(candidate.id || "-")}</dd>
          <dt>分数</dt><dd>${fmt(candidate.score)}</dd>
          <dt>布局</dt><dd>${escapeHtml(candidate.layout || "-")}</dd>
          <dt>模式</dt><dd>${escapeHtml(candidate.mode || "-")}</dd>
          <dt>合成玉</dt><dd>${fmt(daily.orundum)}</dd>
          <dt>龙门币毛</dt><dd>${fmt(daily.lmdGross)}</dd>
          <dt>经验</dt><dd>${fmt(daily.exp)}</dd>
          <dt>补练数</dt><dd>${fmt(candidate.upgradeCount)}</dd>
        </dl>
      `;
      cardsBox.appendChild(card);
    }

    function fmt(value) {
      if (value === null || value === undefined || value === "") return "-";
      const number = Number(value);
      if (Number.isFinite(number)) {
        return number.toLocaleString(undefined, { maximumFractionDigits: 3 });
      }
      return escapeHtml(String(value));
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    loadDefaults().catch((error) => setStatus(error.message, true));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
