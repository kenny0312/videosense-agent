"""本地评测仪表盘：每次跑完自动归档一条运行记录，重建 dashboard.html（中文）+ dashboard.en.html（英文）。

不用 push GitHub、不用起服务器 —— 浏览器打开 evals/dashboard.html，跑完按 F5 刷新；
右上角可切换 English / 中文。

    python -m evals.dashboard          # 重建仪表盘（两种语言）
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
DASH_EN_PATH = os.path.join(_HERE, "dashboard.en.html")

_KIND_COLOR = {"ok": "#0ca30c", "bad": "#d03b3b", "warn": "#d9822b", "neutral": "#6b6a66"}

DIM_LABEL_EN = {
    "required_actions": "Right tools used",
    "no_call": "Declined / asked properly",
    "honesty": "Honest, no fabrication",
    "retrieval": "Found right videos",
    "timestamp": "Timestamps accurate",
    "count": "Counts correct",
    "entity_match": "Entities correct",
    "no_id_leak": "No raw-id leakage",
    "identity": "No provider leakage",
    "safety": "Safe refusals",
    "jga": "Multi-turn memory",
}

_VERDICT_EN = {
    "变好": "Improved",
    "变差 · 打回": "Regressed · blocked",
    "有得有失 · 待人看": "Mixed · needs review",
    "没明显变化": "No significant change",
    "全部通过 · 建立基线": "All passed · baseline set",
}

L = {
    "zh": {
        "dims": DIM_LABEL,
        "mode": {"live": "真跑（Gemini）", "scripted": "脚本（免费）", "compare": "对比演示"},
        "title": "VS 评测仪表盘（本地）",
        "toggle": '<a href="dashboard.en.html" style="font-size:13px">English</a>',
        "empty": "还没有任何运行记录 —— 跑一次 python -m evals.runner 就有了。",
        "latest": "最近一次：{ts} · {mode} · {n} 道题 · 通过率 {rate}% · 必过题 {pa}/{pt} 过",
        "trend": "通过率走势（每个点=一次跑；蓝=真跑，灰=脚本）",
        "dim_sec": "各方面得分",
        "dim_cmp": "（括号=和上一次同类跑相比）",
        "dim_head": ("方面", "得分"),
        "fail_sec": "没过的题",
        "fail_none": "无 —— 全部通过。",
        "fail_head": ("题目", "栽在哪"),
        "hist_sec": "历史（最近 {n} 次）",
        "hist_head": ("时间", "模式", "题数", "通过率", "结论"),
        "pin": "必过",
        "foot": "跑完自动更新，浏览器 F5 即可 —— 不用 push GitHub。",
        "verdict": lambda s: s,
    },
    "en": {
        "dims": DIM_LABEL_EN,
        "mode": {"live": "Live (Gemini)", "scripted": "Scripted (free)", "compare": "Comparison demo"},
        "title": "VS eval dashboard (local)",
        "toggle": '<a href="dashboard.html" style="font-size:13px">中文</a>',
        "empty": "No runs recorded yet — run `python -m evals.runner` once.",
        "latest": "Latest: {ts} · {mode} · {n} tasks · pass rate {rate}% · must-pass {pa}/{pt} passed",
        "trend": "Pass-rate trend (one dot per run; blue = live, gray = scripted)",
        "dim_sec": "Per-dimension scores",
        "dim_cmp": " (delta vs previous run of same mode)",
        "dim_head": ("Dimension", "Score"),
        "fail_sec": "Failed tasks",
        "fail_none": "None — all passed.",
        "fail_head": ("Task", "Failed on"),
        "hist_sec": "History (last {n} runs)",
        "hist_head": ("Time", "Mode", "Tasks", "Pass rate", "Verdict"),
        "pin": "must-pass",
        "foot": "Auto-updates after each run; just refresh — no GitHub push needed.",
        "verdict": lambda s: _VERDICT_EN.get(s, s),
    },
}


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


def _trend_svg(runs: list[dict], lang: dict) -> str:
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
    return (
        f'<div class="sec">{lang["trend"]}</div>'
        f'<div class="card"><svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="pass rate trend">'
        f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#d7d5cc"/>'
        f'<text x="{pad - 6}" y="{pad + 4}" text-anchor="end" font-size="10" fill="#8f8d86">100%</text>'
        f'<text x="{pad - 6}" y="{h - pad + 4}" text-anchor="end" font-size="10" fill="#8f8d86">0%</text>'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="#b9b7af" stroke-width="1.5"/>'
        f'{"".join(dots)}</svg></div>'
    )


def _dim_table(latest: dict, prev: dict | None, lang: dict) -> str:
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
    cmp_note = lang["dim_cmp"] if prev else ""
    h1, h2 = lang["dim_head"]
    return (f'<div class="sec">{lang["dim_sec"]}{cmp_note}</div>'
            f'<table class="hm"><thead><tr><th>{h1}</th><th>{h2}</th></tr></thead><tbody>{rows}</tbody></table>')


def _failed_list(latest: dict, lang: dict) -> str:
    fails = [r for r in latest.get("results", []) if not r.get("passed")]
    if not fails:
        return f'<div class="sec">{lang["fail_sec"]}</div><div class="meta">{lang["fail_none"]}</div>'
    rows = ""
    for r in fails:
        bad = "、".join(lang["dims"].get(k, k) for k, v in r.get("scores", {}).items() if v < 1.0) or "—"
        pin = f'<span class="pin">{lang["pin"]}</span> ' if r.get("pinned") else ""
        rows += f"<tr><td>{pin}{r['id']}</td><td>{bad}</td></tr>"
    h1, h2 = lang["fail_head"]
    return (f'<div class="sec">{lang["fail_sec"]}（{len(fails)}）</div>'
            f'<table class="hm"><thead><tr><th>{h1}</th><th>{h2}</th></tr></thead><tbody>{rows}</tbody></table>')


def _history_table(runs: list[dict], lang: dict) -> str:
    rows = ""
    for r in reversed(runs[-20:]):
        rate = round(_rate(r) * 100)
        color = _KIND_COLOR.get(r.get("verdict_kind", "neutral"), "#6b6a66")
        rows += (f"<tr><td>{r['ts']}</td><td>{lang['mode'].get(r['mode'], r['mode'])}</td>"
                 f"<td style='text-align:right'>{r['tasks']}</td><td style='text-align:right'>{rate}%</td>"
                 f"<td style='color:{color}'>{lang['verdict'](r['verdict_label'])}</td></tr>")
    heads = "".join(f"<th>{h}</th>" for h in lang["hist_head"])
    return (f'<div class="sec">{lang["hist_sec"].format(n=min(len(runs), 20))}</div>'
            f'<table class="hm"><thead><tr>{heads}</tr></thead><tbody>{rows}</tbody></table>')


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


def _render(runs: list[dict], lang: dict) -> str:
    if not runs:
        head, body = "", f'<div class="meta">{lang["empty"]}</div>'
    else:
        latest = runs[-1]
        prev = next((r for r in reversed(runs[:-1]) if r["mode"] == latest["mode"]), None)
        color = _KIND_COLOR.get(latest.get("verdict_kind", "neutral"), "#6b6a66")
        head = f'<span class="pill" style="background:{color}">{lang["verdict"](latest["verdict_label"])}</span>'
        body = (
            '<div class="meta">' + lang["latest"].format(
                ts=latest["ts"], mode=lang["mode"].get(latest["mode"], latest["mode"]),
                n=latest["tasks"], rate=round(_rate(latest) * 100),
                pa=latest["pinned_total"] - latest["pinned_failed"], pt=latest["pinned_total"]) + "</div>"
            + _trend_svg(runs, lang) + _dim_table(latest, prev, lang)
            + _failed_list(latest, lang) + _history_table(runs, lang)
        )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{lang['title']}</title><style>{_CSS}</style></head><body>"
        f'<div class="head"><div class="title">{lang["title"]}</div>'
        f'<div style="display:flex;align-items:center;gap:12px">{lang["toggle"]}{head}</div></div>'
        f"{body}"
        f'<div class="meta" style="margin-top:24px">{lang["foot"]}</div>'
        "</body></html>"
    )


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
        os.startfile(path)  # noqa: S606  (Windows 本地打开)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
