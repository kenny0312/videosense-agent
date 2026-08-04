"""C1 库存快照 + C2「未知≠空」宪法条款。

【全部离线】—— 唯一的取数点 `library_state._query` 被 monkeypatch 掉,套件不碰凭据、
不起 MCP 子进程。真库只在人工核对时打(见模块 docstring)。

三个必须守住的东西:
  1. 红线:快照内容【绝不】出现在 `loop_driver._LOOP_SYSTEM`(import 期冻结的
     字节稳定缓存前缀)。进去 = 每轮都 cache miss,而且不会有任何症状。
  2. 事故形状:`orphan_tables` 必须报出"只在专栏表里、video_facts 查不到"的那批
     (`perception/skydive_schema.py:141-143` 记的跳伞事故)。
  3. 未知 ≠ 零:查询少给了某段 → 对应字段是 null 且进 unavailable_fields,不许默认 0。
"""
import json

import pytest

from pipeline import library_state as ls


@pytest.fixture(autouse=True)
def _clean_cache():
    """每条测试前后都清 TTL 缓存 —— 缓存是模块级状态,漏清会让相邻测试互相污染。"""
    ls.reset_cache()
    yield
    ls.reset_cache()


def _rows(cats=(), *, videos=100, title=100, gcs=100, facts=90,
          verts=(("skydive_segments", 12, 12),)):
    """造一组 (k, name, n) 三元组 —— 与单条 UNION ALL SQL 的返回同形。"""
    out = [{"k": "cat", "name": c, "n": n} for c, n in cats]
    out += [{"k": "meta", "name": "videos", "n": videos},
            {"k": "meta", "name": "title", "n": title},
            {"k": "meta", "name": "gcs_uri", "n": gcs},
            {"k": "meta", "name": "facts", "n": facts}]
    for t, rows_n, join_n in verts:
        out.append({"k": "vert", "name": t, "n": rows_n})
        out.append({"k": "vjoin", "name": t, "n": join_n})
    return out


def _patch(monkeypatch, rows_or_exc):
    calls = {"n": 0}

    def fake(sql):
        calls["n"] += 1
        if isinstance(rows_or_exc, Exception):
            raise rows_or_exc
        return rows_or_exc

    monkeypatch.setattr(ls, "_query", fake)
    return calls


# ── 红线:快照绝不能进冻结的缓存前缀 ────────────────────────────────────
def test_library_state_never_lands_in_frozen_prefix(monkeypatch):
    """`_LOOP_SYSTEM` 是 import 期冻结的字节稳定前缀(隐式缓存靠它命中)。
    库存快照【每个请求都可能不同】,一旦进去,每一轮都是 cache miss —— 而且
    这种退化没有任何症状,只会安静地多花钱,所以必须由测试守。"""
    from pipeline import loop_driver
    _patch(monkeypatch, _rows(cats=[("skydiving", 42)]))
    line = ls.library_state_line()
    assert line, "快照没生成,这条测试就没在验它想验的东西"

    assert "库存快照" not in loop_driver._LOOP_SYSTEM
    assert ls.SCHEMA_VERSION not in loop_driver._LOOP_SYSTEM
    assert line not in loop_driver._LOOP_SYSTEM

    # 但走运行期拼接那一层就必须进得去
    s = loop_driver._loop_system({"t": []}, None, library_state=line)
    assert ls.SCHEMA_VERSION in s
    assert s.startswith(loop_driver._LOOP_SYSTEM), "静态前缀必须逐字节原样打头"


def test_two_requests_with_different_snapshots_share_whole_static_prefix(monkeypatch):
    """两次【快照不同】的请求,公共前缀必须一路盖过整块 _LOOP_SYSTEM + schema。
    这是"没打碎缓存"本身,不是它的间接指标。"""
    import os
    from pipeline import loop_driver
    schema = {"MARK_SCHEMA": ["c" * 300]}
    a = loop_driver._loop_system(schema, None, library_state='{"videos_total":1}')
    b = loop_driver._loop_system(schema, None, library_state='{"videos_total":999}')
    common = os.path.commonprefix([a, b])
    assert loop_driver._LOOP_SYSTEM in common and "c" * 300 in common


# ── C1 SQL:一条、只碰白名单表 ───────────────────────────────────────────
def test_sql_is_single_read_only_statement():
    from pipeline.sql_guard import is_read_only
    sql = ls.build_sql()
    assert is_read_only(sql), "快照 SQL 必须过只读闸(它走的是与大脑同一条 MCP 通道)"
    assert ";" not in sql, "必须是【单条】语句:分号 = 拆成了 N 条(N 次往返 + N 个时刻)"
    assert sql.upper().count(" UNION ALL ") >= 5


def test_sql_touches_only_business_tables():
    """越出 config.BUSINESS_TABLES 就是绕过了 get_schema 的白名单口径。"""
    import re
    from pipeline import config
    sql = ls.build_sql()
    referenced = set(re.findall(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", sql)) \
        | set(re.findall(r"\bJOIN\s+([A-Za-z_][A-Za-z0-9_]*)", sql))
    assert referenced, "没解析出表名,这条测试就没在验它想验的东西"
    assert referenced <= set(config.BUSINESS_TABLES), \
        f"快照 SQL 碰了白名单外的表: {referenced - set(config.BUSINESS_TABLES)}"


def test_sql_groups_controlled_categories_only():
    """只 GROUP BY 受控词表内的 predicate:不加过滤会冒出 ~200 个自由细谓词,
    行数和 token 双失控,而"存量"要的是大类轴。"""
    from pipeline.taxonomy_seed import CATEGORIES
    sql = ls.build_sql()
    assert "GROUP BY vf.predicate" in sql
    for c in ("skydiving", "cooking & food"):
        assert f"'{c}'" in sql
    assert sql.count("', '") >= len(CATEGORIES) - 2      # 整份词表都在 IN 里


# ── C1 字段:两个不可省的 ────────────────────────────────────────────────
def test_snapshot_has_the_two_mandatory_fields(monkeypatch):
    _patch(monkeypatch, _rows(cats=[("skydiving", 42), ("winter sports", 30)]))
    snap = ls.snapshot()
    assert snap["schema_version"] == "vs.library-state/v1"
    assert snap["category_counts"] == [{"category": "skydiving", "videos": 42},
                                       {"category": "winter sports", "videos": 30}]
    assert snap["orphan_tables"] == [
        {"table": "skydive_segments", "rows": 12, "also_in_video_facts": 12}]
    assert snap["videos_total"] == 100
    assert snap["unindexed_videos"] == 10                 # 100 - 90 有 facts 的
    assert snap["metadata_coverage"] == {"title": 100, "gcs_uri": 100, "facts": 90}
    assert snap["scope"] == "shared_catalog"
    assert snap["temporary_uploads_included"] is False


def test_orphan_tables_exposes_the_skydive_accident_shape(monkeypatch):
    """事故原形:312 行只在 skydive_segments、video_facts 里一条都没有 →
    "库里有跳伞视频吗"走 video_facts.predicate 就被答成"没有"。
    快照必须让这个缺口在【开局第一步】就看得见。"""
    _patch(monkeypatch, _rows(verts=[("skydive_segments", 312, 0)]))
    snap = ls.snapshot()
    entry = snap["orphan_tables"][0]
    assert entry == {"table": "skydive_segments", "rows": 312, "also_in_video_facts": 0}
    assert entry["rows"] > entry["also_in_video_facts"], "缺口必须从两个数字上读得出来"


def test_empty_vertical_table_costs_no_tokens(monkeypatch):
    """空壳专栏表没有事故面 —— 不占每轮的注入税。"""
    _patch(monkeypatch, _rows(verts=[("skydive_segments", 0, 0)]))
    assert ls.snapshot()["orphan_tables"] == []


def test_category_truncation_reports_how_many_were_omitted(monkeypatch):
    """截断纪律:只列前 12 类,其余进 categories_omitted(而不是静静消失)。"""
    cats = [(f"c{i:02d}", 100 - i) for i in range(20)]
    _patch(monkeypatch, _rows(cats=cats))
    snap = ls.snapshot()
    assert len(snap["category_counts"]) == ls.MAX_CATEGORIES_SHOWN == 12
    assert snap["categories_total"] == 20
    assert snap["categories_omitted"] == 8
    assert [c["category"] for c in snap["category_counts"]] == [f"c{i:02d}" for i in range(12)]


def test_notes_are_the_three_mandated_sentences(monkeypatch):
    """三句都是【防止大脑拿快照当真值】的:计数会重叠 / 缺席是未知 / 这是快照。
    少一句就少挡一种误用。"""
    _patch(monkeypatch, _rows())
    notes = ls.snapshot()["notes"]
    assert len(notes) == 3
    assert "不能相加" in notes[0]
    assert "未知" in notes[1] and "不表示零" in notes[1]
    assert "快照" in notes[2] and "sql_query" in notes[2]


# ── C1「未知≠零」:少给的段落是 null,不是 0 ─────────────────────────────
def test_missing_rows_become_null_not_zero(monkeypatch):
    """上界截断会从尾部丢掉整条 UNION 分支。那时字段必须是 null 且进
    unavailable_fields —— 默认成 0 就是拿"我没查到"冒充"库里没有",
    正是 C2 要治的那个病在数据面的版本。"""
    _patch(monkeypatch, [{"k": "cat", "name": "skydiving", "n": 42}])   # 只剩大类那一段
    snap = ls.snapshot()
    assert snap["videos_total"] is None
    assert snap["unindexed_videos"] is None
    assert snap["metadata_coverage"] == {"title": None, "gcs_uri": None, "facts": None}
    assert "videos_total" in snap["unavailable_fields"]
    assert "metadata_coverage.facts" in snap["unavailable_fields"]
    assert 0 not in (snap["videos_total"], snap["unindexed_videos"])


def test_healthy_snapshot_reports_nothing_unavailable(monkeypatch):
    _patch(monkeypatch, _rows(cats=[("skydiving", 42)]))
    assert ls.snapshot()["unavailable_fields"] == []


# ── C1 TTL + fail-open ──────────────────────────────────────────────────
def test_ttl_cache_queries_once(monkeypatch):
    calls = _patch(monkeypatch, _rows())
    for _ in range(5):
        ls.snapshot()
    assert calls["n"] == 1, "每个请求都全表统计一次不合算 —— TTL 缓存必须挡住"
    ls.reset_cache()
    ls.snapshot()
    assert calls["n"] == 2


def test_ttl_expiry_refetches(monkeypatch):
    calls = _patch(monkeypatch, _rows())
    t = [1000.0]                                       # 假时钟必须在【第一次】取数前就装好
    monkeypatch.setattr(ls.time, "monotonic", lambda: t[0])
    ls.snapshot()
    assert calls["n"] == 1
    t[0] += ls.TTL_SECONDS / 2
    ls.snapshot()
    assert calls["n"] == 1, "TTL 内不该重查"
    t[0] += ls.TTL_SECONDS
    ls.snapshot()
    assert calls["n"] == 2, "过了 TTL 就该重查(否则快照会一直陈旧下去)"


def test_failure_is_fail_open_and_omits_the_whole_section(monkeypatch):
    """快照拿不到绝不能拖垮请求:snapshot() → None,注入节 → 空串(prompt 里干脆没这节)。"""
    _patch(monkeypatch, RuntimeError("relation does not exist"))
    assert ls.snapshot() is None
    assert ls.library_state_line() == ""


def test_failure_is_also_cached(monkeypatch):
    """失败【也要】进缓存:否则库一挂,每个请求都白等一次 MCP 超时(≥15s),
    快照本身就成了故障放大器 —— fail-open 只做对了一半。"""
    calls = _patch(monkeypatch, RuntimeError("boom"))
    for _ in range(4):
        ls.snapshot()
    assert calls["n"] == 1


def test_line_is_compact_json_with_a_heading(monkeypatch):
    _patch(monkeypatch, _rows(cats=[("skydiving", 42)]))
    line = ls.library_state_line()
    head, _, body = line.partition("\n")
    assert head.startswith("# ")
    parsed = json.loads(body)
    assert parsed["schema_version"] == ls.SCHEMA_VERSION
    # 紧凑分隔符:每轮都注入的段落不留冗余空格(逐字节比,不靠"不含 ', '"那种
    # 会被含逗号的大类名误伤的近似判据)
    assert body == json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    assert len(line) < 2000, "注入税上限 —— 涨过这条线要主动评审,不许悄悄长"


def test_next_vertical_table_is_covered_automatically(monkeypatch):
    """skydive 事故当时的修法是补数据回填,不是补摘要 —— 所以"下一个专栏表还会再犯"。
    专栏表由 BUSINESS_TABLES 减核心四表推出:新表进白名单就自动被 orphan 检查覆盖,
    不依赖有人记得回来改 library_state.py。"""
    from pipeline import config
    monkeypatch.setattr(config, "BUSINESS_TABLES",
                        list(config.BUSINESS_TABLES) + ["surfing_segments"])
    assert "surfing_segments" in ls.vertical_tables()
    assert "FROM surfing_segments" in ls.build_sql()
    _patch(monkeypatch, _rows(verts=[("skydive_segments", 12, 12),
                                     ("surfing_segments", 50, 3)]))
    tables = {e["table"]: e for e in ls.snapshot()["orphan_tables"]}
    assert tables["surfing_segments"]["also_in_video_facts"] == 3


# ── C2 宪法条款 ─────────────────────────────────────────────────────────
def test_unknown_is_not_empty_clause_is_in_the_frozen_prefix():
    """C2 的正文是【静态通用原则】,所以它【应该】进 _LOOP_SYSTEM(与 C1 相反):
    静态 = 不打碎缓存前缀,还能被每一轮免费复用。"""
    from pipeline import loop_driver
    s = loop_driver._LOOP_SYSTEM
    assert "未知 ≠ 空" in s
    assert "工具返回的是你【查过的那部分】" in s
    assert "不等于【库里没有】" in s
    assert "在 video_facts 里没有匹配的" in s, "必须给出【连范围一起说】的正面样例"


def test_unknown_is_not_empty_clause_did_not_go_into_lessons():
    """按 lessons.py 的入集三问,这是通用判断原则(不针对某个工具、写不出退役条件)
    → 归宪法,不进教训集。放错地方会挤占 MAX_LESSONS 预算,还得给它编个退役条件。"""
    from pipeline import lessons
    for l in lessons.LESSONS:
        assert "未知 ≠ 空" not in l.text
