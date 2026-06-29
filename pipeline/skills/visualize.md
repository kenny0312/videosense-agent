---
name: visualize
intent: visualize
handler: planner
order: 4
description: 出图/可视化——散点、折线等,把数据画出来
when_to_use: 用户明确要"画/出图/plot/可视化",或要把上一轮结果换成图来看
examples:
  - "Plot start time vs confidence."
  - "把刚才那批滑雪视频的置信度画成散点图"
---
末节点是 `plot`(kind=scatter|line,x/y 为列名,title 用英文)。上游先用 `sql_query`/
分析节点把要画的两列准备好。要"把上一轮同一份结果再画一张图"时,从多轮上下文回放里找到
那一轮的数据(或其 result_id)接着画;数据有任何变化一律照配方重算。
