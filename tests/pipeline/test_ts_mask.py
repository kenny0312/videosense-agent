import os
from pipeline import mcp_client as M

def test_mask_off_by_default():
    os.environ.pop("GATE_TS_MASK_PREDICATE", None)
    rows = [{"video_id": "v1", "predicate": "falling", "start_ts": 3.0, "end_ts": 5.0}]
    assert M._mask_ts(rows) is rows                       # 原样返回,生产零影响

def test_mask_hits_only_target_predicate():
    os.environ["GATE_TS_MASK_PREDICATE"] = "falling,diving"
    rows = [{"video_id": "v1", "predicate": "falling", "start_ts": 3.0, "end_ts": 5.0},
            {"video_id": "v2", "predicate": "swimming", "start_ts": 7.0, "end_ts": 9.0},
            {"video_id": "v3", "predicate": "Diving", "start_ts": 1.0, "end_ts": 2.0}]
    out = M._mask_ts(rows)
    assert out[0]["start_ts"] is None and out[0]["end_ts"] is None   # 被测谓词:掩掉
    assert out[1]["start_ts"] == 7.0                                 # 别的谓词:不动
    assert out[2]["start_ts"] is None                                # 大小写不敏感
    assert rows[0]["start_ts"] == 3.0                                # 不改原始行(拷贝)
    os.environ.pop("GATE_TS_MASK_PREDICATE", None)

def test_mask_tolerates_odd_shapes():
    os.environ["GATE_TS_MASK_PREDICATE"] = "falling"
    assert M._mask_ts(None) is None
    assert M._mask_ts([{"no_predicate": 1}, "字符串行"]) == [{"no_predicate": 1}, "字符串行"]
    os.environ.pop("GATE_TS_MASK_PREDICATE", None)


# ── 批 5-C:按 video-id 掩(谓词掩的三条旁路的封堵)────────────────────
def test_mask_by_video_id_covers_all_predicates(monkeypatch):
    """考的单位是视频,掩的单位就是视频。dp-main 实测谓词掩被同义孪生行钻穿:
    'celebrating' 掩了,'celebration & awards' 带同款时间戳原样返回(6/6 题全中)。
    video-id 掩下,该视频【所有】谓词行的时间戳都必须没了 —— 同义行无处可漏。"""
    monkeypatch.setenv("GATE_TS_MASK_VIDEO_IDS", "v_gold1,v_gold2")
    monkeypatch.delenv("GATE_TS_MASK_PREDICATE", raising=False)
    rows = [{"video_id": "v_gold1", "predicate": "celebrating", "start_ts": 217.0, "end_ts": 218.0},
            {"video_id": "v_gold1", "predicate": "celebration & awards", "start_ts": 217.0, "end_ts": 218.0},
            {"video_id": "v_gold2", "predicate": "随便什么", "start_ts": 5.0, "end_ts": 9.0},
            {"video_id": "v_other", "predicate": "celebrating", "start_ts": 30.0, "end_ts": 33.0}]
    out = M._mask_ts(rows)
    assert out[0]["start_ts"] is None and out[1]["start_ts"] is None, "孪生谓词行还在漏时间戳"
    assert out[2]["start_ts"] is None, "gold 视频的任意谓词行都得掩"
    assert out[3]["start_ts"] == 30.0, "非 gold 视频不许误伤 —— 掩过头就是把库背景全抹了"
    assert rows[0]["start_ts"] == 217.0, "不改原始行(拷贝)"


def test_mask_video_id_and_predicate_stack(monkeypatch):
    """两种掩码可叠加(老跑次复现要谓词掩,新跑次用 video-id 掩,互不打架)。"""
    monkeypatch.setenv("GATE_TS_MASK_VIDEO_IDS", "v_a")
    monkeypatch.setenv("GATE_TS_MASK_PREDICATE", "diving")
    rows = [{"video_id": "v_a", "predicate": "swimming", "start_ts": 1.0, "end_ts": 2.0},
            {"video_id": "v_b", "predicate": "diving", "start_ts": 3.0, "end_ts": 4.0},
            {"video_id": "v_b", "predicate": "swimming", "start_ts": 5.0, "end_ts": 6.0}]
    out = M._mask_ts(rows)
    assert out[0]["start_ts"] is None          # video-id 命中
    assert out[1]["start_ts"] is None          # 谓词命中
    assert out[2]["start_ts"] == 5.0           # 都没命中
