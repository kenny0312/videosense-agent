"""
方向一 M1 单测 —— 纯离线,mock Gemini(注入 generate),不连 GCP/DB/网络。
    python -m perception.test_analyze_contextual

覆盖:动态 prompt 含各段 + "结论前置"指令、缺省可选段省略;yes/partial/no 解析;
     ```json wrapper 剥离;evidence_ts 可选;失败/坏 JSON/非法枚举 → fail-open enough="no"。
"""
from __future__ import annotations

import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from perception.analyze_video_contextual import (
    AnalyzeRequest, AnalyzeResult, analyze, build_prompt,
)


def _gen(payload):
    """fake generate:无视入参,吐固定文本(dict→JSON 或直接 str)。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return lambda gcs_uri, prompt: text


# ── 动态 prompt ────────────────────────────────────────────
def test_prompt_has_all_sections_and_frontload_rule():
    p = build_prompt(AnalyzeRequest(question="几个人?", context="挑选用",
                                    rubric="清晰度优先", time_range=(3.0, 9.5)))
    assert "几个人?" in p and "挑选用" in p and "清晰度优先" in p
    assert "3-9.5" in p                       # time_range 软约束
    assert "开头" in p                        # 结论前置指令
    assert "enough" in p and "evidence_ts" in p


def test_prompt_omits_absent_optionals():
    p = build_prompt(AnalyzeRequest(question="在干嘛?"))
    assert "在干嘛?" in p
    assert "# 上下文" not in p and "# 判断细则" not in p and "# 关注区间" not in p


# ── yes / partial / no 解析 ─────────────────────────────────
def test_yes_path():
    r = analyze(AnalyzeRequest(question="多精彩?"), "gs://b/v.mp4",
                generate=_gen({"answer": "8/10 近地穿越", "enough": "yes",
                               "confidence": 0.8, "evidence_ts": 42.0}))
    assert isinstance(r, AnalyzeResult)
    assert r.enough == "yes" and r.confidence == 0.8 and r.evidence_ts == 42.0
    assert r.answer.startswith("8/10")


def test_partial_path_evidence_optional():
    r = analyze(AnalyzeRequest(question="开伞了吗?"), "gs://b/v.mp4",
                generate=_gen({"answer": "画面模糊,需看 0:30-0:50", "enough": "partial",
                               "confidence": 0.3}))
    assert r.enough == "partial" and r.evidence_ts is None


def test_no_path():
    r = analyze(AnalyzeRequest(question="有狗吗?"), "gs://b/v.mp4",
                generate=_gen({"answer": "视频里没有狗", "enough": "no", "confidence": 0.2}))
    assert r.enough == "no"


# ── 解析鲁棒性 / fail-open ─────────────────────────────────
def test_strips_json_code_fence():
    wrapped = '```json\n{"answer":"a","enough":"yes","confidence":0.5}\n```'
    r = analyze(AnalyzeRequest(question="x"), "gs://b/v.mp4", generate=_gen(wrapped))
    assert r.enough == "yes" and r.answer == "a"


def test_fail_open_on_bad_json():
    r = analyze(AnalyzeRequest(question="x"), "gs://b/v.mp4", generate=_gen("not json at all"))
    assert r.enough == "no" and r.confidence == 0.0          # fail-open,不抛


def test_fail_open_on_generate_raises():
    def boom(gcs_uri, prompt):
        raise RuntimeError("API down")
    r = analyze(AnalyzeRequest(question="x"), "gs://b/v.mp4", generate=boom)
    assert r.enough == "no" and "API down" in r.answer


def test_invalid_enough_coerced_to_no():
    # 非法枚举【不】让整条失败:enough 容错为 no,answer 保留(不是 fail-open 失败文案)
    r = analyze(AnalyzeRequest(question="x"), "gs://b/v.mp4",
                generate=_gen({"answer": "a", "enough": "maybe", "confidence": 0.5}))
    assert r.enough == "no" and r.answer == "a"


def test_out_of_range_confidence_clamped():
    r = analyze(AnalyzeRequest(question="x"), "gs://b/v.mp4",
                generate=_gen({"answer": "a", "enough": "yes", "confidence": 9}))
    assert r.enough == "yes" and r.answer == "a" and r.confidence == 1.0   # 夹紧,不失败


def test_evidence_ts_coercion():
    # 真模型常给 "M:SS" / 数组 / 乱串 —— 统一成秒,无法解析 → None,且【不】拖垮整条结果
    def ts(payload):
        return analyze(AnalyzeRequest(question="x"), "gs://b/v.mp4",
                       generate=_gen({"answer": "a", "enough": "yes", "evidence_ts": payload})).evidence_ts
    assert ts("0:20") == 20.0
    assert ts("1:02:03") == 3723.0
    assert ts(["0:20"]) == 20.0
    assert ts(42) == 42.0
    assert ts("不知道") is None
    assert ts(None) is None


def test_missing_answer_fails_open():
    # answer 是唯一硬要求:缺了就 fail-open
    r = analyze(AnalyzeRequest(question="x"), "gs://b/v.mp4",
                generate=_gen({"enough": "yes", "confidence": 0.9}))
    assert r.enough == "no"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
