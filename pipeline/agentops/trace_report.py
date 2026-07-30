"""T-3 诊断工具:测试/跑批挂了,先跑这个 —— 输出直接回答"问题出在哪一层设计"。

  python -m pipeline.agentops.trace_report --trace <trace.json>   # 单条:全树时间线
  python -m pipeline.agentops.trace_report --triage <目录或文件>   # 一批:按设计层/因由码聚合

数据源 = trace.dump_trace() 落的 JSON(线上 trace 只在内存环里,不可事后诊断)。
纯离线、零 API、零第三方依赖。放在 pipeline/agentops/ 而非 scripts/ —— 后者被 .gitignore
整目录排除,工具进不了仓库而依赖它的测试会进,CI 必红(审查 HIGH)。
输出刻意全 ASCII 且强制 UTF-8 stdout:验尸工具不能在验尸现场因编码自己死掉。
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter

from pipeline.agentops.trace import CAUSES, COMPONENTS   # 枚举唯一来源

_MARK = {"ok": "+", "error": "x", "retry": "~", "softfail": "!", "refused": "/",
         "running": "?"}
_CAUSE_OWNER = {c: comp for comp, cs in CAUSES.items() for c in cs}   # 因由码 → 设计层
_FAIL = ("error", "softfail", "refused")
_HYGIENE_KEYS = ("cause_missing", "cause_unknown", "cause_component_mismatch",
                 "cost_nonfinite")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render_tree(payload: dict) -> str:
    """按 parent_id 还原树,DFS 打印。孤儿 span(父不在本 trace)挂根下并标注;环形父子不死循环。"""
    steps = payload.get("steps") or []
    by_id = {s.get("span_id"): s for s in steps if s.get("span_id")}
    kids: dict = {}
    roots = []
    for s in steps:
        pid = s.get("parent_id")
        if pid and pid in by_id:
            kids.setdefault(pid, []).append(s)     # 有效父 → 挂到父下
        else:
            roots.append(s)                        # 无父 或 父不在本 trace(孤儿,下面标注)

    out = [f"trace {payload.get('trace_id') or payload.get('task_id') or '(unnamed)'}"
           f"  steps={len(steps)}  {payload.get('total_ms', 0)}ms"
           f"  ${float(payload.get('total_cost_usd') or 0):.4f}"]
    orphan_ids = {s.get("span_id") for s in roots if s.get("parent_id")}
    seen: set = set()                              # 防环:a->b->a 不该无限递归

    def walk(node, indent=0):
        sid = node.get("span_id")
        if sid in seen:
            out.append(f"{'  ' * indent}[!] (cycle at {sid}, stopped)")
            return
        seen.add(sid)
        tok = node.get("tok") or {}
        tokstr = (f" tok={tok.get('in', 0)}/{tok.get('out', 0)}"
                  f"+{tok.get('thought', 0)}th" if tok else "")
        money = f" ${node.get('cost_usd', 0):.4f}" if node.get("cost_usd") else ""
        cause = f" <{node.get('cause')}>" if node.get("cause") else ""
        flags = [k for k in _HYGIENE_KEYS if (node.get("meta") or {}).get(k)]
        flag = f"  !!{','.join(flags)}" if flags else ""
        orph = "  !!orphan_span" if sid in orphan_ids else ""
        err = f"  -> {str(node.get('error'))[:70]}" if node.get("error") else ""
        out.append(f"{'  ' * indent}[{_MARK.get(node.get('status'), '?')}] "
                   f"{str(node.get('component', '?')):5s} {str(node.get('name'))[:46]:46s}"
                   f" {int(node.get('elapsed_ms') or 0):6d}ms{money}{tokstr}{cause}{flag}"
                   f"{orph}{err}")
        for k in kids.get(sid, []):
            walk(k, indent + 1)

    for r in roots:
        walk(r)
    # 全环形图(a→b→a)时 roots 为空 → 上面一行都不输出。把未访问到的当伪根补渲染,
    # 环由 walk 里的 seen 断开(审查 MED:渲染器不能对着环形数据装死)。
    for s in steps:
        if s.get("span_id") not in seen:
            out.append("(unreachable subtree, parent chain is cyclic or missing)")
            walk(s)
    # "钱花在树的哪个枝上必须可见"(Part T):最贵/最慢的三个 span 单列
    costly = sorted((s for s in steps if s.get("cost_usd")),
                    key=lambda s: -float(s.get("cost_usd") or 0))[:3]
    if costly:
        out.append("top cost: " + ", ".join(
            f"{s.get('name')}(${float(s['cost_usd']):.4f}, d{s.get('depth', 0)})"
            for s in costly))
    slow = sorted(steps, key=lambda s: -int(s.get("elapsed_ms") or 0))[:3]
    if slow and int(slow[0].get("elapsed_ms") or 0):
        out.append("top time: " + ", ".join(
            f"{s.get('name')}({int(s.get('elapsed_ms') or 0)}ms, d{s.get('depth', 0)})"
            for s in slow))
    cc = payload.get("cause_counts") or {}
    if cc:
        out.append("causes: " + ", ".join(f"{k}x{v}" for k, v in sorted(cc.items())))
    return "\n".join(out)


def triage(paths: list) -> str:
    """一批 trace → 失败按【设计层】与【因由码】聚合。测试失败后的第一张表。"""
    by_cause, by_comp, hygiene = Counter(), Counter(), Counter()
    n_trace = n_step = n_fail = 0
    cost = 0.0
    for p in paths:
        try:
            payload = _load(p)
        except Exception:                       # 坏 JSON / 读不了 / 编码问题
            hygiene["unreadable_trace_file"] += 1
            continue
        if not isinstance(payload, dict) or "steps" not in payload:
            hygiene["not_a_trace_file"] += 1    # 目录里混进别的 json → 统计而非崩溃
            continue
        n_trace += 1
        try:
            cost += float(payload.get("total_cost_usd") or 0)
        except (TypeError, ValueError):
            hygiene["bad_cost_field"] += 1
        for s in payload.get("steps") or []:
            if not isinstance(s, dict):
                hygiene["bad_step_shape"] += 1
                continue
            n_step += 1
            if s.get("status") not in _FAIL:
                continue
            n_fail += 1
            cause = s.get("cause")
            comp = s.get("component") or "?"
            by_comp[comp] += 1
            by_cause[cause or f"({comp}:NO_CAUSE)"] += 1
            meta = s.get("meta") or {}
            for k in _HYGIENE_KEYS:
                if meta.get(k):
                    hygiene[k] += 1
            # main 层豁免 cause_missing(它是兜底层),但"完全没归因的失败"必须响亮:
            # 否则一个零归因 trace 会被报成"体检合格"(审查 HIGH)。
            if not cause and comp == "main":
                hygiene["unattributed_main_failure"] += 1
            if cause and cause in _CAUSE_OWNER and _CAUSE_OWNER[cause] != comp:
                hygiene["cause_owner_mismatch"] += 1

    out = [f"triage: {n_trace} trace / {n_step} span / {n_fail} failures / ${cost:.4f}"]
    if not n_fail:
        out.append("no failed span.")
    else:
        out.append("\nby design layer:")
        for comp, n in by_comp.most_common():
            known = "" if comp in COMPONENTS else " (UNREGISTERED LAYER!)"
            out.append(f"  {comp:6s}{known} {n:4d}  {n / n_fail * 100:4.0f}%")
        out.append("\nby cause code:")
        for c, n in by_cause.most_common():
            out.append(f"  {c:26s} {n:4d}   (layer={_CAUSE_OWNER.get(c, '-')})")
    if hygiene:
        out.append("\n!! instrumentation hygiene (these are bugs themselves, fix first):")
        for k, n in hygiene.most_common():
            out.append(f"  {k}: {n}")
    return "\n".join(out)


def main(argv=None) -> int:
    # 验尸工具不能因 stdout 编码自己死掉(Windows cp936 / 重定向 / CI capture)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    doc = (__doc__ or "").strip()
    for flag in ("--trace", "--triage"):
        if flag not in argv:
            continue
        i = argv.index(flag)
        if i + 1 >= len(argv):
            print(f"{flag} 需要一个路径参数\n\n{doc}")
            return 2
        target = argv[i + 1]
        if flag == "--trace":
            if not os.path.isfile(target):
                print(f"找不到 trace 文件: {target}\n"
                      f"(本工具吃 dump_trace 落的【文件路径】,不是 request_id;"
                      f"按 id 找请先 --triage 目录看有哪些文件)")
                return 1
            print(render_tree(_load(target)))
            return 0
        paths = (sorted(glob.glob(os.path.join(target, "**", "*.json"), recursive=True))
                 if os.path.isdir(target) else [target])
        if not paths:
            print(f"{target} 下没有 trace json")
            return 1
        print(triage(paths))
        return 0
    print(doc)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
