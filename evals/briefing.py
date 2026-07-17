"""把一次评测跑的结果，导出成一份【专门给大模型看的病历】(markdown)。

用途：跑完点仪表盘上的"下载分析简报"，得到一个 .md，直接扔给 Claude/别的大模型，
让它分析"到底哪儿出问题了、该怎么修"。所以这份文档的写法是为大模型优化的——
开头交代清楚背景和它的任务，中间把每道失败题的全部信息结构化摆出来，结尾直接抛问题。

    python -m evals.briefing            # 给最近一次真跑导简报
    从 dashboard 下载按钮 / runner 跑完自动生成 evals/briefing.md
"""
from __future__ import annotations

import json
import os
import sys

from evals.report import DIM_LABEL

_HERE = os.path.dirname(os.path.abspath(__file__))


def _labels(keys) -> str:
    return "、".join(DIM_LABEL.get(k, k) for k in keys) or "—"


def _fail_dims(r) -> list:
    return [k for k, v in (r.get("scores") or {}).items() if v < 1.0]


def task_feedback(r: dict) -> str:
    """GD-0:一道题的【文本反馈】(GEPA 反思器的"梯度")。输入 = report.results.jsonl 的
    一条记录,输出 = 这道题的完整病历(栽在哪把尺子/用户问/agent答/期望/金标依据/工具轨迹/
    各尺子分)。build_briefing 的失败区就是逐题调它 —— 单一来源,两处共用。"""
    ff = r.get("first_fail") or {}
    ans = ff.get("answer") or r.get("answer") or "(无)"
    tools = ff.get("tools") or r.get("tools") or []
    tool_str = " → ".join(
        f"{t.get('tool')}({t.get('args', '')})"
        + (f" ⇒ {t['out']}" if t.get("out") else "")
        for t in tools[:10]) or "(没调工具)"
    pin = "🔒必过题 " if r.get("pinned") else ""
    L = [f"### {pin}`{r['id']}` — 栽在：{_labels(_fail_dims(r))}", ""]
    L.append(f"- **用户问**：{r.get('question', '(无)')}")
    L.append(f"- **agent 答**：{ans}")
    L.append(f"- **期望（判分标准）**：`{json.dumps(r.get('expect', {}), ensure_ascii=False)}`")
    if r.get("grounding_note"):
        L.append(f"- **金标依据（正确答案为什么是这个）**：{r['grounding_note']}")
    L.append(f"- **agent 调的工具**：{tool_str}")
    L.append(f"- **各尺子得分**：`{json.dumps(r.get('scores', {}), ensure_ascii=False)}`")
    return "\n".join(L)


def build_briefing(run: dict) -> str:
    """run = dashboard 归档的一次运行记录（含 results 明细）。返回 markdown 字符串。"""
    meta = run.get("meta") or {}
    # 计分口径和 runner 一致：环境故障不算，代码崩溃算没过
    scored = [r for r in run.get("results", []) if r.get("status", "ok") != "infra_error"]
    fails = [r for r in scored if not r.get("passed")]
    infra = [r for r in run.get("results", []) if r.get("status") == "infra_error"]
    passed = sum(1 for r in scored if r.get("passed"))

    L = []
    L.append("# VideoSense 评测失败分析简报")
    L.append("")
    L.append("## 给分析者（大模型）的说明")
    L.append("")
    L.append("你正在读一份 AI agent 的自动评测结果。请帮我分析**哪里出了问题、怎么修**。")
    L.append("")
    L.append("- **被测系统**：VideoSense（VS），一个多轮、多模态（视频）理解 agent，大脑是 Gemini。"
             "它能查视频库、看画面内容、做检索/计数/时间定位、播放视频、记用户偏好。")
    L.append("- **评测怎么判分**：每道题声明它用哪几把「尺子」，全部达标才算「过」。尺子都是确定性程序"
             "（不是另一个 AI 打分），含义见每条失败里的「栽在哪把尺子」。")
    L.append("- **测试环境**：跑在一个隔离的假视频库上（16 个视频，不碰生产数据），所以金标是确定的、可核对的。")
    L.append("")
    L.append("**你的任务**：逐条看下面「没过的题」，对每一条判断它属于哪一类，并给出具体建议——")
    L.append("")
    L.append("1. **agent 真缺陷**：模型行为确实错了（该改 agent 的提示词 / 检索逻辑 / 代码）。")
    L.append("2. **评测自己的问题**：金标定错了、或判分尺子太严把对的答案冤枉了（该改评测）。")
    L.append("3. **环境故障**：本地服务没起之类，与 agent 能力无关（忽略即可）。")
    L.append("")
    L.append("判断依据主要看：**agent 答的**对不对得上**期望**和**金标依据**。答案合理但被判挂 → 多半是评测的问题。")
    L.append("")

    L.append("## 本次概况")
    L.append("")
    L.append(f"- 时间：{run.get('ts', '?')} ｜ 模式：{run.get('mode', '?')}")
    L.append(f"- 大脑模型：{meta.get('model', '?')} ｜ 代码版本：{meta.get('commit', '?')}"
             f"{'（有未提交改动）' if meta.get('dirty') else ''} ｜ 每题跑 {meta.get('n', '?')} 次")
    L.append(f"- 通过率：**{passed}/{len(scored)}**（{round(100 * passed / max(len(scored), 1))}%）"
             f" ｜ 结论：{run.get('verdict_label', '?')}")
    L.append(f"- 必过题：{run.get('pinned_total', 0) - run.get('pinned_failed', 0)}/{run.get('pinned_total', 0)}"
             f" 过 ｜ 环境故障（不计分）：{len(infra)} 题")
    j = meta.get("judge") or {}
    if j:
        L.append(f"- AI 裁判参考分（**不进门禁**，对表 {j.get('cert', '?')}）：开放式判据做到 "
                 f"{j.get('ok', 0)}/{j.get('total', 0)}（裁判 {j.get('model', '?')}）")
    radar = meta.get("radar") or {}
    if radar.get("high") or radar.get("low"):
        L.append(f"- 🔍 **AI 裁判冤案雷达**（只判失败题，找'程序挂但裁判说该过'的）：疑似冤案 "
                 f"**{len(radar.get('high', []))} 高置信** + {len(radar.get('low', []))} 低置信"
                 f"（裁判 {radar.get('model', '?')}，仅报警不改判，请人工复核）")
        if radar.get("high"):
            L.append(f"  - 高置信疑似冤案（栽在语义尺子，最该先查）：{'、'.join('`'+x+'`' for x in radar['high'])}")
    for why in run.get("reasons", [])[:8]:
        L.append(f"- {why}")
    # 集中度报警：失败大量堆在同一把尺子上，通常是尺子/题目模板的问题，不是 agent 忽然变笨
    dim_fails: dict = {}
    for r in fails:
        for d in _fail_dims(r):
            dim_fails[d] = dim_fails.get(d, 0) + 1
    if fails and dim_fails:
        top_dim, top_n = max(dim_fails.items(), key=lambda x: x[1])
        if top_n >= 4 and top_n / len(fails) >= 0.4:
            L.append(f"- ⚠ **失败集中报警**：{top_n}/{len(fails)} 道失败都栽在「{DIM_LABEL.get(top_dim, top_dim)}」上——"
                     f"这种集中度通常意味着判分器或题目模板出了问题（冤案），请先重点排查评测侧，再下 agent 变差的结论。")
    L.append("")

    L.append("## 各方面得分")
    L.append("")
    L.append("| 方面 | 得分 |")
    L.append("|---|---|")
    for d, v in sorted((run.get("per_dim") or {}).items(), key=lambda x: x[1]):
        L.append(f"| {DIM_LABEL.get(d, d)} | {round(v * 100)}% |")
    L.append("")

    # 空白答案（Gemini 安全拦截 bug）单独拎出——这类失败是产品 bug 拖挂，不是能力问题
    blank_fails = [r for r in fails if _has_blank(r)]
    if blank_fails:
        L.append(f"> ⚠ 其中 {len(blank_fails)} 道是 **agent 返回了空白答案**（疑似 Gemini 安全拦截 bug，"
                 f"已登记的产品缺陷）拖挂的，不是能力问题——分析时请把它们和真能力失败分开看："
                 f"{'、'.join('`' + r['id'] + '`' for r in blank_fails)}")
        L.append("")

    L.append(f"## 没过的题（{len(fails)} 道，逐条——这是分析重点）")
    L.append("")
    if not fails:
        L.append("*本次没有失败题。*")
    for r in fails:
        L.append(task_feedback(r))
        L.append("")

    if infra:
        L.append("## 环境故障（不计分，通常可忽略）")
        L.append("")
        for r in infra:
            L.append(f"- `{r['id']}`：{(r.get('answer') or '')[:120]}")
        L.append("")

    L.append("## 请回答")
    L.append("")
    L.append("1. 上面每一道没过的题，分别属于哪一类（agent 真缺陷 / 评测的问题 / 环境故障）？为什么？")
    L.append("2. 属于 agent 真缺陷的，最可能的根因是什么？具体怎么修（改提示词？改检索？改代码？）？")
    L.append("3. 如果有「答案其实合理却被判挂」的，指出来——那是评测该改的地方。")
    L.append("4. 按优先级给出一份修复清单。")
    return "\n".join(L)


def write_briefing(run: dict, path: str | None = None) -> str:
    path = path or os.path.join(_HERE, "briefing.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_briefing(run))
    return path


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    from evals.dashboard import load_runs

    runs = load_runs()
    lives = [r for r in runs if r["mode"] == "live"]
    run = lives[-1] if lives else (runs[-1] if runs else None)
    if not run:
        print("还没有任何运行记录 —— 先跑一次 python -m evals.runner。")
        return 1
    path = write_briefing(run)
    print(f"分析简报已生成：{path}")
    print("把这个 .md 扔给 Claude/大模型，让它分析哪里出了问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
