"""C1 库存快照 LibraryStateV1 —— 请求开局就让大脑知道【库里到底有什么】。

## 为什么要这一节

大脑今天开局拿到的是 schema(有哪些表哪些列)+ 受控词表(有哪些大类),
唯独不知道【存量】:库里共多少视频、每个大类各多少、哪张表是空壳、
专栏表里的视频是不是也能从 video_facts 查到。

缺这一节的代价有实证:跳伞视频只落 `skydive_segments`、`video_facts` 里没有,
而"库里有跳伞视频吗"走的是 `video_facts.predicate` —— 于是被答成"没有"
(事故形状记在 `perception/skydive_schema.py:141-143` 的桥接注释里)。当时的修法是
【补数据回填】,不是补摘要,所以同形状的下一个专栏表还会再犯一次。
`vertical_tables` 就是把那次事故做成【每次开局都看得见的一行】。

字段名【刻意不叫 orphan_tables】(设计稿里的旧名):它列的是【每一张非空的专栏表】,
连 312 行全都能从 video_facts 查到的那种也在里面。名字叫 orphan、内容却含非 orphan,
而这个字段名是【大脑直接读到的标签】—— 一张覆盖完好的表顶着"孤儿"的名字进 prompt,
等于系统亲口告诉它这里有缺口。缺口读的是 rows 与 also_in_video_facts 那两个数,
不是读表名。全都列出来是【故意的】:skydive 事故的第一层是大脑压根不知道
`skydive_segments` 这张表存在,只列有缺口的那些就把这层信息又藏回去了。

## 三条不变量(改本文件前先读)

1. **绝不进 `loop_driver._LOOP_SYSTEM`。** 那是 import 期冻结的字节稳定缓存前缀;
   快照每个请求都可能不同,塞进去 = 每一轮都把隐式缓存打碎成 cache miss。
   注入走 `_loop_system()` 运行期拼接那一层(`tests/pipeline/test_library_state.py`
   有一条测试专门钉死这个)。
2. **fail-open,整段省略。** 快照是"锦上添花"的定向信息,拿不到绝不能拖垮请求。
   任何异常 → 返回 None/"" → prompt 里干脆没有这一节。
   失败结果【也进 TTL 缓存】:否则库一挂,每个请求都要白等一次 MCP 超时
   (`config.MCP_CALL_TIMEOUT_S` ≥ 15s),快照本身就成了故障放大器。
3. **一条 SQL。** 查询限定在 `config.BUSINESS_TABLES`,单条 UNION ALL 聚合;
   每个请求都全表统计一次不合算 → 30-60s 进程内 TTL 缓存兜住。

## 为什么 notes 里那三句必须在

快照是【近似值 + 快照时刻】,而大脑天然把注入的数字当权威真值去回答计数题。
三句话各挡一种误用:大类计数可能一视频多类(不能相加当总数)、字段缺席是"未知"
不是"零"(C2「未知≠空」在数据面的镜像)、要精确实时值请去 sql_query。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from pipeline import config
from pipeline.taxonomy_seed import CATEGORIES

log = logging.getLogger("pipeline.library_state")

SCHEMA_VERSION = "vs.library-state/v1"

# 30-60s 之间取中:一次 ingest 不会在一分钟内被反复追问存量,而连发的多轮对话
# (最常见的形态)全部命中缓存,只在第一轮付一次查询钱。
TTL_SECONDS = 45.0

# 截断纪律:只列前 N 个大类,其余合进 categories_omitted。26 个全列 ≈ 900 字符,
# 而尾巴上那些个位数的大类对"库里有什么"几乎不提供定向价值。
MAX_CATEGORIES_SHOWN = 12

# 核心表 vs 专栏表:BUSINESS_TABLES 减去这四张核心表 = 专栏(垂直)表。
# 这样写而不是硬编码 ("skydive_segments",) —— 下一个专栏表进白名单时
# 自动被覆盖检查扫到,不必有人记得回来改这里(skydive 事故正是"没人记得"造成的)。
CORE_TABLES = ("video_metadata", "video_discovery", "video_facts", "video_fact_instances")

NOTES = [
    "分类计数可能重叠,不能相加当作视频总数。",
    "未返回的字段表示未知,不表示零。",
    "这是请求开始时的快照;精确实时值请用 sql_query。",
]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CACHE_LOCK = threading.Lock()
_CACHE: "dict[str, object]" = {"at": 0.0, "value": None, "filled": False}


# ── SQL(单条)────────────────────────────────────────────────────────────

def _lit(s: str) -> str:
    """字符串字面量。词表与表名都是仓内代码常量(taxonomy_seed / config),
    不存在用户输入注入面;转义仍照做,免得日后有人把 env 接进来。"""
    return "'" + str(s).replace("'", "''") + "'"


def vertical_tables() -> "list[str]":
    """专栏表 = 白名单里除核心四表以外的表(约定:都带 video_id 列)。"""
    return [t for t in config.BUSINESS_TABLES
            if t not in CORE_TABLES and _IDENT.match(str(t))]


def build_sql() -> str:
    """一条 SQL 取全部计数(UNION ALL 出 (k, name, n) 三元组)。

    刻意【不】拆成 N 条:N 条就是 N 次 MCP 往返 + N 次事务,而且几条之间的
    时刻不一致(视频总数和分类计数来自不同瞬间)会让 notes 里"这是快照"那句
    从近似变成谎话。
    """
    cats = ", ".join(_lit(c) for c in CATEGORIES)
    parts = [
        # 大类分布:只 GROUP BY 受控词表内的 predicate。不加 IN 过滤的话这里会
        # 冒出 ~200 个自由细谓词,行数和 token 都失控,而"存量"要的是大类轴。
        f"SELECT 'cat' AS k, vf.predicate AS name, COUNT(DISTINCT vf.video_id) AS n\n"
        f"  FROM video_facts vf WHERE vf.predicate IN ({cats}) GROUP BY vf.predicate",
        "SELECT 'meta', 'videos', COUNT(*) FROM video_metadata",
        "SELECT 'meta', 'title', COUNT(title) FROM video_metadata",
        "SELECT 'meta', 'gcs_uri', COUNT(gcs_uri) FROM video_metadata",
        # 有至少一行 video_facts 的视频数 → 差额就是 unindexed_videos(检索不到的那批)
        "SELECT 'meta', 'facts', COUNT(*) FROM video_metadata vm "
        "WHERE EXISTS (SELECT 1 FROM video_facts f WHERE f.video_id = vm.video_id)",
    ]
    for t in vertical_tables():
        parts.append(f"SELECT 'vert', {_lit(t)}, COUNT(*) FROM {t}")
        # 事故探针:专栏表里有多少行的 video_id 在 video_facts 里【也找得到】。
        # rows > also_in_video_facts 就是"只在专栏表里、常规查询看不见"的那批。
        parts.append(
            f"SELECT 'vjoin', {_lit(t)}, COUNT(*) FROM {t} vt "
            f"WHERE EXISTS (SELECT 1 FROM video_facts f WHERE f.video_id = vt.video_id)")
    return "\n UNION ALL ".join(parts)


# ── 行 → LibraryStateV1 ──────────────────────────────────────────────────

def _parse(rows: "list[dict]") -> dict:
    """把 (k, name, n) 三元组折成 LibraryStateV1。

    【缺行 = 未知,不是零】—— 这是 notes 第二句在代码里的对应物。上界截断
    (`sql_bounds.fetch_bounded`)会从尾部丢掉整条 UNION 分支,那时对应字段
    必须是 null 且进 unavailable_fields,绝不能默认成 0 骗大脑。
    """
    cat: "dict[str, int]" = {}
    meta: "dict[str, int]" = {}
    vert: "dict[str, int]" = {}
    vjoin: "dict[str, int]" = {}
    bucket = {"cat": cat, "meta": meta, "vert": vert, "vjoin": vjoin}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        d = bucket.get(str(r.get("k")))
        if d is None or r.get("name") is None or r.get("n") is None:
            continue
        try:
            d[str(r["name"])] = int(r["n"])
        except (TypeError, ValueError):
            continue

    unavailable: "list[str]" = []

    def _need(v, path: str):
        if v is None:
            unavailable.append(path)
        return v

    # 大类:按视频数降序、同数按名字升序(输出幂等,便于跨请求肉眼对比)
    present = sorted(((c, n) for c, n in cat.items() if n > 0),
                     key=lambda p: (-p[1], p[0]))
    shown = present[:MAX_CATEGORIES_SHOWN]

    videos_total = _need(meta.get("videos"), "videos_total")
    with_facts = meta.get("facts")
    unindexed = (videos_total - with_facts
                 if videos_total is not None and with_facts is not None else None)

    verticals = []
    for t in vertical_tables():
        rows_n, join_n = vert.get(t), vjoin.get(t)
        if rows_n is None:
            unavailable.append(f"vertical_tables.{t}")
            continue
        if rows_n <= 0:                       # 空壳表不占 token(它本身就没有事故面)
            continue
        verticals.append({"table": t, "rows": rows_n, "also_in_video_facts": join_n})
        if join_n is None:
            unavailable.append(f"vertical_tables.{t}.also_in_video_facts")

    coverage = {"title": meta.get("title"), "gcs_uri": meta.get("gcs_uri"),
                "facts": with_facts}
    for k, v in coverage.items():
        if v is None:
            unavailable.append(f"metadata_coverage.{k}")
    if unindexed is None:
        unavailable.append("unindexed_videos")

    return {
        "schema_version": SCHEMA_VERSION,
        # 统计的是【共享语料库】那五张表;M5 的临时上传(up_* )只登记在 Redis、
        # 带 TTL、刻意不进 video_metadata(见 pipeline/uploads.py 顶部)→ 恒 false。
        "scope": "shared_catalog",
        "status": "available",
        "temporary_uploads_included": False,
        "videos_total": videos_total,
        "category_counts": [{"category": c, "videos": n} for c, n in shown],
        "categories_total": len(present),
        "categories_omitted": len(present) - len(shown),
        # 每张非空专栏表一行:rows 是它自己的行数,also_in_video_facts 是其中
        # 能从 video_facts 查到的行数。两个数不等 = 差额那批走常规检索看不见。
        "vertical_tables": verticals,
        "unindexed_videos": unindexed,
        "metadata_coverage": coverage,
        "unavailable_fields": unavailable,
        "notes": list(NOTES),
    }


# ── 取数(带 TTL + fail-open)──────────────────────────────────────────────

def _query(sql: str) -> "list[dict]":
    """默认取数路:走 MCP 只读通道(与大脑自己的 sql_query 同一条路)。

    单独一个函数是为了让离线测试有个便宜的 monkeypatch 点 —— 测试【绝不】
    应该因为没有数据库凭据就红。
    """
    from pipeline import mcp_client
    return mcp_client.query_db(sql)


def snapshot(*, force: bool = False) -> "dict | None":
    """LibraryStateV1;拿不到返回 None(调用方整段省略)。

    force=True 跳过 TTL(给手工核对/测试用,生产路径不传)。
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHE["filled"] and (now - float(_CACHE["at"])) < TTL_SECONDS:
            return _CACHE["value"]          # 命中(【成功和失败都缓存】,见文件头不变量 2)
    value: "dict | None"
    try:
        value = _parse(_query(build_sql()))
    except Exception:
        log.warning("库存快照查询失败(fail-open,本段省略)", exc_info=True)
        value = None
    with _CACHE_LOCK:
        _CACHE.update({"at": time.monotonic(), "value": value, "filled": True})
    return value


def reset_cache() -> None:
    """清 TTL 缓存(测试用;生产不调)。"""
    with _CACHE_LOCK:
        _CACHE.update({"at": 0.0, "value": None, "filled": False})


def library_state_line() -> str:
    """prompt 注入节;拿不到 → ""(不占一个 token)。

    正文用紧凑 JSON 而不是自然语言表格:字段名自解释、体积小、且大脑对
    "这是系统给的结构化事实"这件事的判断比散文更稳。
    """
    snap = snapshot()
    if not snap:
        return ""
    return ("# 库存快照(系统在请求开始时统计的近似值;不是实时精确值)\n"
            + json.dumps(snap, ensure_ascii=False, separators=(",", ":")))
