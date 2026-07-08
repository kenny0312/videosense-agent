"""把跑完的结果生成一张大白话 HTML 报告（浏览器直接打开）。

标签全用人话：整体通过率 / 变差的方面 / 必过题 / 找对视频 / 诚实不瞎编 …
单次跑 -> 卡片 + 每题×各方面热力图；给了 baseline -> 再加"各方面相比旧版的变化"条形图。
静态 HTML + 内联 SVG，无外部依赖。
"""
from __future__ import annotations

# 判分器 -> 人话
DIM_LABEL = {
    "required_actions": "工具用得对",
    "no_call": "该问就问",
    "honesty": "诚实不瞎编",
    "retrieval": "找对视频",
    "timestamp": "时间点准",
    "count": "数量对",
    "entity_match": "实体对得上",
    "no_id_leak": "不泄漏原始id",
    "identity": "身份不漏底",
    "safety": "安全拒答",
    "jga": "多轮不忘事",
}
_KIND_COLOR = {"ok": "#0ca30c", "bad": "#d03b3b", "warn": "#d9822b", "neutral": "#6b6a66"}


def _all_dims(results):
    dims = []
    for r in results:
        for d in r["scores"]:
            if d not in dims:
                dims.append(d)
    return dims


def _dim_mean(results, dim):
    vals = [r["scores"][dim] for r in results if dim in r["scores"]]
    return sum(vals) / len(vals) if vals else None


def _cell(score):
    if score is None:
        return '<td style="text-align:center;color:#b9b7af">·</td>'
    if score >= 0.999:
        return '<td style="text-align:center;background:#e7f4e2;color:#0ca30c">✓</td>'
    if score > 0:
        return '<td style="text-align:center;background:#fbf0dd;color:#d9822b">~</td>'
    return '<td style="text-align:center;background:#fbe4e4;color:#d03b3b">✗</td>'


def _cards(results, baseline):
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    rate = round(100 * passed / total) if total else 0
    pinned = [r for r in results if r["pinned"]]
    pinned_fail = sum(1 for r in pinned if not r["passed"])
    cards = [
        ("整体通过率", f"{rate}%", f"{passed}/{total} 道题过了", "#1b1b19"),
        ("必过题", f"{len(pinned) - pinned_fail}/{len(pinned)} 过" if pinned else "无",
         "有失守就打回" if pinned else "", "#d03b3b" if pinned_fail else "#1b1b19"),
        ("测了几个方面", str(len(_all_dims(results))), "每方面单独判", "#1b1b19"),
    ]
    if baseline is not None:
        worse = 0
        for d in _all_dims(results):
            cv, bv = _dim_mean(results, d), _dim_mean(baseline, d)
            if cv is not None and bv is not None and cv - bv <= -0.15:
                worse += 1
        cards.append(("变差的方面", str(worse), "相比旧版", "#d03b3b" if worse else "#1b1b19"))
    html = '<div class="cards">'
    for label, val, sub, color in cards:
        html += (f'<div class="mc"><div class="mcl">{label}</div>'
                 f'<div class="mcv" style="color:{color}">{val}</div>'
                 f'<div class="mcs">{sub}</div></div>')
    return html + "</div>"


def _bars(results, baseline):
    rows = []
    for d in _all_dims(results):
        cv, bv = _dim_mean(results, d), _dim_mean(baseline, d)
        if cv is None or bv is None:
            continue
        rows.append((DIM_LABEL.get(d, d), round((cv - bv) * 100)))
    if not rows:
        return ""
    mx = max((abs(v) for _, v in rows), default=1) or 1
    html = '<div class="sec">各方面相比旧版的变化（百分点）</div><div class="bars">'
    for label, dv in rows:
        w = min(abs(dv) / mx * 45, 45)
        color = "#0ca30c" if dv > 0 else ("#d03b3b" if dv < 0 else "#b9b7af")
        if dv >= 0:
            bar = f'<span class="bar" style="left:50%;width:{w:.1f}%;background:{color};border-radius:0 3px 3px 0"></span>'
        else:
            bar = f'<span class="bar" style="left:calc(50% - {w:.1f}%);width:{w:.1f}%;background:{color};border-radius:3px 0 0 3px"></span>'
        sign = "+" if dv > 0 else ""
        html += (f'<div class="brow"><span class="blab">{label}</span>'
                 f'<span class="btrack"><span class="bmid"></span>{bar}</span>'
                 f'<span class="bval" style="color:{color}">{sign}{dv}</span></div>')
    return html + "</div>"


def _heatmap(results, baseline):
    dims = _all_dims(results)
    basemap = {r["id"]: r for r in baseline} if baseline else {}
    head = "".join(f"<th>{DIM_LABEL.get(d, d)}</th>" for d in dims)
    body = ""
    for r in results:
        b = basemap.get(r["id"])
        flip = b and b["passed"] and not r["passed"]
        pin = '<span class="pin">必过</span> ' if r["pinned"] else ""
        note = ' <span class="flip">← 由过变没过</span>' if flip else ""
        body += f'<tr><td class="task">{pin}{r["id"]}{note}</td>'
        body += "".join(_cell(r["scores"].get(d)) for d in dims)
        body += "</tr>"
    return (f'<div class="sec">每道题 × 各方面</div>'
            f'<table class="hm"><thead><tr><th class="task">题目</th>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table>'
            f'<div class="legend">✓ 过　~ 部分　✗ 没过　· 没测这个方面</div>')


_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;color:#1b1b19;
 max-width:820px;margin:24px auto;padding:0 16px;background:#fff;line-height:1.6}
.head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.title{font-size:18px;font-weight:600}.meta{color:#6b6a66;font-size:13px;margin-bottom:18px}
.pill{padding:6px 14px;border-radius:999px;font-size:14px;font-weight:600;color:#fff}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:8px 0 20px}
.mc{background:#faf9f6;border:1px solid #ece9e0;border-radius:10px;padding:12px 14px}
.mcl{font-size:13px;color:#6b6a66}.mcv{font-size:24px;font-weight:600;margin:2px 0}.mcs{font-size:12px;color:#8f8d86}
.sec{font-size:14px;font-weight:600;margin:20px 0 8px}
.bars{background:#faf9f6;border:1px solid #ece9e0;border-radius:10px;padding:12px 16px}
.brow{display:flex;align-items:center;gap:10px;height:28px}
.blab{width:96px;text-align:right;font-size:13px;color:#4a4945;flex-shrink:0}
.btrack{flex:1;position:relative;height:16px}
.bmid{position:absolute;left:50%;top:-4px;height:24px;width:1px;background:#cfcdc4}
.bar{position:absolute;top:0;height:16px}
.bval{width:44px;text-align:right;font-size:13px;font-weight:600;flex-shrink:0}
table.hm{border-collapse:collapse;width:100%;font-size:13px}
.hm th,.hm td{border:1px solid #ece9e0;padding:6px 8px}
.hm th{background:#faf9f6;font-weight:500;color:#6b6a66;text-align:center}
.hm th.task,.hm td.task{text-align:left;white-space:nowrap}
.pin{background:#eef1fb;color:#3a55c8;font-size:11px;padding:1px 6px;border-radius:6px}
.flip{color:#d03b3b;font-size:12px}
.legend{color:#8f8d86;font-size:12px;margin-top:8px}
"""


def _meta_line(meta) -> str:
    """报告头上如实写清这场怎么跑的（别再硬编码一句和实际对不上的话）。"""
    if not meta:
        return "单次报告"
    return (f"模型 {meta.get('model', '?')} · 代码 {meta.get('commit', '?')}"
            f"{'（有未提交改动）' if meta.get('dirty') else ''} · 每题 {meta.get('n', '?')} 次"
            f" · 尺子指纹 {meta.get('scorer_fp', '?')}")


def render(results, verdict, baseline=None, title="评测报告", meta=None) -> str:
    color = _KIND_COLOR.get(verdict["kind"], "#6b6a66")
    reasons = "".join(f"<li>{r}</li>" for r in verdict["reasons"])
    reasons_html = f'<ul class="meta" style="margin-top:-8px">{reasons}</ul>' if reasons else ""
    body = _cards(results, baseline)
    if baseline is not None:
        body += _bars(results, baseline)
    body += _heatmap(results, baseline)
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        f'<div class="head"><div class="title">{title}</div>'
        f'<span class="pill" style="background:{color}">{verdict["label"]}</span></div>'
        f'<div class="meta">{_meta_line(meta)}</div>'
        f"{reasons_html}{body}</body></html>"
    )
