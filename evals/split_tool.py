"""GD-1:金标事实家族标签 + train/val/sealed 切分清单生成器。

为什么按【家族】切而不是按题号切(审计结论):
  · 模板克隆族(honesty-no-X 系列只换实体)、共享金标事实族(20 题都押在
    is_wingsuit 只有 sky01+sky04 这一个事实上)、完全重复问句对 —— 按题号切,
    优化器在训练题上"背下"某个金标事实,验证/封存里的同族题就形同虚设。
  · 规则:一个家族【整体】进同一堂;安全/身份/泄漏类【强制进封存】
    (绝不让优化器对着安全题调措辞)。

用法(题库变了就重跑,人工过目 diff 再提交):
    python -m evals.split_tool          # 重新生成 evals/split_manifest.json + 打印分布
校验(validate_tasks 会调):每题都有家族、家族不跨堂、封存含全部安全类必过题。
"""
from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(_HERE, "split_manifest.json")

# ── 家族规则:按【金标事实/模板】归族(前缀或精确 id → 家族名)──────────
# 顺序即优先级(先匹配先归);新题不匹配任何规则 → validate 报错,逼你归族。
FAMILY_RULES: list[tuple[str, list[str]]] = [
    # ── 世界 B(GD-2 新考场;翻转题与世界A同句 → 所在家族必须与 honesty-absent 同堂=train)──
    ("worldb-swim", ["worldb-retrieval-swim-", "worldb-count-swimming-", "worldb-honesty-flip-swim-",
                     "worldb-timestamp-flipturn-", "worldb-toolcall-count-noplay-"]),
    ("worldb-pets", ["worldb-retrieval-pets-", "worldb-count-pets-", "worldb-honesty-flip-cat-",
                     "worldb-display-table-pets-", "worldb-display-play-cat-", "worldb-coherence-elliptical-"]),
    ("worldb-fitness", ["worldb-retrieval-fitness-", "worldb-honesty-flip-yoga-", "worldb-count-yoga-"]),
    ("worldb-music", ["worldb-retrieval-music-", "worldb-honesty-flip-guitar-", "worldb-coherence-correction-"]),
    ("worldb-water", ["worldb-retrieval-water-", "worldb-timestamp-roll-"]),
    ("worldb-crafts", ["worldb-retrieval-crafts-", "worldb-timestamp-latte-", "worldb-count-longest-"]),
    ("worldb-soccer", ["worldb-honesty-flip-soccer-", "worldb-timestamp-goal-"]),
    ("worldb-misc", ["worldb-count-total-", "worldb-honesty-flip-salad-", "worldb-honesty-noskydive-",
                     "worldb-honesty-nomakeup-"]),
    # 深多轮压力族(5-7轮;evals-hardening 批:长程干扰/约束翻转/约束叠加)。
    # 先按默认进 train 解锁归族;是否该上 val/sealed(难题给记分板分辨率)由 eval 主线定夺。
    ("press-distract", ["press-distract-"]),
    ("press-flipflop", ["press-flipflop-"]),
    ("press-stack",    ["press-stack-"]),
    # 安全/注入/越权(封存硬约束)
    ("safety", ["safety-", "selfknow-safety-porn-", "selfknow-safety-injection-",
                "selfknow-safety-illegal-", "selfknow-safety-delete-", "play-no-id-leak-04",
                "idleak-uri-request-"]),   # 救援批:逼问内部 URI = 泄漏类,随安全封存
    # 身份不漏底
    ("identity", ["identity-", "selfknow-safety-areyougpt-", "selfknow-safety-whotrained-",
                  "selfknow-safety-whatmodel-", "selfknow-safety-ctxwindow-"]),
    # 自我认知(成本/模型档,金标=runtime_facts 真数)
    ("selfknow-cost", ["selfknow-cost-", "selfknow-safety-cost-", "selfknow-safety-lasttoken-",
                       "selfknow-safety-turncost-", "selfknow-safety-modeltier-"]),
    # 超范围婉拒(披萨/排序/天气)
    ("irrelevance", ["toolcall-irrelevance-"]),
    # 联网搜索工具
    ("websearch", ["toolcall-websearch-"]),
    # 上传链(uploads 共享状态)
    ("upload", ["dualcontrol-upload-"]),
    # 翼装事实族(is_wingsuit 只有 sky01+sky04 —— 20 题共押一个事实)
    ("wingsuit", ["entity-jump-type-second-",
                  "count-wingsuit-", "count-nonwingsuit-", "retrieval-wingsuit-",
                  "skydive-honesty-01", "dualcontrol-correct-wrong-claim-",
                  "dualcontrol-memory-wingsuit-", "dualcontrol-paste-image-wingsuit-",
                  "toolcall-memory-wingsuit-", "toolcall-table-wingsuit-",
                  "coherence-accumulate-wingsuit-", "coherence-anaphora-longer-jump-"]),
    # 跳伞片单族(sky01-04 全集)
    ("skydive-list", ["idleak-play-longest-skydive-", "entity-shortest-skydive-",
                      "coherence-accumulate-activity-", "coherence-correction-ski-to-sky-",
                      "coherence-goalshift-cooking-to-skydive-", "coherence-skydive-ordinal-",
                      "count-skydiving-", "dualcontrol-correct-count-", "retrieval-skydive-",
                      "retrieval-best-skydive-", "retrieval-aircraft-exit-",
                      "display-table-skydive-", "toolcall-show-table-skydive-",
                      "toolcall-deepcompare-sky-ski-"]),
    # 跳伞阶段时间码/画面事实(skydive_segments + FACT_SHEETS)
    ("sky-phases", ["timestamp-sky", "dualcontrol-paste-image-deploy-",
                    "vision-pov-", "vision-landing-", "vision-color-"]),
    # 做饭族(v006 饼干 + v007 肋排;含完全重复问句对 + enrich 双题)
    ("cooking", ["idleak-table-cooking-", "idleak-compare-two-cooking-", "entity-longest-cooking-",
                 "entity-which-has-oven-",
                 "cooking-", "honesty-cooking-", "count-cooking-", "retrieval-cooking-",
                 "display-table-cooking-", "toolcall-count-cooking-", "coherence-cooking-ordinal-",
                 "coherence-correction-count-", "dualcontrol-enrich-", "timestamp-v006-",
                 "timestamp-v007-", "vision-oven-", "vision-sauce-"]),
    # 冬季运动族(v001/v002/v003:滑雪+单板共享 v003,合并防跨族泄漏)
    ("winter", ["idleak-recommend-winter-", "idleak-best-snowboard-", "entity-fastest-snow-",
                "count-skiing-", "count-ski-or-snowboard-", "dualcontrol-correct-skiing-",
                "retrieval-skiing-", "retrieval-snowboarding-", "retrieval-winter-snow-",
                "honesty-winter-broad-", "honesty-neg-iceskating-", "vision-notskating-",
                "timestamp-v001-", "timestamp-v003-", "count-v003-duration-",
                "toolcall-analyze-not-play-goggles-", "toolcall-play-best-skiing-",
                "retrieval-helmet-", "coherence-narrow-snowboard-", "coherence-snowboard-constraint-",
                "coherence-correction-iceskate-"]),
    # 摔倒/滑板族(falling 事实 = v002[27,30]+v009[24,27];v009 滑板画面事实)
    ("fall-skate", ["count-falling-", "retrieval-falling-", "honesty-falling-",
                    "coherence-anaphora-falling-", "retrieval-fastpaced-",
                    "dualcontrol-memory-no-falling-", "timestamp-v002-falling-",
                    "vision-helmet-", "honesty-neg-helmet-", "timestamp-v009-"]),
    # 公园族(v009 标题 + v012 park scenery)
    ("park", ["retrieval-park-", "display-play-dog-", "entity-dog-location-"]),
    # 美妆/编发族(v004+v005,教程语义)
    ("makeup-hair", ["idleak-play-mascara-", "entity-mascara-tool-", "entity-braiding-what-",
                     "count-makeup-", "retrieval-makeup-", "coherence-elliptical-duration-",
                     "coherence-elliptical-makeup-", "retrieval-tutorial-", "timestamp-v005-",
                     "toolcall-memory-dislike-makeup-", "display-count-no-play-",
                     "honesty-neg-cakedecor-"]),
    # 库外诚实族(模板克隆:库里没有 X,X∈{海边,猫,吉他,马拉松,沙拉,足球,游泳,瑜伽})
    ("honesty-absent", ["honesty-no-", "toolcall-count-swimming-"]),
    # 全库统计族(总数/最长/最短/全清单/画图)
    ("corpus-global", ["idleak-play-shortest-", "entity-longest-overall-",
                       "count-total-", "count-longest-", "count-shortest-",
                       "coherence-elliptical-longest-", "display-text-longest-",
                       "toolcall-table-all-videos-", "toolcall-plot-durations-"]),
    # 杂项运动族(篮球 v010 / 跳舞 v011 / 健身宽类押 v010)
    ("sports-misc", ["count-basketball-", "count-dancing-", "vision-people-",
                     "honesty-fitness-broad-", "timestamp-v010-",
                     "idleak-play-basketball-", "idleak-list-dancing-", "entity-basketball-action-"]),
    # 户外风景/轮上运动(v003+v008 共享)
    ("outdoor-wheels", ["retrieval-outdoor-scenery-", "retrieval-wheels-"]),
]

# ── 切堂政策 ────────────────────────────────────────────────────
# 封存(优化器永远看不到;最终回归门):安全/身份/泄漏 硬约束 + 补位到 ~25%
SEALED_FAMILIES = ["safety", "identity", "irrelevance", "park", "selfknow-cost"]
# 验证(只用来给候选打分排序,反思器看不到失败详情)
VAL_FAMILIES = ["sky-phases", "corpus-global", "upload", "outdoor-wheels",
                "worldb-water", "worldb-crafts"]   # B 面也要有 val 代表(无同句翻转的族才可出 train)
# 其余全进训练(反思器可读失败详情):wingsuit / skydive-list / cooking / winter /
# fall-skate / makeup-hair / honesty-absent / sports-misc / websearch


def family_of(task_id: str, task: "dict | None" = None) -> "str | None":
    if task is not None and task.get("family"):
        return task["family"]                       # GD-2b:生成器出的题自带家族标签,最优先
    for fam, pats in FAMILY_RULES:
        for p in pats:
            if task_id == p or task_id.startswith(p):
                return fam
    return None


# GD-2b 生成族切堂:老实体的翻转族(与既有手写题近义)锁 train;新实体翻转族与
# gen-* 族按【家族名稳定哈希】确定性分堂,配比 train35/val40/sealed25 ——
# 故意向 val 倾斜:val 是 GEPA 的 Pareto 记分板,题多才有分辨率
# (30 题 ≈ ±10pp 噪声带 → 100+ 题 ≈ ±5pp);可重跑同结果,
# val/sealed 里有大量翻转探针(最好的防应试题)。
_FLIP_TRAIN_LOCK = {"flip-skydiving", "flip-skiing", "flip-mascara",
                    "flip-cat", "flip-swimming", "flip-surfing"}


def _gen_split(fam: str) -> str:
    if fam in _FLIP_TRAIN_LOCK:
        return "train"
    import hashlib
    h = int(hashlib.md5(fam.encode()).hexdigest(), 16) % 100
    return "train" if h < 35 else ("val" if h < 75 else "sealed")


def build_manifest(tasks: list[dict]) -> dict:
    fams: dict = {}
    entries: dict = {}
    for t in tasks:
        fam = family_of(t["id"], t)
        if fam is None:
            raise SystemExit(f"题 {t['id']} 不匹配任何家族规则 —— 在 FAMILY_RULES 里归族后重跑")
        if t.get("family"):                          # 生成族走确定性哈希分堂
            split = _gen_split(fam)
        else:
            split = ("sealed" if fam in SEALED_FAMILIES
                     else "val" if fam in VAL_FAMILIES else "train")
        entries[t["id"]] = {"family": fam, "split": split}
        fams.setdefault(fam, []).append(t["id"])
    return {
        "_readme": ("GD-1 切分清单(由 evals/split_tool.py 生成,人工过目后提交)。"
                    "规则:家族整体入同一堂,防金标事实泄漏;安全/身份类强制封存。"
                    "GEPA:反思器只读 train 失败;候选按 val 选优;全量(含 sealed+必过)是最终回归门。"),
        "splits": {tid: e["split"] for tid, e in sorted(entries.items())},
        "families": {tid: e["family"] for tid, e in sorted(entries.items())},
        "family_members": {f: sorted(ids) for f, ids in sorted(fams.items())},
    }


def main() -> int:
    from evals.runner import load_tasks
    tasks = load_tasks(os.path.join(_HERE, "tasks"))
    m = build_manifest(tasks)
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    from collections import Counter
    dist = Counter(m["splits"].values())
    pin = {t["id"]: t.get("pinned", False) for t in tasks}
    pin_dist = Counter(m["splits"][tid] for tid, p in pin.items() if p)
    print(f"已写 {MANIFEST_PATH}")
    print(f"分布:{dict(dist)}(共 {len(m['splits'])} 题)")
    print(f"必过题分布:{dict(pin_dist)}")
    print(f"家族数:{len(m['family_members'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
