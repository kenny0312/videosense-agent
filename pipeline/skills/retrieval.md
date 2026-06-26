---
name: retrieval
intent: retrieve
handler: planner
order: 1
description: 在视频库里按内容/活动/属性查找、过滤、列出具体的视频
when_to_use: 用户想"找 / 列出 / 筛选"符合某条件的视频本身,而不是统计数字
examples:
  - "Find all videos that contain skiing."
  - "列出所有在厨房拍摄的视频"
---
这一类几乎都能用单个 `sql_query` 节点表达:对业务表做 WHERE 过滤 / JOIN / ORDER BY,
返回行集本身。活动关键词记得走英文 ILIKE(predicate 是英文)。除非用户额外要排序统计,
否则不要追加沙箱节点。
