"""P0-4(spawn gate 五判据)+ P0-5(semantic_search video_ids 视频内下钻)。

P0-5 的不变量:USE_IN_VIDEO_SEARCH=0(默认)时,参数对大脑不可见、不传参的检索路径与
升级前【逐字节】一致;开了之后指定 video_ids 的检索必须只落在指定集合内(SQL 层 ANY 过滤)。
P0-4 是纯 prompt:五判据文本进 spawn_agents 的 planner_desc,防回归钉子在这里;
触发率验收(5 条简单题 spawn ≤1/5)属 live 探针,不在本文件。
离线、零 API、零 DB(_execute 全部打桩)。
"""
import pytest

from pipeline import config, loop_driver, node_specs, semantic_index
from pipeline.node_executor import Node, _run_semantic_search


# ── P0-5:SQL 层 ──
def test_unfiltered_sql_is_byte_identical_to_before():
    """Part 0 不变量①:不传 video_ids 用的还是原来那条 SQL(独立新 SQL,不往老的拼 WHERE)。"""
    assert "WHERE" not in semantic_index.SEARCH_SQL
    assert semantic_index.SEARCH_SQL == (
        "SELECT video_id, source, snippet, start_ts, end_ts, "
        "1 - (embedding <=> %s::vector) AS score "
        "FROM content_embeddings ORDER BY embedding <=> %s::vector LIMIT %s")
    assert "video_id = ANY(%s)" in semantic_index.SEARCH_SQL_FILTERED


def _capture_execute(monkeypatch):
    calls = []
    monkeypatch.setattr(semantic_index, "_execute",
                        lambda sql, params: calls.append((sql, params)) or [])
    return calls


def test_search_without_ids_uses_original_sql(monkeypatch):
    calls = _capture_execute(monkeypatch)
    semantic_index.search("[0.1]", 8)
    semantic_index.search("[0.1]", 8, video_ids=None)
    semantic_index.search("[0.1]", 8, video_ids=[])          # 空列表 = 不过滤
    assert [c[0] for c in calls] == [semantic_index.SEARCH_SQL] * 3
    assert calls[0][1] == ("[0.1]", "[0.1]", 8)


def test_search_with_ids_filters_at_sql_layer(monkeypatch):
    """验收:指定 video_ids 的检索在 SQL 层就锁死集合(ANY 过滤),不是取回来再筛。"""
    calls = _capture_execute(monkeypatch)
    semantic_index.search("[0.1]", 5, video_ids=["v1", "v2"])
    sql, params = calls[0]
    assert sql == semantic_index.SEARCH_SQL_FILTERED
    assert params == ("[0.1]", ["v1", "v2"], "[0.1]", 5)     # 列表原样进 ANY(%s)


# ── P0-5:声明层门控 ──
def _semantic_decl(monkeypatch):
    # evals 假世界(evals/world.py)会把 config.USE_SEMANTIC_SEARCH 全局关掉且不恢复 ——
    # 本组测试不吃环境余温,显式钉住工具本身是开的。
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    for d in loop_driver.loop_function_declarations():
        if d["name"] == "semantic_search":
            return d
    raise AssertionError("semantic_search 声明不见了")


def test_video_ids_param_invisible_when_flag_off(monkeypatch):
    """关(默认)= 参数从声明里消失,大脑根本看不见 —— 而不是看得见调了才报错。"""
    monkeypatch.setattr(config, "USE_IN_VIDEO_SEARCH", False)
    assert "video_ids" not in _semantic_decl(monkeypatch)["parameters"]["properties"]


def test_video_ids_param_visible_when_flag_on(monkeypatch):
    monkeypatch.setattr(config, "USE_IN_VIDEO_SEARCH", True)
    decl = _semantic_decl(monkeypatch)
    assert decl["parameters"]["properties"]["video_ids"]["type"] == "array"
    assert "video_ids" not in decl["parameters"].get("required", [])   # 可选


def test_specs_not_polluted_by_declaration_stripping(monkeypatch):
    """声明剥离必须发生在深拷贝之后 —— SPECS 本体不许被改(改了开关就再也开不回来)。"""
    monkeypatch.setattr(config, "USE_IN_VIDEO_SEARCH", False)
    _semantic_decl(monkeypatch)                              # 触发一次剥离
    assert "video_ids" in node_specs.SPECS["semantic_search"].parameters["properties"]


# ── P0-5:执行层 ──
def _offline(monkeypatch):
    """闸门后全打桩:被守护的回归一旦发生,测试要在【离线】就挂掉 ——
    不打桩的话,回归会在有凭证的机器上真打 embed API + 生产 Neon(review 确认)。"""
    import pipeline.embeddings as emb
    monkeypatch.setattr(emb, "embed_query",
                        lambda q: (_ for _ in ()).throw(AssertionError("不该走到 embed")))
    monkeypatch.setattr(semantic_index, "search",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该走到检索")))


def test_executor_rejects_ids_when_flag_off(monkeypatch):
    """直连 API/回放可能带旧参数 —— 关着时要软失败教育,绝不静默忽略过滤(忽略=错误结果)。"""
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(config, "USE_IN_VIDEO_SEARCH", False)
    _offline(monkeypatch)
    node = Node(id="c1", tool="semantic_search",
                inputs={"query": "pool table", "video_ids": ["v1"]}, depends_on=[])
    with pytest.raises(ValueError, match="USE_IN_VIDEO_SEARCH"):
        _run_semantic_search(node)


def test_executor_rejects_non_list_ids_instead_of_silent_fullscan(monkeypatch):
    """review 确认:video_ids="v001"(字符串)此前被静默归空 → 开着闸也全库检索,
    大脑把全库命中当指定视频里的时刻引用 —— 比不过滤更毒。坏类型必须报错教育。"""
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(config, "USE_IN_VIDEO_SEARCH", True)
    _offline(monkeypatch)
    for bad in ("v001", 123, {"v": 1}):
        node = Node(id="c1", tool="semantic_search",
                    inputs={"query": "pool", "video_ids": bad}, depends_on=[])
        with pytest.raises(ValueError, match="字符串数组"):
            _run_semantic_search(node)
    node = Node(id="c1", tool="semantic_search",
                inputs={"query": "pool", "video_ids": ["", None]}, depends_on=[])
    with pytest.raises(ValueError, match="空值"):
        _run_semantic_search(node)


def test_executor_passes_ids_to_both_retrieval_paths(monkeypatch):
    """双路检索(原文 + 英译)必须同过滤 —— 漏一路,过滤就是装饰。"""
    from pipeline import node_executor
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(config, "USE_IN_VIDEO_SEARCH", True)
    monkeypatch.setattr(node_executor, "_translate_query_en", lambda q: "billiards")
    import pipeline.embeddings as emb
    monkeypatch.setattr(emb, "embed_query", lambda q: [0.1])
    monkeypatch.setattr(emb, "vec_literal", lambda v: "[0.1]")
    seen = []

    def fake_search(vec_lit, k, video_ids=None):
        seen.append(video_ids)
        return [{"n": 1, "video_id": "v1", "source": "analyze", "snippet": "s",
                 "start_ts": 0.0, "end_ts": 1.0, "score": 0.8, "relevance": "strong",
                 "label": "s"}]
    monkeypatch.setattr(semantic_index, "search", fake_search)
    node = Node(id="c1", tool="semantic_search",
                inputs={"query": "打台球", "video_ids": ["v1", "v2"]}, depends_on=[])
    res = _run_semantic_search(node)
    assert res.ok
    assert seen and all(ids == ["v1", "v2"] for ids in seen)   # 每一路都带了过滤
    assert len(seen) >= 2                                      # 原文 + 英译两路都跑了


def test_executor_default_path_untouched(monkeypatch):
    """不传 video_ids:传给检索层的过滤恒为 None(与升级前行为等价)。"""
    from pipeline import node_executor
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(config, "USE_IN_VIDEO_SEARCH", False)
    monkeypatch.setattr(node_executor, "_translate_query_en", lambda q: None)
    import pipeline.embeddings as emb
    monkeypatch.setattr(emb, "embed_query", lambda q: [0.1])
    monkeypatch.setattr(emb, "vec_literal", lambda v: "[0.1]")
    seen = []

    def fake_search(vec_lit, k, video_ids=None):
        seen.append(video_ids)
        return [{"n": 1, "video_id": "v9", "source": "analyze", "snippet": "s",
                 "start_ts": 0.0, "end_ts": 1.0, "score": 0.8, "relevance": "strong",
                 "label": "s"}]
    monkeypatch.setattr(semantic_index, "search", fake_search)
    node = Node(id="c1", tool="semantic_search", inputs={"query": "pool"}, depends_on=[])
    assert _run_semantic_search(node).ok
    assert seen == [None]


# ── P0-4:spawn gate 五判据(prompt 防回归钉子)──
def test_spawn_desc_steers_not_only_brakes():
    """Phase 1 实测:原描述 922 字里刹车是引导的 3.4 倍,且五判据的 ③(单步子任务不配拆)
    与 ⑤(K 个 agent ≈ K 次分析的钱)【结构性排除】了本实验的场景 —— 模型不拆是在守指令。
    重写后必须:①有正面触发规则 ②讲清真收益是"步数"不是"钱" ③给可照做的示范
    ④正面引导的篇幅不少于反面。"""
    desc = node_specs.SPECS["spawn_agents"].planner_desc
    assert "什么时候该拆" in desc and "什么时候别拆" in desc
    # 真收益必须写明:买的是步数不是钱(旧描述让模型算出"不省钱 → 不值"而拒拆)
    assert "步数预算" in desc and "钱基本不变" in desc
    # 最该拆的情形要和实测失败模式对齐(待看量 > 剩余步数)
    assert "超过你剩余步数" in desc
    # 可照做的示范(抽象判据模型学不会)
    assert "照着做" in desc and "instruction 写成" in desc
    # 决策要发生在"数得清"之后,不是开局盲猜
    assert "数一下还有多少要看" in desc
    pos = desc[desc.index("什么时候该拆"):desc.index("什么时候别拆")]
    neg = desc[desc.index("什么时候别拆"):]
    assert len(pos) >= 0.7 * len(neg), f"正面引导 {len(pos)} 字 vs 反面 {len(neg)} 字,又写成刹车了"


def test_spawn_gate_reaches_brain_via_declarations(monkeypatch):
    """判据要真进大脑看到的声明(不只是躺在 SPECS 里)。"""
    monkeypatch.setattr(config, "USE_SUBAGENTS", True)
    decl = next(d for d in loop_driver.loop_function_declarations()
                if d["name"] == "spawn_agents")
    assert "什么时候该拆" in decl["description"]      # 引导真的进了大脑看到的声明


def _stub_multi_hit(monkeypatch, rows):
    """打桩:embed/翻译/检索,让执行器吃到指定行。"""
    from pipeline import node_executor
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(config, "USE_IN_VIDEO_SEARCH", True)
    monkeypatch.setattr(node_executor, "_translate_query_en", lambda q: None)
    import pipeline.embeddings as emb
    monkeypatch.setattr(emb, "embed_query", lambda q: [0.1])
    monkeypatch.setattr(emb, "vec_literal", lambda v: "[0.1]")
    monkeypatch.setattr(semantic_index, "search", lambda *a, **k: [dict(r) for r in rows])


def test_in_video_drilldown_keeps_multiple_moments_per_video(monkeypatch):
    """review-HIGH:_dedupe_by_video 每视频只留一行 → 视频内下钻退化成 top-1,k 形同虚设。
    有过滤时同一视频的多个时刻必须都保留。"""
    def row(n, ts, score):
        return {"n": n, "video_id": "v1", "source": "analyze", "snippet": f"moment-{n}",
                "start_ts": ts, "end_ts": ts + 5, "score": score, "relevance": "strong",
                "label": f"moment-{n}"}
    _stub_multi_hit(monkeypatch, [row(1, 10.0, 0.9), row(2, 40.0, 0.85), row(3, 90.0, 0.8)])
    node = Node(id="c1", tool="semantic_search",
                inputs={"query": "goal", "video_ids": ["v1"], "k": 8}, depends_on=[])
    res = _run_semantic_search(node)
    assert res.ok and isinstance(res.value, list)
    assert len(res.value) == 3                               # 三个时刻全在,不是 top-1
    assert [r["start_ts"] for r in res.value] == [10.0, 40.0, 90.0]


def test_fullscan_dedupe_by_video_unchanged(monkeypatch):
    """不传过滤:仍按视频聚合(全库检索要视频广度)—— 老行为一字不动。"""
    rows = [{"n": 1, "video_id": "v1", "source": "analyze", "snippet": "a",
             "start_ts": 0.0, "end_ts": 5.0, "score": 0.9, "relevance": "strong", "label": "a"},
            {"n": 2, "video_id": "v1", "source": "analyze", "snippet": "b",
             "start_ts": 9.0, "end_ts": 14.0, "score": 0.85, "relevance": "strong", "label": "b"}]
    _stub_multi_hit(monkeypatch, rows)
    node = Node(id="c1", tool="semantic_search", inputs={"query": "goal"}, depends_on=[])
    res = _run_semantic_search(node)
    assert res.ok and len(res.value) == 1                    # 每视频一行(广度语义)


def test_filtered_no_strong_envelope_scopes_its_claim(monkeypatch):
    """review 确认:只查了指定视频却宣称『库里没有』= 教大脑说事实性错误
    (内容可能就在集合外)。过滤态的信封必须把断言范围钉在指定视频上。"""
    rows = [{"n": 1, "video_id": "v1", "source": "analyze", "snippet": "meh",
             "start_ts": 0.0, "end_ts": 5.0, "score": 0.3, "relevance": "weak", "label": "meh"}]
    _stub_multi_hit(monkeypatch, rows)
    node = Node(id="c1", tool="semantic_search",
                inputs={"query": "goal", "video_ids": ["v1"]}, depends_on=[])
    res = _run_semantic_search(node)
    assert res.ok and res.value.get("no_strong_match")
    assert "库里没有" not in res.value["note"]               # 不许全库口径断言
    assert "指定" in res.value["note"] and "去掉 video_ids" in res.value["note"]
    assert res.value["scoped_to"] == ["v1"]


def test_eval_fake_world_search_accepts_video_ids():
    """review-HIGH:三臂实验(EVAL_SEMANTIC=1 + USE_IN_VIDEO_SEARCH=1)跑在假世界上 ——
    假 search 签名不齐,下钻臂调一次炸一次 TypeError,实验测到的是坏工具。"""
    import json as _json
    from evals.world import build_cosine_search
    index = [("v1", "goal moment", 10.0, 15.0, [1.0, 0.0]),
             ("v2", "goal replay", 20.0, 25.0, [0.9, 0.1])]
    search = build_cosine_search(index)
    q = _json.dumps([1.0, 0.0])
    assert {r["video_id"] for r in search(q, 5)} == {"v1", "v2"}          # 不传 = 全库(老行为)
    got = search(q, 5, video_ids=["v2"])
    assert [r["video_id"] for r in got] == ["v2"]                         # 过滤只落在集合内
    assert search(q, 5, video_ids=["v9"]) == []
