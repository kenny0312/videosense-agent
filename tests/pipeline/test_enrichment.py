"""V1.5 富化:解析纯函数 + enrich 主流程(stub)+ /v1/enrich 端点校验的离线单测。
    python -m pytest tests/pipeline/test_enrichment.py
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from pipeline.enrichment import MAX_SNIPPET, entries_from_enrichment


# ── 解析:caption 恒有;有话才有 transcript 段;烂段跳过 ────────────
def test_entries_full():
    data = {"caption": "A spin class instructor coaches riders.", "has_speech": True,
            "language": "en",
            "segments": [{"start_s": 0.0, "end_s": 9.0, "text": "Hey guys, it's Brooke."},
                         {"start_s": 9.0, "end_s": 12.3, "text": "Stay strong."}]}
    out = entries_from_enrichment("v1", data)
    assert [s for s, _ in out] == ["caption", "video", "transcript", "transcript"]  # v2:+video 粗向量行
    key, snip, s0, e0 = out[2][1]                             # v2:out[1] 是 vid: 粗向量行
    assert key == "tr:v1:0" and snip == "Hey guys, it's Brooke." and (s0, e0) == (0.0, 9.0)
    assert out[0][1][0] == "cap:v1" and out[1][1][0] == "vid:v1"


def test_entries_no_speech_keeps_caption_only():
    out = entries_from_enrichment("v2", {"caption": "Wingsuit flight.", "has_speech": False,
                                         "segments": [{"start_s": 0, "end_s": 1, "text": "ghost"}]})
    assert len(out) == 2 and out[0][0] == "caption" and out[1][0] == "video"  # v2:无话仍有 caption+video


def test_entries_skips_junk_and_caps_length():
    data = {"caption": "c" * 9999, "has_speech": True,
            "segments": ["garbage", {"text": ""}, {"start_s": "x", "end_s": None, "text": "ok"}]}
    out = entries_from_enrichment("v3", data)
    assert out[0][1][1] == "c" * MAX_SNIPPET               # caption 截断
    assert len(out) == 3                                    # v2:caption+video+1段;烂段/空文本跳过
    assert out[1][1][2] is None and out[1][1][3] is None


def test_entries_empty():
    assert entries_from_enrichment("v", {}) == []
    assert entries_from_enrichment("v", {"caption": "  "}) == []


# ── enrich_video 主流程(全 stub:genai/embed/upsert)────────────
def test_enrich_video_stubbed(monkeypatch):
    import json
    from pipeline import enrichment as en, embeddings as emb, semantic_index as si
    from pipeline import genai_client

    class _Resp:
        text = json.dumps({"caption": "cap", "has_speech": True, "language": "en",
                           "segments": [{"start_s": 0, "end_s": 3, "text": "hello"}]})
        usage_metadata = None
    class _Models:
        def generate_content(self, **kw):
            return _Resp()
    class _C:
        models = _Models()
    monkeypatch.setattr(genai_client, "_CLIENT", _C())
    monkeypatch.setattr(emb, "embed_texts", lambda texts, **kw: [[0.0] * 768 for _ in texts])
    written = []
    monkeypatch.setattr(si, "index_entry", lambda vid, src, entry, lit: written.append((src, entry[0])) or True)
    monkeypatch.setattr(en, "embed_texts", emb.embed_texts)
    monkeypatch.setattr(en, "index_entry", si.index_entry)
    stats = en.enrich_video("v9", "gs://b/v9.mp4")
    assert stats["rows"] == 3 and stats["has_speech"] and stats["segments"] == 1  # v2:+video 行
    assert ("caption", "cap:v9") in written and ("transcript", "tr:v9:0") in written


# ── /v1/enrich 端点:非法 id / 未知视频 / 幂等 ─────────────────
def test_enrich_endpoint_validation(monkeypatch):
    import base64
    from fastapi.testclient import TestClient
    import api.server as srv                                   # 不 reload(顺序无关);直接关掉鉴权中间件
    from pipeline import enrichment as en, node_executor as ne, config
    monkeypatch.setattr(srv, "_ACCESS_KEYS", [])               # 无鉴权 → 免 Basic 头
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(en, "already_enriched", lambda vid: vid == "done_1")
    monkeypatch.setattr(ne, "_resolve_gcs", lambda vid: "gs://b/x.mp4" if vid == "good_1" else None)
    c = TestClient(srv.app)
    assert c.post("/v1/enrich", json={"video_id": "bad' id"}).status_code == 422
    assert c.post("/v1/enrich", json={"video_id": "done_1"}).json()["status"] == "already"
    assert c.post("/v1/enrich", json={"video_id": "nope_1"}).status_code == 404
    monkeypatch.setattr(en, "enrich_video", lambda vid, gcs: {"rows": 0})
    assert c.post("/v1/enrich", json={"video_id": "good_1"}).json()["status"] == "started"


# ── A8(P0-5):enrich 判死 —— 死线按素材时长 / 状态位 / 查询端点 ───────────────
def _srv():
    import api.server as srv
    return srv


def _clear_enrich_status():
    srv = _srv()
    with srv._ENRICH_LOCK:
        srv._ENRICH_STATUS.clear()


def test_enrich_deadline_scales_with_duration():
    """死线口径 max(5min, min(60min, 2×时长))—— 关键是它【看时长】:
    taskstate.TASK_LEASE_MIN 那种常数会给 10 秒短片和 3 小时讲座同一个数。"""
    srv = _srv()
    f, cap = srv.ENRICH_DEADLINE_FLOOR_SEC, srv.ENRICH_DEADLINE_CAP_SEC
    assert srv._enrich_deadline_sec(10) == f                 # 10s 短片:下限兜着
    assert srv._enrich_deadline_sec(600) == 1200             # 10min:正好 2×
    assert srv._enrich_deadline_sec(3 * 3600) == cap         # 3h:封顶
    assert srv._enrich_deadline_sec(30) != srv._enrich_deadline_sec(3600)   # 常数做不到这条


def test_enrich_deadline_unknown_duration_gets_full_leash():
    """测不到时长 → 给上限,绝不误杀。判死是为了让卡住的活有终点,不是为了砍慢活。"""
    srv = _srv()
    cap = srv.ENRICH_DEADLINE_CAP_SEC
    bad_values = (None, 0, -5, "abc", "", float("nan"), float("inf"), float("-inf"), [1])
    for bad in bad_values:
        # 清洗口径:脏值必须【真的被洗成 None】。只断言最终死线 == cap 是【空转的】——
        # min(cap, 2*inf) == cap 恰好成立,inf 一路混过清洗、混进状态位,直到
        # GET /v1/enrich/{vid} 序列化时 500。断言中间值才抓得住。
        assert srv._hinted_duration_sec(bad) is None, (
            f"{bad!r} 必须被洗成 None,不能靠 min() 把它夹回上限来掩盖")
        # 端到端口径(端点走的这条)
        assert srv._enrich_deadline_sec(srv._hinted_duration_sec(bad)) == cap
        # 直喂口径:_enrich_deadline_sec 自己也必须扛住脏值,不许依赖上游先洗一遍
        assert srv._enrich_deadline_sec(bad) == cap, f"{bad!r} 应归到测不到一档 → 给满绳"
    assert srv._hinted_duration_sec(42.5) == 42.5            # 正常值原样透传
    assert srv._hinted_duration_sec("42.5") == 42.5


def _enrich_client(monkeypatch, *, enrich_fn, already=lambda vid: False):
    from fastapi.testclient import TestClient
    from pipeline import config, enrichment as en, node_executor as ne
    srv = _srv()
    monkeypatch.setattr(srv, "_ACCESS_KEYS", [])                 # 关掉鉴权中间件(同既有端点单测)
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", True)
    monkeypatch.setattr(en, "already_enriched", already)
    monkeypatch.setattr(ne, "_resolve_gcs", lambda vid: "gs://b/x.mp4")
    monkeypatch.setattr(en, "enrich_video", enrich_fn)
    _clear_enrich_status()
    return TestClient(srv.app)


def _poll_status(c, vid: str, want: str, timeout: float = 5.0) -> dict:
    import time
    js: dict = {}
    end = time.time() + timeout
    while time.time() < end:
        js = c.get(f"/v1/enrich/{vid}").json()
        if js.get("status") == want:
            return js
        time.sleep(0.02)
    return js


def test_enrich_status_ok_path(monkeypatch):
    """跑完了:状态位落 ok,结果原样可查(旧实现只 log 一行,外面查不到任何东西)。"""
    c = _enrich_client(monkeypatch, enrich_fn=lambda vid, gcs: {"rows": 7})
    r = c.post("/v1/enrich", json={"video_id": "up_ok1", "duration_sec": 600}).json()
    assert r["status"] == "started" and r["deadline_sec"] == 1200      # 2×10min
    js = _poll_status(c, "up_ok1", "ok")
    assert js["status"] == "ok" and js["result"] == {"rows": 7} and js["deadline_sec"] == 1200


def test_enrich_status_failed_path(monkeypatch):
    """里面炸了:状态位落 failed 带错因,且【worker 不崩】(端点仍能继续服务)。"""
    def boom(vid, gcs):
        raise RuntimeError("genai 挂了")
    c = _enrich_client(monkeypatch, enrich_fn=boom)
    assert c.post("/v1/enrich", json={"video_id": "up_bad1"}).json()["status"] == "started"
    js = _poll_status(c, "up_bad1", "failed")
    assert js["status"] == "failed" and "genai 挂了" in js["error"] and not js.get("timeout")
    # worker 没死:同一个 client 还能再接一单
    assert c.post("/v1/enrich", json={"video_id": "up_bad2"}).json()["status"] == "started"


def test_enrich_deadline_kills_hung_work_and_records_late_outcome(monkeypatch):
    """卡住的活到点判死 → failed(timeout);它日后真跑完 → 只补记 late_status,不翻案。"""
    import threading
    srv = _srv()
    release = threading.Event()

    def hang(vid, gcs):
        release.wait(10)
        return {"rows": 1}

    c = _enrich_client(monkeypatch, enrich_fn=hang)
    monkeypatch.setattr(srv, "_enrich_deadline_sec", lambda d: 0.2)    # 别让单测真等 5 分钟
    assert c.post("/v1/enrich", json={"video_id": "up_hang"}).json()["deadline_sec"] == 0.2
    js = _poll_status(c, "up_hang", "failed")
    assert js["status"] == "failed" and js["timeout"] is True and "deadline exceeded" in js["error"]
    release.set()                                                      # 放它跑完
    end = __import__("time").time() + 5.0
    while __import__("time").time() < end:
        js = c.get("/v1/enrich/up_hang").json()
        if js.get("late_status"):
            break
        __import__("time").sleep(0.02)
    assert js["late_status"] == "ok" and js["status"] == "failed"      # 迟到的成功不翻案


def test_enrich_status_endpoint_validation_and_fallback(monkeypatch):
    """查询端点:非法 id 422;进程内没记录 → 退回"富化过没有"的探测;只读、不触发富化。"""
    fired = []
    c = _enrich_client(monkeypatch,
                       enrich_fn=lambda vid, gcs: fired.append(vid),
                       already=lambda vid: vid == "seen_1")
    assert c.get("/v1/enrich/bad' id").status_code == 422
    assert c.get("/v1/enrich/seen_1").json()["status"] == "already"    # 跨实例兜底
    assert c.get("/v1/enrich/never_1").json()["status"] == "unknown"
    assert fired == []                                                 # GET 一次也没触发富化


def test_enrich_status_disabled_when_semantic_off(monkeypatch):
    from fastapi.testclient import TestClient
    from pipeline import config
    srv = _srv()
    monkeypatch.setattr(srv, "_ACCESS_KEYS", [])
    monkeypatch.setattr(config, "USE_SEMANTIC_SEARCH", False)
    assert TestClient(srv.app).get("/v1/enrich/whatever").json() == {"status": "disabled"}


def test_enrich_status_table_is_bounded(monkeypatch):
    """长跑进程的状态位不许无限涨(否则一个观测设施变成内存泄漏)。"""
    srv = _srv()
    _clear_enrich_status()
    for i in range(srv._ENRICH_STATUS_MAX + 40):
        srv._enrich_set(f"v{i}", "ok")
    assert len(srv._ENRICH_STATUS) == srv._ENRICH_STATUS_MAX
    assert "v0" not in srv._ENRICH_STATUS                              # 最早登记的先淘汰
    _clear_enrich_status()


def test_m2_parse_verdicts():
    """M2 证据先行重抽的解析:按谓词对齐、烂项丢弃、区间非法丢弃。"""
    from perception.setup_timestamps_v2 import parse_verdicts
    text = ('[{"predicate":"skiing","present":true,"evidence":"skier mid-slope","start_s":5,"end_s":40},'
            '{"predicate":"ICE skating","present":false},'
            '{"predicate":"unknown","present":true,"start_s":1,"end_s":2},'
            '{"predicate":"snow","present":true,"start_s":9,"end_s":3}]')
    out = parse_verdicts(text, ["skiing", "ice skating", "snow"])
    assert [v["predicate"] for v in out] == ["skiing", "ice skating"]   # 越名丢弃/倒序区间丢弃
    assert out[0]["present"] and out[0]["start"] == 5.0
    assert out[1]["present"] is False
    assert parse_verdicts("not json", ["a"]) == []
