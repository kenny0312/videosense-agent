"""B0-2a:评测跑不许写生产库。

为什么需要:`evals/longhorizon_run.py` 只隔离了 `ANALYZE_CACHE_NS`,语义索引这一路
零隔离 —— `node_executor._index_analyze_result` 用同一组生产凭据 UPSERT 进
`content_embeddings`。实测后果:**1256 行评测残留留在一个总共 514 条视频的生产库里
(占 18.6%)**,用户做 semantic_search 会命中评测垃圾,其中还有"No, there is no one
climbing a rock wall"这种否定结论 —— 作为"证据"命中一条内容完全无关的视频。

## 为什么是 raise 而不是 `if EVAL_READ_ONLY: return`

任务书 §7 的原话:「代码级 `raise` 而非 fail-open 跳过(fail-open 会让『没写成』和
『没触发』不可区分)」。这条理由是对的:静默 return 之后,`content_embeddings` 没长
既可能是"闸挡住了",也可能是"那条路根本没跑到" —— 而后者意味着闸是摆设。

## 但字面照做会自相矛盾,所以这么处理

`_index_analyze_result` 是**旁路写钩子**(analyze 出结果顺手入索引)。让它的异常一路
抛上去,会把【已经花钱买到】的 analyze 结果一起弄丢 —— 那正是 A4 刚修掉的病。

所以:**抛 `EvalWriteBlocked`,由旁路的调用点专门接住并计数**。
可区分性由计数器保证(`blocked_count() > 0` 证明那条路确实跑到了、确实被挡了),
而不是靠让整次请求崩掉。非旁路的写(update_memory 是用户显式要求的工具调用)
则让它照常抛到工具层 —— 那里失败是正确的,大脑会看到"这一轮不能写记忆"。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_blocked: dict[str, int] = {}


class EvalWriteBlocked(RuntimeError):
    """EVAL_READ_ONLY=1 时,某条写生产库的路径被挡下了。

    是个专用类型,不是裸 RuntimeError —— 旁路调用点要能【只】接住它,
    而不是顺手把真实的写库故障也一起吞掉(那就又回到静默失败了)。
    """


def assert_writes_allowed(what: str) -> None:
    """写生产库之前调一次。EVAL_READ_ONLY 关(默认)时是空操作。

    EVAL_READ_ONLY_ALLOW(逗号分隔的 what 名单)= 显式豁免。闸挡的是【写生产库】,
    不是"写"这个动作本身 —— 评测假世界把 user_memory 换成了 world_state 替身
    (evals/world.py install),那条路物理上到不了生产,再拦它就是拦错了对象:
    多轮基线实测 dualcontrol-memory-wingsuit-only-26 记忆全轴满分、唯独
    state_assertions 0 —— agent 干对了,是闸把替身写入拦了。豁免必须由装了替身
    的那一方显式声明(install() 设 env),不许默认;索引两路(真打生产 pg)照拦。
    """
    from pipeline import config
    if not getattr(config, "EVAL_READ_ONLY", False):
        return
    import os
    allowed = {x.strip() for x in os.environ.get("EVAL_READ_ONLY_ALLOW", "").split(",")
               if x.strip()}
    if what in allowed:
        return
    with _lock:
        _blocked[what] = _blocked.get(what, 0) + 1
    raise EvalWriteBlocked(
        f"EVAL_READ_ONLY=1:拒绝写生产库({what})。评测跑不得改动生产数据 —— "
        "这不是故障,是闸在工作。要跑带写入的实验,请指向独立库或把该功能关掉。")


def blocked_count(what: str | None = None) -> int:
    """被挡下过几次。**这是"闸真的挡住了"与"那条路压根没跑到"的唯一区分方式**,
    验收就靠它:评测跑完 checksum 不变【且】计数 > 0,才说明闸有效而不是摆设。"""
    with _lock:
        return _blocked.get(what, 0) if what else sum(_blocked.values())


def reset_blocked() -> None:
    """测试用:清零计数。"""
    with _lock:
        _blocked.clear()
