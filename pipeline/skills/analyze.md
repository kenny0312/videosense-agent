---
name: analyze
intent: analyze
handler: planner
order: 3
description: 需要数据科学处理的分析——时间对齐/插值、回归、相关、阈值扫描等
when_to_use: 单条 SQL 算不出来,要先取数再用 pandas/scipy/statsmodels 做计算
examples:
  - "Regress confidence on clip start time for skiing videos."
  - "把视频事实和传感器序列按时间对齐后看相关性"
---
典型形态:先用 `sql_query` 取数 → 再串沙箱节点(`merge_asof` / `interpolate` /
`ols_regress` / `python` 逃生舱)。表达不了的自定义分析用 `python` 节点 + 自然语言
instruction。结果是结论/摘要,不一定出图(要图再加 `visualize` 那步)。
