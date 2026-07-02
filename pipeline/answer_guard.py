"""收口答案的确定性守卫(机械规则下沉;设计 prompt-constitution-lessons.md §5)。

第一刀:id 清洗器 —— 模型答案里的裸视频 id(prompt 教训 L01 管"第一时间写对",
这里管"写错也出不了门"):
  · 能对应到【最近一次 show 结果】items 的 → 替换成「第 N 个」(前端就按这个编号);
  · 对应不到的 → 删除(宁可少说,不可泄漏;评审定夺 §8-②);
  · 命中数进 loop_metrics(id_scrub_hits)—— 长期为 0 = 模型已自觉,L01 可退役(闭环)。

对抗 review 加固(2026-07-02):
  · \\b 在中日韩文贴邻处失效(CJK 也是 \\w)→ 换 ASCII 显式 lookaround,「视频803…」也抓得到;
    v_ 段的 [-\\w] 同理会吞中文 → 收紧为 ASCII 类;
  · 残渣清理只作用于【清洗点】(哨兵法),不再全文扫括号 —— 答案里合法的 ''/() 不受伤;
  · 全程 fail-open:守卫自身任何异常 → 原文返回(绝不让兜底把已有答案搞崩)。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# 与探针同形状,但边界用 ASCII lookaround(\b 对 CJK 贴邻失效):
#   长串数字 id / GX 相机文件名(可带 _n 后缀)/ ActivityNet v_ 串 / 上传 up_ 串
_CORE = (r"\d{6,}_\d{4,}_\d{8,}"
         r"|GX\d{6}(?:_\d+)?"
         r"|v_[0-9A-Za-z_-]{9,}"
         r"|up_[0-9a-f]{16,}")
ID_PAT = re.compile(rf"(?<![0-9A-Za-z_-])(?:{_CORE})(?![0-9A-Za-z_-])")

_SENTINEL = "\x00"
_WRAPPED_SENTINEL = re.compile(r"[(（\[【`'\"]\s*\x00\s*[)）\]】`'\"]")   # 只清"包着哨兵"的空壳
_LOOSE_SENTINEL = re.compile(r"\x00 ?")                                  # 哨兵本体(至多吃一个尾随空格,保留前导空格防粘词)


def _latest_show_map(ledger_values: Iterable[Any]) -> dict[str, int]:
    """按执行顺序扫 ledger,取【最近一次】带 items 的 show 结果 → {video_id: n}。
    id 键兼容 video_id / id 两种形状;坏行跳过(fail-open)。"""
    idmap: dict[str, int] = {}
    for v in ledger_values:
        items = v.get("items") if isinstance(v, dict) else None
        if not isinstance(items, list):
            continue
        m: dict[str, int] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            vid = it.get("video_id") or it.get("id")
            try:
                n = int(it.get("n"))
            except (TypeError, ValueError):
                continue
            if vid:
                m[str(vid)] = n
        if m:
            idmap = m                       # 后者覆盖前者 = 最近一次生效(与「第 N 个」语义一致)
    return idmap


def scrub_ids(answer: str, ledger_values: Iterable[Any] = ()) -> tuple[str, int]:
    """清洗答案文本里的裸 id。返回 (清洗后文本, 命中数);无命中原样返回(零开销路径)。
    守卫自身异常 → (原文, 0):兜底绝不反噬答案。"""
    if not answer:
        return answer, 0
    try:
        idmap = _latest_show_map(ledger_values)
        hits = 0

        def _rep(m: re.Match) -> str:
            nonlocal hits
            hits += 1
            n = idmap.get(m.group(0))
            if n is None:
                return _SENTINEL            # 映射不到 → 删除(绝不泄漏)
            prefix = m.string[max(0, m.start() - 12):m.start()]
            if f"第 {n} 个" in prefix or f"第{n}个" in prefix:
                return _SENTINEL            # 前文刚用「第N个」指认过 → 别重复,删残留
            return f"第 {n} 个"

        out = ID_PAT.sub(_rep, answer)
        if hits:                            # 残渣清理只围着哨兵做,不碰答案其它部分
            prev = None
            while prev != out:              # 逐层坍缩嵌套包壳(每层替回哨兵);合法括号无哨兵→永不匹配
                prev = out
                out = _WRAPPED_SENTINEL.sub(_SENTINEL, out)
            out = _LOOSE_SENTINEL.sub("", out)
        return out, hits
    except Exception:
        return answer, 0
