"""P0-7:长程引擎 gate 实验题库(E-3 B2)构建脚本。

产出两个【冻结】文件(dev/holdout 分开存,红队 C7:防 C 臂对题库过拟合直进裁决):
  evals/longhorizon_bank.dev.json      6 题 + 1 弃权探针(调试/组件验收用)
  evals/longhorizon_bank.holdout.json  12 题 + 1 弃权探针(只在三臂主跑时打开)

配方(docs/longhorizon-multiagent-plan.md §3.2,红队修正 C1/C4/C5/C7 已入):
  · 只选动作行命中 3–10 的谓词(gold ≤10 < MAX_VIDEOS_PER_REQUEST=12,不被配额天花板压扁);
  · 【排除类目标签谓词】:categories.label 与 video_facts.predicate 共用命名空间,
    归类行(rationale 前缀 "category:")会虚高命中数、且"找滑板视频"会歧义到类目 —— 选题只用
    纯动作谓词,gold 计算一律 AND rationale NOT LIKE 'category:%%';
  · T1(9):全库找 X → video_id 集合 + 按大类归类 + 时间线(gold = SQL 直连算出,本脚本产);
  · T2(9):找出所有含 X 的视频 + 每条内时间段 + 画面证据。防 SQL 捷径:现库几乎所有命中
    都带 ts(与任务书假设不符,实测),落成【实验快照对 T2 谓词做 ts 掩码】+ gold 库外预标
    (pro 双模型交叉,红队 C4;预标是 live 步骤,本脚本只留 pending 位);
  · 2 空集探针单列(红队 C5,考弃权):golf / snorkeling —— 实测 predicate 与 rationale
    双零命中(文档原例"水下潜水"库里有 scuba 命中 2,不合格,已换);
  · 半数题面同义改写(不直译谓词,考语义映射)。

跑法(只读生产库;凭证走 pipeline.config 的 .env 装载):
  PYTHONUTF8=1 python evals/longhorizon_bank_build.py
重跑校验:输出应与已冻结文件一致(环境冻结期内);不一致 = 库漂移,主跑前必须重冻结。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ── 选题(谓词 → 题面;rephrased = 题面为同义改写,不含谓词直译)──────────────
_T1_SUFFIX = ",并给每条标注它属于哪个大类"
T1 = [
    ("swimming",        "找出库里所有出现游泳的视频" + _T1_SUFFIX,                              False),
    ("driving car",     "找出库里所有有人开车的视频" + _T1_SUFFIX,                              False),
    ("playing drums",   "找出库里所有有人打鼓的视频" + _T1_SUFFIX,                              False),
    ("riding horse",    "找出库里所有有人骑马的视频" + _T1_SUFFIX,                              False),
    ("throwing ball",   "找出库里所有有人扔球的视频" + _T1_SUFFIX,                              False),
    ("cheering",        "找出库里所有观众在为场上的人助威呐喊的视频" + _T1_SUFFIX,              True),
    ("hitting piñata",  "找出库里所有在打那种聚会上装满糖果的彩色纸糊玩偶的视频" + _T1_SUFFIX,  True),
    ("kayaking",        "找出库里所有有人划着单人小艇在水面前进的视频" + _T1_SUFFIX,            True),
    ("shoveling snow",  "找出库里所有有人在清理地上积雪的视频" + _T1_SUFFIX,                    True),
]
T2 = [
    ("falling",               "找出所有出现摔倒的视频,并给出每条里摔倒发生的时间段和画面证据",          False),
    ("celebrating",           "找出所有出现庆祝场面的视频,并给出每条里庆祝的时间段和画面证据",          False),
    ("playing water polo",    "找出所有有人打水球的视频,并给出每条里打水球的时间段和画面证据",          False),
    ("playing tennis",        "找出所有有人打网球的视频,并给出每条里打网球的时间段和画面证据",          False),
    ("diving",                "找出所有有人跳水的视频,并给出每条里跳水的时间段和画面证据",              False),
    ("playing dodgeball",     "找出所有在玩「躲避飞来的球」的团队游戏的视频,并给出时间段和画面证据",    True),
    ("performing gymnastics", "找出所有有人在垫上或器械上做翻腾平衡动作的视频,并给出时间段和画面证据",  True),
    ("rock climbing",         "找出所有有人徒手或借助绳索攀爬岩壁的视频,并给出时间段和画面证据",        True),
    ("dribbling basketball",  "找出所有有人拍着球运球移动的视频,并给出时间段和画面证据",                True),
]
PROBES = [  # 空集探针(单列,考弃权;负空间已实测双零)
    ("snorkeling", "找出库里所有浮潜的视频", "dev",
     "有游泳/皮划艇/水球等大量水上内容作相似诱惑,但浮潜为零"),
    ("golf",       "找出库里所有打高尔夫的视频", "holdout",
     "有网球/羽毛球/乒乓球等大量球类内容作相似诱惑,但高尔夫为零"),
]
DEV_T1 = {"swimming", "hitting piñata", "kayaking"}
DEV_T2 = {"falling", "playing dodgeball", "dribbling basketball"}

ACTION_FILTER = "AND rationale NOT LIKE 'category:%%'"     # 排除归类行(与动作行同名时防虚高)


def build():
    from pipeline import config                              # .env 装载副作用(只读凭证)
    from pipeline import semantic_index as si

    cats = {r[0] for r in si._execute("SELECT label FROM categories", ())}
    vocab = sorted(cats)
    for pred, _q, _r in T1 + T2:
        assert pred not in cats, f"选题错误:{pred} 是类目标签,动作/类目歧义,换题"

    def gold_for(pred: str) -> dict:
        # 【无排序轴】:任务书原案"时间线"不可判(511/514 同秒批量入库);换"时长排序"后
        # review 实测 496/514 条 duration_sec 为 NULL → gold 全退化成 id 字典序(真排时长
        # 反被扣分)。两个候选轴都死 → 整轴砍掉,T1 只判 集合 + 归类(scorer 同步)。
        rows = si._execute(
            f"""SELECT vf.video_id,
                       array_agg(DISTINCT cf.predicate) FILTER (WHERE cf.predicate IS NOT NULL)
                FROM video_facts vf
                LEFT JOIN video_facts cf ON cf.video_id = vf.video_id AND cf.matched
                     AND cf.rationale LIKE 'category:%%'
                WHERE vf.predicate = %s AND vf.matched {ACTION_FILTER.replace('rationale', 'vf.rationale')}
                GROUP BY vf.video_id ORDER BY vf.video_id""", (pred,))
        vids = [r[0] for r in rows]
        assert 3 <= len(vids) <= 10, f"{pred}:动作行命中 {len(vids)} 不在 3-10(选题过时,重选)"
        return {
            "video_ids": sorted(vids),
            "count": len(vids),
            "per_video": {r[0]: {"categories": sorted(r[1] or [])} for r in rows},
        }

    def spans_available(pred: str) -> int:
        r = si._execute(
            f"""SELECT COUNT(*) FROM video_facts WHERE predicate = %s AND matched
                AND start_ts IS NOT NULL {ACTION_FILTER}""", (pred,))
        return int(r[0][0])

    def probe_verified(term: str) -> bool:
        a = si._execute("SELECT COUNT(*) FROM video_facts WHERE matched AND predicate ILIKE %s",
                        (f"%{term}%",))
        b = si._execute("SELECT COUNT(*) FROM video_facts WHERE matched AND rationale ILIKE %s",
                        (f"%{term}%",))
        return int(a[0][0]) == 0 and int(b[0][0]) == 0

    dev, holdout = [], []
    for pred, q, reph in T1:
        item = {"id": f"t1-{pred.replace(' ', '-')}", "tier": "T1", "predicate": pred,
                "question": q, "rephrased": reph, "gold": gold_for(pred)}
        (dev if pred in DEV_T1 else holdout).append(item)
    for pred, q, reph in T2:
        item = {"id": f"t2-{pred.replace(' ', '-')}", "tier": "T2", "predicate": pred,
                "question": q, "rephrased": reph, "gold": gold_for(pred),
                # 防 SQL 捷径:实验快照把这些谓词的 start_ts/end_ts 置 NULL(现库几乎全带 ts,
                # 任务书"选无 ts 实例"在实测数据上不可行 —— 掩码等效且更强)。
                "ts_mask": True, "db_spans_available": spans_available(pred),
                # T2 定位 gold 库外预标(pro 双模型交叉 + 低置信只进集合判分,红队 C4)——
                # live 步骤,等预算批准;在此之前定位分不可算(scorer 会拒算而不是给 0)。
                "gold_localization": {"status": "pending_prelabel", "spans": {}}}
        (dev if pred in DEV_T2 else holdout).append(item)
    for term, q, split, trap in PROBES:
        assert probe_verified(term), f"探针 {term} 不再是空集(库漂移),重选"
        item = {"id": f"probe-{term}", "tier": "PROBE", "predicate": term, "question": q,
                "rephrased": False, "trap": trap,
                "gold": {"video_ids": [], "count": 0, "per_video": {}}}
        (dev if split == "dev" else holdout).append(item)

    # ── 冻结纪律(review 确认旧版无牙:重跑静默覆盖、无时间戳、无哈希)──
    # 默认【校验模式】:与已冻结文件的 items 内容哈希比对,不一致 → 报错退出(库漂移必须
    # 被看见,不许静默烙进冻结文件);要重冻结必须显式 --refreeze。
    refreeze = "--refreeze" in sys.argv
    out = Path(__file__).resolve().parent
    import datetime
    import hashlib
    for name, items in (("dev", dev), ("holdout", holdout)):
        content_sha = hashlib.sha256(
            json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        meta = {"frozen_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "items_sha256": content_sha, "category_vocab": vocab,
                "answer_contract": {"video_ids": "list[str]", "count": "int",
                                    "per_video": "{vid: {category, start_ts, end_ts, evidence}}"},
                "notes": "环境冻结要求:关闭 use-to-grow(_index_analyze_result)、DB 快照冻结、"
                         "T2 谓词 ts 掩码、analyze 缓存按 arm×rep 命名空间(红队 C3)。"}
        p = out / f"longhorizon_bank.{name}.json"
        if p.exists() and not refreeze:
            old = json.loads(p.read_text(encoding="utf-8"))
            old_sha = (old.get("meta") or {}).get("items_sha256")
            if old_sha == content_sha:
                print(f"{p.name}: 校验通过(items 哈希一致,冻结未动)")
                continue
            raise SystemExit(f"{p.name}: 【库漂移】当前库产出的 items 与冻结文件不一致"
                             f"(冻结 {old_sha and old_sha[:12]} vs 现算 {content_sha[:12]})。"
                             "主跑前必须人工裁决:要么回滚库,要么显式 --refreeze 重冻结。")
        p.write_text(json.dumps({"meta": meta, "split": name, "items": items},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{p.name}: 已冻结 {len(items)} 条"
              f"(T1={sum(1 for i in items if i['tier'] == 'T1')}"
              f" T2={sum(1 for i in items if i['tier'] == 'T2')}"
              f" PROBE={sum(1 for i in items if i['tier'] == 'PROBE')})"
              f" sha={content_sha[:12]}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    build()
