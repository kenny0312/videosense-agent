"""评测里的「看画面」必须走完生产那条路 —— 假的只许是"模型看见了什么"。

## 这条测试为什么存在

以前假件挡在 executor 外面:见 `analyze_video` 就直接回一个写好的信封,
`_do` 到感知层之间那一整段生产逻辑全被跳过。代价是四样东西同时失真,
而且四样都【没有任何症状】,只会让评测的分数悄悄偏掉:

  ① `MAX_VIDEOS_PER_REQUEST` 配额闸没挂 —— 实测调 15 次 0 次被拦、计数器恒 0,
     生产第 13 次就拒。评测里可以无限白嫖分析,是抬分。
  ② 结果里【没有 video_id】—— 生产是 `{"video_id": vid, **dump}`,假件回的是 `[env]`。
     一步并行看 5 个视频时,评测的大脑分不清哪条对应哪个视频,是压分。
  ③ `evidence_ts` 生产是单个 float 或 None,假件给的是空列表 —— 类型都不对。
  ④ 子 agent 拿到的是 `_make_executor` 的【内层】闭包,外面包的那层它看不见 ——
     于是子 agent 的 analyze 会绕过假件去打真感知层。

现在假件下沉到 `perception.analyze_video_contextual._gemini_generate`
(那个注入点是它自己文档里写明留给离线用的),上面全部照跑,四样自动全对。

## 全程离线

唯一被替换的是"发给 Gemini 的那一次调用"。gcs_uri 解析走假库、缓存是进程内的、
语义索引写入被 `EVAL_READ_ONLY` 挡下 —— 没有一处出网。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture()
def installed(monkeypatch):
    """装好假世界,并在用完后把感知层那个模块属性还原。

    `EvalBackend.install()` 是直接改模块属性的(不走 monkeypatch),而它现在会动
    `perception` 里的东西 —— 不还原的话,后面的用例会拿到一个被替换过的感知层,
    那正是本仓最恨的那种"顺序相关"。
    """
    from perception import analyze_video_contextual as avc
    from pipeline import analyze_cache
    from evals.world import EvalBackend

    real_generate = avc._gemini_generate
    monkeypatch.setenv("EVAL_READ_ONLY", "1")
    # 生产的 L1 analyze 缓存现在【真的在这条路上】(这正是下沉换来的保真度之一),
    # 于是同 video_id + 同问题的第二次调用会命中缓存、根本不进 _gemini_generate。
    # 用例之间不清就是顺序相关:先写的那条用例喂暖了缓存,后面那条测的是缓存不是接线。
    # (这不是假设 —— 本文件第二条用例第一次写出来时就栽在这上面,拿到的是上一条的结果。)
    analyze_cache.clear()
    backend = EvalBackend("fidelity-probe", world="A").install()
    yield backend
    avc._gemini_generate = real_generate
    analyze_cache.clear()


def _executor(owner: str = "fidelity-probe"):
    from pipeline import loop_driver as ld
    from pipeline.agentops.trace import Trace
    from pipeline.agentops.treeguard import TreeGuard
    from sandbox.client import SandboxClient

    tr = Trace(quiet=True)
    return ld._make_executor(SandboxClient(), tr, {}, None, owner=owner,
                             guard=TreeGuard(trace=tr))


def _analyze(ex, i: int, vid: str):
    return ex(f"c{i}", "analyze_video", {"video_id": vid, "question": f"第{i}问 画面里有什么"},
              {}, [])


def test_the_fake_sits_at_the_perception_layer_not_above_the_executor(installed):
    """装完之后被换掉的必须是感知层那一次调用,而不是 executor。"""
    from perception import analyze_video_contextual as avc

    assert avc._gemini_generate.__name__ == "_fake_gemini_generate", (
        "假件没装到感知层 —— 那配额闸/结果形状/子 agent 三条又会一起失真")


def test_analyze_goes_through_the_real_quota_gate(installed):
    """生产的 12 次上限必须真的拦人。

    这是长程评测最该量的东西之一(资源纪律),以前它在评测里根本没挂。
    """
    from pipeline import config

    cap = int(config.MAX_VIDEOS_PER_REQUEST)
    assert cap >= 2, "配额上限被调成了 0/1,这条测试就没在验它想验的东西"
    ex = _executor()
    vids = ["sky01", "sky02", "sky03", "sky04", "v006", "v007", "v009",
            "v002", "v011", "v001", "sky01", "sky02", "sky03", "v006"]
    assert len(vids) > cap, "样本数没超过上限,拦不拦得住测不出来"

    blocked = sum(1 for i, v in enumerate(vids)
                  if "上限" in (str(_analyze(ex, i, v).value) or ""))
    assert blocked == len(vids) - cap, f"该拦 {len(vids) - cap} 次,实际拦了 {blocked} 次"
    assert ex.analyze_quota["analyzed"] == cap, (
        f"配额计数器停在 {ex.analyze_quota} —— 计数没走生产那条路")


def test_result_has_the_production_shape(installed):
    """形状照生产:字典、video_id 打头、evidence_ts 是 float 或 None(不是列表)。"""
    ex = _executor()
    r = _analyze(ex, 0, "sky01")
    assert r.ok, f"假世界里 analyze 应该成功:{r.stderr}"
    assert isinstance(r.value, dict), (
        f"结果是 {type(r.value).__name__},生产是 dict —— 下游 subagents._salvage_analyses "
        "第一句就是 isinstance(v, dict),列表会被整批丢掉")
    assert list(r.value)[0] == "video_id", (
        "video_id 不在第一个键 —— 生产特意把它放最前,好让 preview 露出「哪个视频 + 结论」")
    assert r.value["video_id"] == "sky01"
    assert "sky01" in str(r.preview), "预览里看不到 video_id,大脑就分不清哪条对应哪个视频"


def test_the_production_envelope_validator_is_in_the_path(installed, monkeypatch):
    """假件吐的东西必须经过生产那套字段矫正,不是原样递给大脑。

    这条是补上来的:原来我写的是"断言 evidence_ts 是 float 或 None",变异验证时
    把假件改回吐空列表 `[]`,测试【照样全绿】—— 因为 `AnalyzeResult._coerce_ts`
    在生产侧已经把它矫正掉了。也就是说那句断言分辨不出"矫正器在不在路上",
    它验的是矫正器的功劳,不是接线的功劳。

    真正该钉的是【矫正器确实在这条路上】。所以这里让假件吐三样都不合规的:
    枚举写错、置信度越界、时间戳给列表 —— 三样都必须被生产矫正过来。
    矫正器哪天从评测这条路上掉出去了(比如又有人在上层短路),这条立刻红。
    """
    import json as _json

    from perception import analyze_video_contextual as avc

    monkeypatch.setattr(avc, "_gemini_generate", lambda *a, **k: _json.dumps(
        {"answer": "看到了", "enough": "MAYBE", "confidence": 9.9, "evidence_ts": []},
        ensure_ascii=False))
    v = _analyze(_executor(), 0, "sky01").value
    assert v["enough"] == "no", f"非法枚举没被矫正成保守值,拿到 {v['enough']!r}"
    assert v["confidence"] == 1.0, f"越界置信度没被夹紧,拿到 {v['confidence']!r}"
    assert v["evidence_ts"] is None, f"列表型时间戳没被矫正,拿到 {v['evidence_ts']!r}"


def test_video_uploaded_mid_run_is_still_analyzable(installed):
    """跑到一半上传的视频也要能看 —— 所以 video_id 是【现查】假库,不是开场缓存一张表。"""
    installed.upload("up_midrun", title="跑中上传", activities=["yoga"], duration=30.0)
    r = _analyze(_executor(), 0, "up_midrun")
    assert r.ok and r.value["video_id"] == "up_midrun", (
        f"跑中新增的视频看不了({r.stderr})—— 反查表被缓存死了?")


def test_nothing_wraps_the_executor_on_the_way_to_run_loop():
    """静态锁:两条评测车道交给 run_loop 的必须是 `_make_executor` 的返回值【本身】。

    包一层的代价不是理论问题:函数属性不跟着包装走,而 `_make_executor` 往闭包上挂了
    `tree_guard` / `tree_nodes` / `analyze_quota`,`run_loop` 的 C4 余额回灌读的正是
    `getattr(execute, "analyze_quota", None)` —— 包一层它就恒为 None,
    整段"你还剩几个配额、这次请求花了多少钱"从来没进过评测的 prompt。
    """
    for path in ("evals/world.py", "evals/session.py"):
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and (n.func.attr if isinstance(n.func, ast.Attribute)
                      else getattr(n.func, "id", "")) == "run_loop"]
        assert calls, f"{path} 里没找到 run_loop(...) —— 这条测试的前提要重看"
        for c in calls:
            assert len(c.args) >= 3, f"{path}:run_loop 的前三个参数是位置传的,形状变了"
            third = c.args[2]
            assert isinstance(third, ast.Name), (
                f"{path}:交给 run_loop 的 executor 是 {type(third).__name__} 而不是一个裸变量 —— "
                "中间又包了一层?那 analyze_quota 会丢,余额回灌在评测里就死了。"
                "要包请先读 evals/world.py 里 wrap_execute 那段墓碑注释。")
