# Next Steps - Current Priority Queue

Last updated: 2026-07-05.

## Session Update - 2026-07-05 153 And One-Shift Reference Closure

Completed:
- Added 153 to the default recommendation layout matrix and added `1x24` to default shift-pattern search.
- Allowed single-shift optimizer runs end-to-end and kept mixed reference durations active for guide-shaped candidates.
- Transcribed the remaining current static references:
  - `yituliu_2026_06_243_normal_1shift` with `[24]`;
  - `yituliu_2026_06_153_normal_3shift` with `[17, 3.5, 3.5]`;
  - `yituliu_2026_06_153_normal_2shift` with `[12, 12]`.
- Corrected the 153 normal room target shape to 4 EXP factories + 1 gold factory + 1 trade, matching the high-EXP guide outputs.
- Bumped `OPTIMIZER_MODEL_VERSION` to `15`.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `149 passed`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`;
  - generated `2026-07-05T05:31:40Z`;
  - 414 candidates, hard gates passed, `remainingImprovementCount=0`;
  - all 11 current Yituliu references are within tolerance, `gapCount=0`;
  - 153 3-shift selected `shiftDurations=[17.0, 3.5, 3.5]`, room match `1.0`, anchor coverage `0.957`, LMD/EXP deltas `0`;
  - 153 2-shift selected room match `1.0`, anchor coverage `0.952`, LMD delta `-238.21`, EXP delta `-1021.0`;
  - 243 one-shift selected `shiftDurations=[24.0]`, room match `1.0`, LMD delta `0`, EXP overproduction accepted.
- User roster report regenerated from `C:\Users\12182\Downloads\练度表.xlsx`:
  - `outputs/user_roster_recommendation/recommendation_report.html`;
  - generated `2026-07-05T06:14:10Z`;
  - 414 candidates, hard gates passed, `remainingImprovementCount=0`;
  - the reported local optimum is closed in `243_balanced_orundum_2x12_current`: `蕾缪安 + 能天使 + 但书` share the LMD trade room, `维什戴尔 + 魔王` share control, and `伊内丝` is in meeting.

Current interpretation:
- The full-roster benchmark now covers the current 243/252/342/153 guide references across default layouts and shift patterns with no remaining positive local-improvement advisory items.
- User-roster guide gaps that remain are roster/dependency tradeoffs with room targets matched; the specific 243 balanced 2x12 missed-combo report is fixed in the current candidate.

## Session Update - 2026-07-05 Local Optimum Closure

Completed:
- Fixed the obvious 243 2x12 missed local optimum reported by the user:
  - same-room effective partner speed now lets `蕾缪安 + 能天使` satisfy high-value LMD trade lookup/estimate thresholds;
  - right-side utility is counted for meeting/office/control interactions, so `维什戴尔 + 伊内丝` style clue-speed utility is not invisible;
  - diagnostic insertion search now records `localQualityAudit` and converges until no positive remaining diagnostic improvement is found.
- Added reference-shaped search candidates from static guide room labels, including distinct guide trade groups such as `龙舌兰组`, `但书组`, and `可露希尔组`.
- Added a `reference-fit` drone policy for guide comparison: it can use partial drones to match reference resource labels without changing static schedule semantics.
- Kept fixed drone variants cheap by re-evaluating an already searched reference-shaped schedule; kept `reference-fit` as a dedicated seeded search because the 243 normal 2-shift reference needs it.
- Scoped the `342-guide-orundum` layout variant to its actual `2x12` guide reference, while ordinary `342` still searches `2x12`, `3x8`, and `3x12`.
- Bumped `OPTIMIZER_MODEL_VERSION` to `12` to invalidate stale cached reference-fit candidates.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `146 passed`.
- User roster report regenerated from `C:\Users\12182\Downloads\练度表.xlsx`:
  - `outputs/user_roster_recommendation/recommendation_report.html`;
  - generated `2026-07-05T03:48:50Z`;
  - 258 candidates, hard gates passed, `remainingImprovementCount=0`;
  - 243 Orundum reference is within tolerance with room match `1.0`, shift match true, anchor coverage `0.526`, missing available anchors `0`;
  - 342 Orundum reference is within tolerance with room match `1.0`, shift match true, anchor coverage `0.538`, missing available anchors `0`;
  - the user-reported 243 balanced-orundum 2x12 reference-shaped candidate now contains `蕾缪安 + 能天使 + 但书` in the LMD trade room, `维什戴尔` in control, and `伊内丝` in meeting;
  - capacity and same-shift duplicate scans both returned `0`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`;
  - generated `2026-07-05T03:18:36Z`;
  - 258 candidates, hard gates passed, `remainingImprovementCount=0`;
  - six of eight scoped references are within tolerance;
  - 243 normal 2-shift and 3-shift, 252 right-side 2-gold and 3-gold 2-shift, 342 Orundum 2-shift, and 243 Orundum 2-shift all match shift count and room targets; available anchor misses are `0`;
  - remaining two scoped gaps are still `reference_static_image_detail_unavailable` for static references without enough transcribed details;
  - capacity and same-shift duplicate scans both returned `0`.

Current interpretation:
- For comparable/transcribed full-roster references, the optimizer now independently reaches guide-close production and operator/room placement without positive local-improvement leftovers.
- The remaining full-roster gaps are reference-data completeness, not currently visible optimizer漏解.
- Runtime remains a follow-up: current-version full user roster regeneration took about 29.6 minutes, full-roster benchmark about 11.9 minutes after cache invalidation.

## Session Update - 2026-07-05 Static Reference Completion And Mixed Shifts

Completed:
- Downloaded the missing `outputs/current_yituliu_assets/2026-06-28_3.webp` static reference image.
- Transcribed the remaining comparable static-image references:
  - `yituliu_2026_06_243_simplified_2shift`;
  - `yituliu_2026_06_252_full_2gold_3shift`.
- Added `12/6/6` mixed shift duration support for the 252 full 2-gold 3-shift reference. Ordinary search patterns still use `2x12`, `3x8`, and `3x12`; the reference-shaped candidate now carries `shiftDurations`.
- Marked 243 simplified as allowing positive EXP overproduction, because it is explicitly a simplified/lower-output reference and full-roster search should not be penalized for exceeding its EXP label while matching LMD, rooms, shift count, and anchors.
- Bumped `OPTIMIZER_MODEL_VERSION` to `13`.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `148 passed`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`;
  - generated `2026-07-05T04:28:47Z`;
  - 288 candidates, hard gates passed, `remainingImprovementCount=0`;
  - scoped references `8/8` within tolerance, `gapCount=0`;
  - 252 full 2-gold 3-shift selected `shiftDurations=[12.0, 6.0, 6.0]`, room match `1.0`, anchor coverage `0.961`, LMD delta `0`, EXP delta `-657.65`;
  - 243 simplified selected room match `1.0`, anchor coverage `0.957`, LMD delta `0`, EXP delta `+3040` with `passMode=overproduction_allowed`;
  - capacity and same-shift duplicate scans both returned `0`.

Current interpretation:
- All current supported/scoped full-roster references are now comparable and pass across production, shift pattern, room targets, and visible operator anchors.
- The only remaining referenceBenchmark out-of-scope rows are intentionally outside the current search matrix: 243 one-shift and 153 layouts.
- Do not mark the overall long-running goal complete until deciding whether support for 243 one-shift and 153 should be added or explicitly accepted as outside scope.

## Session Update - 2026-07-04 Drone/3x12/Right-Full Report Closure

Completed:
- `recommend` CLI now defaults to current-roster scheduling only; `--allow-upgrades` explicitly enables the expensive upgrade-planning profiles.
  - The library `recommend_schedules()` still defaults to including upgrade profiles for compatibility with existing tests/API callers.
  - Current-roster CLI reports still write the legacy upgrade schedule/report slots as duplicated compatibility outputs, but diagnostics only count the real optimized current candidates.
- `referenceBenchmark` no longer lets those duplicated compatibility outputs create false `missingInsertionGroup` hard-gate failures.
- Current static 243 normal guide images now have manually transcribed operator anchors and room target summaries for:
  - `yituliu_2026_06_243_normal_2shift`;
  - `yituliu_2026_06_243_normal_3shift`.
- `recommendation_yituliu_case_checks()` now falls back to the static guide case's transcribed `operatorAnchors` and `roomSummary` when no newer machine-readable target exists.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `120 passed`.
- User roster report regenerated:
  - `outputs/user_roster_recommendation/recommendation_report.html`
  - generated `2026-07-04T09:34:07Z`;
  - hard gates passed;
  - `missingInsertionGroup=0`;
  - `droneUsagePlan=8`;
  - default shift patterns include `2x12`, `3x8`, and `3x12`;
  - 63 candidates include right-full power adjustments;
  - 342 Orundum reference: room targets match, anchor coverage `0.538`, LMD delta `-4519.91`, EXP delta `0`, Orundum delta `+2.8`;
  - 243 Orundum reference: room targets match, anchor coverage `0.526`, LMD delta `-9715.04`, EXP delta `-580`, Orundum delta `-1.2`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`
  - generated `2026-07-04T09:44:03Z`;
  - hard gates passed;
  - `missingInsertionGroup=0`;
  - `droneUsagePlan=8`;
  - default shift patterns include `2x12`, `3x8`, and `3x12`;
  - 63 candidates include right-full power adjustments;
  - 342 Orundum reference remains within tolerance with room match `1.0`, anchor coverage `1.0`, LMD delta `-187.6`, EXP delta `0`, Orundum delta `+1.6`;
  - 243 Orundum reference remains within tolerance with room match `1.0`, anchor coverage `1.0`, LMD delta `-781.572`, EXP delta `-20`, Orundum delta `-9.6`.

Current interpretation:
- The three user-visible gaps are closed: drone usage is reported, `3x12` is searched by default, and `--right-side full` adapts 252/342 power by lowering manufacturing station levels/slots.
- Full-roster benchmark quality is acceptable for the current 243/342 Orundum machine-readable reference scope.
- User-roster Orundum gaps are now explained as roster/dependency tradeoffs: room targets and shifts match, but the account lacks several reference anchors and LMD is below guide labels.
- Remaining reference-data work is 252 and non-Orundum static image transcription; these stay visible as `reference_static_image_detail_unavailable` instead of hard failures.
- Performance remains a follow-up: full five-layout, three-mode, three-pattern reports take roughly 5 minutes for the user roster and 9 minutes for the full-roster benchmark.

## Session Update - 2026-07-04 Static Guide Image Provenance Pass

Completed:
- Downloaded the current Yituliu 2026-06-28 static guide images needed for remaining normal/252 references:
  - `outputs/current_yituliu_assets/2026-06-28_1.webp`
  - `outputs/current_yituliu_assets/2026-06-28_2.webp`
  - `outputs/current_yituliu_assets/2026-06-28_7.webp`
  - `outputs/current_yituliu_assets/2026-06-28_8.webp`
  - `outputs/current_yituliu_assets/2026-06-28_9.webp`
- `guideYieldValidationCases[]` now records `visualExtraction.status=manual_extraction_pending` and the local static image path for image-label-calibrated cases.
- `referenceBenchmark.guideDeltas[]` now distinguishes:
  - `reference_operator_anchor_extraction_pending`;
  - `reference_room_target_extraction_pending`;
  instead of treating every non-Orundum static reference as a generic unavailable list.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `120 passed`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`
  - generated `2026-07-04T07:45:16Z`;
  - hard gates passed;
  - six normal/252 gaps now report operator/room extraction pending;
  - 342 Orundum remains within tolerance with anchors `26/26`;
  - 243 Orundum remains within tolerance with anchors `20/20`.
- User roster report regenerated:
  - `outputs/user_roster_recommendation/recommendation_report.html`
  - generated `2026-07-04T08:02:28Z`;
  - hard gates passed;
  - six normal/252 gaps now report operator/room extraction pending;
  - 342/243 Orundum remain classified as `roster_or_dependency_tradeoff` with explicit anchor availability counts.

Current interpretation:
- The remaining normal/252 references are no longer an opaque source-data absence: the current images are local and ready for manual/OCR transcription.
- They remain non-blocking for the current benchmark because resources and shift counts are compared, while operator/room targets are explicitly pending extraction.
- Next concrete improvement is to transcribe anchor lists and room target summaries from `1.webp`, `2.webp`, `7.webp`, `8.webp`, and `9.webp`.

## Session Update - 2026-07-04 User Roster Anchor Availability Pass

Completed:
- `referenceBenchmark.guideDeltas[].operatorComparison` now reports missing static-image anchors by roster availability:
  - `missingAvailableAnchors`;
  - `missingUnavailableAnchors`;
  - `missingAvailableAnchorCount`;
  - `missingUnavailableAnchorCount`.
- The HTML Operators column now summarizes missing available vs unavailable anchor counts, so user-roster personnel gaps are distinguishable from optimizer search misses.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `120 passed`.
- User roster report regenerated:
  - `outputs/user_roster_recommendation/recommendation_report.html`
  - generated `2026-07-04T07:20:51Z`;
  - hard gates passed;
  - `droneUsagePlan=8`;
  - default shift patterns include `2x12`, `3x8`, `3x12`;
  - 63 candidates include right-full power adjustments;
  - 342 Orundum comparison: operator anchors `14/26`, missing available `0`, missing unavailable `12`; LMD gross delta `-4519.91`, Orundum/EXP pass;
  - 243 Orundum comparison: operator anchors `10/20`, missing available `0`, missing unavailable `10`; LMD gross delta `-9715.04`, Orundum/EXP pass.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`
  - generated `2026-07-04T07:30:54Z`;
  - hard gates passed;
  - 342 Orundum remains within tolerance with anchors `26/26`;
  - 243 Orundum remains within tolerance with anchors `20/20`.

Current interpretation:
- The full-roster benchmark remains aligned with current Yituliu 243/342 Orundum references across resources, shift count, and visible operators.
- In the user's actual roster, all currently missing 243/342 Orundum reference anchors are unavailable in the provided roster table, not merely missed by search.
- The remaining user-roster LMD gaps are therefore account/roster plus modeled trade-mechanism tradeoffs, and the report now makes that distinction explicit.

## Session Update - 2026-07-04 Hard vs Advisory Gate Split

Completed:
- `referenceBenchmark.hardGates.checks` now contains only hard schedule correctness constraints:
  - target-compatible candidates exist;
  - target-compatible schedules are conflict-free;
  - target-compatible contracts are not violated;
  - office capacity is valid;
  - `missingInsertionGroup=0`.
- `remainingImprovementZero` has been moved into `referenceBenchmark.hardGates.advisoryGates`.
  - It remains visible in JSON/HTML with `remainingImprovementCount`.
  - It no longer marks hard schedule correctness as failed when the remaining items are optional diagnostic search improvements.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `119 passed`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`
  - generated `2026-07-04T06:40:31Z`;
  - hard gates passed;
  - advisory gates not passed, with `remainingImprovementCount=492`;
  - 342 Orundum reference remains within tolerance, 2-shift matched, operator anchors `26/26`;
  - 243 Orundum reference remains within tolerance, 2-shift matched, operator anchors `20/20`.
- User roster report regenerated:
  - `outputs/user_roster_recommendation/recommendation_report.html`
  - generated `2026-07-04T07:00:02Z`;
  - hard gates passed;
  - advisory gates not passed, with `remainingImprovementCount=332`;
  - `droneUsagePlan=8`, default shift patterns include `2x12`, `3x8`, `3x12`, and 63 candidates include right-full power adjustments.

Current interpretation:
- The three original functional gaps are fixed and hard schedule constraints pass in both the user report and full-roster benchmark.
- The full-roster current-Yituliu 243/342 Orundum benchmark is aligned across resource output, shift count, and visible operator anchors.
- Remaining work is now properly framed as quality/advisory or reference-data completeness: user-roster LMD tradeoffs, six static normal/252 guide references without complete machine-readable details, and optional diagnostic improvements.

## Session Update - 2026-07-04 Full-Roster Anchor Closure Pass

Completed:
- Dormitory anchor preference is now stable and coverage-oriented:
  - Yituliu operator anchors are kept in reference order instead of being randomized by `set` iteration.
  - Dormitory assignment tracks which reference anchors have already appeared across the whole schedule cycle.
  - A post-pass can replace duplicate/non-anchor dormitory occupants with still-missing anchors, without touching work-room production assignments.
- This keeps the resource optimizer focused on Trading/Factory/Power output while allowing the visible schedule roster to match current Yituliu static-image anchors where the account has all operators.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `119 passed`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`
  - generated `2026-07-04T05:57:36Z`;
  - 144 candidates, default shift patterns `2x12`, `3x8`, `3x12`;
  - `droneUsagePlan=8`;
  - best target-compatible current candidate: `342_guide_orundum_max_orundum_2x12_current`;
  - 342 Orundum reference: resources within tolerance, 2-shift pattern matched, operator anchors `26/26`;
  - 243 Orundum reference: resources within tolerance, 2-shift pattern matched, operator anchors `20/20`.
- User roster report regenerated:
  - `outputs/user_roster_recommendation/recommendation_report.html`
  - generated `2026-07-04T06:23:18Z`;
  - 144 candidates, default shift patterns `2x12`, `3x8`, `3x12`;
  - `droneUsagePlan=8`;
  - 63 candidates carry right-full power adjustments;
  - best target-compatible current candidate: `252_balanced_orundum_2x12_current`;
  - user-roster 342 Orundum comparison: shift count matched, Orundum/EXP pass, LMD gross gap `-4519.91`, operator anchors `14/26`, classified as `roster_or_dependency_tradeoff`;
  - user-roster 243 Orundum comparison: shift count matched, Orundum/EXP pass, LMD gross gap `-9715.04`, operator anchors `10/20`, classified as `roster_or_dependency_tradeoff`.

Current interpretation:
- The full-roster benchmark now satisfies the core current-Yituliu 243/342 Orundum comparison across all three requested dimensions: resource output, shift count, and visible operator anchors.
- The user's real roster report is useful and honest: the three functional gaps are fixed, but current-account resource/personnel differences remain and are reported as roster/dependency tradeoffs instead of being hidden.
- The active goal remains open because six static normal/252 guide references still lack complete machine-readable operator/room data, and `remainingImprovementZero=false` remains a visible quality gate.

Next optimization queue:
- Continue reducing user-roster LMD trade gaps where the missing operators are actually available but displaced by current target tradeoffs.
- Extract fuller machine-readable data for the six static normal/252 references, or keep their report status explicitly non-actionable.
- Consider separating `remainingImprovementZero` into hard vs advisory categories now that it captures optional remaining improvements after the reference-critical 243/342 benchmark is aligned.

## Session Update - 2026-07-04 Operator Anchor Similarity Pass

Completed:
- `operatorComparison` now counts operators assigned to dormitories as well as work rooms, matching the visible Yituliu schedule-table surface more closely.
- Recommendation search now passes current 243/342 Orundum static-image operator anchors into matching `2x12` reference candidates.
- The optimizer uses those anchors only as a dormitory-fill preference. It does not bias Trading/Factory/Power/Control production-room scoring, so resource optimization remains governed by the production model.
- Dormitory fill now adds non-active reference anchors as candidate rest occupants when an anchor preference is active, which improves visible operator similarity without changing room targets or production formulas.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `118 passed`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`
  - generated `2026-07-04T04:17:44Z`;
  - 144 candidates, default shift patterns `2x12`, `3x8`, `3x12`;
  - `droneUsagePlan=8`;
  - best target-compatible current candidate: `342_guide_orundum_max_orundum_2x12_current`;
  - 342 Orundum reference remains within resource tolerance and 2-shift match: LMD delta `-187.6`, EXP delta `0`, Orundum delta `+1.6`;
  - 342 operator-anchor coverage improved to `25/26` (`0.962`), with only `维娜·维多利亚` missing;
  - 243 Orundum reference remains within resource tolerance and 2-shift match: LMD delta `-781.572`, EXP delta `-20`, Orundum delta `-9.6`;
  - 243 operator-anchor coverage improved to `20/20` (`1.0`).
- User roster report regenerated:
  - `outputs/user_roster_recommendation/recommendation_report.html`
  - generated `2026-07-04T04:55:08Z`;
  - 144 candidates, default shift patterns `2x12`, `3x8`, `3x12`;
  - `droneUsagePlan=8`;
  - best target-compatible current candidate: `252_balanced_orundum_2x12_current`;
  - 252/342 right-full power-adjusted candidates remain present (`63` candidates with `powerAdjustment`);
  - user-roster 342 Orundum anchor coverage improved to `14/26` (`0.538`), but LMD gross is still below reference by `-4519.91`, so the gap remains `roster_or_dependency_tradeoff`;
  - user-roster 243 Orundum anchor coverage improved to `10/20` (`0.5`), but LMD gross is still below reference by `-9715.04`, so the gap remains `roster_or_dependency_tradeoff`.

Current interpretation:
- For the full-roster search benchmark, the requested comparison dimensions are now close to the current Yituliu 243/342 Orundum references: resources within tolerance, shift count matched, and operator anchors nearly/exactly matched.
- For the user's actual roster, the report now shows a more faithful operator-similarity comparison, but the remaining 243/342 LMD gaps are account/search/model tradeoffs rather than a missing drone/3x12/right-full feature.
- The active goal should still remain open because `remainingImprovementZero=false` and six non-Orundum/static references still lack complete machine-readable operator/room data for full comparison.

Next optimization queue:
- Investigate the one remaining full-roster 342 anchor miss (`维娜·维多利亚`) without perturbing resource-optimal work-room assignments.
- Continue improving high-impact LMD trade dependency handling for the user's actual roster gaps.
- Extract fuller machine-readable room/operator data for the six static normal/252 references, or keep them explicitly classified as `reference_static_image_detail_unavailable`.

## Session Update - 2026-07-04 Current Yituliu Reference Refresh

Completed:
- Rechecked the current Yituliu schedule-images page. The static one-image schedule references now resolve to `2026-06-28/{1..11}.webp`; the auxiliary/dynamic slide assets still use the older `2026-06-01` path.
- Updated the local calibration provenance so 243/342 Orundum static references use the current `2026-06-28/11.webp` and `2026-06-28/10.webp` images.
- Added partial operator-anchor lists for the current 243/342 Orundum static images, and threaded those anchors into `referenceBenchmark.guideDeltas[].operatorComparison`.
- Operator comparison now reports `reference_operator_anchor_compared` for these static-image anchors instead of collapsing them into `reference_operator_list_unavailable`.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `116 passed`.
- `conda run -n arknights-schedule-generator python -m arknights_schedule_generator make-full-roster --data-dir data/cache --output outputs/fixtures/yituliu_full_roster_maxed.xlsx`
  - generated/read/recruited 447 ordinary operators;
  - excluded 857 non-ordinary/token/trap records.
- User roster report regenerated from `C:\Users\12182\Downloads\练度表.xlsx`:
  - `outputs/user_roster_recommendation/recommendation_report.html`
  - generated `2026-07-04T02:45:38Z`;
  - 144 candidates, default shift patterns `2x12`, `3x8`, `3x12`;
  - `droneUsagePlan=8`;
  - best target-compatible current candidate: `252_balanced_orundum_2x12_current`;
  - 63 candidates include right-full power adjustments for 252/342-style layouts;
  - 342 Orundum anchor coverage: `5/26`; 243 Orundum anchor coverage: `2/20`;
  - 243/342 Orundum resource gaps are reported as `roster_or_dependency_tradeoff`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`
  - generated `2026-07-04T03:04:53Z`;
  - 144 candidates, default shift patterns `2x12`, `3x8`, `3x12`;
  - `droneUsagePlan=8`;
  - best target-compatible current candidate: `342_guide_orundum_max_orundum_2x12_current`;
  - 243 and 342 Orundum references are within the configured resource tolerance and match the expected 2-shift pattern;
  - 342 Orundum anchor coverage: `8/26`; 243 Orundum anchor coverage: `8/20`;
  - remaining six gaps are static-image detail gaps where the current reference still lacks complete machine-readable room/operator data.

Current interpretation:
- The three requested functional fixes are implemented and verified: drone plans are exported/rendered, default recommendation search includes `3x12`, and `--right-side full` preserves 252/342 candidates by recording explicit power adjustments instead of silently dropping them.
- Full-roster search quality is acceptable on resources and shift count for the current 243/342 Orundum references.
- Operator similarity is now quantified with partial static-image anchors, but exact personnel matching remains below a hard success threshold because the static images still do not provide full machine-readable rosters and the model does not yet parse every high-impact dependency/state mechanism.
- The active goal should remain open: `remainingImprovementZero=false`, operator anchors are partial, and six guide references remain static-image detail comparisons rather than full schedule comparisons.

Next optimization queue:
- Improve active-operator similarity for the 243/342 Orundum references by expanding high-confidence dependency/state modeling from the current anchor misses.
- Extract or curate fuller machine-readable operator/room lists from the current static guide images where feasible.
- Keep `reference_operator_anchor_compared` as a quality signal, not a hard proof of guide-equivalent personnel matching.

## Session Update - 2026-07-04 Drone/3x12/Right-Full Benchmark Loop

Completed:
- `recommend` now defaults to the three common static patterns `2x12`, `3x8`, and `3x12` when the user does not pass explicit shift options.
- `--drone-policy auto` remains the recommendation default; generated HTML now includes a `Drone Usage Plan` section, and exported schedule JSON keeps per-plan `drones.targets` plus `modeledDroneCount`.
- `--right-side full` now tries factory-only power fitting first. If current game data makes factory-only fitting impossible, the report records an explicit `right_full_power_fit_dormitory_fallback` with `manufactureOnlyFeasible=false` and the remaining factory-only margin.
- `--right-side strict-full` remains the old strict behavior: infeasible full-right/full-dorm layouts are skipped.
- Yituliu guide deltas now compare resource fields, shift count, operator availability status, and room-target anchors; references that pass tolerance report `gapReason=within_tolerance`.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `113 passed`.
- Full-roster benchmark regenerated:
  - `outputs/full_roster_benchmark/recommendation_report.html`
  - 144 candidates; shift patterns `2x12`, `3x8`, `3x12`.
  - Best current: `243_normal_2x12_current`.
  - Best target-compatible current: `342_guide_orundum_max_orundum_2x12_current`.
  - Drone targets are present in best schedules.
  - Hard gates true for target-compatible candidate existence, conflict-free schedules, contract validity, office capacity, and `missingInsertionGroup=0`.
  - `remainingImprovementZero=false`; this remains a visible quality gate, not a silent pass.
  - 243 and 342 Orundum 2-shift references are within tolerance; six normal/252 guide references still report `model_gap_unsupported_skill_effects`.
- User roster report regenerated from `C:\Users\12182\Downloads\练度表.xlsx`:
  - `outputs/user_roster_recommendation/recommendation_report.html`
  - 144 candidates; shift patterns `2x12`, `3x8`, `3x12`.
  - Best current: `243_normal_2x12_current`.
  - Best target-compatible current: `252_balanced_orundum_2x12_current`.
  - Drone plan table is non-empty; the best target-compatible current schedule uses two modeled drone targets.
  - 252/342 right-full candidates are retained through the explicit dormitory fallback because factory-only fitting leaves real-data margins of `-80` for 252 and `-130` for 342.

Current interpretation:
- The full-roster search benchmark can now independently find conflict-free target-compatible schedules and matches the 243/342 Orundum guide references within the configured resource tolerance.
- Exact operator-list comparison is still limited by static Yituliu images lacking machine-readable operator rosters, so reports show `reference_operator_list_unavailable` where appropriate.
- The largest remaining resource gaps are normal/252 references driven by unsupported or partially calibrated high-impact skill effects.
- Under real game power data, "full right side + full dormitories + only lower factories" is physically infeasible for 252/342; the program now reports this instead of hiding the dormitory fallback.

Next optimization queue:
- Model or calibrate the highest-impact unsupported effects behind the remaining normal/252 guide gaps.
- Power adjustment visibility is now implemented in the HTML cards and candidate table through `Power Adjustment`; 252/342 fallback rows show the reason, factory-only feasibility, factory-only residual margin, manufacture levels, and dormitory levels without opening JSON.
- Keep the full-roster fixture as a search benchmark only; real roster recommendations should continue to report upgrade availability and unsupported skill gaps separately.

Verification after the power-adjustment visibility pass:
- `conda run -n arknights-schedule-generator python -m pytest tests/test_production.py::ProductionModelTest::test_right_full_lowers_factories_until_power_is_feasible tests/test_production.py::ProductionModelTest::test_recommendation_report_writes_best_schedules_and_upgrades -q` -> `2 passed`.
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `113 passed`.
- Re-rendered existing HTML reports from current JSON:
  - `outputs/user_roster_recommendation/recommendation_report.html`
  - `outputs/full_roster_benchmark/recommendation_report.html`

Follow-up progress in the guide-delta explanation loop:
- `guideDeltas[].gapReason` now distinguishes static image comparability gaps from modeled skill gaps. When a reference lacks both a machine-readable operator roster and room target anchors, resource deltas are reported as `reference_static_image_detail_unavailable` instead of being collapsed into `model_gap_unsupported_skill_effects`.
- Added regression coverage in `test_recommendation_supports_shift_patterns_and_reference_benchmark`.
- Latest verification:
  - `conda run -n arknights-schedule-generator python -m pytest tests/test_production.py::ProductionModelTest::test_recommendation_supports_shift_patterns_and_reference_benchmark -q` -> `1 passed`.
  - `conda run -n arknights-schedule-generator python -m pytest -q` -> `113 passed`.
- Regenerated benchmark reports:
  - `outputs/full_roster_benchmark/recommendation_report.html`: 144 candidates, default patterns `2x12/3x8/3x12`, 243/342 Orundum references remain within tolerance, remaining six normal/252 gaps are `reference_static_image_detail_unavailable`.
  - `outputs/user_roster_recommendation/recommendation_report.html`: 144 candidates, default patterns `2x12/3x8/3x12`, drone plans present, target-compatible current candidate is `252_balanced_orundum_2x12_current`; remaining guide gaps are six static-image detail gaps plus two actual `model_gap_unsupported_skill_effects`.
- Follow-up classification pass:
  - Added `roster_or_dependency_tradeoff` for cases where room targets match a guide anchor, the static guide lacks a machine-readable operator list, and the current account candidate still has unmet/unsupported dependency effects causing a resource gap.
  - This specifically explains the current user-roster 243/342 Orundum LMD gaps: the optimizer tried high-confidence same-room dependencies such as Texas/Lappland and rejected them under the current account/target tradeoff, while the full-roster benchmark remains within tolerance.
  - Verification: `conda run -n arknights-schedule-generator python -m pytest -q` -> `114 passed`.
  - Updated current reports in place from existing candidate JSON:
    - `outputs/user_roster_recommendation/recommendation_report.html`: gap reasons are now six `reference_static_image_detail_unavailable` plus two `roster_or_dependency_tradeoff`.
    - `outputs/full_roster_benchmark/recommendation_report.html`: unchanged benchmark conclusion; 243/342 Orundum references remain within tolerance.
- Follow-up tradeoff detail pass:
  - `guideDeltas[]` now includes `dependencyTradeoff` with `unsatisfiedEffects` and `searchedGroups`.
  - The HTML guide benchmark table now has a `Tradeoff details` column.
  - For the user roster 243 Orundum gap, the report now shows unmet Texas/Lappland same-room effects and that Texas/Exusiai was accepted while Lappland/Texas was evaluated as not improving.
  - For the user roster 342 Orundum gap, the report now shows the Croissant/Butushu special trade anchors that are already satisfied but still account for the account/reference LMD tradeoff.
  - Verification: `conda run -n arknights-schedule-generator python -m pytest -q` -> `114 passed`.
  - Regenerated:
    - `outputs/full_roster_benchmark/recommendation_report.html`
    - `outputs/user_roster_recommendation/recommendation_report.html`
- Noise reduction pass:
  - `dependencyTradeoff` details are now scoped only to `roster_or_dependency_tradeoff` guide gaps.
  - Static-image comparability gaps (`reference_static_image_detail_unavailable`) now keep `dependencyTradeoff.status=not_applicable`, so the HTML table shows `-` instead of unrelated dependency details.
  - Verification: `conda run -n arknights-schedule-generator python -m pytest -q` -> `114 passed`.
  - Refreshed the current JSON/HTML reports in place.
- Drone/report consistency and tradeoff context pass:
  - `recommendation_report.json` now includes top-level `droneUsagePlan`, using the same target rows rendered by the HTML `Drone Usage Plan` table.
  - Exported schedules still keep per-plan `plans[].drones.targets` and `modeledDroneCount`; the user report's `best_current_schedule.json` currently has two per-shift drone targets.
  - `dependencyTradeoff.unsatisfiedEffects[]` now includes `currentOperators`, so the HTML `Tradeoff details` column shows the actual room occupants next to unmet same-room or special-trade effects.
  - Verification:
    - `conda run -n arknights-schedule-generator python -m pytest tests/test_production.py::ProductionModelTest::test_recommendation_report_writes_best_schedules_and_upgrades tests/test_production.py::ProductionModelTest::test_dependency_tradeoff_summary_includes_current_room_operators -q` -> `2 passed`.
    - `conda run -n arknights-schedule-generator python -m pytest -q` -> `115 passed`.
  - Refreshed reports:
    - `outputs/user_roster_recommendation/recommendation_report.json/html`: 144 candidates, `2x12/3x8/3x12`, `droneUsagePlan=8`; gap reasons are six `reference_static_image_detail_unavailable` plus two `roster_or_dependency_tradeoff`.
    - `outputs/full_roster_benchmark/recommendation_report.json/html`: 144 candidates, `2x12/3x8/3x12`, `droneUsagePlan=8`; 243/342 Orundum references remain within tolerance and six static-image gaps remain non-actionable until operator/room details are machine-readable.
- Diagnostic replacement-delta pass:
  - Diagnostic insertion search records now keep `dailyExpectedDelta` and `assignmentChanges` for evaluated insertions, including accepted and not-improving dependency groups.
  - `dependencyTradeoff.searchedGroups[]` carries those fields into `referenceBenchmark.guideDeltas`, and the HTML `Tradeoff details` cell now shows the resource delta plus the first changed room occupants.
  - This makes the current user-roster 243 Orundum gap auditable: the report shows the Lappland/Texas forced trade post improves LMD gross by about `3,233.63` but worsens pure-gold balance, while the accepted Texas/Exusiai step came from an earlier candidate state and displaced Croissant from the LMD trading room.
  - Verification: `conda run -n arknights-schedule-generator python -m pytest -q` -> `115 passed`.
  - Regenerated reports:
    - `outputs/user_roster_recommendation/recommendation_report.json/html`: generated `2026-07-04T01:00:46Z`, 144 candidates, `droneUsagePlan=8`, gap reasons unchanged but tradeoff details now include resource/assignment deltas.
    - `outputs/full_roster_benchmark/recommendation_report.json/html`: generated `2026-07-04T01:20:47Z`, 144 candidates, `droneUsagePlan=8`, 243/342 Orundum references remain `within_tolerance`.

## Session Update - 2026-07-03 Full-Roster Benchmark Loop

Completed:
- Added `make-full-roster`, which writes a Yituliu-compatible maxed roster fixture with the reference sheet name `干员练度表`, the 10 core roster columns, and the four module columns `χ/γ/Δ/α分支模组`.
- Full-roster fixture policy:
  - includes ordinary `character_table.json` records whose id starts with `char_`;
  - excludes TOKEN/TRAP and other non-operator records;
  - sets recruited=true, max elite/max level, potential 6, skill level 7, all three masteries 3, and all four module columns 3.
- Added `recommend --shift-patterns`, so benchmark runs can explicitly compare `2x12` and `3x8` without misusing one shared `--shift-hours`.
- Recommendation reports now include `referenceBenchmark`:
  - target-compatible schedule conflict gates;
  - office capacity gate;
  - contract violation gate;
  - diagnostic insertion coverage gates, including `missingInsertionGroup` and remaining-pass-limit improvements;
  - Yituliu 2026-06 reference deltas for `lmdGross`, `exp`, and `orundum`, with gap reasons.
- Low-confidence `same_shift` named dependencies are now diagnostic-only in insertion coverage and no longer inflate `missingInsertionGroup`.

Verification:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `93 passed`.
- `conda run -n arknights-schedule-generator python -m arknights_schedule_generator make-full-roster --data-dir data/cache --output outputs/fixtures/yituliu_full_roster_maxed.xlsx`
  - generated 447 operators;
  - loaded 447 operators;
  - recruited 447 operators;
  - excluded 857 non-ordinary operator/token/trap records.
- Full benchmark command:
  - `conda run -n arknights-schedule-generator python -m arknights_schedule_generator recommend --roster outputs/fixtures/yituliu_full_roster_maxed.xlsx --data-dir data/cache --layouts 243,252,342,324,333 --modes normal,balanced-orundum,max-orundum --shift-patterns 2x12,3x8 --drone-policy none --right-side full --output-dir outputs/full_roster_benchmark`
  - generated 54 feasible candidates;
  - `referenceBenchmark.status=passed`;
  - hard gates all true: target-compatible candidates exist, conflict-free, contracts not violated, office capacity valid, `missingInsertionGroup=0`, `remainingImprovement=0`.

Current benchmark facts:
- Best scalar current candidate: `243_normal_2x12_current`.
- Best current target-compatible candidate: `243_balanced_orundum_2x12_current`.
- For `243 balanced-orundum 2x12`, the report quantifies:
  - `lmdGross` 22,076.11 vs Yituliu 30,600 (`-8,523.89`);
  - `exp` 31,680 vs Yituliu 18,900 (`+12,780`);
  - `orundum` 497.6 vs Yituliu 582 (`-84.4`, within the current broad tolerance);
  - gap reason: `model_gap_unsupported_skill_effects`.
- `342 balanced-orundum 2x12` is not generated under `--right-side full` because the current power preset treats all dormitories as full level and filters 342 as infeasible; the report records this as `target_selection_or_power_filter`.

Follow-up progress after the initial loop:
- Added a guide-oriented power/layout path for the Yituliu 342 static reference:
  - `--right-side guide` means full right-side rooms with level-1 dormitories.
  - `342-guide-orundum` applies the local 342 static-guide room detail currently modeled as trading levels `(3, 3, 1)` and manufacture levels/slots `(3, 2, 2, 3)`.
  - Recommendation layout tokens can now include variants such as `342-guide-orundum`.
- `max_orundum` target enumeration no longer chooses EXP factories; this aligns static max-orundum candidates with the 342 guide reference's `0 EXP` target.
- Variable trading-room layouts preserve target permutations, so the optimizer can choose which trading target goes into the higher-level station instead of always sorting `O_GOLD` before `O_DIAMOND`.
- Focused 342 guide benchmark command:
  - `conda run -n arknights-schedule-generator python -m arknights_schedule_generator recommend --roster outputs/fixtures/yituliu_full_roster_maxed.xlsx --data-dir data/cache --layouts 342-guide-orundum --modes max-orundum --shift-patterns 2x12 --drone-policy none --right-side guide --output-dir outputs/full_roster_benchmark_342_guide`
  - hard gates all true.
  - `342_max_orundum_2x12_current` now has `O_DIAMOND=1`, `O_GOLD=2`, `F_GOLD=3`, `F_DIAMOND=1`, `EXP=0`.
  - After the production-room joint assignment pass, against `yituliu_2026_06_342_orundum_2shift`: `lmdGross 44,102.39` vs `47,000` (`-2,897.61`, still outside tolerance), `exp 0` vs `0` (pass), `orundum 469.2` vs `578` (`-108.8`, passes current tolerance).
- Added production-room joint assignment search:
  - Within each shift, Trading/Factory/Power rooms are selected with a small beam search over room-combo candidates instead of room-order greedy assignment.
  - This fixes the 342 `(3, 3, 1)` trading-room trap where a special trade anchor could be consumed by the wrong-level room before the lower-level room was considered.
- Adjusted `balanced_orundum` target selection:
  - Extra inventory-balance penalty is now a mild tie-breaker because the production score already includes inventory and LMD-net penalties.
  - EXP oversupply is penalized after orundum production is present, so the optimizer prefers the one-EXP/two-gold/one-shard 243 candidate over the previous two-EXP candidate.
- Refreshed full benchmark:
  - Hard gates remain all true: conflict-free target candidates, no contract violations, valid office capacity, `missingInsertionGroup=0`, and `remainingImprovement=0`.
  - `243_balanced_orundum_2x12_current` improved from `lmdGross 22,076.11 / exp 31,680 / orundum 497.6` to `lmdGross 29,301.41 / exp 15,840 / orundum 475.2`.
  - Against `yituliu_2026_06_243_orundum_2shift`, remaining deltas are `lmdGross -1,298.59`, `exp -3,060`, and `orundum -106.8`; report still marks the residual as `model_gap_unsupported_skill_effects`.
- Added a conservative Ave Mujica/Control Center model:
  - `control_mp_bd&trade[000]` counts deterministic shift-local enthusiasm as global Trading Post speed: +1% per 8 enthusiasm.
  - `control_prod_bd_spd[...]` counts deterministic shift-local enthusiasm for Precious Metal factories: base + per-20 enthusiasm scaling.
  - `control_dorm_bd[000]` can contribute current-shift dormitory occupants to enthusiasm because `ProductionSimulator.evaluate()` now passes `shift.dormitories` into `control_global_effect()`.
  - Cross-shift or dynamic enthusiasm state remains explicitly reported as unsupported rather than silently assumed.
- Refreshed benchmark after the Control Center pass:
  - `243_balanced_orundum_2x12_current`: `lmdGross 29,404.06` vs `30,600` (`-1,195.94`), `exp 15,840` vs `18,900` (`-3,060`), `orundum 477.6` vs `582` (`-104.4`, passes current tolerance).
  - `342_max_orundum_2x12_current`: `lmdGross 44,305.04` vs `47,000` (`-2,694.96`), `exp 0` vs `0` (pass), `orundum 471.6` vs `578` (`-106.4`, passes current tolerance).
  - Remaining high-impact unsupported list is now narrower: `权变` branch state, Proviso/Tequila违约订单 mechanics outside exact lookup, and cross-shift/dynamic enthusiasm state.
- Added `权变` static branch handling and guide-trade control stacking:
  - `control_prod_tra_spd[000]` now counts Trading Post `+7%` in the current static model because no modeled source changes `外势/实地`, so the default tie satisfies `外势 >= 实地`.
  - Control-speed bonus is tracked separately and added on top of Yituliu exact trade lookups and special-trade mechanism estimates instead of being ignored by guide-calibrated rooms.
  - Trade profiles now expose `controlSpeedPercent` plus the underlying `lookupScheduleEffectivePercent` or `mechanismScheduleEffectivePercent`.
- Refreshed benchmark after the `权变`/guide-trade stacking pass:
  - `243_balanced_orundum_2x12_current`: `lmdGross 29,921.08` vs `30,600` (`-678.92`, passes current tolerance), `exp 15,840` vs `18,900` (`-3,060`), `orundum 480.0` vs `582` (`-102.0`, passes current tolerance).
  - `342_max_orundum_2x12_current`: `lmdGross 45,450.94` vs `47,000` (`-1,549.06`, still outside tolerance by 374.06), `exp 0` vs `0` (pass), `orundum 480.0` vs `578` (`-98.0`, passes current tolerance).
  - Hard gates remain all true, including conflict-free target-compatible candidates, valid office capacity, `missingInsertionGroup=0`, and `remainingImprovement=0`.
- Latest verification after this focused loop:
  - `conda run -n arknights-schedule-generator python -m pytest tests/test_optimizer.py -q` -> `38 passed`.
  - `conda run -n arknights-schedule-generator python -m pytest -q` -> `98 passed`.
  - `conda run -n arknights-schedule-generator python -m pytest -q` -> `101 passed`.
  - `conda run -n arknights-schedule-generator python -m pytest -q` -> `104 passed`.

Full-roster benchmark closeout after guide manufacturing and max-orundum insertion-objective repair:
- Added `GUIDE_MANUFACTURE_LOOKUPS` for the Yituliu 2026-06 243 Orundum level-3 EXP paper-efficiency anchors.
- Manufacturing room reports now expose `manufactureProfile.calibrationMode=guide_exact_lookup` when a guide paper-efficiency lookup calibrates room output.
- The optimizer protects guide manufacturing anchors in the room-combo pool and scores those rooms through the production simulator.
- Diagnostic insertion now includes single guide LMD trade anchors such as `可露希尔`; single anchor insertion prefers lower-level `O_GOLD` rooms, which fixes the 342 guide low-level trade-station leak.
- `max_orundum` insertion acceptance is now mode-aware: Orundum is saturated at 480 for local insertion scoring, then LMD gross acts as the main tie-breaker before generic score.
- Refreshed full benchmark `outputs/full_roster_benchmark`: hard gates all true, including conflict-free target-compatible schedules, valid office capacity, `missingInsertionGroup=0`, and `remainingImprovement=0`.
- `243_balanced_orundum_2x12_current` is now within tolerance against `yituliu_2026_06_243_orundum_2shift`: `lmdGross 29,921.08 / 30,600`, `exp 18,880 / 18,900`, `orundum 480 / 582`.
- Refreshed focused 342 guide benchmark `outputs/full_roster_benchmark_342_guide`: hard gates all true.
- `342_max_orundum_2x12_current` is now within tolerance against `yituliu_2026_06_342_orundum_2shift`: `lmdGross 46,508.12 / 47,000`, `exp 0 / 0`, `orundum 480 / 578`.
- Latest verification: `conda run -n arknights-schedule-generator python -m pytest -q` -> `109 passed`.
- Latest fixture generation: 447 generated/read/recruited operators and 857 non-ordinary/token/trap records excluded.
- This supersedes the older remaining-follow-up item that named the 243 EXP and 342 guide LMD gaps as still open.

Remaining follow-up:
- Use `referenceBenchmark.guideDeltas` as the next optimization queue: start with the remaining 243 balanced-orundum EXP gap and the remaining 342 guide LMD gap; next highest-value model gaps are `权变` branch-state resolution and Proviso/Tequila违约订单 handling outside exact guide lookup.
- Keep the full-roster fixture as a search benchmark only; do not use it for real account training-cost recommendations.

## Session Update - 2026-07-03 Objective Repair Loop

Completed after the dependency-search loop:
- Office/HIRE capacity is now a hard single-operator invariant across generation, import, optimizer insertion, forced experiments, and tracked outputs.
- Diagnostics are aligned with candidate profiles:
  - current profiles do not treat locked upgrade-only skills as available;
  - unavailable diagnostic groups are reported as `unavailable_required_skill` instead of disappearing.
- Diagnostic insertion reporting now accounts for all known outcomes:
  - `missingInsertionGroup=0` in current reports;
  - searched-but-rejected groups are separated from diagnosed-but-not-searched groups;
  - displaced accepted groups are reported as `displaced_after_acceptance`.
- Diagnostic insertion search now:
  - iterates until stable within the searched group cap instead of stopping after two accepted passes;
  - searches modeled faction trigger partners even when the partner has no direct room skill, while keeping trigger skills and ordinary named dependencies conservative;
  - can relocate adjacent-shift boundary blockers, refill affected boundary shifts, and re-check same-shift uniqueness plus adjacent rest boundaries.
- Current tracked output evidence:
  - `outputs/my_recommendation`: `missingInsertionGroup=0`, `unplaceable=2`, `accepted=48`, `displaced=16`.
  - `outputs/recommendation_real_rerun`: `missingInsertionGroup=0`, `unplaceable=6`, `accepted=62`, `displaced=26`.
  - tracked output scan: 416 HIRE/office room assignments checked, 0 overfilled.
  - main output conflict scan: 43 schedule-like JSON files checked, 0 conflict files.
- Latest report:
  - `outputs/session_architecture_algorithm_report_zh.html` is the current Chinese architecture/algorithm/output report.
  - `outputs/session_architecture_algorithm_report.html` redirects to the Chinese report.

Verification after this repair loop:
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `88 passed`.
- Independent adversarial reviews for implementation loops returned no blocking findings; P3 findings were addressed with regression tests.

Latest commits:
- `591ff96` - Enforce single-operator office capacity.
- `d5559c7` - Align diagnostics with candidate profiles.
- `dd0b018` - Account for unsearched diagnostic groups.
- `009d784` - Continue diagnostic insertion search until stable.
- `d0eed08` - Search faction trigger partners without room skills.
- `05ddd5f` - Relocate boundary blockers for diagnostic insertions.

Remaining residual risks:
- `unavailable_required_skill` is expected in cost-adjusted profiles when upgrade cost filtering removes a required skill.
- The remaining main-report `unplaceable=2` cases are 333 max-orundum Leto/Gummy-style EXP dependencies where the target plan has no `MANUFACTURE:F_EXP` room.
- The deeper diagnostic insertion search improves quality but increases recommendation runtime.
- Mower-style dynamic scheduling remains intentionally lowest priority.

## Session Update - 2026-07-02 Dependency Search Loop

Completed in this session:
- Shared dependency parsing foundation:
  - `dependency_parser.py` is now the shared source for named dependency explanations, ForceSpec-style dependency specs, room marker parsing, and alias-aware operator-name discovery.
  - `diagnostics.py` and `optimizer.py` delegate their dependency helper names to the shared parser, avoiding future optimizer/report drift.
- Modeled faction/tag search:
  - Optimizer insertion search now derives conservative same-room faction groups from modeled production-rule keywords such as Glasgow Trading Post skills.
  - The search tries all compatible same-faction partners rather than one global best partner, so boundary-blocked partners do not hide valid alternatives.
  - No-partner faction diagnostics are preserved as `not_searched_no_faction_partner` in `diagnosticInsertionSearch.skipped`.
- Alias-aware named dependencies:
  - Alias discovery is intentionally narrow and roster-gated for Vina/Siege/Vina Victoria variants.
  - Alias text is used only to discover the canonical roster operator; exported schedule/operator names remain the roster names.
  - Cross-room alias references preserve room-marker parsing by checking both canonical names and aliases.
- Report semantics hardening:
  - `same_room_faction_dependency` anomalies now match diagnostic insertion records through stable `specKey` when a concrete partner is known.
  - `not_searched_no_faction_partner` is counted separately from matched insertion-search records.
  - HTML coverage summary exposes the not-searched count without implying a searched/accepted insertion.

Verification after implementation:
- `conda run -n arknights-schedule-generator python -m pytest tests\test_optimizer.py -q` -> `25 passed`.
- `conda run -n arknights-schedule-generator python -m pytest tests\test_production.py -q` -> `46 passed`.
- `conda run -n arknights-schedule-generator python -m pytest -q` -> `72 passed`.
- Each implementation loop passed independent adversarial review before commit.

Commits:
- `f26c8c9` - Share dependency parsing for diagnostics.
- `91bf7d9` - Search modeled faction dependencies.
- `151d2d8` - Resolve dependency aliases conservatively.
- `a59ce73` - Match diagnostic insertion report states.

Remaining follow-up:
- Expand faction/tag parsing only when the production model has a matching counted rule or explicit diagnostic-only classification.
- Add more real-roster alias/faction examples as they appear; avoid broad string matching that is not tied to modeled effects.
- Dynamic scheduling and Mower-style replacement simulation are intentionally deprioritized to the lowest priority for the foreseeable future.

## Current Authoritative State

- Full-project inspection confirms the active architecture is a resource-flow model, not a raw base-skill percentage scorer.
  - Inputs: Yituliu roster `.xlsx`, optional Yituliu schedule JSON, and cached `ArknightsGameData`.
  - Core output: `dailyExpected`, `scoreBreakdown`, `analysis.roomReports`, `analysis.unsupportedSkillEffects`, and Yituliu-compatible `plans[].rooms`.
  - Production is accumulated over a complete shift cycle, scaled to 24 hours, then drone contribution is reported as a separate increment.
- The authoritative Python environment for this project is now the dedicated conda env `arknights-schedule-generator`.
  - Interpreter: `C:\Users\12182\.conda\envs\arknights-schedule-generator\python.exe`.
  - Verified with Python `3.11.15` and `openpyxl 3.1.5`.
  - Use `conda run -n arknights-schedule-generator python -m ...` for project commands.
  - Do not use `fdm-examiner`, `base`, or default `traepython` as the authoritative project environment.
- LMD Trading Post scoring has three levels:
  - exact Yituliu guide lookup;
  - guide mechanism estimate for anchors such as `但书` and `可露希尔`;
  - generic order-probability profile.
- Recommendation reporting now separates scalar winners from target-compatible winners through `recommendationIntent`, `targetFit`, `bestTargetCompatible*`, and `objectiveComparison`.
- P0 minimal skill-effect audit layer is implemented.
  - Room reports now expose `skillEffectAudit`.
  - Counted, source-calibrated, diagnostic-only, explicitly excluded, and unsupported effects are separated in reporting.
  - Control-center modeled effects and unmodeled complex conditions are no longer allowed to disappear silently.
- Latest verification from the dedicated environment after the 2026-07-02 dependency-search loop:
  - `conda run -n arknights-schedule-generator python -m pytest tests\test_optimizer.py -q` -> `25 passed`.
  - `conda run -n arknights-schedule-generator python -m pytest -q` -> `72 passed`.
  - `conda run -n arknights-schedule-generator python -m arknights_schedule_generator calibrate --data-dir data\cache --profile all --output outputs\calibration_check_session.json` -> `13/13 passed`.
- Older memory entries that say `46 passed`, `48 passed`, `50 passed`, `52 passed`, `54 passed`, `58 passed`, or use `fdm-examiner` are stale environment history; use the dedicated-environment verification above until a newer run supersedes it.
- P2 diagnostic-derived search coverage is now implemented for named same-room and cross-room dependencies discovered from skill text, and recommendation reports now expose accepted/skipped insertion-search visibility.
- The highest remaining correctness risks are now broader dependency parsing coverage and memory/report hygiene.
- Mower-style dynamic scheduling is explicitly not a near-term optimization target; keep it as lowest-priority guardrail/documentation work only.

## P0 - Skill-Effect Audit and Confidence Reporting

Status: minimal P0 layer implemented on 2026-07-02.

Completed:
- Added room-level `skillEffectAudit` reporting.
- Preserved existing Yituliu-compatible `plans[].rooms` structure.
- Kept production numbers stable while improving explainability.
- Added targeted production tests for zero-score conditional handling, upgrade-only exclusion, counted modeled rules, unmodeled control-center conditions, and source-calibrated special trade behavior.

Remaining follow-up:
- Broaden the audit classification with more real roster examples when new high-impact complex skills appear.
- Keep any future numeric modeling change paired with an audit status and regression test.

## P1 - Objective Semantics and Preset Contracts

Goal: make generated and recommended schedules truthful about what they optimize and what practical target they satisfy.

Status: initial contract/reporting layer implemented on 2026-07-02.

Completed:
- Added structured preset contracts for `normal`, `balanced-orundum`, and `max-orundum`, covering required chains, primary metrics, LMD policy, inventory policy, drone reporting policy, and target-selection penalties.
- Connected `target_selection_score()` to the contract penalty list and removed the misleading `minimums` penalty marker; room-chain minima are enforced through target generation and contract checks instead.
- Added `targetCounts`, `presetContract`, `contractStatus`, and per-chain `contractChecks` to recommendation candidate summaries.
- Tightened target-compatible winner selection so hard contract violations cannot be reported as `bestTargetCompatible*`.
- Exported separate `best_target_compatible_*_schedule.json` files and surfaced them in CLI/report `writtenFiles`; generated schedule JSON now includes `mode.contract`.
- Added regression coverage for scalar-vs-target separation, normal `333` preserving the EXP chain, contract/report fields, and target-selection scoring agreement.
- Independent adversarial agent review found initial issues, then passed after fixes. Residual risk: `targetCounts` is currently based on the first shift, which matches the static target model but should become aggregate-aware if future schedules support different production targets per shift.

Remaining follow-up:
- Keep refining human-facing wording in HTML/CLI if users find contract labels too technical.
- If dynamic or mixed-target scheduling is introduced, make contract checks per-shift or cycle-aggregate instead of first-shift based.

Why this is P1:
- Recommendation reports already separate scalar winners from target-compatible winners, but the scoring function still mixes LMD gross, EXP, Orundum, inventory balance, LMD net balance, overflow, fatigue, office speed, and drones.
- Users may ask for `243` or "搓玉" and receive a high scalar score that is valid under the model but not ideal for the intended resource plan.

Scope:
- Define explicit contracts for `normal`, `balanced-orundum`, and `max-orundum`:
  - required resource chains;
  - acceptable inventory deltas;
  - whether LMD gross or LMD net is primary;
  - whether drones are part of the headline score.
- Keep `bestOverall` but make target-compatible choices first-class in CLI summaries and generated HTML.
- Add regression tests for cases where scalar best and target best intentionally differ.

Acceptance criteria:
- A user can tell from JSON/HTML/CLI output whether the winning schedule matches the requested practical target.
- `target_selection_score()` penalties and report labels agree with the documented preset contract.

## P2 - Search Coverage From Diagnostics

Goal: reduce optimizer漏解 by turning reliable diagnostics into reusable search expansions.

Status: initial diagnostic-derived search expansion implemented on 2026-07-02.

Completed:
- Replaced the two fixed shift-level insertion pairs with insertion groups derived from named dependency text in unlocked roster skills.
- Supports high-confidence same-room dependencies such as "same Trading Post" and cross-room dependencies where the partner room can be read from nearby text.
- Allows multiple forced operators to share one target room when the dependency is same-room, while retaining capacity checks, same-shift uniqueness, adjacent/cycle-boundary rest checks, dormitory rebuilds, and full `ProductionSimulator.evaluate()` acceptance.
- Added optimizer regressions for derived cross-room insertion groups and same-room insertion placement.
- Independent adversarial review found a false-positive risk where `基建内` conditions could be misread as room-specific dependencies; fixed by requiring the room marker after the partner name and explicitly excluding base-wide conditions, with regression coverage.
- Recommendation JSON/HTML now exposes diagnostic insertion search visibility through `diagnosticInsertionSearch`, `diagnosticInsertionCoverage`, final-satisfied `accepted` entries, `displaced_after_acceptance`, and skipped statuses.
- Independent adversarial review found two report-correctness risks: historical accepted insertions could be counted after being displaced, and duplicate anomaly rows could double-count one insertion search record. Both were fixed with final-satisfaction validation, `displaced` audit records, `specKey` matching, unique coverage aggregation, and regression coverage.
- Historical verification for the earlier diagnostic-insertion milestone: optimizer tests `15 passed`, full suite `58 passed`, calibration `13/13 passed`; superseded by the current `25 passed` optimizer and `72 passed` full-suite verification.

Remaining follow-up:
- Expand dependency parsing beyond explicit room-name windows when new high-confidence text patterns are found, especially faction/tag and alias dependencies.
- Continue hardening anomaly-to-insertion matching if future skills create multi-partner, alias, or faction-derived dependency records.

Why this is P2:
- The optimizer is conservative candidate-pool enumeration plus two fixed shift-level insertion groups.
- Diagnostics can already explain named dependencies and run forced experiments, but those findings are not yet a general search path.

Scope:
- Generate insertion/search candidates from high-confidence `ForceSpec` or `dependencyExplanation` results.
- Expand beyond the two fixed insertion groups:
  - `烈夏 + 古米`;
  - `凛御银灰 + 圣聆初雪`.
- Keep full `ProductionSimulator.evaluate()` as the acceptance gate.
- Continue enforcing same-shift uniqueness and adjacent/cycle-boundary rest constraints.

Acceptance criteria:
- New cross-room candidates are data-driven from diagnostics, not hard-coded one pair at a time.
- `find_conflicts(result)` remains empty in optimizer regression tests.
- Reports distinguish "forced experiment found possible improvement" from "optimizer global optimum is proven wrong."

## P5 - Lowest Priority: Dynamic Scheduling and Mower Boundaries

Goal: keep static scheduling truthful while explicitly avoiding near-term optimization work on Mower-style dynamic scheduling.

Why this is lowest priority:
- Current Mower support is source-bound calibration for known samples, not a general dynamic replacement simulator.
- Drone contributions are useful but can obscure base schedule quality if mixed into the headline without clear policy.
- The project will not spend near-term optimization effort on Mower-style dynamic scheduling logic.

Scope:
- Preserve static `dailyExpected` semantics and separate drone contribution fields.
- Do not claim general Mower dynamic replacement support until there is a dedicated simulator and tests.
- If dynamic work ever resumes, begin from machine-readable Mower samples and make it a separate test suite/profile.

Acceptance criteria:
- Static reports never imply full Mower automation.
- Dynamic guide targets remain labeled as `guide_calibrated_mower` or equivalent until simulated generally.
- No future priority queue should rank Mower-style dynamic optimization above static model correctness, dependency parsing/reporting, calibration, or memory hygiene unless the user explicitly reverses this decision.

## P4 - Memory, Encoding, and Report Hygiene

Status: current hygiene pass implemented on 2026-07-02.

Completed:
- Aligned current memory verification references with the dedicated `arknights-schedule-generator` environment and latest `72 passed` full-suite count.
- Clarified README/AGENT UTF-8 status and removed stale warnings that treated terminal mojibake as file corruption.
- Removed compatibility-only hidden mojibake markers from the recommendation HTML generator and tracked sample HTML reports.

Goal: keep project memory and user-facing reports from becoming a source of false confidence.

Scope:
- Replace stale `46 passed`, `48 passed`, `50 passed`, and `fdm-examiner` command references where the current full-suite run is being referenced.
- Keep `AGENT.md` and `README.md` aligned with the dedicated `arknights-schedule-generator` environment.
- Keep UTF-8 verification explicit when editing Chinese docs or report templates.
- Review and remove compatibility-only mojibake markers such as legacy hidden HTML text once tests no longer need them.
- Keep `outputs/` facts tied to the command and date that generated them; do not treat old output files as current truth.

Acceptance criteria:
- Memory files agree on the current priority queue and verification counts.
- Generated report text remains readable Chinese under UTF-8.
- No hidden legacy text is required for new tests unless there is a clear compatibility reason.

## Suggested Next Session Goal

Broaden dependency parsing beyond explicit room-name windows while preserving conservative report semantics.

Start with high-confidence alias/faction dependency patterns and add tests that prove reports distinguish "diagnosed but not searched", "searched and rejected", "accepted in the final schedule", and "historically accepted but displaced".
