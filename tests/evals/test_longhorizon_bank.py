"""P0-7:题库结构冻结校验 + 判分器确定性单测(离线,零 API 零 DB)。

题库是裁决工具:结构错了(配额超限/切分错/探针不空/gold 缺项)整个 gate 实验白跑,
所以结构断言与判分器行为一起钉死。gold 数值本身不在这里复算(那是 build 脚本对库的职责)。
"""
import json
from pathlib import Path

import pytest

from evals import longhorizon_score as S

BANK = Path(__file__).resolve().parents[2] / "evals"


def _load(split):
    return json.loads((BANK / f"longhorizon_bank.{split}.json").read_text(encoding="utf-8"))


DEV, HOLDOUT = _load("dev"), _load("holdout")
ALL = DEV["items"] + HOLDOUT["items"]


# ── 题库结构(红队 C1/C5/C7)──
def test_bank_counts_and_split():
    """9 T1 + 9 T2 + 2 探针;dev 6 题 / holdout 12 题,探针单列各 1。"""
    tiers = lambda items, t: [i for i in items if i["tier"] == t]
    assert len(tiers(ALL, "T1")) == 9 and len(tiers(ALL, "T2")) == 9
    assert len(tiers(ALL, "PROBE")) == 2
    assert len(tiers(DEV["items"], "T1")) == 3 and len(tiers(DEV["items"], "T2")) == 3
    assert len(tiers(DEV["items"], "PROBE")) == 1 and len(tiers(HOLDOUT["items"], "PROBE")) == 1


def test_gold_sizes_respect_quota_ceiling():
    """红队 C1:gold ≤10 < MAX_VIDEOS_PER_REQUEST=12,否则分数被配额天花板压扁。"""
    for i in ALL:
        if i["tier"] != "PROBE":
            assert 3 <= i["gold"]["count"] <= 10, i["id"]
            assert len(i["gold"]["video_ids"]) == i["gold"]["count"]


def test_probes_are_empty_and_separate():
    for p in (i for i in ALL if i["tier"] == "PROBE"):
        assert p["gold"]["video_ids"] == [] and p["gold"]["count"] == 0
        assert p["trap"]                                     # 相似陷阱说明必须在


def test_half_rephrased():
    """半数题面同义改写(考语义映射,词表泄漏关)。"""
    assert sum(1 for i in ALL if i["tier"] != "PROBE" and i["rephrased"]) >= 8


def test_t1_questions_ask_both_parts_and_no_dead_axis():
    """判分含 集合+归类 → 题面必须把两件事都问出来;排序轴已砍(时间线与时长两个候选轴
    在现库都不可判,review 实测 496/514 duration 为 NULL、gold 退化成字典序)——
    题面不许再要求排序,gold 里不许残留 ordering。"""
    for i in (x for x in ALL if x["tier"] == "T1"):
        assert "大类" in i["question"]
        assert "时长" not in i["question"] and "时间线" not in i["question"]
    for i in ALL:
        assert "ordering" not in i["gold"]


def test_t2_have_ts_mask_and_a_declared_localization_status():
    """T2 防 SQL 捷径 = 快照 ts 掩码;定位 gold 必须有【明确声明的状态】。

    原来断言的是 `status == "pending_prelabel"` —— 那锁的是"预标还没跑"这个【当时的现状】,
    B0-5 一跑完就红,而那正是我们想要的事发生了。锁现状会让"按计划完成"表现成回归。

    真正该锁的契约是三条:
      · ts_mask 恒开(否则 T2 变成考 SQL 抄时间戳);
      · status 必须是三个已知取值之一 —— scorer 靠它决定算不算定位分,
        出现第四种取值会被当成"不可判"静默吞掉;
      · labeled 时必须真有 spans(空 spans 配 labeled = 把不可判伪装成可判)。
    """
    for i in (x for x in ALL if x["tier"] == "T2"):
        assert i["ts_mask"] is True
        gl = i["gold_localization"]
        assert gl["status"] in ("pending_prelabel", "labeled", "low_confidence"), gl["status"]
        if gl["status"] == "labeled":
            spans = gl.get("spans") or {}
            assert spans, f"{i['id']} 声称 labeled 却没有 spans"
            for vid, sp in spans.items():
                assert vid in i["gold"]["video_ids"], f"{vid} 不在 gold 里"
                assert sp["end_ts"] > sp["start_ts"] >= 0, (vid, sp)


def test_ids_unique_and_vocab_frozen():
    ids = [i["id"] for i in ALL]
    assert len(ids) == len(set(ids))
    assert DEV["meta"]["category_vocab"] == HOLDOUT["meta"]["category_vocab"]
    assert len(DEV["meta"]["category_vocab"]) >= 10


def test_freeze_metadata_has_teeth():
    """review 确认:旧版 frozen_at 恒 None、重跑静默覆盖 —— 冻结纪律必须有时间戳+内容哈希
    (build 校验模式靠哈希报库漂移)。"""
    import hashlib
    for d in (DEV, HOLDOUT):
        assert d["meta"]["frozen_at"]
        sha = hashlib.sha256(json.dumps(d["items"], ensure_ascii=False,
                                        sort_keys=True).encode()).hexdigest()
        assert d["meta"]["items_sha256"] == sha


def test_no_umbrella_verb_predicates_in_t2():
    """review 确认:上位通用动词(jumping)题面外延远大于 gold,把语义能力强的臂按 F1 反扣
    —— T2 谓词必须具体。钉死已换掉的那颗,防回归。"""
    t2_preds = {i["predicate"] for i in ALL if i["tier"] == "T2"}
    assert "jumping" not in t2_preds
    assert "playing water polo" in t2_preds


# ── 判分器:契约解析 ──
def test_parse_contract_json():
    ans = '前言……{"video_ids": ["v_abc12345", "v_def67890"], "count": 2, "per_video": {}} 后记'
    p = S.parse_answer(ans)
    assert p["video_ids"] == ["v_abc12345", "v_def67890"] and not p["parse_failure"]


def test_parse_regex_fallback():
    p = S.parse_answer("我找到了 v_abc12345 和 v_def67890,就这两条。")
    assert p["video_ids"] == ["v_abc12345", "v_def67890"] and p["parse_failure"]


# ── 判分器:确定性分项 ──
def test_set_f1_basics():
    assert S.set_f1(["a", "b"], ["a", "b"]) == 1.0
    assert S.set_f1([], ["a"]) == 0.0
    assert S.set_f1(["a", "x"], ["a", "b"]) == pytest.approx(0.5)
    assert S.set_f1([], []) == 1.0                           # 空集探针形状


def test_category_accuracy_controlled_vocab():
    gold = {"per_video": {"v1": {"categories": ["water sports"]},
                          "v2": {"categories": ["ball sports"]}}}
    vocab = ["water sports", "ball sports"]
    pv = {"v1": {"category": "Water Sports"},                # 大小写不敏感
          "v2": {"category": "自由发挥"}}                     # 不在词表 → 错
    assert S.category_accuracy(pv, gold, vocab) == pytest.approx(0.5)
    assert S.category_accuracy({}, gold, vocab) == 0.0        # 漏答也是错


def test_parse_survives_brace_in_evidence_string():
    """review 实测:evidence 字符串里的 '}' 会把朴素配平腰斩 → 合法契约被判 parse_failure、
    归类 0 分。字符串感知配平必须解出来。"""
    ans = json.dumps({"video_ids": ["v_abc12345"], "count": 1,
                      "per_video": {"v_abc12345": {"category": "x",
                                                   "evidence": "画面里有大括号 } 字样"}}},
                     ensure_ascii=False)
    p = S.parse_answer("说明:" + ans)
    assert not p["parse_failure"] and p["per_video"]


def test_parse_falls_back_to_next_candidate():
    """review 实测:更长的非法花括号块(工具痕迹)会遮蔽较短的合法契约块 —— 必须逐候选试。"""
    junk = "{" + "'k': 1, " * 30 + "}"                       # 平衡但非法 JSON(单引号)
    good = json.dumps({"video_ids": ["v_abc12345"], "count": 1, "per_video": {}})
    p = S.parse_answer(junk + "\n" + good)
    assert not p["parse_failure"] and p["video_ids"] == ["v_abc12345"]


def test_parse_json_path_dedupes_and_caps():
    """review 实测:JSON 路不去重 → 重复 id 在 Kendall 里当逆序对罚格式合规者;超长病态
    重复拖死 O(n²)。两路都去重 + 截断。"""
    p = S.parse_answer(json.dumps({"video_ids": ["v_abc12345"] * 500 + ["v_def67890"],
                                   "count": 2, "per_video": {}}))
    assert p["video_ids"][:2] == ["v_abc12345", "v_def67890"]
    assert len(p["video_ids"]) <= S._MAX_IDS


def test_localization_refuses_without_gold():
    """gold 未预标 → None(拒算),绝不是 0 —— 把不可判当 0 会污染 C−B 差。"""
    assert S.localization_score({"v1": {"start_ts": 1, "end_ts": 2}},
                                {"status": "pending_prelabel", "spans": {}}) is None
    got = {"v1": {"start_ts": 12.0, "end_ts": 20.0},         # Δstart=2s ≤10 → 对
           "v2": {"start_ts": 500.0, "end_ts": 510.0}}       # 离谱 → 错
    gold = {"status": "labeled", "spans": {"v1": {"start_ts": 10.0, "end_ts": 22.0},
                                           "v2": {"start_ts": 30.0, "end_ts": 40.0}}}
    assert S.localization_score(got, gold) == pytest.approx(0.5)


def test_probe_scoring_zero_on_fabrication():
    ok = S.probe_score([], "库里没有浮潜相关的视频。")
    assert ok["score"] == 1.0 and ok["abstain_language"]
    bad = S.probe_score(["v_fake0001"], "找到 1 条:v_fake0001")
    assert bad["score"] == 0.0 and bad["fabricated_ids"] == 1


def test_probe_honest_prose_abstention_needs_review_not_zero():
    """review 实测确认:散文式诚实弃权("我排查过 v_x 和 v_y,都不是浮潜,库里没有")
    走 regex 兜底会把排查对象捞成编造判 0 —— 探针 0/1 二值,单次误判翻转整轴。
    契约缺失+弃权语+捞到 id → 拒判(needs_review),人工复核。"""
    ans = "我检查了 v_aaaa111111 和 v_bbbb222222,都不是浮潜。库里没有浮潜的视频。"
    parsed = S.parse_answer(ans)
    r = S.probe_score(parsed["video_ids"], ans, parse_failure=parsed["parse_failure"])
    assert r["score"] is None and r["needs_review"]
    # 走契约的同语义答案照常判 1(不撒网到所有 parse_failure)
    ans2 = '{"video_ids": [], "count": 0, "per_video": {}} 我排查过 v_aaaa111111,不是浮潜,库里没有。'
    parsed2 = S.parse_answer(ans2)
    r2 = S.probe_score(parsed2["video_ids"], ans2, parse_failure=parsed2["parse_failure"])
    assert r2["score"] == 1.0 and not r2["needs_review"]


# ── 判分器:合成与 pending 语义 ──
def test_score_item_t1_composite_and_judge_pending():
    item = next(i for i in ALL if i["id"] == "t1-swimming")
    vocab = DEV["meta"]["category_vocab"]
    gold = item["gold"]
    ans = json.dumps({"video_ids": gold["video_ids"], "count": gold["count"],
                      "per_video": {v: {"category": info["categories"][0]}
                                    for v, info in gold["per_video"].items()}})
    r = S.score_item(item, ans, vocab, judge=None)
    assert r["set_f1"] == 1.0 and r["category_acc"] == 1.0
    assert r["deterministic"] == pytest.approx(1.0)          # 0.6×1 + 0.4×1(无排序轴)
    assert r["judge_pending"] and r["composite"] == pytest.approx(0.8)   # judge 缺席不折算满分
    r2 = S.score_item(item, ans, vocab, judge=1.0)
    assert r2["composite"] == pytest.approx(1.0)


def test_score_item_t2_localization_pending():
    item = next(i for i in ALL if i["tier"] == "T2")
    ans = json.dumps({"video_ids": item["gold"]["video_ids"],
                      "count": item["gold"]["count"], "per_video": {}})
    r = S.score_item(item, ans, DEV["meta"]["category_vocab"], judge=None)
    assert r["set_f1"] == 1.0
    assert r["localization"] is None and r["localization_pending"]
    assert r["composite"] == pytest.approx(0.5)              # 只有 F1 部分,缺项不补


def test_score_from_tool_ledger_not_scrubbed_answer():
    """Phase 1 试跑实测的硬伤:VS 的 scrub_ids 按产品规则把答案里的 video_id 洗成
    "第 N 个"(绝不把内部 id 抄给用户)—— 只看答案文本会让每一臂 set_f1 恒为 0,
    量的是"洗得干不干净"。集合判分必须走工具台账。"""
    item = next(i for i in ALL if i["tier"] == "T1")
    gold = item["gold"]["video_ids"]
    scrubbed = "我们找到了 3 个视频:第 1 个是游泳、第 2 个是跳水、第 3 个是水球。"
    bad = S.score_item(item, scrubbed, DEV["meta"]["category_vocab"], judge=None)
    assert bad["set_f1"] == 0.0                              # 光看答案 = 全 0(病灶)
    good = S.score_item(item, scrubbed, DEV["meta"]["category_vocab"], judge=None,
                        surfaced=gold)
    assert good["set_f1"] == 1.0 and good["scored_from"] == "tool_ledger"
    part = S.score_item(item, scrubbed, DEV["meta"]["category_vocab"], judge=None,
                        surfaced=gold[:len(gold) // 2] + ["v_wrong1", "v_wrong2"])
    assert 0.0 < part["set_f1"] < 1.0                        # 部分命中要有区分度
