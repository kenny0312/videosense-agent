"""
skills —— "大类任务 → workflow" 的声明式注册表。

- 每个 skills/*.md 声明一个大类(route):description / when_to_use / examples 喂给 Router
  当词表;handler 决定它由谁执行。
- loader  : 扫描 .md、渲染 router 词表、提供 route→handler/intent 的查询。
- handlers: smalltalk 回复生成器 + 自定义 workflow 的分派表 HANDLERS。

加一个大类 = 加一个 .md(+ 需要新 workflow 时在 handlers.HANDLERS 注册),
router 与 orchestrator 自动适配,无需改它们的判断逻辑。详见 skills/README.md。
"""
from pipeline.skills import handlers, loader

__all__ = ["loader", "handlers"]
