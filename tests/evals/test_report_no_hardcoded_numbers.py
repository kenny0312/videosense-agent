"""批次 1.5 R1 的棘轮:报告生成器【只排版不造数】。

为什么要有这条测试:`longhorizon_report.py` 生成的文档正在被当成立项证据引用。
它曾经把「48 次错误」写死在表头而实际是 54、把「35 个里 25 个(71%)」和整张
严格/宽松 gold 对照表写死成字符串 —— 数据变了文字不变,报告会自己跟自己打架,
而读的人无从分辨哪个数字是算出来的、哪个是上一版剩下的。

所以规则是:**报告里出现的每一个样本数、比率、金额、倍数,都必须来自 f-string 插值。**
写死的只允许是"名词"(标题、栏目名、解释性文字)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "evals" / "longhorizon_report.py"

# 只检查真正会印到报告里的行:w("...") / w(f"...")。
_W_CALL = re.compile(r"^\s*(?:w\(|\s+)([fr]?\"[^\"]*\"|[fr]?'[^']*')", re.M)

# 危险形态:字面量里出现"数字 + 计量单位/百分号/小数",且这一行不是 f-string。
_LITERAL_NUMBER = re.compile(
    r"(?<![\w.])\d+(?:\.\d+)?\s*(?:%|次|条|个|倍|美元|\$|人天|秒|s\b)"
    r"|\$\s*\d+(?:\.\d+)?"
    r"|(?<![\w.])0\.\d{2,}"          # 0.653 这类分数
)

# 白名单:这些是【配置常量的名字】或【规则说明】,不是从数据算出来的量,写死是对的。
_ALLOWED_SUBSTRINGS = (
    "长度≤2",          # 词干规则的说明
    "深度 2",           # 专有名词:depth-2
    "Phase 2",
    "批次 1.5",
)


def _literal_report_strings() -> list[str]:
    src = SRC.read_text(encoding="utf-8")
    out = []
    for line in src.splitlines():
        s = line.strip()
        if not (s.startswith('w("') or s.startswith("w('")
                or s.startswith('"') or s.startswith("'")):
            continue
        if s.startswith('w(f"') or s.startswith("w(f'") or s.startswith('f"') or s.startswith("f'"):
            continue                                  # f-string = 从数据插值,合规
        out.append(s)
    return out


def test_report_has_no_hardcoded_sample_counts_or_rates():
    """报告里的样本数/比率/金额/倍数必须是插值出来的,不许写死。"""
    bad = []
    for s in _literal_report_strings():
        if any(a in s for a in _ALLOWED_SUBSTRINGS):
            continue
        m = _LITERAL_NUMBER.search(s)
        if m:
            bad.append(f"{m.group(0)!r} in {s[:110]}")
    assert not bad, (
        "报告生成器里出现了写死的数字(样本数/比率/金额/倍数)。"
        "这些必须由数据算出来 —— 写死的数字在数据变了之后会和报告正文互相打架:\n  "
        + "\n  ".join(bad))


def test_report_does_not_recommend_the_retracted_advice():
    """「子 agent 失败退配额」是已撤回的错误建议(会让整棵树花掉两倍于配额上限的预算)。

    报告里可以【提到它并标注已撤回】(那是诚实记录),但不许把它当成【推荐】。
    判据:出现"退配额"的那一行必须同时带撤回标记。
    """
    src = SRC.read_text(encoding="utf-8")
    for i, line in enumerate(src.splitlines(), 1):
        if "退配额" in line:
            assert ("撤回" in line) or ("~~" in line), (
                f"{SRC.name}:{i} 提到了「退配额」却没有标注已撤回 —— "
                "这条建议是错的,不能再以推荐的形态出现")


def test_report_does_not_cite_moving_line_numbers_in_subagents():
    """不许引 `subagents.py:<行号>` —— 代码一搬行号就失效,而报告是给人看的证据。

    引用要用【函数名/分支名】这种搬家也还在的锚点。
    """
    src = SRC.read_text(encoding="utf-8")
    hits = re.findall(r"subagents\.py:\d+", src)
    assert not hits, (
        f"报告里引了会漂移的行号 {hits} —— 改用函数名锚点(如 `_run_one` 的无结论分支)")


@pytest.mark.parametrize("mod", ["evals.longhorizon_report", "evals.longhorizon_gold_audit"])
def test_modules_import_clean(mod):
    """两个模块都必须能纯离线 import(报告生成不得依赖 DB/网络)。"""
    __import__(mod)
