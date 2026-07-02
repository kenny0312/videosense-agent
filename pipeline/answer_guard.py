"""收口答案的确定性守卫(机械规则下沉;设计 prompt-constitution-lessons.md §5)。

第一刀:id 清洗器 —— 模型答案里的裸视频 id(prompt 教训 L01 管"第一时间写对",
这里管"写错也出不了门"):
  · 能对应到【最近一次 show 结果】items 的 → 替换成「第 N 个」(前端就按这个编号);
  · 对应不到的 → 删除(宁可少说,不可泄漏;评审定夺 §8-②);
  · 命中数进 loop_metrics(id_scrub_hits)—— 长期为 0 = 模型已自觉,L01 可退役(闭环)。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# 与探针同一套形状:长串数字 id / GX 相机文件名(可带 _n 后缀)/ ActivityNet v_ 串 / 上传 up_ 串
ID_PAT = re.compile(
    r"\b\d{6,}_\d{4,}_\d{8,}\b|\bGX\d{6}(?:_\d+)?\b|\bv_[-\w]{9,}\b|\bup_[0-9a-f]{16,}\b")

_EMPTY_WRAP = re.compile(r"[(（\[【`'\"]\s*[)）\]】`'\"]")   # 清掉删除后留下的空括号/空反引号
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def _latest_show_map(ledger_values: Iterable[Any]) -> dict[str, int]:
    """按执行顺序扫 ledger,取【最近一次】带 items 的 show 结果 → {video_id: n}。"""
    idmap: dict[str, int] = {}
    for v in ledger_values:
        items = v.get("items") if isinstance(v, dict) else None
        if not isinstance(items, list):
            continue
        m = {str(it["video_id"]): int(it["n"]) for it in items
             if isinstance(it, dict) and it.get("video_id") and it.get("n")}
        if m:
            idmap = m                       # 后者覆盖前者 = 最近一次生效(与「第 N 个」语义一致)
    return idmap


def scrub_ids(answer: str, ledger_values: Iterable[Any] = ()) -> tuple[str, int]:
    """清洗答案文本里的裸 id。返回 (清洗后文本, 命中数);无命中原样返回(零开销路径)。"""
    if not answer:
        return answer, 0
    idmap = _latest_show_map(ledger_values)
    hits = 0

    def _rep(m: re.Match) -> str:
        nonlocal hits
        hits += 1
        n = idmap.get(m.group(0))
        if n is None:
            return ""                       # 映射不到 → 删除(绝不泄漏)
        prefix = m.string[max(0, m.start() - 12):m.start()]
        if f"第 {n} 个" in prefix or f"第{n}个" in prefix:
            return ""                       # 前文刚用「第N个」指认过 → 别重复,删残留
        return f"第 {n} 个"

    out = ID_PAT.sub(_rep, answer)
    if hits:                                # 只有真清洗过才收拾残渣
        out = _EMPTY_WRAP.sub("", out)
        out = _MULTI_SPACE.sub(" ", out)
    return out, hits
