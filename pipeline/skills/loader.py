"""
Skill 注册表 —— 把每个"大类任务"声明成一个 skills/*.md(一个 .md = 一个 route)。

loader 在 import 期扫描本目录所有 *.md(README.md 除外),解析 frontmatter,产出:
  · ROUTES               : list[Skill],按 order 再按 name 排序
  · render_catalog()     : 注入 Router prompt 的"可用任务类别"清单(router 据此选 route)
  · skill_for(route)     : route 名 → Skill(查不到 → None)
  · handler_for(route)   : route 名 → handler 键("planner"/<未来自定义>);未知 → "planner"
  · intent_for(route)    : route 名 → 兼容旧字段 RouterVerdict.intent
  · route_for_intent(i)  : 旧 intent → route 名(给老链路/缺字段时回填,反向兼容)

设计意图(打地基):
  加一个大类 = 丢一个 .md 进来 —— router 自动学会这个类别,orchestrator 自动按
  其 handler 分派。无需改 router.py / orchestrator.py 的任何判断逻辑。

刻意零重依赖、纯文件解析:不在 import 期碰 vertexai / GCP / 网络。frontmatter 用一个
极小的手写解析器(仿 config.py 解析 neon.env 的风格),不引入 PyYAML 这种传递依赖;
任何单个 .md 解析失败都被吞掉、跳过该文件,绝不拖垮整个 import(fail-open)。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("pipeline.skills.loader")

_SKILLS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HANDLER = "planner"   # 没写 handler 的 skill 默认走现有 Planner→DAG 主链路


@dataclass(frozen=True)
class Skill:
    name: str                       # route 名,等于文件名(不含 .md);router 输出的 route 取自这里
    description: str                 # 一句话:这个大类在做什么(进 router 选择清单)
    when_to_use: str = ""           # 何时归到这一类(给 router 的判别线索)
    handler: str = DEFAULT_HANDLER  # 执行该类用哪个 handler:"planner" 或 skills/handlers.py 里注册的键
    intent: str = "other"           # 兼容旧 RouterVerdict.intent(retrieve|aggregate|analyze|visualize|...)
    examples: tuple[str, ...] = ()  # few-shot:典型问法(进 router 清单,帮模型选对 route)
    order: int = 100                # 清单里的排序(小在前)
    body: str = ""                  # frontmatter 之后的正文:给该类的额外规划/工作流指引(未来用)


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """切出 --- 包裹的 frontmatter。支持 `key: value` 与 `key:` 后跟 `- item` 列表两种。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text.strip()
    fm, body = lines[1:end], "\n".join(lines[end + 1:]).strip()

    meta: dict = {}
    i = 0
    while i < len(fm):
        raw = fm[i]
        if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
            i += 1
            continue
        key, _, val = raw.partition(":")
        key, val = key.strip(), val.strip()
        if val == "":                                   # 可能是列表(examples:)
            items, j = [], i + 1
            while j < len(fm) and fm[j].lstrip().startswith("- "):
                items.append(_unquote(fm[j].lstrip()[2:]))
                j += 1
            meta[key] = items if items else ""
            i = j if items else i + 1
            continue
        meta[key] = _unquote(val)
        i += 1
    return meta, body


def _load_skill(path: str) -> Skill | None:
    try:
        with open(path, encoding="utf-8") as f:
            meta, body = _parse_frontmatter(f.read())
        name = str(meta.get("name") or os.path.splitext(os.path.basename(path))[0]).strip()
        if not name:
            return None
        ex = meta.get("examples") or []
        if isinstance(ex, str):
            ex = [ex] if ex else []
        try:
            order = int(meta.get("order", 100))
        except (TypeError, ValueError):
            order = 100
        return Skill(
            name=name,
            description=str(meta.get("description", "")).strip(),
            when_to_use=str(meta.get("when_to_use", "")).strip(),
            handler=str(meta.get("handler") or DEFAULT_HANDLER).strip(),
            intent=str(meta.get("intent") or "other").strip(),
            examples=tuple(str(e).strip() for e in ex if str(e).strip()),
            order=order,
            body=body,
        )
    except Exception as e:                              # 单个 .md 坏掉不影响其它(fail-open)
        log.warning("跳过无法解析的 skill 文件 %s: %r", path, e)
        return None


def _load_all() -> list[Skill]:
    out: list[Skill] = []
    try:
        names = sorted(os.listdir(_SKILLS_DIR))
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".md") or fn.lower() == "readme.md":
            continue
        s = _load_skill(os.path.join(_SKILLS_DIR, fn))
        if s is not None:
            out.append(s)
    out.sort(key=lambda s: (s.order, s.name))
    return out


ROUTES: list[Skill] = _load_all()
_BY_NAME: dict[str, Skill] = {s.name: s for s in ROUTES}


# ── 查询 API(orchestrator / router 用)──────────────────────────────────
def skill_for(route: str | None) -> Skill | None:
    return _BY_NAME.get((route or "").strip())


def handler_for(route: str | None) -> str:
    s = skill_for(route)
    return s.handler if s else DEFAULT_HANDLER


def intent_for(route: str | None) -> str:
    s = skill_for(route)
    return s.intent if s else "other"


def route_for_intent(intent: str | None) -> str:
    """旧 intent → route 名(反向兼容:老调用方/模型只给了 intent 时回填 route)。"""
    intent = (intent or "").strip()
    if not intent:
        return ""
    return next((s.name for s in ROUTES if s.intent == intent), "")


def known_routes() -> set[str]:
    return set(_BY_NAME)


def render_catalog() -> str:
    """拼成 Router prompt 里的"可用任务类别(route)"清单。空注册表 → 兜底静态串。"""
    if not ROUTES:
        return "- retrieval / aggregate / analyze / visualize(默认四类)"
    rows = []
    for s in ROUTES:
        ex = " / ".join(f'"{e}"' for e in s.examples[:2])
        hint = f"({s.when_to_use})" if s.when_to_use else ""
        tail = f" 示例:{ex}" if ex else ""
        rows.append(f"- {s.name} — {s.description}{hint}{tail}")
    return "\n".join(rows)
