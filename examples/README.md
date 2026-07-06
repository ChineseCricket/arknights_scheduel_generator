# 示例文件

这个目录只保留少量可公开、可重新生成的示例文件，方便检查一图流练度表、排班 JSON 和评分报告的大致结构。

生成示例前需要先准备游戏数据：

```powershell
python -m arknights_schedule_generator update-data --data-dir .\data\cache
```

然后运行：

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

python -m arknights_schedule_generator score `
  --roster .\examples\fixtures\yituliu_full_roster_maxed.xlsx `
  --data-dir .\data\cache `
  --schedule .\examples\outputs\schedule_243_balanced_orundum.json `
  --mode balanced-orundum `
  --drone-policy auto `
  --output .\examples\outputs\score_243_balanced_orundum.json
```
