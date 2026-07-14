"""搜索空间:候选 = 对 gen0 prompt 的一组【稀疏覆盖】(overrides)。

可变的只有两类(设计 §3):
  lessons 条文正文(改写;槽位有空时可新增;不许删 —— v1)
  工具声明 planner_desc(改写)
宪法(loop_driver._CONSTITUTION)与数据事实【锁死】,不进空间。

应用语义:apply(overrides) 总是【先回到 gen0 原貌再叠加】,所以候选之间无残留;
跑完必须 reset()。全部发生在本进程内存(refresh_loop_system),生产文件零改动。
"""
from __future__ import annotations

import dataclasses

from pipeline import lessons as _lessons
from pipeline import loop_driver as _ld
from pipeline import node_specs as _ns

MAX_LESSON_CHARS = 600     # 单条教训上限(IFScale:指令越密遵循越差,不许越改越长)
MAX_TOOL_CHARS = 900       # 单个工具声明上限

# gen0 原貌快照 —— import 时冻结(必须在任何变异之前 import 本模块)
_PRISTINE_LESSONS: list = list(_lessons.LESSONS)
_PRISTINE_SPECS: dict = dict(_ns.SPECS)


def space_doc(overrides: "dict | None" = None) -> str:
    """给反思器看的【空间说明书】:每条教训/每个工具的 id + 文本。
    传 overrides(父本的覆盖)时展示的是【父本的现文本】—— 反思器必须对着
    父本实际在用的 prompt 开方,不是 gen0 旧貌(审计 m4/m10/m16)。"""
    lov = (overrides or {}).get("lessons") or {}
    tov = (overrides or {}).get("tools") or {}
    news = sorted(k for k in lov if k.startswith("NEW"))
    n_total = len(_PRISTINE_LESSONS) + len(news)
    L = ["## 可改的教训(lesson:<id>;≤%d字;现有 %d/%d 条)"
         % (MAX_LESSON_CHARS, n_total, _lessons.MAX_LESSONS)]
    for l in _PRISTINE_LESSONS:
        L.append(f"- lesson:{l.id} — {lov.get(l.id, l.text)}")
    for k in news:
        L.append(f"- lesson:{k}(本谱系已新增)— {lov[k]}")
    L.append("")
    L.append(f"## 可改的工具声明(tool:<名>;≤{MAX_TOOL_CHARS}字)")
    for name, spec in _PRISTINE_SPECS.items():
        L.append(f"- tool:{name} — {tov.get(name, ' '.join(spec.planner_desc.split()))}")
    return "\n".join(L)


def validate(overrides: dict) -> list[str]:
    """校验一组覆盖是否合法。返回错误列表(空=合法)。"""
    errs = []
    known_lessons = {l.id for l in _PRISTINE_LESSONS}
    for lid, text in (overrides.get("lessons") or {}).items():
        if not isinstance(text, str) or not text.strip():
            errs.append(f"lesson:{lid} 文本为空")
        elif len(text) > MAX_LESSON_CHARS:
            errs.append(f"lesson:{lid} 超长({len(text)}>{MAX_LESSON_CHARS})")
        if lid not in known_lessons and not lid.startswith("NEW"):
            errs.append(f"lesson:{lid} 不存在(新增用 NEW1/NEW2)")
    n_new = sum(1 for k in (overrides.get("lessons") or {}) if k.startswith("NEW"))
    if len(_PRISTINE_LESSONS) + n_new > _lessons.MAX_LESSONS:
        errs.append(f"教训超预算({len(_PRISTINE_LESSONS)}+{n_new}>{_lessons.MAX_LESSONS})")
    for tool, desc in (overrides.get("tools") or {}).items():
        if tool not in _PRISTINE_SPECS:
            errs.append(f"tool:{tool} 不存在")
        elif not isinstance(desc, str) or not desc.strip():
            errs.append(f"tool:{tool} 描述为空")
        elif len(desc) > MAX_TOOL_CHARS:
            errs.append(f"tool:{tool} 超长({len(desc)}>{MAX_TOOL_CHARS})")
    return errs


def apply(overrides: dict) -> None:
    """先回 gen0 原貌,再叠加覆盖,然后重拼 prompt。合法性由调用方先 validate。"""
    new_lessons = []
    lov = overrides.get("lessons") or {}
    for l in _PRISTINE_LESSONS:
        if l.id in lov:
            new_lessons.append(dataclasses.replace(l, text=lov[l.id],
                                                   origin=l.origin + " → GEPA改写"))
        else:
            new_lessons.append(l)
    seq = 0
    for lid in sorted(k for k in lov if k.startswith("NEW")):
        seq += 1
        new_lessons.append(_lessons.Lesson(
            id=f"L9{seq}", born="GEPA", origin="GEPA 进化新增(谱系见 run 报告)",
            text=lov[lid], sunset="下轮 GEPA 复评;两轮无贡献退役"))
    _lessons.LESSONS = new_lessons

    tov = overrides.get("tools") or {}
    for name in _PRISTINE_SPECS:
        spec = _PRISTINE_SPECS[name]
        _ns.SPECS[name] = (dataclasses.replace(spec, planner_desc=tov[name])
                           if name in tov else spec)
    _ld.refresh_loop_system()


def reset() -> None:
    """回到 gen0 原貌(跑完/异常退出都必须调,防污染同进程后续评估)。"""
    apply({})


def diff_doc(overrides: dict) -> str:
    """人审用:候选相对 gen0 改了什么,老文本 vs 新文本并排。"""
    L = []
    old_lessons = {l.id: l.text for l in _PRISTINE_LESSONS}
    for lid, text in (overrides.get("lessons") or {}).items():
        L.append(f"### lesson:{lid}\n- 旧:{old_lessons.get(lid, '(新增)')}\n- 新:{text}")
    for tool, desc in (overrides.get("tools") or {}).items():
        old = " ".join(_PRISTINE_SPECS[tool].planner_desc.split())
        L.append(f"### tool:{tool}\n- 旧:{old}\n- 新:{desc}")
    return "\n\n".join(L) or "(与 gen0 相同)"
