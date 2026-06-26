---
name: skydive
intent: retrieve
handler: planner
order: 5
description: 跳伞/翼装视频的阶段与类型查询——按出舱/自由落体/开伞/降落等阶段,以及跳伞类型(wingsuit 等)检索、统计、对比
when_to_use: 用户问的是跳伞/翼装相关——自由落体时长、开伞、出舱、降落、翼装、滑翔、某阶段在不在、哪些跳缺了某阶段;而不是通用活动检索或情绪判断
examples:
  - "把我所有跳的自由落体时长排个序"
  - "哪些视频只有自由落体、没有拍到开伞或降落?"
---
跳伞数据在 `skydive_segments` 表(每个视频一行)。受控阶段各占一组列:
`<phase>_start_ts / <phase>_end_ts / <phase>_confidence`,phase ∈
{aircraft(出舱前), exit(出舱), freefall(自由落体), deploy(开伞), canopy(开伞后), landing(降落)}。
另有 `jump_type`(wingsuit/freefly/belly/tracking/tandem/base/other)、`is_wingsuit`、
`summary`、`freefall_sec`(派生:自由落体时长)。

# 关键:阶段列【可能为 NULL】(不是每个视频都有全部阶段)
- 判断"有没有某阶段" 用 `<phase>_start_ts IS NOT NULL`,**不要**用 `= 0` 或 `> 0`
  (0 秒是合法时间戳,不代表缺失;缺失一律是 NULL)。
- "缺了某阶段" 用 `<phase>_start_ts IS NULL`(例:找只有自由落体没开伞的 →
  `freefall_start_ts IS NOT NULL AND deploy_start_ts IS NULL`)。
- 算阶段时长优先用 `freefall_sec` 等派生列,或 `end_ts - start_ts` 且两端都 `IS NOT NULL`;
  聚合(AVG/SUM)时 NULL 会被 SQL 自动忽略,无需特殊处理。
- 要展示视频本身时,join `video_metadata` 取 title/gcs_uri。

# 播放/展示视频
当用户想【看 / 播放 / 展示 / 给我看】视频或某片段时,DAG 末尾加一个 `show_video` 节点,
上游是选出 `video_id` 的 `sql_query`。要跳播到某阶段,就在上游 SELECT 里带上
`start_ts`(及可选 `end_ts` / `label`),例如"给我看最长那条的开伞那一刻" →
`SELECT video_id, deploy_start_ts AS start_ts, '开伞' AS label FROM skydive_segments ...`。
