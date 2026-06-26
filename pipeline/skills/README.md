# skills/ —— 大类任务 → workflow 的声明式注册表

把"路由器认识哪些大类任务、每类怎么执行"从硬编码改成**数据驱动**:每个大类是一个
`*.md`(一个 **route**),`loader.py` 在 import 期扫描它们,产出 Router 的选择词表和
`route → handler / intent` 的查询。

## 一个 skill.md 的结构

```markdown
---
name: retrieval          # route 名(= 文件名),Router 输出的 route 取自这里
intent: retrieve         # 兼容旧 RouterVerdict.intent 字段
handler: planner         # 执行端:"planner"=现有 Planner→DAG;或 handlers.py 注册的自定义键
order: 1                 # Router 清单里的排序(小在前)
description: 一句话说明这个大类在做什么        # 进 Router 词表
when_to_use: 何时归到这一类                    # 进 Router 词表,帮判别
examples:                                       # few-shot 典型问法,进 Router 词表
  - "Find all videos that contain skiing."
  - "列出所有在厨房拍摄的视频"
---
正文:给该类任务的额外规划/工作流指引(自由文本,未来 workflow 可读取 Skill.body)。
```

## 数据流

```
skills/*.md ──loader──► Router prompt 的"可用任务类别"清单
                  └────► RouterVerdict.route(模型挑一个最贴切的)
                              │
              orchestrator: handler_for(route)
                              │
              ┌───────────────┴───────────────┐
        handler == "planner"            handler == 其它
        (现四类都是这条)               查 handlers.HANDLERS[handler]
        → 现有 Planner→DAG 主链路        → 调用你的自定义 workflow 函数
```

## 加一个新大类

1. **只是又一种能用 Planner→DAG 表达的查询** → 新建一个 `*.md`,`handler: planner` 即可。
   Router 自动学会这个类别,无需改任何 `.py`。

2. **要走完全不同的 workflow**(比如情感分析有自己的多步流程):
   - 新建 `*.md`,frontmatter 里写 `handler: my_workflow`;
   - 在 `handlers.py` 的 `HANDLERS` 注册 `"my_workflow"` → 你的函数,签名:
     ```python
     def my_workflow(nl, *, verdict, session, context, schema, resolved_ids) -> str:
         return "给用户的答案"
     ```
   `orchestrator.run_query` 见到非 `"planner"` 的 handler 会自动按表分派调用,
   不需要改它的判断逻辑。

## 边界(刻意如此)

- `loader` 纯文件解析,**不**在 import 期碰 vertexai/GCP/网络;单个 `.md` 解析失败会被
  跳过而非拖垮整体(fail-open)。
- `smalltalk` / `meta` / `refuse` 是**门(gate)**,由 `RouterVerdict.decision` /
  `turn_type` 驱动,不属于 route,因此不在本目录建 `.md`;闲聊回复的生成器在
  `handlers.smalltalk_reply`。
