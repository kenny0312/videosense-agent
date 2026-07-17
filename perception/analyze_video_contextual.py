"""
方向一 · M1：上下文化、loop 感知的视频内容理解【通用原语】。

和 perception/skydive_extract.py 同构地调 Gemini 多模态(Part.from_uri 直读 GCS),但:
  ① prompt【动态】—— question + context + rubric + time_range 运行期拼装(不写死);
  ② 输出【最小信封】AnalyzeResult —— answer 自由文本 + enough/confidence/evidence_ts 薄控制位:
     既不把模型逼成"填表",又给 loop 一个【短钩子】判断"够不够回答"。

Gemini 调用经 `generate` 注入,**离线可单测**(测试传 fake,不连 GCP/网络)。任何失败【fail-open】
→ 返回 enough="no" 的结果(loop 能据此另作打算),绝不抛错卡住主循环。

设计见 docs/design/realtime-video-understanding.md。本文件是【纯库】,M2 再由 node_executor 接进 loop。
"""
from __future__ import annotations

import contextvars
import json
import os
from typing import Callable, Literal

from pydantic import BaseModel, field_validator

PERCEPTION_MODEL = os.environ.get("PERCEPTION_MODEL", "gemini-2.5-flash")   # 默认(快/省)
PRO_MODEL = os.environ.get("PERCEPTION_PRO_MODEL", "gemini-2.5-pro")        # Pro 模式(准/慢)
RETRY_LIMIT = 2
# fail-open 失败信封的 answer 前缀 —— 调用方(node_executor 缓存)据此【不缓存】失败,避免把
# 瞬时报错钉死成"答案"反复回放。
FAILURE_ANSWER_PREFIX = "(分析失败,无法看清这段视频)"

# 本请求级模型覆盖:orchestrator 据 pro_video 在 run_query 开头 set;_gemini_generate 读。
# run_query 全程同步同线程,深处的 analyze 也读得到;每请求开头都重设,跨请求不串。
MODEL_OVERRIDE: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "analyze_model_override", default=None)


# ── 最小信封 ────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    """对【某一段视频】要分析什么(video_id→gcs_uri 的解析是 M2/node_executor 的事)。"""
    question: str                                   # 任意子任务:"多精彩?" / "几个人?" / "在干嘛?"
    context: str | None = None                      # loop 注入:总目标/已知/为何分析/上一步发现
    rubric: str | None = None                       # 判断/评分细则(与用户对话敲定,见设计 §5)
    time_range: tuple[float, float] | None = None   # 关注区间(M1:prompt 软约束)


def _to_seconds(v) -> float | None:
    """把模型五花八门的时间戳(42 / "0:20" / "1:02:03" / ["0:20"])统一成秒;无法解析 → None。"""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s.lower() in ("null", "none"):
        return None
    try:
        if ":" in s:                                # H:MM:SS / M:SS
            sec = 0.0
            for p in s.split(":"):
                sec = sec * 60 + float(p)
            return sec
        return float(s)
    except Exception:
        return None


class AnalyzeResult(BaseModel):
    """最小信封。只有 answer 是硬要求;enough/confidence/evidence_ts 全【宽松容错】——
    模型给得不规范(枚举写错、置信度越界、时间戳是 "0:20"/数组)也不让整条结果失败:
    信封的意义就是稳,把"够不够回答"这个钩子和自由文本答案稳稳交给 loop。"""
    answer: str                                     # 自由文本;结论写在【最前】(preview 只露前 ~80 字)
    enough: Literal["yes", "partial", "no"] = "no"  # loop 的钩子:这段视频够不够回答 question
    confidence: float = 0.5                          # 0-1
    evidence_ts: float | None = None                # 最关键时刻(秒)→ 透传给 show_video 跳播

    @field_validator("enough", mode="before")
    @classmethod
    def _coerce_enough(cls, v):
        s = str(v).strip().lower()
        return s if s in ("yes", "partial", "no") else "no"   # 非法枚举 → 保守判 no

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_conf(cls, v):
        try:
            return max(0.0, min(1.0, float(v)))               # 越界 → 夹紧到 [0,1]
        except Exception:
            return 0.5

    @field_validator("evidence_ts", mode="before")
    @classmethod
    def _coerce_ts(cls, v):
        return _to_seconds(v)


# ── 动态 prompt 工厂 ────────────────────────────────────────
def build_prompt(req: AnalyzeRequest) -> str:
    parts = [
        "你是视频内容分析助手。看这段视频,回答下面的问题。",
        "把结论写在 answer 开头第一句,再用你在视频里【实际看到的】内容(画面/动作/时刻)展开,别脑补。",
        "按问题【本身】的要求作答 —— 要挑选就给结论、要描述就描述、要评分才评分;评判标准以问题/细则为准,"
        "别自作主张套固定格式或硬给「X/10」之类分数。",
    ]
    if req.context:
        parts.append(f"# 上下文\n{req.context}")
    parts.append(f"# 问题\n{req.question}")
    if req.rubric:
        parts.append(f"# 判断细则\n{req.rubric}")
    if req.time_range:
        t0, t1 = req.time_range
        parts.append(f"# 关注区间\n只关注 {t0:g}-{t1:g} 秒。")
    parts.append(
        "# 输出\n严格输出 JSON,字段:\n"
        "  answer: 结论在前的自由文本回答\n"
        '  enough: "yes" | "partial" | "no" —— 这段视频是否足以回答上面的问题\n'
        "  confidence: 0-1\n"
        "  evidence_ts: 支撑结论的最关键时刻【秒数,纯数字如 20】,没有就 null(不要用 0:20 格式、不要数组)\n"
        "信息不足以回答时,enough 给 partial/no,并在 answer 写清还差什么。"
    )
    return "\n\n".join(parts)


# ── 解析(复用 perception 的清理范式)──────────────────────────
def _parse(raw: str) -> AnalyzeResult:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return AnalyzeResult.model_validate(data)


# ── 真实 Gemini 调用(P1 起走 google-genai;惰性 import,离线测试不会走到这里)───────────
def _gemini_generate(gcs_uri: str, prompt: str, time_range=None) -> str:
    """P1:旧 vertexai SDK 已过官方移除期限,运行时路径迁 google-genai(共享 genai_client 单例,
    global 端点)。M4.5 的硬裁剪不再走 _raw_part proto hack —— genai 的 VideoMetadata 原生支持
    start/end offset(行为等价,真视频回归验过 token 数)。"""
    from google.genai import types
    from pipeline.agentops import usage
    from pipeline.genai_client import get_client
    name = MODEL_OVERRIDE.get() or PERCEPTION_MODEL   # 本请求选了 Pro 就用 pro,否则默认 flash
    low = gcs_uri.lower()                              # mime 跟扩展名走(上传的 .mov/.webm 别被当 mp4)
    mime = ("video/quicktime" if low.endswith(".mov")
            else "video/webm" if low.endswith(".webm") else "video/mp4")
    vm = None
    if time_range:                                    # M4.5:硬裁剪 —— Gemini 只处理 [起,止] 秒这一段
        start, end = float(time_range[0]), float(time_range[1])
        vm = types.VideoMetadata(start_offset=f"{start:g}s", end_offset=f"{end:g}s")   # 保留亚秒精度
    video = types.Part(file_data=types.FileData(file_uri=gcs_uri, mime_type=mime),
                       video_metadata=vm)
    resp = get_client().models.generate_content(
        model=name,
        contents=[video, prompt],
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=2048,
                                           response_mime_type="application/json"))
    usage.add_usage(resp, name)          # M4.1:视频分析 token 上报(genai 字段名相同)
    return resp.text


# ── 对外:看一段视频回答 question,返回最小信封 ──────────────────
def analyze(req: AnalyzeRequest, gcs_uri: str, *,
            generate: Callable[..., str] = _gemini_generate) -> AnalyzeResult:
    """看 gcs_uri 这段视频回答 req.question → 最小信封。失败 fail-open → enough='no'。
    generate(gcs_uri, prompt, time_range)->raw_json 可注入(离线单测传 fake)。"""
    prompt = build_prompt(req)
    last_err = ""
    for _ in range(RETRY_LIMIT + 1):
        try:
            return _parse(generate(gcs_uri, prompt, req.time_range))
        except Exception as e:
            last_err = str(e)
    return AnalyzeResult(answer=f"{FAILURE_ANSWER_PREFIX}[{last_err[:120]}]",
                         enough="no", confidence=0.0)
