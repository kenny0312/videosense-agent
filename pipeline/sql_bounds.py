"""B1 有界读取引擎 —— 真 PG(psycopg2)与 mock(内存 SQLite)共用同一套上界语义。

为什么单独一个模块,而不是塞进 server.py:
任务书 §5-B1 要求 `mcp_server/server.py` 与 `repl/_mock_db.py`【同步语义】。
"同步"最可靠的实现是同一份代码,但 `repl/_mock_db.py` 不能 import `mcp_server.server`
—— 那会把整个 MCP SDK 拖进每一个只想用假库的测试,还会在 import 时执行
`logging.basicConfig()`(全局副作用,会改掉整个测试进程的日志配置)。
所以把纯逻辑抽到这里:只依赖 `json` + `pipeline.config`,零副作用,两边各 import 一次。
放在 `pipeline/` 而不是 `mcp_server/`,是因为 `mcp_server` 没有 `__init__.py`
(靠 namespace package + `sys.path.insert` 才 import 得到),而 `pipeline` 两边本来
就都在 import(`from pipeline import config`)—— 沿用已经成立的路径,不新增风险。
命名跟着已有的 sql_guard / sql_validate / sql_fixer 一族走。

三条不变量(改这个文件前先读):
  1. 上界(行/字节/超时)是【安全项】,恒生效,不由 USE_BOUNDED_SQL 控制;
     开关只决定"要不要把截断这件事报出去"。因此本模块【不读开关】——
     开关只出现在调用方决定 wire 形状的那一处。
  2. 同一条 SQL 在开关开/关时必须返回【完全相同的行集】。所以字节预算里
     恒定扣掉信封开销(哪怕这次不发信封),否则开关就偷偷变成了安全项。
  3. 没截断时,调用方拿到的 rows 必须与"裸 fetchall + [dict(r) ...]"逐字节等价。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, NamedTuple

from pipeline import config


class BoundedResult(NamedTuple):
    """一次有界读取的全部结果。meta / wire 都从这里派生,别在别处再算一遍。"""
    rows: list[dict]
    columns: list[str]
    truncated: bool
    total_seen: int          # 服务端实际【扫到】的行数(截断时至少是 cap+1)
    returned: int            # 真正返回的行数 == len(rows)
    reason: str | None       # "row_cap" | "byte_cap" | "single_row_too_large"


# 信封开销:`{"rows": [...], "columns": [...], "truncated": true, ...}` 里除 rows 之外的部分。
# 用一个偏大的代表性骨架量一次,再留 128B 余量给长列名/大数字 —— 宁可少收一行,
# 不可让最终 JSON 超过 SQL_MAX_BYTES。
_ENVELOPE_OVERHEAD = len(json.dumps(
    {"rows": [], "columns": [], "truncated": True,
     "total_seen": 0, "returned": 0, "reason": "single_row_too_large"},
    ensure_ascii=False).encode("utf-8")) + 128

# JSON 数组里两行之间的分隔符 `, `(json.dumps 默认 separators,indent=None 时是 ', ')
_JOINER_BYTES = 2
# 数组自身的 `[` `]`
_BRACKET_BYTES = 2


def _row_bytes(row: dict) -> int:
    """这一行【作为最终 JSON 的一部分】占多少 UTF-8 字节。

    注意不是 `len(str(row))` —— 那是 Python repr 的长度,对 CJK 按【字符】计
    (一个汉字 1,实际 UTF-8 占 3),对 None/True 之类还会写成 Python 字面量。
    这里用与 server.py 出口完全一致的 dumps 参数,量出来的就是真实上线字节。
    """
    return len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))


def columns_of(cur: Any) -> list[str]:
    """列名走 `cursor.description`,不从行 dict 反推。

    为什么重要:从行反推的话,零行结果 == 零列,大脑分不清
    "这张表没有匹配的行" 和 "我不知道这张表有哪些列"。前者该收工,
    后者该去 get_schema —— 判断反了就是一次白烧的工具调用。
    DB-API 保证 execute 之后 description 就绪,即使结果集为空。
    """
    desc = getattr(cur, "description", None) or ()
    return [d[0] for d in desc]


def fetch_bounded(
    cur: Any,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
    batch: int | None = None,
) -> BoundedResult:
    """从一个已 execute 的 DB-API cursor 有界地读取。

    行为(与任务书 §5-B1 逐条对应):
      · `fetchmany(batch)` 分批,绝不 `fetchall()`;
      · 最多保存 max_rows 行,读到第 max_rows+1 行【只用于确认截断,绝不进 rows】;
      · 按最终 JSON 的 UTF-8 字节累计,超 max_bytes 停;
      · 第一行就超限 → rows=[] + reason="single_row_too_large"(不返回半行:
        半行 JSON 既解析不了,截一半的 dict 又会让大脑以为那就是全部字段)。
    截断后【不把游标读干】—— 读干就等于没有上界,内存该顶穿还是顶穿。
    """
    max_rows = config.SQL_MAX_ROWS if max_rows is None else max_rows
    max_bytes = config.SQL_MAX_BYTES if max_bytes is None else max_bytes
    batch = config.SQL_FETCH_BATCH if batch is None else batch

    columns = columns_of(cur)
    # 不变量 2:信封开销恒定扣除,让行集与开关状态无关。
    budget = max_bytes - _ENVELOPE_OVERHEAD
    used = _BRACKET_BYTES

    rows: list[dict] = []
    total_seen = 0
    truncated = False
    reason: str | None = None

    while not truncated:
        chunk: Iterable[Any] = cur.fetchmany(batch)
        if not chunk:
            break
        for raw in chunk:
            total_seen += 1
            if len(rows) >= max_rows:
                # 这一行是"第 cap+1 行":只用来证明后面还有,绝不进 rows。
                truncated, reason = True, "row_cap"
                break
            row = dict(raw)
            cost = _row_bytes(row) + (_JOINER_BYTES if rows else 0)
            if used + cost > budget:
                truncated = True
                if not rows:
                    reason = "single_row_too_large"
                else:
                    reason = "byte_cap"
                break
            rows.append(row)
            used += cost

    return BoundedResult(
        rows=rows,
        columns=columns,
        truncated=truncated,
        total_seen=total_seen,
        returned=len(rows),
        reason=reason,
    )


# ── wire / meta 的形状(server.py 与 mcp_client.py 的唯一真源)────────────────

def needs_envelope(res: BoundedResult, *, report: bool) -> bool:
    """要不要发信封(而不是今天那个裸 JSON 数组)。

    只有两种情况裸数组说不出话:
      · 截断了 —— 数组里没地方写"其实还有";
      · 零行 —— 数组里没地方写列名(见 columns_of 的注释)。
    其余情况一律裸数组:非空未截断时列名从行 key 就能看出来,没必要动 wire。
    `report=False`(USE_BOUNDED_SQL 关)时永远裸数组 —— 与今天逐字节等价。
    """
    return report and (res.truncated or not res.rows)


def build_envelope(res: BoundedResult) -> dict:
    """截断/零行时的 wire 形状。截断四键【只在真截断时出现】。"""
    env: dict = {"rows": res.rows, "columns": res.columns}
    if res.truncated:
        env["truncated"] = True
        env["total_seen"] = res.total_seen
        env["returned"] = res.returned
        env["reason"] = res.reason
    return env


def fill_meta(meta: dict | None, res: BoundedResult, *, report: bool) -> None:
    """把截断信息填进调用方传来的 out 参数。

    契约:【没截断就不碰截断四键】。上游用 `meta.get("truncated")` 判断,
    所以这四个键的存在本身就是信号,不能有"truncated=False"这种半吊子填法。
    columns 是另一回事(不是截断信号),report 开着且服务端报了列名时就填。
    """
    if meta is None or not report:
        return
    if res.truncated:
        meta["truncated"] = True
        meta["total_seen"] = res.total_seen
        meta["returned"] = res.returned
        meta["reason"] = res.reason
    if res.truncated or not res.rows:
        meta["columns"] = res.columns
