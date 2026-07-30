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
