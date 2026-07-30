"""P0-3:per-tree(=per-request)美元熔断 + 墙钟闸。

治的病:一次请求里一棵 agent 树把钱烧穿。DVD 复现实测 Trap 税 = 12× 成本方差
(中位 $0.089 / 最深坑 $1.10),而 RL_* 的日顶/会话顶是【跨请求事后】记账,拦不住
请求内的单次爆炸。

两处挂点,缺一不可(红队 B1):
  ① 工具执行前(loop_driver._make_executor 的 execute 包装)—— 拦住昂贵工具;
  ② 主循环每步 generate 前(loop_driver.run_loop)—— 拦住"进入 Trap 循环只思考
     不调工具"的烧钱;3.5-flash 输出价 $9/M,思考 token 尤其疼。
只挂 ① 的熔断在"模型不再调工具、光反复思考+说话"时永远看不见。

并发正确性(红队 B3,review 变异验证后重修):钱是调用【结束后】才经 add_usage 落账的,
check 到落账之间的"在飞"窗口里,K 个并行 worker 互相看不见对方的钱 —— 光加锁只能保证
trip 一次,防不住 K 个 under-cap 检查全放行(超冲 = K × 真实单次成本)。所以放行必须
【预留】:admit() 放行时把保守估价计入 _pending,settle() 在调用完成后释放(实测已落账);
判据 = spent + pending + 本次估价 > cap。

触发后是【软收口】不是 kill:回一段信封教大脑就已有证据作答、没查到的写【未核查】。
线程不杀 —— 半途 kill 会把已经花掉的钱变成零产出(wasted 全额),而软收口至少交货。

设计依据:docs/longhorizon-master-plan.md Part T/Part 0;
成本口径来自 pipeline.agentops.usage.summarize()(P0-1 已修全口径:含思考与工具用提示 token)。
"""
from __future__ import annotations

import threading
import time

from pipeline import config
from pipeline.agentops import usage

# 用户可见的记账行前缀。subagents 靠它把回流父脑的子 agent 答案里的记账行剥掉
# (主脑不该把系统记账话术当"证据"综合进最终答案,review 确认)。
GUARD_NOTE_PREFIX = "\n\n(系统) 本次触发成本护栏"


class TreeGuard:
    """一次请求(一棵树)共享一个实例。子 agent 复用父 execute 闭包 → 天然同一个 guard。

    契约:两个挂点用 admit()/settle() 配对(放行即预留,完成即释放);
    交付点(收口/硬终止)用 reconcile() 补记账 + final_note() 现算披露文案 ——
    文案必须在交付时现算,触闸瞬间的快照会把宽限期烧的钱漏在披露外(review 确认)。
    """

    def __init__(self, cost_cap: float | None = None, wall_cap_s: float | None = None,
                 call_estimate: float | None = None, trace=None):
        self.cost_cap = config.MAX_TREE_COST_USD if cost_cap is None else cost_cap
        self.wall_cap_s = config.MAX_TREE_WALL_S if wall_cap_s is None else wall_cap_s
        self.call_estimate = (config.TREE_CALL_ESTIMATE_USD if call_estimate is None
                              else call_estimate)
        self.t0 = time.monotonic()
        self.tripped: str | None = None       # None | "budget" | "wall"
        self.trip_detail = ""
        self._pending = 0.0                   # 在飞预留:admit 放行累加,settle 释放(红队 B3)
        self._cost_at_trip = 0.0
        self._hwm = 0.0                       # 花费高水位:见 spent() —— 只记得钱,不忘钱
        self._envelope_fed = False            # 信封是否真喂过大脑(决定"已标注【未核查】"能不能说)
        self._lock = threading.Lock()
        self._trace = trace
        self._logged = False

    # ── 状态 ──
    @property
    def enabled(self) -> bool:
        return self.cost_cap > 0 or self.wall_cap_s > 0

    def spent(self) -> float:
        """当前全树累计【已落账】花费(全口径:P0-1 修过的 summarize 含思考/工具用提示 token)。

        取"实测值与高水位的较大者"并抬高水位 —— usage 是 contextvar,**裸线程拿不到父
        上下文**(run_fanout 用 copy_context 所以正常,但任何忘了 copy 的调用路径会读到 $0,
        闸当即失明)。高水位让 guard 只会记得钱、不会忘记钱:失明的线程至多不推进账目,
        绝不会把已花的钱抹掉放行。自测逮出的真 bug。
        """
        try:
            measured = float(usage.summarize().get("cost_usd") or 0.0)
        except Exception:                     # 观测绝不拖垮请求
            measured = 0.0
        if measured > self._hwm:
            self._hwm = measured
        return self._hwm

    @property
    def wasted_usd(self) -> float:
        """触闸后仍发生的花费(浪费不许隐形)。【现算】而非在 check 里推进 ——
        触闸后模型只 generate 不调工具(最常见的宽限形态)时没人再调闸,
        存量字段会永远停在 0(review 确认:披露系统性错报 $0.0000)。"""
        if not self.tripped:
            return 0.0
        return max(0.0, self.spent() - self._cost_at_trip)

    def elapsed_s(self) -> float:
        return time.monotonic() - self.t0

    def snapshot(self) -> dict:
        """给 trace/审计用的一行状态。"""
        s = self.spent()
        return {"spent_usd": round(s, 6), "cost_cap": self.cost_cap,
                "elapsed_s": round(self.elapsed_s(), 1), "wall_cap_s": self.wall_cap_s,
                "tripped": self.tripped, "wasted_usd": round(self.wasted_usd, 6)}

    # ── 核心:预估后比 + 在飞预留 ──
    def admit(self, *, estimate: float | None = None, what: str = "") -> str | None:
        """闸门检查+预留。返回 None = 放行(已把 estimate 计入在飞账,调用方【必须】在
        finally 里 settle(同一 estimate));返回软失败信封文本 = 已触闸,调用方别执行。

        判据是"预估后比":spent + 在飞预留 + 本次估价 > cap 即拦。事后比会让最后一次调用
        总能越线;没有预留则 K 个并行调用在钱落账前全放行(超冲 K×,review 变异验证)。
        """
        if not self.enabled:
            return None
        est = self.call_estimate if estimate is None else estimate
        with self._lock:
            if self.tripped:                                    # 已触闸:后续一律拦
                return self._envelope()
            if self.wall_cap_s > 0 and self.elapsed_s() > self.wall_cap_s:
                self._trip("wall", f"已跑 {self.elapsed_s():.0f}s > 墙钟 {self.wall_cap_s:.0f}s")
                return self._envelope()
            if self.cost_cap > 0:
                s = self.spent()
                if s + self._pending + est > self.cost_cap:
                    self._trip("budget",
                               f"本请求累计 ${s:.4f} + 在飞预留 ${self._pending:.4f} + "
                               f"本次预估 ${est:.4f} > 熔断线 ${self.cost_cap:.2f}"
                               f"({what or '本次调用'}【没执行】)")
                    return self._envelope()
            self._pending += est
        return None

    def settle(self, estimate: float | None = None):
        """释放 admit 的在飞预留(调用已结束,实测金额由 add_usage 落账接管)。
        必须与 admit 传【同一】estimate;放在 finally 里,异常也要释放。"""
        if not self.enabled:
            return
        est = self.call_estimate if estimate is None else estimate
        with self._lock:
            self._pending = max(0.0, self._pending - est)

    def reconcile(self, what: str = "收口"):
        """交付点补记账:只判定+记录(trace 照写),【不】产生信封、不预留。
        预算可能正好在最后一次 generate 上烧穿 —— 前置闸没看到那笔钱,这里补上,
        让 final_note 有话可说;不拦已产出的答案。"""
        if not self.enabled:
            return
        with self._lock:
            if self.tripped:
                return
            if self.wall_cap_s > 0 and self.elapsed_s() > self.wall_cap_s:
                self._trip("wall", f"已跑 {self.elapsed_s():.0f}s > 墙钟 "
                                   f"{self.wall_cap_s:.0f}s({what}时发现)")
                return
            if self.cost_cap > 0:
                s = self.spent()
                if s > self.cost_cap:
                    self._trip("budget", f"本请求累计 ${s:.4f} > 熔断线 "
                                         f"${self.cost_cap:.2f}({what}时发现)")

    # ── 内部 ──
    def _trip(self, kind: str, detail: str):
        """必须在持锁下调用。"""
        self.tripped = kind
        self.trip_detail = detail
        self._cost_at_trip = self.spent()
        if self._trace is not None and not self._logged:
            self._logged = True
            try:
                cause = "GUARD_BUDGET" if kind == "budget" else "GUARD_WALL"
                st = self._trace.step(f"tree guard tripped ({kind})", component="guard")
                st.bill(cost_usd=0.0)
                # snapshot 是【触闸时刻】的快照(wasted 此刻恒 0);交付时的终值见 final_note。
                st.soft(cause, error=detail[:160], **self.snapshot())
            except Exception:
                pass

    def grace_envelope(self) -> str:
        """触闸后给【尚未见过信封的 conversation】补喂收口指令。触闸可能发生在子 agent
        或工具闸里 —— 主 loop 的对话从没收到"标【未核查】"的指令,final_note 的
        "已在上文标注"声称就会变假(review 确认:跨会话假陈述)。"""
        return self._envelope() if self.tripped else ""

    def _envelope(self) -> str:
        """软失败信封:教大脑收口 + 强制弃权标注。文案是护栏的一部分,不是提示语润色。"""
        self._envelope_fed = True
        return (f"[系统·成本护栏] {self.trip_detail}。"
                "请【立刻基于已经拿到的证据收口作答】:不要再调用任何工具;"
                "已核实的部分正常给结论并引用证据;没查到/没来得及查的部分必须明确写"
                "【未核查】,不要用推测填补,也不要假装分析过没分析的视频。")

    def final_note(self, *, mark_claim: bool | None = None) -> str:
        """触闸时附在答案末尾的一行(对用户透明:为什么停、浪费了多少)。

        【交付点现算】:spent/wasted 都取当下值 —— 触闸瞬间的快照会把宽限期烧的钱漏掉。
        mark_claim:是否声称"未核查的部分已在上文标注" —— 只有信封确实喂过大脑、且答案
        是模型写的(非硬终止占位文案)时才为真;收口处首次发现超支(模型从没见过信封)
        或硬终止(答案不是模型写的)时说这话就是假陈述(review 确认)。默认跟随
        _envelope_fed;硬终止路径显式传 False。
        """
        if not self.tripped:
            return ""
        s = self.snapshot()
        if self.tripped == "wall":
            line = (f"已运行 {s['elapsed_s']:.0f}s / 墙钟上限 {self.wall_cap_s:.0f}s"
                    f"(期间花费 ${s['spent_usd']:.4f})")
        else:
            line = f"累计 ${s['spent_usd']:.4f} / 上限 ${self.cost_cap:.2f}"
        claim = self._envelope_fed if mark_claim is None else mark_claim
        tail = ";未核查的部分已在上文标注。" if claim else "。"
        return (f"{GUARD_NOTE_PREFIX}({self.tripped}):{line},"
                f"触闸后额外发生 ${s['wasted_usd']:.4f}{tail}")
