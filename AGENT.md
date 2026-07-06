# Agent 记忆入口

这个项目是一个明日方舟基建排班生成/评分工具。后续 agent 进入项目时，先读本文件，再按需要读 `docs/memory/` 下的模块记忆。

## Python 环境

- 本项目专用 conda 环境：`arknights-schedule-generator`。
- 解释器：`C:\Users\12182\.conda\envs\arknights-schedule-generator\python.exe`。
- 当前验证版本：Python `3.11.15`，`openpyxl 3.1.5`。
- 安装/刷新命令：

```powershell
conda run -n arknights-schedule-generator python -m pip install -e . pytest
```

- 常用命令统一使用：

```powershell
conda run -n arknights-schedule-generator python -m ...
```

- 环境验证命令：

```powershell
conda run -n arknights-schedule-generator python -c "import sys, openpyxl; print(sys.executable); print(sys.version); print(openpyxl.__version__)"
```

```powershell
conda run -n arknights-schedule-generator python -m pytest tests\test_production.py -q
```

```powershell
conda run -n arknights-schedule-generator python -m pytest -q
```

```powershell
conda run -n arknights-schedule-generator python -m arknights_schedule_generator calibrate --data-dir data\cache --profile all --output outputs\calibration_check_session.json
```

- 不要把 `fdm-examiner` 当作本项目环境；那是其他项目语境遗留。
- `traepython` 是当前 shell 默认环境，但缺少 `openpyxl`，不能作为本项目权威验证环境。
- `base` 只可作为历史诊断参考，不是本项目隔离环境。
- 若 `docs/memory/` 或 `outputs/` 中的历史记录还出现 `fdm-examiner` 或裸 `python -m ...`，先确认它是不是历史证据；当前开发与验证以本节环境口径为准。

## Git 操作约定

- 每次完成一轮文件修改、测试、生成输出或其他关键操作后，都要运行 `git status --short --branch`，确认当前分支和变更范围。
- 默认远端为 `origin`，地址为 `https://github.com/ChineseCricket/arknights_scheduel_generator.git`；默认主线分支为 `main`，后续提交和 push 都基于 `main` / `origin/main`，除非用户明确要求临时分支。
- 需要交付代码时，先完整检查并 stage 预期变更，再 commit；用户明确要求 push 时，commit 成功后立即 push 当前分支。
- 不要回滚或覆盖用户/其他 agent 的未说明变更；若必须处理冲突，先说明具体文件和风险。

## 项目目的

- 从一图流网站导出的用户干员练度表 `.xlsx` 和一图流排班 JSON 出发，生成或评分明日方舟基建排班。
- 重点支持“搓玉”和“不搓玉”两类目标，输出每日预期 `龙门币 / 经验 / 合成玉`，同时报告赤金、源石碎片、材料和龙门币成本净变化。
- 最终希望生成没有同班冲突、能长期循环、接近社区成熟攻略收益的一图流兼容排班 JSON。

## 当前可信状态

- 收益模型已经从“技能百分比总分”转为资源流模型，核心字段是 `dailyExpected` 和 `scoreBreakdown`。
- 一图流 2026-06「泡影苍霆」版本样例已作为主要校准源：
  - 视频源：`BV19jVZ69Evp`
  - 静态 `243 搓玉 一天两换`：标注 `30.6k LMD + 14.7k / 18.9k EXP / 582 玉`，公式复现通过。
  - 静态 `右满 342 搓玉 一天两换`：标注 `47.0k LMD + 13.7k / 0 EXP / 578 玉`，公式复现通过。
  - 动态 `右满 342 搓玉 动态换班 跑单`：标注 `77.0k LMD / 0 EXP / 540 玉 / 68.0k 赤金等值`，已绑定 Mower 计划 `1775555941084837`，以 `guide_calibrated_mower` 方式匹配。
- `outputs/production_calibration_report.json` 是机器报告；`outputs/production_calibration_report.md` 和 `.html` 是人工阅读版。

## 常用命令

```powershell
conda run -n arknights-schedule-generator python -m arknights_schedule_generator.cli update-data --data-dir data/cache
```

```powershell
conda run -n arknights-schedule-generator python -m arknights_schedule_generator.cli score `
  --schedule "C:\Users\12182\Downloads\324搓玉_260624.json" `
  --roster "C:\Users\12182\Downloads\干员练度表.xlsx" `
  --mode balanced-orundum `
  --metric-profile guide `
  --shard-formula rock `
  --drone-policy none `
  --output outputs/user_243_orundum_score_recalibrated.json
```

```powershell
conda run -n arknights-schedule-generator python -m arknights_schedule_generator.cli generate `
  --roster "C:\Users\12182\Downloads\干员练度表.xlsx" `
  --data-dir data/cache `
  --layout 243 `
  --mode balanced-orundum `
  --shift-count 2 `
  --shift-hours 12 `
  --shift-times 08:00,20:00 `
  --output outputs/schedule_243_balanced_orundum.json
```

```powershell
conda run -n arknights-schedule-generator python -m arknights_schedule_generator.cli calibrate `
  --data-dir data/cache `
  --profile all `
  --output outputs/production_calibration_report.json
```

```powershell
conda run -n arknights-schedule-generator python -m unittest discover -s tests -p 'test*.py'
```

## 关键口径

- 排班文件名不可信。用户文件名可能叫 `324搓玉`，但布局必须优先读 JSON 内 `scheduleType`；用户样例实际是 `2贸4制3电`，即 `243`。
- `lmdGross` 是贸易站毛龙门币收入；`lmdNet` 会扣除搓玉制造源石碎片的龙门币成本。社区攻略里常见的 `4w+ LMD` 多数对应 `lmdGross` 或含额外加速，不等于 `lmdNet`。
- 紫色加号是资源特定的额外加速参考值，不属于基础 `dailyExpected`，也不能把 EXP/LMD/赤金三个紫色加号同时相加。
- 绿色 `0.843/0.792` 是排班侧指标，不是 LMD、EXP、赤金或合成玉收益字段。
- `drone-policy none` 是基础产能口径；需要看无人机收益时单独跑 `lmd-trade`、`gold-factory`、`exp-factory` 或 `auto`。

## 最近用户样例结论

- 用户 `C:\Users\12182\Downloads\324搓玉_260624.json` 通过 `scheduleType` 识别为 `243 / 3班`，当前导入按 `8h/8h/8h` 评分，不是每班 12 小时。
- 最新保守评分：
  - 无无人机：`lmdGross 12526.13 / lmdNet -46225.87 / exp 10106.67 / orundum 387.2`
  - 无人机给 LMD：`lmdGross 17812.86 / lmdNet -40939.14 / exp 10106.67 / orundum 387.2`
- 这个结果低于一图流 243 搓玉标注，主要因为用户贸易站使用了 `但书`、`巫恋`、`可露希尔` 等特殊机制组合，而当前模型只对少数一图流速查表组合直接校准。

## 后续开发原则

- 先校准，再优化。不要把未校准的“近最优”结果当成可信收益。
- 未建模复杂技能必须进入 `unsupportedSkillEffects` 或显式假设，不能静默计 0。
- 新增校准口径时，优先把来源、图上标注、程序复现差异写进报告和测试。
- 修改文档时注意 UTF-8。PowerShell 控制台可能乱码，必要时用 Python 读回检查字符码。
