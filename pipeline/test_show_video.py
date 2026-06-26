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
        assert isinstance(res.value, str) and "1" in res.value
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
        assert "暂不可播放" in res.value
    finally:
        video_url.sign_gcs_uri = orig


def test_show_video_empty_when_no_ids():
    res = nx._run_show_video(_node(), {})
    assert res.ok and res.videos == [] and res.value["shown"] == 0   # 空也不崩


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
