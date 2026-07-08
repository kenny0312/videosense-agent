"""本地评测仪表盘：每次跑完自动归档，重建 dashboard.html（中文）+ dashboard.en.html（英文）。

看什么都在这一页：结论、通过率（带波动区间）、走势、各方面得分、
和上次比新挂/新过、每道失败题一屏下钻（题目/答案/期望/工具链）、稳定优势、历史。
不用 push GitHub、不用起服务器 —— 跑完按 F5 刷新，右上角切换语言。

    python -m evals.dashboard          # 重建
    python -m evals.dashboard --open   # 重建并在浏览器打开
"""
from __future__ import annotations

import glob
import html as html_mod
import json
import os
import sys
from datetime import datetime

from evals.report import DIM_LABEL
from evals.scorers import wilson

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(_HERE, "runs")
DASH_PATH = os.path.join(_HERE, "dashboard.html")
DASH_EN_PATH = os.path.join(_HERE, "dashboard.en.html")

_KIND_COLOR = {"ok": "#0ca30c", "bad": "#d03b3b", "warn": "#d9822b", "neutral": "#6b6a66"}

DIM_LABEL_EN = {
    "required_actions": "Right tools used",
    "no_call": "Declined / asked properly",
    "no_forbidden": "No unwanted actions",
    "honesty": "Honest, no fabrication",
    "retrieval": "Found right videos",
    "timestamp": "Timestamps accurate",
    "count": "Counts correct",
    "entity_match": "Entities correct",
    "no_id_leak": "No raw-id leakage",
    "identity": "No provider leakage",
    "safety": "Safe refusals",
    "jga": "Multi-turn memory",
    "state_assertions": "World state landed",
}

_VERDICT_EN = {
    "变好": "Improved",
    "变差 · 打回": "Regressed · blocked",
    "有得有失 · 待人看": "Mixed · needs review",
    "没明显变化": "No significant change",
    "全部通过 · 建立基线": "All passed · baseline set",
    "已出分 · 建立基线": "Scored · baseline set",
}

L = {
    "zh": {
        "dims": DIM_LABEL,
        "mode": {"live": "真跑（Gemini）", "scripted": "脚本（免费）", "compare": "对比演示"},
        "title": "VS 评测仪表盘（本地）",
        "toggle": '<a href="dashboard.en.html" style="font-size:13px">English</a>',
        "empty": "还没有任何运行记录 —— 跑一次 python -m evals.runner 就有了。",
        "meta": "最近一次：{ts} · {mode} · 模型 {model} · 代码 {commit}{dirty} · 每题 {n} 次 · 尺子指纹 {fp}",
        "dirty_mark": "（有未提交改动）",
        "cards": ("通过率（只算真计分）", "波动区间", "必过题", "环境故障（不计分）"),
        "skipped": "另有 {n} 道题要「用户改共享状态」才能判，本次跳过：{ids}",
        "trend": "通过率走势（每个点=一次跑；蓝=真跑，灰=脚本）",
        "dim_sec": "各方面得分",
        "dim_cmp": "（括号=和上一次同类跑相比）",
        "dim_head": ("方面", "得分"),
        "flips": "和上次比（按同一道题配对）",
        "new_fail": "新挂 {n} 题",
        "new_pass": "新过 {n} 题",
        "fail_sec": "没过的题 · 点开下钻",
        "fail_none": "无 —— 全部通过。",
        "drill": ("题目", "它答了", "期望", "工具链", "金标依据"),
        "strength": "稳定优势（连续 ≥3 次同类跑全过的方面）",
        "strength_none": "暂无（历史不够 3 次，多跑几次就有了）",
        "hist_sec": "历史（最近 {n} 次）",
        "hist_head": ("时间", "模式", "模型", "题数", "通过率", "结论"),
        "pin": "必过",
        "cost": "本次花费：调大脑 {llm} 次 · 看画面 {an} 次 · 总耗时 {min} 分钟",
        "foot": "跑完自动更新，浏览器 F5 即可 —— 不用 push GitHub。",
        "verdict": lambda s: s,
    },
    "en": {
        "dims": DIM_LABEL_EN,
        "mode": {"live": "Live (Gemini)", "scripted": "Scripted (free)", "compare": "Comparison demo"},
        "title": "VS eval dashboard (local)",
        "toggle": '<a href="dashboard.html" style="font-size:13px">中文</a>',
        "empty": "No runs recorded yet — run `python -m evals.runner` once.",
        "meta": "Latest: {ts} · {mode} · model {model} · code {commit}{dirty} · n={n} per task · scorer fp {fp}",
        "dirty_mark": " (uncommitted changes)",
        "cards": ("Pass rate (scored only)", "Confidence range", "Must-pass", "Infra errors (excluded)"),
        "skipped": "{n} more tasks need user-side world actions and were skipped: {ids}",
        "trend": "Pass-rate trend (one dot per run; blue = live, gray = scripted)",
        "dim_sec": "Per-dimension scores",
        "dim_cmp": " (delta vs previous run of same mode)",
        "dim_head": ("Dimension", "Score"),
        "flips": "Vs previous run (paired by task)",
        "new_fail": "{n} newly failing",
        "new_pass": "{n} newly passing",
        "fail_sec": "Failed tasks · click to drill down",
        "fail_none": "None — all passed.",
        "drill": ("Question", "Agent said", "Expected", "Tool calls", "Gold basis"),
        "strength": "Stable strengths (dimensions at 100% for ≥3 consecutive runs)",
        "strength_none": "None yet (needs 3+ runs of history)",
        "hist_sec": "History (last {n} runs)",
        "hist_head": ("Time", "Mode", "Model", "Tasks", "Pass rate", "Verdict"),
        "pin": "must-pass",
        "cost": "This run: {llm} brain calls · {an} video looks · {min} min total",
        "foot": "Auto-updates after each run; just refresh — no GitHub push needed.",
        "verdict": lambda s: _VERDICT_EN.get(s, s),
    },
}


# ── 归档 ────────────────────────────────────────────────────────────
def save_run(results: list[dict], verdict: dict, mode: str, ts: str | None = None,
             meta: dict | None = None) -> str:
    os.makedirs(RUNS_DIR, exist_ok=True)
    ts = ts or datetime.now().strftime("%Y%m%d-%H%M%S")
    per_dim: dict = {}
    for r in results:
        for d, v in r.get("scores", {}).items():
            per_dim.setdefault(d, []).append(v)
    scored = [r for r in results if r.get("status", "ok") == "ok"]

    def _slim(r):
        """归档瘦身：留下钻要用的字段，答案截断。"""
        keep = {k: r.get(k) for k in ("id", "passed", "pinned", "status", "scores",
                                      "n", "successes", "pass_k", "dims", "kind",
                                      "question", "grounding_note", "tools", "cost")}
        keep["answer"] = (r.get("answer") or "")[:500]
        ff = r.get("first_fail")
        keep["first_fail"] = ({"answer": (ff.get("answer") or "")[:500],
                               "tools": ff.get("tools", [])[:12],
                               "scores": ff.get("scores", {})} if ff else None)
        keep["expect"] = r.get("expect", {})
        return keep

    rec = {
        "ts": ts,
        "mode": mode,
        "meta": meta or {},
        "tasks": len(scored),
        "infra": sum(1 for r in results if r.get("status") == "infra_error"),
        "passed": sum(1 for r in scored if r.get("passed")),
        "pinned_total": sum(1 for r in scored if r.get("pinned")),
        "pinned_failed": sum(1 for r in scored if r.get("pinned") and not r.get("passed")),
        "verdict_label": verdict.get("label", "?"),
        "verdict_kind": verdict.get("kind", "neutral"),
        "reasons": verdict.get("reasons", []),
        "per_dim": {d: (sum(v) / len(v) if v else 0.0) for d, v in per_dim.items()},
        "cost": {
            "llm_calls": sum((r.get("cost") or {}).get("llm_calls", 0) for r in results),
            "analyze_calls": sum((r.get("cost") or {}).get("analyze_calls", 0) for r in results),
            "wall_ms": sum((r.get("cost") or {}).get("wall_ms", 0) for r in results),
        },
        "results": [_slim(r) for r in results],
    }
    path = os.path.join(RUNS_DIR, f"run-{ts}-{mode}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    return path


def load_runs() -> list[dict]:
    runs = []
    for f in sorted(glob.glob(os.path.join(RUNS_DIR, "run-*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                runs.append(json.load(fh))
        except Exception:
            continue
    return runs


def latest_run(mode: str) -> dict | None:
    """某个模式最近的一次运行记录（真跑找对比基准用）。"""
    for r in reversed(load_runs()):
        if r.get("mode") == mode:
            return r
    return None


# ── 渲染 ────────────────────────────────────────────────────────────
def _rate(rec) -> float:
    return rec["passed"] / rec["tasks"] if rec["tasks"] else 0.0


def _esc(s) -> str:
    return html_mod.escape(str(s or ""))


def _trend_svg(runs, lang) -> str:
    if len(runs) < 2:
        return ""
    w, h, pad = 640, 150, 30
    n = len(runs)
    pts, dots = [], []
    color = {"live": "#3987e5", "scripted": "#8f8d86", "compare": "#d9822b"}
    for i, r in enumerate(runs):
        x = pad + (w - 2 * pad) * (i / max(n - 1, 1))
        y = h - pad - (h - 2 * pad) * _rate(r)
        pts.append(f"{x:.0f},{y:.0f}")
        dots.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="4" fill="{color.get(r["mode"], "#888")}">'
                    f'<title>{r["ts"]} {lang["mode"].get(r["mode"], r["mode"])} {round(_rate(r) * 100)}%</title></circle>')
    return (f'<div class="sec">{lang["trend"]}</div>'
            f'<div class="card"><svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="trend">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#d7d5cc"/>'
            f'<text x="{pad - 6}" y="{pad + 4}" text-anchor="end" font-size="10" fill="#8f8d86">100%</text>'
            f'<text x="{pad - 6}" y="{h - pad + 4}" text-anchor="end" font-size="10" fill="#8f8d86">0%</text>'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="#b9b7af" stroke-width="1.5"/>'
            f'{"".join(dots)}</svg></div>')


def _dim_table(latest, prev, lang) -> str:
    rows = ""
    for d, v in sorted(latest.get("per_dim", {}).items()):
        label = lang["dims"].get(d, d)
        cur = round(v * 100)
        delta = ""
        if prev and d in prev.get("per_dim", {}):
            dv = round((v - prev["per_dim"][d]) * 100)
            if dv:
                c = "#0ca30c" if dv > 0 else "#d03b3b"
                delta = f'<span style="color:{c}">（{"+" if dv > 0 else ""}{dv}）</span>'
        rows += f"<tr><td>{label}</td><td style='text-align:right'>{cur}%{delta}</td></tr>"
    if not rows:
        return ""
    h1, h2 = lang["dim_head"]
    return (f'<div class="sec">{lang["dim_sec"]}{lang["dim_cmp"] if prev else ""}</div>'
            f'<table class="hm"><thead><tr><th>{h1}</th><th>{h2}</th></tr></thead><tbody>{rows}</tbody></table>')


def _flips_section(latest, prev, lang) -> str:
    if not prev:
        return ""
    prev_map = {r["id"]: r for r in prev.get("results", []) if r.get("status", "ok") == "ok"}
    cur = [r for r in latest.get("results", []) if r.get("status", "ok") == "ok"]
    new_fail = [r["id"] for r in cur if r["id"] in prev_map
                and prev_map[r["id"]].get("passed") and not r.get("passed")]
    new_pass = [r["id"] for r in cur if r["id"] in prev_map
                and not prev_map[r["id"]].get("passed") and r.get("passed")]
    if not new_fail and not new_pass:
        return ""
    box = ('<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">'
           f'<div class="card" style="border-left:3px solid #d03b3b;border-radius:0 10px 10px 0">'
           f'<b style="font-size:12px">{lang["new_fail"].format(n=len(new_fail))}</b>'
           f'<div style="font-size:12px;color:#4a4945">{_esc("、".join(new_fail[:8]))}</div></div>'
           f'<div class="card" style="border-left:3px solid #0ca30c;border-radius:0 10px 10px 0">'
           f'<b style="font-size:12px">{lang["new_pass"].format(n=len(new_pass))}</b>'
           f'<div style="font-size:12px;color:#4a4945">{_esc("、".join(new_pass[:8]))}</div></div></div>')
    return f'<div class="sec">{lang["flips"]}</div>{box}'


def _fail_cards(latest, lang) -> str:
    fails = [r for r in latest.get("results", []) if not r.get("passed")]
    infra = [r for r in fails if r.get("status") == "infra_error"]
    fails = [r for r in fails if r.get("status", "ok") == "ok"]
    if not fails and not infra:
        return f'<div class="sec">{lang["fail_sec"]}</div><div class="meta">{lang["fail_none"]}</div>'
    q, a, e, t, g = lang["drill"]
    cards = ""
    for r in fails:
        bad = "、".join(lang["dims"].get(k, k) for k, v in r.get("scores", {}).items() if v < 1.0) or "—"
        pin = f'<span class="pin">{lang["pin"]}</span> ' if r.get("pinned") else ""
        sample = r.get("first_fail") or {}
        ans = sample.get("answer") or r.get("answer") or ""
        tools = sample.get("tools") or r.get("tools") or []
        tool_str = " → ".join(f"{x.get('tool')}({_esc(x.get('args', ''))[:60]})" for x in tools[:8]) or "—"
        expect = json.dumps(r.get("expect", {}), ensure_ascii=False)[:300]
        cards += (
            f'<details class="card" style="margin-bottom:8px"><summary style="cursor:pointer;font-size:13px">'
            f'{pin}<b>{_esc(r["id"])}</b>　<span style="color:#d03b3b;font-size:12px">{bad}</span></summary>'
            f'<table class="drill"><tr><td>{q}</td><td>{_esc(r.get("question"))}</td></tr>'
            f'<tr><td>{a}</td><td>{_esc(ans[:400])}</td></tr>'
            f'<tr><td>{e}</td><td><code style="font-size:11px">{_esc(expect)}</code></td></tr>'
            f'<tr><td>{t}</td><td style="font-size:11px">{tool_str}</td></tr>'
            f'<tr><td>{g}</td><td style="color:#6b6a66">{_esc(r.get("grounding_note"))}</td></tr></table></details>')
    for r in infra:
        cards += (f'<div class="card" style="margin-bottom:8px;border-left:3px solid #b9b7af;border-radius:0 10px 10px 0">'
                  f'<b style="font-size:13px">{_esc(r["id"])}</b>　'
                  f'<span style="font-size:12px;color:#6b6a66">{_esc((r.get("answer") or "")[:160])}</span></div>')
    return f'<div class="sec">{lang["fail_sec"]}（{len(fails)}+{len(infra)}）</div>{cards}'


def _strengths(runs, latest, lang) -> str:
    same = [r for r in runs if r["mode"] == latest["mode"]][-3:]
    if len(same) < 3:
        return f'<div class="sec">{lang["strength"]}</div><div class="meta">{lang["strength_none"]}</div>'
    stable = []
    for d in latest.get("per_dim", {}):
        if all(r.get("per_dim", {}).get(d, 0) >= 0.999 for r in same):
            stable.append(lang["dims"].get(d, d))
    body = ("、".join(stable) if stable else lang["strength_none"])
    return (f'<div class="sec">{lang["strength"]}</div>'
            f'<div class="card" style="font-size:13px;color:#0a6b0a">{body}</div>')


def _history_table(runs, lang) -> str:
    rows = ""
    for r in reversed(runs[-20:]):
        rate = round(_rate(r) * 100)
        color = _KIND_COLOR.get(r.get("verdict_kind", "neutral"), "#6b6a66")
        model = (r.get("meta") or {}).get("model", "")
        rows += (f"<tr><td>{r['ts']}</td><td>{lang['mode'].get(r['mode'], r['mode'])}</td>"
                 f"<td style='font-size:11px'>{_esc(model)[:22]}</td>"
                 f"<td style='text-align:right'>{r['tasks']}</td><td style='text-align:right'>{rate}%</td>"
                 f"<td style='color:{color}'>{lang['verdict'](r['verdict_label'])}</td></tr>")
    heads = "".join(f"<th>{h}</th>" for h in lang["hist_head"])
    return (f'<div class="sec">{lang["hist_sec"].format(n=min(len(runs), 20))}</div>'
            f'<table class="hm"><thead><tr>{heads}</tr></thead><tbody>{rows}</tbody></table>')


_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;color:#1b1b19;
 max-width:880px;margin:24px auto;padding:0 16px;background:#fff;line-height:1.6}
.head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.title{font-size:18px;font-weight:600}.meta{color:#6b6a66;font-size:13px}
.pill{padding:6px 14px;border-radius:999px;font-size:14px;font-weight:600;color:#fff}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:10px 0 4px}
.mc{background:#faf9f6;border:1px solid #ece9e0;border-radius:10px;padding:10px 14px}
.mcl{font-size:12px;color:#6b6a66}.mcv{font-size:22px;font-weight:600;margin:1px 0}.mcs{font-size:11px;color:#8f8d86}
.card{background:#faf9f6;border:1px solid #ece9e0;border-radius:10px;padding:10px 14px}
.sec{font-size:14px;font-weight:600;margin:20px 0 8px}
table.hm{border-collapse:collapse;width:100%;font-size:13px}
.hm th,.hm td{border:1px solid #ece9e0;padding:6px 10px;text-align:left}
.hm th{background:#faf9f6;font-weight:500;color:#6b6a66}
table.drill{border-collapse:collapse;width:100%;font-size:12px;margin-top:8px}
.drill td{border-top:1px solid #ece9e0;padding:5px 8px;vertical-align:top}
.drill td:first-child{width:70px;color:#8f8d86;white-space:nowrap}
.pin{background:#eef1fb;color:#3a55c8;font-size:11px;padding:1px 6px;border-radius:6px}
"""


def _render(runs, lang) -> str:
    if not runs:
        head, body = "", f'<div class="meta">{lang["empty"]}</div>'
    else:
        latest = runs[-1]
        prev = next((r for r in reversed(runs[:-1]) if r["mode"] == latest["mode"]), None)
        meta = latest.get("meta") or {}
        color = _KIND_COLOR.get(latest.get("verdict_kind", "neutral"), "#6b6a66")
        head = f'<span class="pill" style="background:{color}">{lang["verdict"](latest["verdict_label"])}</span>'
        lo, hi = wilson(latest["passed"], latest["tasks"] or 1)
        c1, c2, c3, c4 = lang["cards"]
        cost = latest.get("cost", {})
        meta_line = lang["meta"].format(
            ts=latest["ts"], mode=lang["mode"].get(latest["mode"], latest["mode"]),
            model=_esc(meta.get("model", "?")), commit=_esc(meta.get("commit", "?")),
            dirty=lang["dirty_mark"] if meta.get("dirty") else "",
            n=_esc(meta.get("n", "?")), fp=_esc(meta.get("scorer_fp", "?")))
        cards = (
            '<div class="cards">'
            f'<div class="mc"><div class="mcl">{c1}</div><div class="mcv">{round(_rate(latest) * 100)}%</div>'
            f'<div class="mcs">{latest["passed"]}/{latest["tasks"]}</div></div>'
            f'<div class="mc"><div class="mcl">{c2}</div><div class="mcv">{round(lo * 100)}~{round(hi * 100)}%</div>'
            f'<div class="mcs">95%</div></div>'
            f'<div class="mc"><div class="mcl">{c3}</div>'
            f'<div class="mcv" style="color:{"#d03b3b" if latest["pinned_failed"] else "#0ca30c"}">'
            f'{latest["pinned_total"] - latest["pinned_failed"]}/{latest["pinned_total"]}</div><div class="mcs">&nbsp;</div></div>'
            f'<div class="mc"><div class="mcl">{c4}</div><div class="mcv">{latest.get("infra", 0)}</div>'
            f'<div class="mcs">&nbsp;</div></div></div>')
        skipped = (meta.get("skipped") or [])
        skip_line = (f'<div class="meta">{lang["skipped"].format(n=len(skipped), ids=_esc("、".join(skipped[:6])))}</div>'
                     if skipped else "")
        cost_line = (f'<div class="meta">{lang["cost"].format(llm=cost.get("llm_calls", 0), an=cost.get("analyze_calls", 0), min=round(cost.get("wall_ms", 0) / 60000, 1))}</div>'
                     if cost.get("llm_calls") else "")
        reasons = "".join(f"<li>{_esc(x)}</li>" for x in latest.get("reasons", []))
        reasons_html = f'<ul class="meta" style="margin:4px 0 0">{reasons}</ul>' if reasons else ""
        body = (f'<div class="meta">{meta_line}</div>{reasons_html}{cards}{cost_line}{skip_line}'
                + _trend_svg(runs, lang) + _dim_table(latest, prev, lang)
                + _flips_section(latest, prev, lang) + _fail_cards(latest, lang)
                + _strengths(runs, latest, lang) + _history_table(runs, lang))
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{lang['title']}</title><style>{_CSS}</style></head><body>"
            f'<div class="head"><div class="title">{lang["title"]}</div>'
            f'<div style="display:flex;align-items:center;gap:12px">{lang["toggle"]}{head}</div></div>'
            f"{body}"
            f'<div class="meta" style="margin-top:24px">{lang["foot"]}</div>'
            "</body></html>")


def rebuild() -> str:
    runs = load_runs()
    with open(DASH_PATH, "w", encoding="utf-8") as fh:
        fh.write(_render(runs, L["zh"]))
    with open(DASH_EN_PATH, "w", encoding="utf-8") as fh:
        fh.write(_render(runs, L["en"]))
    return DASH_PATH


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    path = rebuild()
    print(f"仪表盘已重建（中/英）：{path}")
    if "--open" in (argv or sys.argv[1:]):
        os.startfile(path)  # noqa: S606
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
