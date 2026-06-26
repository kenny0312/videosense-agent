---
name: aggregate
intent: aggregate
handler: planner
order: 2
description: 计数、求和、分组统计、排行、占比这类聚合数字
when_to_use: 用户要的是一个数 / 一张统计表(总数、每类多少、Top-N),而不是视频清单
examples:
  - "How many videos are there in total?"
  - "每个活动类别各有多少条已确认事实?"
---
关系类聚合(COUNT / SUM / GROUP BY / 排序取 Top-N)优先用单个 `sql_query` 节点直接写完整
SQL,不要拆成多节点。需要对一串阈值各算一遍时用 `threshold_sweep`。
