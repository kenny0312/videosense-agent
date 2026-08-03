"""B0-3 错误判分优先 + B0-4 统一评测交付契约 —— 判分口径的两条钉子(离线,零 API 零 DB)。

**为什么这两件事值得单开一个文件**:它们不是"分数算得准不准"的问题,是"这把尺子在
量什么"的问题。两条病灶都已在真数据上复现过:

  · B0-3:`gate-main-v3.jsonl` 里 `probe-golf` 的 B/C rep11 两行 `terminated=error`、
    `error=AttributeError("'NoneType' object has no attribute 'execute'")`、answer 长度 0,
    旧口径两行都拿 **1.0** —— 崩溃 → 零编造 → 空集探针满分。B 臂在这条红线上"赢" C 臂,
    一半靠这次 AttributeError。它没说"我不知道",它是崩了。
  · B0-4:答案契约要的是自然语言 + show_video(要求输出 id 的 JSON 会被 scrub_ids 洗掉),
    而判分只把 `surfaced` 覆盖到了 `video_ids`,`per_video` 一个字没覆盖 →
    实算 72 次非 PROBE 跑 parse_failure **72/72**、36 次 T1 的 category_acc **一律 0.0**,
    T1 的 40% / T2 的 30% 权重结构性恒为零。

产品侧的 `show_video(items=...)` 这会儿还没合进来,所以本文件**一律手造侧信道字典**,
不 import、不依赖产品侧的新参数。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import longhorizon_score as S
from evals import longhorizon_verdict as V

ROOT = Path(__file__).resolve().parents[2]
BANK = json.loads((ROOT / "evals" / "longhorizon_bank.holdout.json").read_text(encoding="utf-8"))
VOCAB = BANK["meta"]["category_vocab"]
ITEMS = {i["id"]: i for i in BANK["items"]}
PROBE = next(i for i in BANK["items"] if i["tier"] == "PROBE")
T1 = next(i for i in BANK["items"] if i["tier"] == "T1")
T2 = next(i for i in BANK["items"] if i["tier"] == "T2")


def _row(item, arm="B", rep=11, **kw):
    """一条最小可判的跑次记录(形状照 evals/runs/gate-*.jsonl)。"""
    r = {"id": item["id"], "tier": item["tier"], "arm": arm, "rep": rep,
         "answer": "", "terminated": "text", "cost_usd": 0.1, "wall_s": 10.0,
         "tools": [], "surfaced": []}
    r.update(kw)
    return r


# ────────────────────────────── B0-3 无效跑次 ──────────────────────────────
def test_crashed_probe_run_is_invalid_not_full_marks():
    """病灶原样重放:崩溃的空集探针跑次【不许】再拿 1.0。

    这就是 probe-golf B/C rep11 的形状 —— terminated=error、answer 长度 0、
    surfaced 为 None(异常分支根本没走到记账那一步)。
    """
    old = S.score_item(PROBE, "", VOCAB, judge=None, surfaced=None)
    assert old["score"] == 1.0, "没传 terminated 时是老行为(满分)—— 病灶本身"
    new = S.score_item(PROBE, "", VOCAB, judge=None, surfaced=None, terminated="error")
    assert new["invalid"] is True
    assert new["score"] is None and new["composite"] is None
    assert new["invalid_reason"] == "error"
    assert new.get("score") != 1.0


def test_invalid_is_refusal_not_zero():
    """【拒判】不是【判 0】:崩了和答错了走不同的处置,判 0 会把两件事混成一件。"""
    r = S.score_item(T1, "", VOCAB, judge=None, surfaced=["v_x"], terminated="error")
    assert r["score"] is None and r["composite"] is None
    assert "set_f1" not in r and "category_acc" not in r


@pytest.mark.parametrize("term", ["error", "max_steps", "tree_guard", "repeat"])
def test_all_non_text_terminations_are_invalid(term):
    """只有 error 判无效 —— 它是【测量本身崩了】,我们对 agent 的能力一无所知。

    max_steps / repeat / tree_guard 是【有产出的结局】:A1 之后它们交的是诚实的部分收口
    + 完整 ledger,而判分从台账取数,不受占位文案影响。实测 204 行里 max_steps 有 22 行、
    其中 7 行真的 show_video 摆出了 2~8 个视频;error 12 行里交付了任何东西的是 0 行。
    把 max_steps 一起剔掉既丢信号,又系统性利好"更容易烧穿步数"的臂
    (实测 v2 的 T1/A 因此跳 +0.099,是全表最大的 Δ,而那是排除规则的产物)。
    """
    r = S.score_item(T1, "随便什么答案", VOCAB, judge=None, surfaced=["v_x"], terminated=term)
    if term == "error":
        assert r["invalid"] and r["invalid_reason"] == term
    else:
        assert not r.get("invalid"), f"{term} 是有产出的结局,不该被剔出判分"
        assert r.get("set_f1") is not None


def test_normal_run_still_scored():
    """正常收口照旧判分 —— 别把闸门修成把所有跑次都挡在外面。"""
    r = S.score_item(PROBE, "库里没有高尔夫的视频。", VOCAB, judge=None,
                     surfaced=[], terminated="text")
    assert not r["invalid"] and r["score"] == 1.0


def test_terminated_none_keeps_old_behaviour():
    """terminated=None = 调用方没有跑次上下文(单测/手工复算单条答案)→ 按 valid 处理。"""
    r = S.score_item(T1, "", VOCAB, judge=None, surfaced=T1["gold"]["video_ids"])
    assert not r["invalid"] and r["set_f1"] == 1.0


def test_verdict_aggregate_drops_invalid_and_counts_them():
    """裁决器:无效跑次不进 per[],单独计数并按终止形态拆开。

    旧写法 `:67` 收了个 `err` 字段,`:87-109` 一次都没用过 —— 崩溃的跑次照旧混在均分里。
    """
    rows = [
        _row(PROBE, arm="B", rep=11, terminated="error", surfaced=None,
             error="AttributeError(...)"),
        _row(PROBE, arm="C", rep=11, terminated="error", surfaced=None,
             error="AttributeError(...)"),
        _row(PROBE, arm="B", rep=12, terminated="text", answer="库里没有。", surfaced=[]),
        _row(T1, arm="C", rep=11, terminated="max_steps", surfaced=["v_a"]),
    ]
    per, invalid = V.aggregate(rows, ITEMS, VOCAB)
    # 只有 error 无效;max_steps 那行是【有产出的结局】,照常进 per[]
    assert sum(sum(c.values()) for c in invalid.values()) == 2
    assert invalid[("PROBE", "B")]["error"] == 1
    assert invalid[("PROBE", "C")]["error"] == 1
    assert ("T1", "C") not in invalid, "max_steps 被剔出去了 —— 那是有交付的结局,不是崩溃"
    assert len(per[(T1["id"], "C")]) == 1
    # C 臂探针一次有效跑次都没有 → 那一格是【无样本】,不是 0 分,更不是 1.0
    assert ("probe-golf", "C") not in per
    assert [x["score"] for x in per[("probe-golf", "B")]] == [1.0]


def test_verdict_aggregate_would_have_scored_crash_as_one_before():
    """反向钉:同一批行,如果不看 terminated,崩溃那两行会各拿 1.0 进均分。

    这条测的是"我们修掉的到底是多大一件事" —— 不是风格问题,是红线轴上的满分。
    """
    rows = [_row(PROBE, arm="C", rep=11, terminated="error", surfaced=None)]
    naive = S.score_item(PROBE, "", VOCAB, judge=None, surfaced=None)
    assert naive["score"] == 1.0
    per, invalid = V.aggregate(rows, ITEMS, VOCAB)
    assert not per and sum(sum(c.values()) for c in invalid.values()) == 1


def test_pairwise_wins_does_not_hand_c_a_free_point():
    """剔掉无效跑次的连带风险:B 臂整道题缺席时,C 臂不许白捡一分。

    旧写法 `next((b["score"] ...), 0)` 会把缺席读成 0 分。v3 里 `t2-rock-climbing`
    的 B 臂两次 rep 全是 error —— 这条路真的走得到,不是假想。
    """
    c = [{"id": "q1", "score": 0.4}, {"id": "q2", "score": 0.1}]
    b = [{"id": "q1", "score": 0.3}]                     # q2 的 B 臂全崩了
    win, pairable, unpairable = V.pairwise_wins(c, b)
    assert win == 1 and [x["id"] for x in pairable] == ["q1"] and unpairable == ["q2"]
    # 老口径会算成 2/2:0.1 >= 兜底的 0
    assert win != 2


# ────────────────────────── B0-4 交付契约 / 取数侧信道 ──────────────────────────
def test_per_video_from_ledger_merges_first_non_none_per_field():
    """同一视频被摆两次(先交付、后补时段)时逐字段合并,不是"第一行整行胜出"。

    只取第一行会把后补的定位丢掉 —— 那一项在 T2 里值 30% 权重。
    """
    meta = [
        {"video_id": "v_a", "category": "climbing", "start_ts": None, "end_ts": None},
        {"video_id": "v_a", "category": None, "start_ts": 12.0, "end_ts": 20.0},
        {"video_id": "v_b", "category": None, "start_ts": None, "end_ts": None},
    ]
    pv = S.per_video_from_ledger(meta)
    assert pv["v_a"] == {"category": "climbing", "start_ts": 12.0, "end_ts": 20.0}
    assert pv["v_b"] == {"category": None, "start_ts": None, "end_ts": None}
    assert S.per_video_from_ledger(None) == {} and S.per_video_from_ledger([]) == {}


def test_t1_category_comes_from_tool_ledger_not_answer_json():
    """T1 归类的 40% 权重:从台账取到大类就判得出分;答案文本里一个字都不用有。"""
    gold_pv = T1["gold"]["per_video"]
    meta = [{"video_id": vid, "category": info["categories"][0],
             "start_ts": None, "end_ts": None} for vid, info in gold_pv.items()]
    scrubbed = "我们找到了 7 个视频:第 1 个……第 7 个。"      # scrub_ids 之后的真实形状
    r = S.score_item(T1, scrubbed, VOCAB, judge=None, terminated="text",
                     surfaced=list(gold_pv), surfaced_meta=meta)
    assert r["set_f1"] == 1.0
    assert r["category_acc"] == 1.0                      # 病灶时期这里一律 0.0
    assert r["deterministic"] == pytest.approx(1.0)
    # 台账里没有 category(产品侧还没合进来)→ 老实退回 0,不假装有
    bare = [{"video_id": vid} for vid in gold_pv]
    r2 = S.score_item(T1, scrubbed, VOCAB, judge=None, terminated="text",
                      surfaced=list(gold_pv), surfaced_meta=bare)
    assert r2["category_acc"] == 0.0


def test_t2_localization_comes_from_tool_ledger():
    """T2 定位的 30% 权重:台账带 start_ts/end_ts + gold 已预标 → 判得出分。

    gold 未预标时照旧【拒算】返回 None(不许把不可判伪装成 0),这条不动。
    """
    item = dict(T2)
    vids = item["gold"]["video_ids"][:2]
    item["gold_localization"] = {"status": "labeled", "spans": {
        vids[0]: {"start_ts": 10.0, "end_ts": 22.0},
        vids[1]: {"start_ts": 30.0, "end_ts": 40.0}}}
    meta = [{"video_id": vids[0], "start_ts": 12.0, "end_ts": 20.0},   # Δstart=2s → 对
            {"video_id": vids[1], "start_ts": 500.0, "end_ts": 510.0}]  # 离谱 → 错
    r = S.score_item(item, "文字说明省略", VOCAB, judge=None, terminated="text",
                     surfaced=vids, surfaced_meta=meta)
    assert r["localization"] == pytest.approx(0.5)
    assert not r["localization_pending"]
    pending = dict(item, gold_localization={"status": "pending_prelabel", "spans": {}})
    assert S.score_item(pending, "", VOCAB, judge=None, terminated="text",
                        surfaced=vids, surfaced_meta=meta)["localization"] is None


def test_parse_failure_is_scoring_input_missing_not_answer_format():
    """`parse_failure` 的语义 = 【判分输入取不到】,不是"答案没写成 JSON"。

    契约本来就【不要】JSON(要了会被 scrub_ids 洗掉)。台账在场时判分根本不读答案 JSON,
    解不解得开与分数无关 → 恒 False。这不是把失败改成成功:
    交付为空(surfaced=[])照旧 set_f1=0,那是【交付为空】,与【尺子读不到交付】两件事。
    """
    prose = "我们找到了 3 个视频:第 1 个、第 2 个、第 3 个。"
    with_ledger = S.score_item(T1, prose, VOCAB, judge=None, terminated="text",
                               surfaced=["v_a"], surfaced_meta=[{"video_id": "v_a"}])
    assert with_ledger["parse_failure"] is False
    assert with_ledger["answer_json_contract"] is False      # 诊断位如实记录
    assert with_ledger["scored_from"] == "tool_ledger"
    # 交付为空 ≠ 判分输入取不到
    empty = S.score_item(T1, prose, VOCAB, judge=None, terminated="text", surfaced=[])
    assert empty["parse_failure"] is False and empty["set_f1"] == 0.0
    # 台账整个缺席(老格式跑次)→ 判分只能退回答案文本,解不开就是真的取不到
    legacy = S.score_item(T1, prose, VOCAB, judge=None, terminated="text")
    assert legacy["parse_failure"] is True


def test_surfaced_meta_alone_supplies_the_id_set():
    """只给 surfaced_meta 也能判集合 —— 侧信道是【一个】台账,不是两个要同步的字段。"""
    gold = T1["gold"]["video_ids"]
    meta = [{"video_id": v} for v in gold]
    r = S.score_item(T1, "", VOCAB, judge=None, terminated="text", surfaced_meta=meta)
    assert r["set_f1"] == 1.0 and r["scored_from"] == "tool_ledger"


def test_answer_contract_follows_the_real_tool_signature(monkeypatch):
    """契约随工具真实签名走 —— 签名有 items 就说,没有就一个字都不多说。

    产品侧改动与本文件分属两个代理,合入时序不保证。写死 items 会在合入之前
    让模型发出一个被 schema 拒收的参数 —— 把好端端的跑次变成 terminated=error,
    正是 B0-3 刚修掉的那种"数据"。

    【测的是行为,不是当时的现状】:第一版断言"今天还没有 items",产品侧一合进来
    这条就红了 —— 而机制其实是对的。锁现状会让"功能按预期生效"表现成回归。
    所以两个方向都用 monkeypatch 造签名来验。
    """
    import dataclasses

    from evals import longhorizon_run as R
    from pipeline import node_specs

    spec = node_specs.SPECS["show_video"]

    def _with_props(props):
        params = json.loads(json.dumps(spec.parameters))       # 深拷贝,不动全局
        params["properties"] = props
        patched = dict(node_specs.SPECS)                       # NodeSpec 是 frozen dataclass
        patched["show_video"] = dataclasses.replace(spec, parameters=params)
        monkeypatch.setattr(node_specs, "SPECS", patched)

    # ① 签名【没有】 items → 契约与今天逐字节一致,绝不多说
    _with_props({"video_ids": {"type": "array"}})
    base = R.answer_contract()
    assert base == R.ANSWER_CONTRACT
    assert "items" not in base

    # ② 签名【有】 items → 追加那一段
    _with_props({"video_ids": {"type": "array"}, "items": {"type": "array"}})
    upgraded = R.answer_contract()
    assert upgraded.startswith(R.ANSWER_CONTRACT)              # 只新增,不改已有措辞
    assert "items" in upgraded and "category" in upgraded and "start_ts" in upgraded


def test_surfaced_meta_only_adds_fields(monkeypatch):
    """禁改区边界:新台账只是把 `videos[]` 里已有的字段抄一份,不改 rows/preview 形状,
    也不改 `surfaced` 今天的行为(去重后的 id 列表)。"""
    from evals import longhorizon_run as R

    class _ER:
        ok = True
        videos = [{"video_id": "v_a", "title": "A", "start_ts": 1.0, "end_ts": 2.0,
                   "signed_url": "https://x", "marks": [{"ts": 1.0}]},
                  {"video_id": "v_a", "title": "A", "category": "climbing"},
                  {"video_id": "v_b", "title": "B"}]

    class _LO:
        results = {"c1": _ER()}

    assert R._surfaced_video_ids(_LO()) == ["v_a", "v_b"]      # 今天的行为,原样
    meta = R._surfaced_meta(_LO())
    assert [m["video_id"] for m in meta] == ["v_a", "v_a", "v_b"]   # 如实留档,不去重
    assert set(meta[0]) == set(R._LEDGER_KEYS)                 # 只抄这几个字段
    assert meta[0]["category"] is None and meta[1]["category"] == "climbing"
    assert S.per_video_from_ledger(meta)["v_a"] == {
        "category": "climbing", "start_ts": 1.0, "end_ts": 2.0}


# ─────────────────── 全量复算:拿真历史跑次当回归样本(数据在才跑)───────────────────
_RUNS = ROOT / "evals" / "runs" / "gate-main-v3.jsonl"


@pytest.mark.skipif(not _RUNS.exists(),
                    reason="本地 evals/runs 归档不在(该目录 gitignore),数据依赖测试跳过")
def test_historical_v3_recompute_matches_acceptance():
    """真数据回归:v3 那批里 probe-golf 的两行崩溃跑次不再得 1.0,且有效跑次 parse failure = 0。"""
    rows = [json.loads(l) for l in _RUNS.read_text(encoding="utf-8").splitlines() if l.strip()]
    per, invalid = V.aggregate(rows, ITEMS, VOCAB)
    crashed = [r for r in rows if r["id"] == "probe-golf" and r["terminated"] == "error"]
    assert len(crashed) == 2
    for r in crashed:
        assert S.score_item(ITEMS[r["id"]], r.get("answer") or "", VOCAB,
                            surfaced=r.get("surfaced"),
                            terminated=r["terminated"])["invalid"] is True
    # 无效 = 只有 error。数字【从数据算】而不是写死 —— 写死的期望值在口径一改就会
    # 变成"测试红了所以口径错了",而实际是口径对了、期望值过期了(本文件已经踩过一次)。
    n_err = sum(1 for r in rows if r["terminated"] == "error")
    n_ms = sum(1 for r in rows if r["terminated"] == "max_steps")
    assert sum(sum(c.values()) for c in invalid.values()) == n_err
    assert n_ms > 0, "这批里本来就有 max_steps,不然下面这条断言是空转的"
    assert all("max_steps" not in c for c in invalid.values()), (
        "max_steps 被剔出判分了 —— 那是有交付的结局(实测 7 行真的摆出了 2~8 个视频),"
        "剔掉它既丢信号,又系统性利好更容易烧穿步数的臂")
    flat = [x for runs in per.values() for x in runs]
    assert sum(1 for x in flat if x["parse_failure"]) == 0, "验收:parse failure = 0"
