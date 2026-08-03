"""
show_video 工具测试 —— 不依赖 GCP(签名用桩)。mock DB 提供 video_metadata。
    REPL_USE_MOCK_DB=1 python -m pipeline.test_show_video

验证:从上游行/inputs 收集 video_id(白名单+去重)、查 video_metadata 补标题、
片段 marks、签名 fail-open(playable=false 也不崩)、DAG 校验放行 show_video。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("REPL_USE_MOCK_DB", "1")     # 必须在 import pipeline 之前
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import pipeline.video_url as video_url
from pipeline import node_executor as nx
from pipeline.dag_schema import Node, parse_dag


def _node(inputs=None, deps=None):
    return Node(id="n2", tool="show_video", inputs=inputs or {}, depends_on=deps or [])


# ── 收集逻辑 ──────────────────────────────────────────────
def test_collect_from_upstream_rows():
    items = nx._collect_items(_node(deps=["n1"]), {"n1": [
        {"video_id": "sky01", "start_ts": 11.0, "label": "freefall"},
        {"video_id": "sky04"},
    ]})
    assert [i["video_id"] for i in items] == ["sky01", "sky04"]
    assert items[0]["start_ts"] == 11.0 and items[0]["label"] == "freefall"


def test_collect_dedup_and_id_whitelist():
    items = nx._collect_items(_node(deps=["n1"]), {"n1": [
        {"video_id": "sky01"}, {"video_id": "sky01"},          # 去重
        {"video_id": "bad id; DROP"},                          # 非法 id 丢弃
        {"id": "sky02"},                                       # 用 id 兜底
    ]})
    assert [i["video_id"] for i in items] == ["sky01", "sky02"]


def test_collect_from_inputs_when_no_upstream():
    items = nx._collect_items(_node(inputs={"video_ids": ["sky03", "sky03", "x;y"]}), {})
    assert [i["video_id"] for i in items] == ["sky03"]


# ── inputs.items:带标注的展示清单(类目 chip + 片段跳转)────────
def test_items_take_precedence_over_upstream_and_video_ids():
    """给了 items 就以它为准:顺序是 items 的顺序,上游行和 video_ids 都不再参与。"""
    node = _node(inputs={"items": [{"video_id": "sky04"}, {"video_id": "sky01"}],
                         "video_ids": ["sky03"]},
                 deps=["n1"])
    items = nx._collect_items(node, {"n1": [{"video_id": "sky02"}]})
    assert [i["video_id"] for i in items] == ["sky04", "sky01"]


def test_items_carry_category_and_segment():
    items = nx._collect_items(_node(inputs={"items": [
        {"video_id": "sky01", "category": "skydiving", "start_ts": 12, "end_ts": 30.5},
    ]}), {})
    assert items[0]["category"] == "skydiving"
    assert items[0]["start_ts"] == 12.0 and items[0]["end_ts"] == 30.5


def test_items_category_normalized_and_offvocab_reported_not_dropped_silently():
    """中文/大小写走受控词表归一;词表外的【不自造】,但原话留在 category_raw 供回报。"""
    items = nx._collect_items(_node(inputs={"items": [
        {"video_id": "sky01", "category": "跳伞"},          # 中文别名 → 受控大类
        {"video_id": "sky02", "category": "Climbing"},      # 大小写 → casefold 命中
        {"video_id": "sky03", "category": "极限运动大合集"},   # 词表外
        {"video_id": "sky04"},                              # 没标注
    ]}), {})
    assert [i["category"] for i in items] == ["skydiving", "climbing", None, None]
    assert [i["category_raw"] for i in items] == [None, None, "极限运动大合集", None]


def test_items_bad_shapes_are_tolerated_not_fatal():
    items = nx._collect_items(_node(inputs={"items": [
        "sky01",                                   # 手滑写成 video_ids 的形状 → 仍收
        {"video_id": "sky01"},                     # 去重
        {"video_id": "bad id; DROP"},              # 非法 id 丢弃(白名单)
        {"video_id": "sky02", "start_ts": "第 3 秒"},  # 脏时间戳 → 当没给,不塞进播放器
        {"nope": 1},                               # 没 video_id → 丢弃
    ]}), {})
    assert [i["video_id"] for i in items] == ["sky01", "sky02"]
    assert items[1]["start_ts"] is None


def test_items_absent_leaves_legacy_paths_byte_identical():
    """没给 items → 老路径逐字节不变(新增字段只是多两个 key,取数来源不变)。"""
    up = {"n1": [{"video_id": "sky01", "start_ts": 11.0, "label": "freefall"}]}
    items = nx._collect_items(_node(deps=["n1"]), up)
    assert [i["video_id"] for i in items] == ["sky01"]
    assert items[0]["category"] is None and items[0]["start_ts"] == 11.0
    # items=[] / 非列表 都不算"给了",照走老路
    assert nx._collect_items(_node(inputs={"items": []}, deps=["n1"]), up)[0]["video_id"] == "sky01"
    assert nx._collect_items(_node(inputs={"items": "x"}, deps=["n1"]), up)[0]["video_id"] == "sky01"


# ── 节点执行(mock DB 补元数据 + 签名桩)────────────────────
def test_show_video_builds_payload():
    orig = video_url.sign_gcs_uri
    nx_orig = nx.sign_gcs_uri if hasattr(nx, "sign_gcs_uri") else None
    video_url.sign_gcs_uri = lambda uri, **k: "https://signed.example/x.mp4"   # 桩:签名成功
    try:
        res = nx._run_show_video(_node(deps=["n1"]),
                                 {"n1": [{"video_id": "sky01", "start_ts": 62.0, "label": "开伞"}]})
        assert res.ok and len(res.videos) == 1
        v = res.videos[0]
        assert v["video_id"] == "sky01"
        assert v["title"] == "Wingsuit Jump Over Alps"           # 来自 mock video_metadata
        assert v["playable"] is True and v["signed_url"].startswith("https://")
        assert v["marks"] == [{"ts": 62.0, "label": "开伞"}]
        # ③:value 带 note + 有序编号 items(供下一轮「第 N 个」映射)
        assert isinstance(res.value, dict) and "1 个视频" in res.value["note"]
        assert res.value["items"] == [{"n": 1, "video_id": "sky01", "title": "Wingsuit Jump Over Alps"}]
    finally:
        video_url.sign_gcs_uri = orig


def test_show_video_failopen_unsigned():
    orig = video_url.sign_gcs_uri
    video_url.sign_gcs_uri = lambda uri, **k: None              # 桩:签不出(本地无 SA)
    try:
        res = nx._run_show_video(_node(inputs={"video_ids": ["sky02"]}), {})
        assert res.ok and len(res.videos) == 1                  # 不崩
        v = res.videos[0]
        assert v["playable"] is False and v["signed_url"] is None
        assert v["gcs_uri"]                                      # 仍带回 gcs_uri 供前端降级展示
        assert "暂不可播放" in res.value["note"]
        assert res.value["items"][0]["n"] == 1 and res.value["items"][0]["video_id"] == "sky02"
    finally:
        video_url.sign_gcs_uri = orig


def test_show_video_empty_when_no_ids():
    res = nx._run_show_video(_node(), {})
    assert res.ok and res.videos == [] and res.value["shown"] == 0   # 空也不崩
    assert res.value["note"] == "没有可展示的视频(上游无 video_id)"   # 老路径文案不变


def test_show_video_side_channel_carries_category():
    """产品侧信道:每条 videos[] 带 category(前端出类目 chip);没标注 = None。"""
    orig = video_url.sign_gcs_uri
    video_url.sign_gcs_uri = lambda uri, **k: "https://signed.example/x.mp4"
    try:
        res = nx._run_show_video(_node(inputs={"items": [
            {"video_id": "sky01", "category": "跳伞", "start_ts": 62.0, "end_ts": 71.0},
            {"video_id": "v011", "category": "dancing"},
            {"video_id": "sky02"},
        ]}), {})
        assert [v["video_id"] for v in res.videos] == ["sky01", "v011", "sky02"]
        assert [v["category"] for v in res.videos] == ["skydiving", "dancing", None]
        # 时间段走既有字段,items 给了就用 items 的
        assert res.videos[0]["start_ts"] == 62.0 and res.videos[0]["end_ts"] == 71.0
        assert res.videos[0]["marks"] == [{"ts": 62.0, "label": "62s"}]
        # 只加字段:既有字段形状/语义原样
        assert res.videos[0]["title"] == "Wingsuit Jump Over Alps"
        assert res.videos[0]["playable"] is True
        assert res.value["items"][1] == {"n": 2, "video_id": "v011", "title": "Salsa Dancing Lessons"}
        assert "⚠️" not in res.value["note"]
    finally:
        video_url.sign_gcs_uri = orig


def test_show_video_offvocab_category_is_reported_not_silently_eaten():
    """词表外类目:不硬拒(工具照常成功、视频照常摆出),但把被丢的原话回报给大脑。"""
    orig = video_url.sign_gcs_uri
    video_url.sign_gcs_uri = lambda uri, **k: "https://signed.example/x.mp4"
    try:
        res = nx._run_show_video(_node(inputs={"items": [
            {"video_id": "sky01", "category": "极限运动大合集"},
        ]}), {})
        assert res.ok and len(res.videos) == 1          # 不报错、不吞视频
        assert res.videos[0]["category"] is None        # 不自造大类
        assert "极限运动大合集" in res.value["note"] and "⚠️" in res.value["note"]
    finally:
        video_url.sign_gcs_uri = orig


def test_show_video_items_all_invalid_says_why():
    res = nx._run_show_video(_node(inputs={"items": [{"video_id": "第 1 个"}]}), {})
    assert res.ok and res.videos == [] and res.value["shown"] == 0
    assert "items" in res.value["note"]                 # 说清是 items 的问题,不甩锅上游


# ── DAG 校验放行 show_video ────────────────────────────────
def test_show_video_passes_dag_validation():
    dag = parse_dag({"nodes": [
        {"id": "n1", "tool": "sql_query", "inputs": {"sql": "SELECT video_id FROM skydive_segments"}, "depends_on": []},
        {"id": "n2", "tool": "show_video", "inputs": {}, "depends_on": ["n1"]},
    ]})
    assert [n.tool for n in dag.nodes] == ["sql_query", "show_video"]


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t(); print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1; print(f"  FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())


def test_items_path_does_not_silently_lose_label_and_score():
    """选 items 标注类目,不许因此丢掉 label / score。

    上游行集那条路一直带着这两个,而前端【真的在用】:
      · score → 置信度 chip(web/index.html 的 chip)+ 片段条着色
      · marks[].label → 时间标记上的字("开伞"而不是"62s")
    契约第一版只给了 category / start_ts / end_ts,于是模型一旦开始标注类目,
    这两样就静默消失 —— 而我们正把它往 items 这条路上引,那是净 UX 回退。
    """
    from pipeline.dag_schema import Node

    node = Node(id="c0_0", tool="show_video", depends_on=[], inputs={"items": [
        {"video_id": "v_aaa", "category": "skydiving", "start_ts": 62,
         "label": "开伞", "score": 0.91},
    ]})
    got = nx._collect_items(node, {})
    assert len(got) == 1
    assert got[0]["label"] == "开伞", "时间标记会退化成只显示秒数"
    assert got[0]["score"] == 0.91, "置信度 chip 和片段着色都会消失"


def test_items_path_ignores_junk_label_and_score():
    """脏值不许流到前端:score 要能进 toFixed(2),label 要能当文本渲染。"""
    from pipeline.dag_schema import Node

    node = Node(id="c0_0", tool="show_video", depends_on=[], inputs={"items": [
        {"video_id": "v_aaa", "score": "很高", "label": ""},
        {"video_id": "v_bbb", "score": True, "label": None},
    ]})
    got = nx._collect_items(node, {})
    assert [g["score"] for g in got] == [None, None], (
        "非数字/布尔的 score 流到了前端 —— v.score.toFixed(2) 会炸")
    assert [g["label"] for g in got] == [None, None]
