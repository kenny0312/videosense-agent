"""本地评测仪表盘：每次跑完自动归档一条运行记录，重建一张自包含的 dashboard.html。

不用 push GitHub、不用起服务器 —— 浏览器打开 evals/dashboard.html，跑完按 F5 刷新。
（参考 Inspect AI `inspect view` / promptfoo `promptfoo view` 的思路，做成更简单的静态版。）

    python -m evals.dashboard          # 重建仪表盘
    python -m evals.dashboard --open   # 重建并在浏览器打开
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime

from evals.report import DIM_LABEL

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(_HERE, "runs")
DASH_PATH = os.path.join(_HERE, "dashboard.html")

MODE_LABEL = {"live": "真跑（Gemini）", "scripted": "脚本（免费）", "compare": "对比演示"}
_KIND_COLOR = {"ok": "#0ca30c", "bad": "#d03b3b", "warn": "#d9822b", "neutral": "#6b6a66"}


# ── 归档一次运行 ─────────────────────────────────────────────────────
def save_run(results: list[dict], verdict: dict, mode: str, ts: str | None = None) -> str:
    os.makedirs(RUNS_DIR, exist_ok=True)
    ts = ts or datetime.now().strftime("%Y%m%d-%H%M%S")
    per_dim: dict = {}
    for r in results:
        for d, v in r.get("scores", {}).items():
            per_dim.setdefault(d, []).append(v)
    rec = {
        "ts": ts,
        "mode": mode,
        "tasks": len(results),
        "passed": sum(1 for r in results if r.get("passed")),
        "pinned_total": sum(1 for r in results if r.get("pinned")),
        "pinned_failed": sum(1 for r in results if r.get("pinned") and not r.get("passed")),
        "verdict_label": verdict.get("label", "?"),
        "verdict_kind": verdict.get("kind", "neutral"),
        "per_dim": {d: (sum(v) / len(v) if v else 0.0) for d, v in per_dim.items()},
        "results": [{"id": r["id"], "passed": r.get("passed"), "pinned": r.get("pinned"),
                     "scores": r.get("scores", {})} for r in results],
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


# ── 重建仪表盘 HTML ──────────────────────────────────────────────────
def _rate(rec) -> float:
    return rec["passed"] / rec["tasks"] if rec["tasks"] else 0.0


def _trend_svg(runs: list[dict]) -> str:
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
                    f'<title>{r["ts"]} {MODE_LABEL.get(r["mode"], r["mode"])} {round(_rate(r) * 100)}%</title></circle>')
    return (
        f'<div class="sec">通过率走势（每个点=一次跑；蓝=真跑，灰=脚本）</div>'
        f'<div class="card"><svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="通过率走势">'
        f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#d7d5cc"/>'
        f'<text x="{pad - 6}" y="{pad + 4}" text-anchor="end" font-size="10" fill="#8f8d86">100%</text>'
        f'<text x="{pad - 6}" y="{h - pad + 4}" text-anchor="end" font-size="10" fill="#8f8d86">0%</text>'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="#b9b7af" stroke-width="1.5"/>'
        f'{"".join(dots)}</svg></div>'
    )


def _dim_table(latest: dict, prev: dict | None) -> str:
    rows = ""
    for d, v in sorted(latest.get("per_dim", {}).items()):
        label = DIM_LABEL.get(d, d)
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
    cmp_note = "（括号=和上一次同类跑相比）" if prev else ""
    return (f'<div class="sec">各方面得分{cmp_note}</div>'
            f'<table class="hm"><thead><tr><th>方面</th><th>得分</th></tr></thead><tbody>{rows}</tbody></table>')


def _failed_list(latest: dict) -> str:
    fails = [r for r in latest.get("results", []) if not r.get("passed")]
    if not fails:
        return '<div class="sec">没过的题</div><div class="meta">无 —— 全部通过。</div>'
    rows = ""
    for r in fails:
        bad = "、".join(DIM_LABEL.get(k, k) for k, v in r.get("scores", {}).items() if v < 1.0) or "—"
        pin = '<span class="pin">必过</span> ' if r.get("pinned") else ""
        rows += f"<tr><td>{pin}{r['id']}</td><td>{bad}</td></tr>"
    return (f'<div class="sec">没过的题（{len(fails)}）</div>'
            f'<table class="hm"><thead><tr><th>题目</th><th>栽在哪</th></tr></thead><tbody>{rows}</tbody></table>')


def _history_table(runs: list[dict]) -> str:
    rows = ""
    for r in reversed(runs[-20:]):
        rate = round(_rate(r) * 100)
        color = _KIND_COLOR.get(r.get("verdict_kind", "neutral"), "#6b6a66")
        rows += (f"<tr><td>{r['ts']}</td><td>{MODE_LABEL.get(r['mode'], r['mode'])}</td>"
                 f"<td style='text-align:right'>{r['tasks']}</td><td style='text-align:right'>{rate}%</td>"
                 f"<td style='color:{color}'>{r['verdict_label']}</td></tr>")
    return (f'<div class="sec">历史（最近 {min(len(runs), 20)} 次）</div>'
            f'<table class="hm"><thead><tr><th>时间</th><th>模式</th><th>题数</th><th>通过率</th><th>结论</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;color:#1b1b19;
 max-width:860px;margin:24px auto;padding:0 16px;background:#fff;line-height:1.6}
.head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.title{font-size:18px;font-weight:600}.meta{color:#6b6a66;font-size:13px}
.pill{padding:6px 14px;border-radius:999px;font-size:14px;font-weight:600;color:#fff}
.card{background:#faf9f6;border:1px solid #ece9e0;border-radius:10px;padding:10px 14px}
.sec{font-size:14px;font-weight:600;margin:20px 0 8px}
table.hm{border-collapse:collapse;width:100%;font-size:13px}
.hm th,.hm td{border:1px solid #ece9e0;padding:6px 10px;text-align:left}
.hm th{background:#faf9f6;font-weight:500;color:#6b6a66}
.pin{background:#eef1fb;color:#3a55c8;font-size:11px;padding:1px 6px;border-radius:6px}
"""


def rebuild() -> str:
    runs = load_runs()
    if not runs:
        body = '<div class="meta">还没有任何运行记录 —— 跑一次 python -m evals.runner 就有了。</div>'
        head = ""
    else:
        latest = runs[-1]
        prev = next((r for r in reversed(runs[:-1]) if r["mode"] == latest["mode"]), None)
        color = _KIND_COLOR.get(latest.get("verdict_kind", "neutral"), "#6b6a66")
        head = (f'<span class="pill" style="background:{color}">{latest["verdict_label"]}</span>')
        rate = round(_rate(latest) * 100)
        body = (
            f'<div class="meta">最近一次：{latest["ts"]} · {MODE_LABEL.get(latest["mode"], latest["mode"])} · '
            f'{latest["tasks"]} 道题 · 通过率 {rate}% · 必过题 '
            f'{latest["pinned_total"] - latest["pinned_failed"]}/{latest["pinned_total"]} 过</div>'
            + _trend_svg(runs) + _dim_table(latest, prev) + _failed_list(latest) + _history_table(runs)
        )
    html = (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>VS 评测仪表盘</title><style>{_CSS}</style></head><body>"
        f'<div class="head"><div class="title">VS 评测仪表盘（本地）</div>{head}</div>'
        f"{body}"
        '<div class="meta" style="margin-top:24px">跑完自动更新，浏览器 F5 即可 —— 不用 push GitHub。</div>'
        "</body></html>"
    )
    with open(DASH_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    return DASH_PATH


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    path = rebuild()
    print(f"仪表盘已重建：{path}")
    if "--open" in (argv or sys.argv[1:]):
        os.startfile(path)  # noqa: S606  (Windows 本地打开)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
