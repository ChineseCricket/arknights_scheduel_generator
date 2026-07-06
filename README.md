# 明日方舟基建排班表自动生成器

从一图流导出的 `干员练度表.xlsx` 读取干员练度，结合
[`ArknightsGameData`](https://github.com/Kengxxiao/ArknightsGameData) 的国服基建数据，
生成、评分和推荐一图流/Mower 风格的明日方舟基建排班 JSON。

当前版本面向命令行使用，重点能力包括：

- 读取一图流练度表，识别已拥有干员、精英化/等级、技能专精和模组等级。
- 生成常见基建布局排班，例如 `243`、`252`、`333`、`342`。
- 支持 `normal`、`balanced-orundum`、`max-orundum` 三类生产目标。
- 估算每日龙门币、经验、合成玉、赤金/源石碎片变化、材料消耗、无人机收益和心情风险。
- 对已有排班 JSON 进行评分，或批量比较布局/模式并输出推荐报告。

> 说明：这是一个仍在校准中的规划工具。游戏内订单随机性、复杂基建技能联动和一图流展示口径可能与模型估算存在差异，输出应作为排班决策参考，而不是严格收益承诺。

## 安装

建议使用 Python 3.10+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .
```

开发和测试环境：

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
```

## 更新游戏数据

首次运行前需要下载国服游戏数据缓存：

```powershell
python -m arknights_schedule_generator update-data --data-dir .\data\cache
```

缓存目录 `data/cache/` 默认不会进入 Git。需要刷新时可加 `--force`。

## 生成排班

```powershell
python -m arknights_schedule_generator generate `
  --roster "C:\path\to\干员练度表.xlsx" `
  --data-dir .\data\cache `
  --layout 243 `
  --mode balanced-orundum `
  --shift-count 2 `
  --shift-hours 12 `
  --shift-times 08:00,20:00 `
  --drone-policy auto `
  --output .\outputs\schedule_243_balanced_orundum.json
```

常用参数：

- `--layout`: 基建布局，例如 `243`、`252`、`333`、`342`。
- `--right-side`: 右侧设施/宿舍电力预设，默认 `full`。
- `--mode`: `normal`、`balanced-orundum` 或 `max-orundum`。
- `--shift-count` / `--shift-hours` / `--shift-times`: 班次数量、时长和起始时间。
- `--drone-policy`: `none`、`lmd-trade`、`gold-factory`、`shard-factory`、`exp-factory` 或 `auto`。
- `--allow-upgrades`: 允许把未解锁但可通过升级获得的基建技能纳入规划。

## 评分已有排班

```powershell
python -m arknights_schedule_generator score `
  --roster "C:\path\to\干员练度表.xlsx" `
  --data-dir .\data\cache `
  --schedule "C:\path\to\schedule.json" `
  --mode balanced-orundum `
  --output .\outputs\score_report.json
```

评分报告会包含综合分、每日预期、房间明细、冲突和暂未完整建模的技能效果。

## 推荐排班

```powershell
python -m arknights_schedule_generator recommend `
  --roster "C:\path\to\干员练度表.xlsx" `
  --data-dir .\data\cache `
  --layouts 243,252,342 `
  --modes normal,balanced-orundum,max-orundum `
  --shift-patterns 2x12,3x8 `
  --drone-policy auto `
  --output-dir .\outputs\recommendation
```

推荐流程会写出 JSON 报告、HTML 阅读报告和候选排班文件。默认只评估当前练度；需要补练建议时加 `--allow-upgrades`。

## 示例

仓库中的 `examples/` 保留少量可公开的 fixture 和输出，用于快速了解文件形态：

- `examples/fixtures/yituliu_full_roster_maxed.xlsx`: 使用当前游戏数据生成的满练一图流格式练度表。
- `examples/outputs/schedule_243_balanced_orundum.json`: 由示例满练表生成的 243 平衡搓玉排班。
- `examples/outputs/score_243_balanced_orundum.json`: 对示例排班的评分结果。

重新生成示例：

```powershell
python -m arknights_schedule_generator make-full-roster `
  --data-dir .\data\cache `
  --output .\examples\fixtures\yituliu_full_roster_maxed.xlsx

python -m arknights_schedule_generator generate `
  --roster .\examples\fixtures\yituliu_full_roster_maxed.xlsx `
  --data-dir .\data\cache `
  --layout 243 `
  --mode balanced-orundum `
  --shift-count 2 `
  --shift-hours 12 `
  --shift-times 08:00,20:00 `
  --drone-policy auto `
  --output .\examples\outputs\schedule_243_balanced_orundum.json
```

## 项目结构

```text
arknights_schedule_generator/  核心包和 CLI
tests/                         单元测试和回归测试
examples/                      少量公开示例
data/cache/                    本地游戏数据缓存，默认忽略
outputs/                       本地生成结果，默认忽略
```

## 许可证

本项目使用 MIT 许可证发布。详见 [LICENSE](LICENSE)。
