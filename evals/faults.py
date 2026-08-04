"""批次 3.5:故障注入 seam(合并任务书 v1.1 §9)。

## 这个模块存在的唯一理由

任务书 §13.4 把「故障注入下 Verdict 保留率 = 100%」判成**不可测**,理由一句话:
"注入设施在批次 3.5 之前不存在"。`evals/world.py:make_exec(values, fail)` 只能按
【工具名】让某次调用返回一个 `stderr="boom"` 的空壳 —— 用它测不出任何真实韧性问题,
因为生产里根本不存在"boom"这种故障。

这里建的就是那个物理前提。**Verdict** = 一次跑批里【已经花钱买到手】的东西
(成功的工具结果 + 一句不撒谎的交代)。保留 = 故障发生时,故障【之前】买到的东西
一件都没跟着丢。这条串起批次 0 的一串修复(A1 部分交付 / 残值回收 / A4 失败不伪装 /
B3 不误进 SqlFixer),seam 的价值是让它们**可回归**,而不是每次靠人记得。

## 设计红线:注入的故障必须长得跟生产一样

注入一个虚构形状的故障,测出来的是"对虚构故障的韧性",没有意义。本模块里每一种
故障形状都对着生产的**判据代码**核实过,并且各配一条测试直接断言"生产分类器认得它":

  ① 瞬时错误   → `pipeline.loop_driver._is_transient` 必须判 True(见 transient())
  ② SQL 故障   → **复用** `repl._mock_db.MockSqlError`(带 .pgcode),不另写一套;
                  上游 `node_executor` 读的就是 `getattr(e, "pgcode", None)` 这一行
  ③ 预算耗尽   → 用**真** `TreeGuard` + **真** `usage.add_usage` 落账,让它在第 N 次
                  admit 上【自己】触闸;不替换 spent()、不改 TreeGuard(那测的是替身)
  ④ 租约过期   → CAS(`taskstate.CHECKPOINT_SQL` 等)影响 **0 行**,与真 PG 在
                  lease_token 守卫不匹配时的返回逐字节同形
  ⑤ 进程杀点   → `WorkerKilled` 继承 **BaseException**:Cloud Run 回收实例时没有任何
                  `except Exception` 会跑,用普通 Exception 模拟会被 node_executor 的
                  兜底吞成软失败,那就不是"杀进程"而是"报了个错"

## 最坏的情况是"注入了却没生效"

那会让整套测试全绿而韧性为零。所以 `FaultInjector` 记账每一次真实生效
(`fired`),并提供 `verify()`:**声明了却一次都没生效的规则 → 当场炸**。
每条注入用例都该调它(见 tests/evals/test_fault_injection.py)。

不碰 `pipeline/` —— 那是被测对象。为了让注入好写而改被测代码,等于把考题改简单。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

# ══════════════════════════════════════════════════════════════════
#  一、故障形状(全部对着生产判据核实过,别自己发明)
# ══════════════════════════════════════════════════════════════════

# ── ⑤ 进程杀点 ────────────────────────────────────────────────────
class WorkerKilled(BaseException):
    """worker 当场消失(Cloud Run 实例回收 / OOM kill / SIGKILL)。

    **继承 BaseException 是本类的全部要点**:真被杀的进程不会执行任何
    `except Exception` 分支。而 `node_executor.execute_node` 和
    `subagents._run_one` 都有 `except Exception` 兜底 —— 用普通 Exception 模拟
    进程死亡,会被它们吞成一条软失败回喂大脑,测出来的是"报错处理得挺好",
    与"实例没了"毫无关系。
    """


# ── ① 瞬时错误:判据在 pipeline/loop_driver.py:_is_transient ──────────
# 那个函数两条路:
#   路 A  getattr(e, "code") 或 getattr(e.response, "status_code") ∈ {429,500,502,503,504}
#   路 B  【异常类名】小写后含 servererror / resourceexhausted / unavailable /
#         deadline / timeout / connectionerror / serviceunavailable
# 下面每个类各走一条,合起来把两条路都盖住。类名不是随便起的:改名 = 路 B 静默失效。

class _CodedError(Exception):
    """路 A-1:SDK 风格 —— 异常自带 .code(google-genai / vertexai 的形状)。"""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


class _ResponseError(Exception):
    """路 A-2:requests 风格 —— .response.status_code。

    生产里这个形状由 `loop_driver.OpenAICompatConversation._post` 亲手造出来
    (`e.response = r  # 给 _is_transient 嗅 status_code`),阶段B 的 OAI 兼容通道
    (Qwen / OpenRouter / vLLM)全走它。
    """

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.response = type("_Resp", (), {"status_code": status_code})()


class ServerError(Exception):
    """路 B:google-genai 的 5xx 包装类,靠类名被认出来。"""


class ResourceExhausted(Exception):
    """路 B:429 配额耗尽(vertexai / google-api-core 的类名)。"""


class ServiceUnavailable(Exception):
    """路 B:503。"""


class DeadlineExceeded(Exception):
    """路 B:gRPC 超时。"""


class Timeout(Exception):
    """路 B:requests.Timeout 一类。"""


_TRANSIENT_KINDS: dict[str, Callable[[], BaseException]] = {
    "429":        lambda: _CodedError("429 RESOURCE_EXHAUSTED: quota exceeded", 429),
    "500":        lambda: _CodedError("500 INTERNAL", 500),
    "502":        lambda: _ResponseError("oai-compat HTTP 502: bad gateway", 502),
    "503":        lambda: _CodedError("503 UNAVAILABLE: model overloaded", 503),
    "504":        lambda: _ResponseError("oai-compat HTTP 504: gateway timeout", 504),
    "server":     lambda: ServerError("500 INTERNAL. {'error': {'code': 500}}"),
    "quota":      lambda: ResourceExhausted("429 Quota exceeded for generate_content"),
    "unavailable": lambda: ServiceUnavailable("503 The service is currently unavailable."),
    "deadline":   lambda: DeadlineExceeded("504 Deadline Exceeded"),
    "timeout":    lambda: Timeout("Read timed out. (read timeout=180)"),
    "connection": lambda: ConnectionError("Connection aborted, RemoteDisconnected"),
}

TRANSIENT_KINDS = tuple(_TRANSIENT_KINDS)


def transient(kind: str = "429") -> BaseException:
    """造一个 `_is_transient` 判 True 的异常。kind 见 TRANSIENT_KINDS。

    **别绕过本工厂手搓异常**:随手 `RuntimeError("429 too many requests")` 两条路
    都不命中(没有 .code、类名里没有关键词),`_is_transient` 判 False,注入的
    "429" 根本不会走重试路径 —— 测出来的韧性是假的。
    tests/evals/test_fault_injection.py 里有一条测试直接断言这一点。
    """
    try:
        return _TRANSIENT_KINDS[kind]()
    except KeyError:
        raise ValueError(f"未知的瞬时错误形状 {kind!r};可选:{TRANSIENT_KINDS}") from None


def deterministic(code: int = 400) -> Exception:
    """**阴性对照**:确定性错误(400 参数非法),`_is_transient` 必须判 False。

    没有阴性对照,"transient 判 True" 这个断言可能只是因为分类器恒真 —— 那样
    重试路径会把 400 也重试三遍,白烧两次钱。
    """
    return _CodedError(f"{code} INVALID_ARGUMENT: request contains an invalid argument", code)


# ── ② SQL 故障:复用 repl/_mock_db.MockSqlError,别两边各写一套 ────────
# (任务书 §9 原话:"复用批次 1 给 _mock_db.py 加的伪造能力"。上游
#  node_executor._run_sql_query 只读 `getattr(e, "pgcode", None)` 这一行,
#  同一个异常类同时喂真库形状和假库形状,不会漂移。)
_SQL_KINDS = {
    # 不可自愈:改 SQL 只会得到另一条同样重的 SQL → "超时 → 改SQL → 再超时" 烧钱循环
    "timeout": ("57014", "canceling statement due to statement timeout"),
    "lock":    ("55P03", "could not obtain lock on relation \"video_facts\""),
    # 可自愈三兄弟:SqlFixer 重写一次就可能对
    "syntax":  ("42601", "syntax error at or near \"FORM\""),
    "column":  ("42703", "column \"activity\" does not exist"),
    "table":   ("42P01", "relation \"video_fact\" does not exist"),
    # 传输层:连 SQLSTATE 都拿不到(pgcode=None)
    "transport": (None, "server closed the connection unexpectedly"),
}

SQL_KINDS = tuple(_SQL_KINDS)


def sql_fault(kind: str = "timeout", message: str | None = None):
    """造一个 `MockSqlError`(带 .pgcode),形状与 psycopg2 在上游读的那一个字段相同。

    kind → SQLSTATE:timeout=57014 / lock=55P03 / syntax=42601 / column=42703 /
    table=42P01 / transport=None。
    """
    from repl._mock_db import MockSqlError

    try:
        code, default_msg = _SQL_KINDS[kind]
    except KeyError:
        raise ValueError(f"未知的 SQL 故障形状 {kind!r};可选:{SQL_KINDS}") from None
    return MockSqlError(message or default_msg, code)


# ── 真实事故②的原料:pro 档 max_output_tokens 把带 evidence 的 JSON 从中间截断 ──
def truncated_analyze_json() -> str:
    """复现真实事故:pro 档 `ANALYZE_MAX_OUTPUT_TOKENS` 不够,带 evidence 的 JSON
    从中间被切断,`_parse` 抛 "Unterminated string starting at line 6"。

    这不是编的:`pipeline/config.py:230` 和 `perception/analyze_video_contextual.py:171`
    两处注释都记着"实测 pro 档两次标注全部 ANALYZE_FAILED / Unterminated string"。
    """
    return (
        '{\n'
        '  "answer": "视频里有人在雪坡上滑雪,全程可见",\n'
        '  "enough": "yes",\n'
        '  "confidence": 0.9,\n'
        '  "evidence_ts": 12,\n'
        '  "evidence": "12s 处滑雪者从坡顶切入,雪板与雪面接触清晰可见,随后'
    )


def sandbox_absent_error() -> AttributeError:
    """复现真实事故①:三份 gate jsonl 共 12 条
    `AttributeError("'NoneType' object has no attribute 'execute'")`
    (见 tests/pipeline/test_sandbox_absent.py 文件头)。

    形状要害:它是从**沙箱客户端为 None** 的那一行抛出来的属性错误,不是某个
    被精心包装过的业务异常 —— 所以它会一路掀掉整个 run_loop,ledger 里已经付过
    费的 analyze/检索结果跟着一起没。这正是 Verdict 保留率要量的东西。
    """
    return AttributeError("'NoneType' object has no attribute 'execute'")


# ══════════════════════════════════════════════════════════════════
#  二、调度:什么时候注入、注入了没有
# ══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Returns:
    """效果 = 【返回】这个值(而不是抛异常)。④ 的 CAS 空结果走它。"""
    value: Any


#: ④ 租约过期回传:CAS 的 UPDATE ... WHERE lease_token=%(token)s 影响 0 行。
#: 真 PG 在租约被别人抢走时返回的就是空结果集 —— 不是异常,是"什么都没更新"。
CAS_EMPTY = Returns([])


@dataclass(frozen=True)
class Fault:
    """一条注入规则。

    effect —— 生效时干什么:
        · BaseException 实例/类 → 抛它(⑤ 的 WorkerKilled 也走这里)
        · Returns(v)            → 返回 v,不调用真实现
        · callable              → 调 effect(),用它的返回值(需要每次造新对象时用)
    where  —— 只对这个 key 计数(工具名 / "generate" / SQL 语句种类)。
              None = 对任意 key 都可生效(计数仍按 key 各自算 —— _match 里
              只有一个按当前 key 分桶的计数表,没有全局桶)。
    at     —— 在该 key 的第几次调用上生效(1 = 第一次)。
    times  —— 连续生效几次。analyze 的重试循环要连挂 RETRY_LIMIT+1 次才走到
              ANALYZE_FAILED;只挂 1 次测到的是"重试救回来了",是另一回事。
    """
    effect: Any
    where: str | None = None
    at: int = 1
    times: int = 1
    label: str = ""

    def covers(self, key: str, n: int) -> bool:
        return (self.where is None or self.where == key) and self.at <= n < self.at + self.times


class FaultNeverFired(AssertionError):
    """声明了注入规则,却一次都没生效 —— 最坏的情况(测试全绿、韧性为零)。"""


class FaultInjector:
    """故障调度器 + 审计账。

    审计账是本类的一半价值:`fired` 记下每一次真实生效,`verify()` 在
    "声明了却没生效"时当场炸。没有它,一条写错 where 的注入会让整套用例
    悄悄退化成"无故障跑批也能过"。
    """

    def __init__(self, *faults: Fault):
        self.faults: list[Fault] = list(faults)
        self.counts: dict[str, int] = {}
        self.fired: list[dict] = []
        self._hits: list[int] = [0] * len(self.faults)
        # Verdict 记账(由 wrap_exec 填):cid → 成功结果指纹
        self.bought: dict[str, tuple] = {}
        self.bought_before_fault: dict[str, tuple] | None = None
        # 计数是读-改-写,而这个注入器恰好站在全系统唯一并发的那条路上:
        # 同一步 >1 个 analyze 会进 ThreadPoolExecutor(MAX_ANALYZE_PARALLEL 默认 3)。
        # 生产自己的同类计数器(_make_executor 的 quota)就是带锁的 —— 照做。
        # 不带锁的症状:times=1 的注入在并发下偶发生效两次/零次,用例随机红绿。
        self._lock = threading.Lock()

    # ── 调度 ──
    def _match(self, key: str) -> "Fault | None":
        with self._lock:
            n = self.counts[key] = self.counts.get(key, 0) + 1
            for i, f in enumerate(self.faults):
                if f.covers(key, n):
                    self._hits[i] += 1
                    self.fired.append({"key": key, "nth": n, "label": f.label or key})
                    if self.bought_before_fault is None:
                        self.bought_before_fault = dict(self.bought)
                    return f
        return None

    def mark(self, label: str) -> None:
        """手工登记"故障在此刻发生了"。

        给**不经 apply() 的注入**用 —— ③ 预算触闸和 ④ 租约易主都不是"某次调用被换掉",
        而是被测系统自己在某一刻改变了状态。这里同样会把此刻的 `bought` 快照成
        `bought_before_fault`,让 Verdict 保留率的分母对所有五种注入是同一把尺子。
        """
        with self._lock:
            self.fired.append({"key": label, "nth": self.counts.get(label, 0) + 1,
                               "label": label, "manual": True})
            self.counts[label] = self.counts.get(label, 0) + 1
            if self.bought_before_fault is None:
                self.bought_before_fault = dict(self.bought)

    def apply(self, key: str, call: Callable[[], Any]) -> Any:
        """命中 → 按 effect 抛/返回;没命中 → call()(真实现)。"""
        f = self._match(key)
        if f is None:
            return call()
        eff = f.effect
        if isinstance(eff, Returns):
            return eff.value
        if isinstance(eff, type) and issubclass(eff, BaseException):
            raise eff()
        if isinstance(eff, BaseException):
            raise eff
        if callable(eff):
            return eff()
        return eff

    # ── 审计 ──
    @property
    def fired_count(self) -> int:
        return len(self.fired)

    def unfired(self) -> list[Fault]:
        return [f for f, h in zip(self.faults, self._hits) if h == 0]

    def verify(self) -> "FaultInjector":
        """声明了却一次都没生效 → 炸。每条注入用例都该调它。

        为什么是硬错误而不是警告:注入没生效的用例会【绿】,而它证明的东西是零。
        今天已经因此带着红提交过一次的那种坑,在这里必须响。
        """
        miss = self.unfired()
        if miss:
            raise FaultNeverFired(
                "注入声明了却一次都没生效(where 写错 / 被测路径没走到)——"
                f"这条用例证明不了任何事:{[f.label or f.where or '*' for f in miss]};"
                f"实际计数:{self.counts}")
        return self


# ══════════════════════════════════════════════════════════════════
#  三、层适配器:把注入器接到各个真实接缝上
# ══════════════════════════════════════════════════════════════════

def _dumps(v) -> str:
    try:
        return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return repr(v)


def _fingerprint(er) -> tuple:
    """一条成功工具结果的不可变摘要 —— Verdict 比对用。

    比 value 本身稳:value 可能带不可哈希的嵌套结构,而"买到的东西没丢"要判的是
    同一件东西还在,不是对象同一性。

    【preview 必须一起进指纹】:loop_driver 回喂给大脑的 payload 是
    {result_id, preview, n} —— 那边的注释原话是"preview 才是真正回喂给大脑的字段
    (value 不进 prompt)"。只按 value 算的话,"value 一模一样、preview 被腰斩成半句"
    这种结果指纹逐字节相同 → 保留率 100%。而这个仓刚刚为"护栏收口指令被 _preview
    砍掉"改过两次代码(_gate_preview、_soft_note 全文回喂),那正是这把尺子
    原本看不见的那一维:证据还在,但大脑读到的那份已经残了。
    """
    return (bool(getattr(er, "ok", False)), int(getattr(er, "n", 0) or 0),
            _dumps(getattr(er, "value", None)), _dumps(getattr(er, "preview", None)))


def _is_bought(er) -> bool:
    """这条结果算不算"花钱买到手的东西"。

    ok=True 还不够 —— 配额闸和成本护栏拦下的调用【就是 ok=True】
    (loop_driver 里 return ExecResult(ok=True, value={..., "gate": "blocked"})),
    它一帧没看、一分没花。把它算进分子分母,保留率量的就成了"信封还在不在"。
    判据直接复用生产那条 _is_gate_envelope,不另写一套:生产为它专门留了
    gate=="blocked" 这个标记,还写明【不许】改用 enough 去认(真干过活的枝
    也合法地带 enough="no",subagents 的计数壳与残值回收都踩过这个坑)。
    """
    if not getattr(er, "ok", False):
        return False
    from pipeline import loop_driver as _ld
    return not _ld._is_gate_envelope(getattr(er, "value", None))


def wrap_exec(execute: Callable, injector: FaultInjector, *,
              spend_per_call: float = 0.0, key: "Callable[[str], str] | None" = None):
    """把注入器接到 **工具执行接缝**(`run_loop` 收的那个 execute 闭包)。

    计数 key 默认 = 工具名,所以 `Fault(where="analyze_video", at=2)` 读作
    "第 2 次 analyze_video 上生效"。

    spend_per_call:每次工具调用真烧多少美元(经 `usage.add_usage` 落进**真**账)。
    ③ 预算耗尽用它 —— 钱在【调用结束后】才落账,与生产时序一致
    (treeguard 模块头"在飞窗口"说的正是这个)。

    顺带记 Verdict 账:每条 ok=True 的结果进 `injector.bought`;第一次注入生效时
    自动快照成 `bought_before_fault` —— 那就是"故障之前已经花钱买到手的东西"。
    """
    def wrapped(cid, name, inputs, upstream, uses):
        k = key(name) if key else name
        try:
            res = injector.apply(k, lambda: execute(cid, name, inputs, upstream, uses))
        finally:
            if spend_per_call:
                spend_usd(spend_per_call)
        if _is_bought(res):        # 闸门信封不算"买到"(ok=True 但一帧没看、一分没花)
            injector.bought[cid] = _fingerprint(res)
        return res

    wrapped.injector = injector
    # 生产的 execute 闭包挂了三样东西给上游读(tree_guard / tree_nodes / analyze_quota),
    # 包一层就全丢了 —— run_loop 的 C4 回灌 notice 直接读 execute.analyze_quota。
    for attr in ("tree_guard", "tree_nodes", "analyze_quota", "seen"):
        if hasattr(execute, attr):
            setattr(wrapped, attr, getattr(execute, attr))
    return wrapped


def wrap_conversation(conv, injector: FaultInjector, *, key: str = "send"):
    """把注入器接到 **大脑接缝**(run_loop 收的那个 conversation)。

    注意:`run_loop` 直接调 `conversation.send`,**外面没有重试**;重试在
    `GeminiConversation.send` 内部经 `_send_with_retry` 完成。所以在这里注入
    瞬时错误 = 复现"重试全用光之后大脑彻底调不动"的形状,而不是重试路径本身。
    要测重试路径,直接对 `loop_driver._send_with_retry` 注入(见测试)。
    """
    class _Faulty:
        def __init__(self, inner):
            self._inner = inner

        def send(self, msg):
            return injector.apply(key, lambda: self._inner.send(msg))

        def __getattr__(self, item):        # last_thoughts 等透传
            return getattr(self._inner, item)

    return _Faulty(conv)


def wrap_generate(injector: FaultInjector, ok_raw: str | None = None, *,
                  key: str = "generate") -> Callable:
    """把注入器接到 **analyze 的 generate 接缝**
    (`analyze_with_outcome(..., generate=...)`)。

    没命中就返回 ok_raw(一份合法 JSON)。命中且 effect 是字符串 → 直接把那段
    **坏文本**交给真 `_parse`,让它自己炸 —— 这样测到的是生产的解析路径,
    而不是我们替它决定"这算失败"。
    """
    ok_raw = ok_raw or json.dumps(
        {"answer": "视频里有人在滑雪", "enough": "yes", "confidence": 0.9,
         "evidence_ts": 12}, ensure_ascii=False)

    def gen(gcs_uri, prompt, time_range=None):
        return injector.apply(key, lambda: ok_raw)

    gen.injector = injector
    return gen


def wrap_query_db(injector: FaultInjector, base: Callable | None = None, *,
                  key: str = "sql"):
    """把注入器接到 **数据库接缝**(`mcp_client.query_db` / 假库 `mock_run_sql`)。

    签名与两者完全一致 `(sql, meta=None) -> list[dict]` —— 差一个字,评测跑
    每次都会 TypeError(node_executor._query_db 的注释里记着这条)。
    没命中就走 base(默认 = 假库真查),所以"故障之前的那几条查询"是真结果,
    Verdict 保留率量的才是真东西。
    """
    if base is None:
        from repl._mock_db import mock_run_sql as base   # noqa: N813

    def query_db(sql, meta=None):
        return injector.apply(key, lambda: base(sql, meta))

    query_db.injector = injector
    return query_db


# ── ④ 租约过期:按 taskstate 的规范 SQL 认语句种类 ────────────────────
def sql_kind(sql: str) -> str:
    """把一条 `taskstate` 规范 SQL 认成种类名(供 Fault(where=...) 用)。

    认的是【语句特征】而不是解析 SQL —— 与 tests/pipeline/test_task_runner.py 的
    FakeDB 同一套判据,两边不会漂移。
    """
    s = " ".join(str(sql).split())
    if s.startswith("UPDATE agent_tasks SET status='running', lease_until"):
        return "claim"
    if "spent_usd = spent_usd + %(est)s" in s:
        return "precharge"
    if "SET plan=" in s:
        return "checkpoint"
    if "wasted_usd = wasted_usd + %(actual)s" in s:
        return "settle_failed"
    if s.startswith("UPDATE agent_tasks SET status='running', budget_cap"):
        return "resume"
    if s.startswith("UPDATE agent_tasks SET status="):
        return "terminal"
    if s.startswith("SELECT status, wave_n"):
        return "reread"
    if "INSERT INTO agent_task_events" in s:
        return "event"
    return "other"


def wrap_db_execute(execute: Callable, injector: FaultInjector) -> Callable:
    """把注入器接到 **任务底座的 DB 接缝**(`task_runner._execute(sql, params)`)。

    计数 key = `sql_kind(sql)`,所以 ④ 写作
    `Fault(CAS_EMPTY, where="checkpoint")` —— 落检查点那一刻租约已被别人抢走,
    `UPDATE ... AND lease_token=%(token)s` 影响 0 行,整波战果按僵尸丢弃。
    这是 `taskstate.py` 里 CAS 围栏的**唯一**失败形状:不抛异常,只是空结果集。
    """
    def wrapped(sql, params=None):
        return injector.apply(sql_kind(sql), lambda: execute(sql, params))

    wrapped.injector = injector
    return wrapped


# ══════════════════════════════════════════════════════════════════
#  四、③ 预算在特定时刻耗尽
# ══════════════════════════════════════════════════════════════════

def spend_usd(dollars: float, model: str = "gemini-2.5-flash") -> None:
    """往**真** usage 账上落一笔指定金额的钱(用 input token 反推)。

    走的是生产同一条路 `usage.add_usage` —— `TreeGuard.spent()` 读的正是
    `usage.summarize()["cost_usd"]`。**不**替换 spent()、**不**改 TreeGuard:
    那样测的是替身,闸门自己的"预估后比 + 在飞预留"判据一行都没被执行到。
    """
    from pipeline.agentops import usage

    # add_usage 在没 reset 过的上下文里【静默跳过】(它的第一条分支就是 `if u is None: return`)。
    # 那会让③整条注入无声失效:钱一分没落账 → 闸永不触 → 用例照样绿。这里响一声。
    if usage._USAGE.get() is None:
        raise RuntimeError(
            "usage 上下文没初始化,spend_usd 会被 add_usage 静默丢弃 —— "
            "注入将无声失效。先调 pipeline.agentops.usage.reset_usage()。")
    price = usage._PRICE.get(usage._norm_model(model)) or usage._PRICE["gemini-2.5-flash"]
    tin = int(dollars / price["in"] * 1e6)

    class _UM:
        prompt_token_count = tin
        candidates_token_count = 0
        total_token_count = tin
        cached_content_token_count = 0

    class _Resp:
        usage_metadata = _UM()

    usage.add_usage(_Resp(), model)


@dataclass
class BudgetFault:
    """③ 让**真** TreeGuard 在第 N 次 admit 上【自己】触闸。

    机制:解一个"每次调用花多少钱"出来,使 admit 的判据
    `spent + pending + est > cap` 恰好在第 N 次成立。
    第 k 次 admit 之前已落账 k-1 笔(钱在调用结束后才落账,pending 已被 settle 清零):
        第 k 次 admit 的左边 = (k-1)·per + est
    取 per = (cap - est) / (N - 1.5) 则
        第 N 次:   (N-1)·per + est = cap + 0.5·per  > cap  ✓ 触闸
        第 N-1 次: (N-2)·per + est = cap - 0.5·per  < cap  ✓ 放行
    N=1 直接预先花掉 cap(第一次 admit 就拦,一分钱不花)。

    触闸后整棵树**永久** tripped —— 那是 TreeGuard 自己的语义(`admit` 第一行
    `if self.tripped: return self._envelope()`),这里不模拟、不复述。
    """
    guard: Any
    per_call: float
    trip_at: int

    def burn(self) -> None:
        """模拟"一次调用结束、钱落账"。放在 admit/settle 之后调。"""
        if self.per_call > 0:
            spend_usd(self.per_call)


def trip_marker(injector: FaultInjector, label: str = "budget"):
    """一个最小 trace 替身:TreeGuard 自己触闸时会往 trace 记一条
    (`_trip` 里的 `self._trace.step("tree guard tripped …")`),这里借那条既有钩子
    把"触闸时刻"登记进注入器,顺带快照 Verdict 分母。

    走闸门**自己的**审计路径而不是包一层 admit —— 它同时证明了触闸真的发生过
    (`_trip` 是唯一会调 trace 的地方),而不是我们在旁边猜它触了。
    """
    class _Step:
        def bill(self, **_kw):
            pass

        def soft(self, *_a, **_kw):
            pass

        def ok(self, **_kw):
            pass

        def fail(self, **_kw):
            pass

    class _Trace:
        steps: list = []

        def step(self, *_a, **_kw):
            injector.mark(label)
            return _Step()

    return _Trace()


def budget_trips_at(trip_at: int, *, cost_cap: float = 0.60,
                    call_estimate: float = 0.05, trace=None) -> BudgetFault:
    """造一个会在第 `trip_at` 次 admit 上触闸的**真** TreeGuard。

    用法(每次调用 = admit → 干活 → settle → burn):
        bf = budget_trips_at(3)
        for i in range(5):
            env = bf.guard.admit(estimate=bf.guard.call_estimate)
            if env: break            # i == 2 时(第 3 次 admit)拿到软失败信封
            bf.guard.settle(bf.guard.call_estimate)
            bf.burn()
    """
    from pipeline.agentops.treeguard import TreeGuard

    if trip_at < 1:
        raise ValueError("trip_at 从 1 起")
    g = TreeGuard(cost_cap=cost_cap, wall_cap_s=0, call_estimate=call_estimate, trace=trace)
    if trip_at == 1:
        spend_usd(cost_cap)                       # 已超支 → 第一次 admit 必拦
        return BudgetFault(guard=g, per_call=0.0, trip_at=1)
    per = (cost_cap - call_estimate) / (trip_at - 1.5)
    if per <= 0:
        raise ValueError(f"cost_cap({cost_cap}) 太小,装不下 {trip_at} 次 "
                         f"call_estimate({call_estimate}) —— 第一次就会触闸")
    return BudgetFault(guard=g, per_call=per, trip_at=trip_at)


# ══════════════════════════════════════════════════════════════════
#  五、Verdict:这一批真正的产出
# ══════════════════════════════════════════════════════════════════

#: 诚实的终止形态。**全都不是** answer=None —— A1 之后 run_loop 在 max_steps /
#: repeat / tree_guard 三条路上交的都是"诚实的部分收口文案 + 完整 ledger"。
#: 回 None 会被 orchestrator 当"瞬时波动"劝用户重发,等于把刚烧掉的钱请回来重烧
#: (loop_driver 的 MAX_STEPS_ANSWER 注释里记着实证:78 次跑 7 次中招)。
HONEST_TERMINATED = ("text", "max_steps", "repeat", "tree_guard")


@dataclass(frozen=True)
class Verdict:
    """一次跑批"已经买到手"的东西。

    三件事合起来才叫"Verdict 还在":
      ① answer 不是 None(A1:部分交付,不许把交付点伪装成故障);
      ② 成功的工具结果一条都没丢(花过钱买到的证据);
      ③ terminated 如实归因(A4:失败不伪装成成功)。
    """
    answer: str | None
    terminated: str
    bought: dict[str, tuple] = field(default_factory=dict)

    @property
    def delivered(self) -> bool:
        return self.answer is not None and self.terminated in HONEST_TERMINATED


def verdict_of(res) -> Verdict:
    """LoopResult → Verdict。"""
    ledger = getattr(res, "ledger", None) or {}
    return Verdict(answer=getattr(res, "answer", None),
                   terminated=getattr(res, "terminated", "?"),
                   bought={cid: _fingerprint(er) for cid, er in ledger.items()
                           if _is_bought(er)})    # 与 wrap_exec 同一把尺子,闸门信封两边都不算


def retention(res, injector: FaultInjector) -> "tuple[float, list[str]]":
    """**故障注入下的 Verdict 保留率**:故障【之前】买到的东西,故障之后还剩多少。

    分母 = 注入第一次生效那一刻,执行器已经交付过的"真买到"结果
    (ok=True 且不是闸门信封 —— 见 _is_bought);
    分子 = 其中在最终 ledger 里【仍然存在、且指纹相同】的。
    分母为 0(故障发生在第一次成功之前)→ 1.0,没买到东西就没什么可丢的 ——
    但这是【空转的满分】,单独用 retention 的调用方要自己分辨;
    走 assert_verdict_preserved 的话它会替你把这种情况当场揭穿(见那边 ④')。

    返回 (保留率, 丢失的 cid 列表)。
    """
    before = injector.bought_before_fault
    if before is None:
        before = dict(injector.bought)
    if not before:
        return 1.0, []
    now = verdict_of(res).bought
    lost = [cid for cid, fp in before.items() if now.get(cid) != fp]
    return (len(before) - len(lost)) / len(before), lost


def assert_verdict_preserved(res, injector: FaultInjector, *,
                             expect_terminated: str | None = None,
                             allow_empty_denominator: bool = False) -> Verdict:
    """发布门槛「故障注入下 Verdict 保留率 = 100%」的断言体。

    四条一起判(少一条都能被绕过):
      ① 注入真的生效了(否则这条断言证明不了任何事)
      ②  answer 不是 None
      ③ terminated 落在诚实名单里(可指定确切值)
      ④ 保留率 == 1.0
      ④' 分母 > 0 —— 否则那个 1.0 是空转的:故障打在第一次成功【之前】,
         压根没有东西可丢,"保留率 100%"什么都没证明。最容易写出来的注入
         (默认 at=1)恰好就是这种。确实想只验 ②③(比如"开局就挂,系统要
         诚实收口")的,显式传 allow_empty_denominator=True 表态。
    """
    if not injector.faults and not injector.fired:
        # ① 的另一半:零声明的注入器 verify() 恒过(没有 Fault 就没有"没生效"可查)。
        # 忘了把 Fault(...) 传进 FaultInjector(...) 的用例会悄悄退化成"无故障跑批
        # 也能过" —— 那正是本模块自称要消灭的那种绿灯。刻意的无故障对照组
        # 不该走这个断言:它叫 assert_verdict_PRESERVED,没故障就没有"保留"可言。
        raise FaultNeverFired(
            "这个注入器一条 Fault 都没有、也没 mark 过任何故障 —— "
            "assert_verdict_preserved 在无故障跑批上恒过,证明不了任何事。"
            "对照组请直接断言结果,别借这个门槛的名字。")
    injector.verify()
    v = verdict_of(res)
    assert v.answer is not None, (
        "answer=None —— orchestrator 会把它当【瞬时波动】劝用户重发,"
        "整份 ledger 连同已经花掉的钱一起丢(A1 治的就是这个)")
    assert v.terminated in HONEST_TERMINATED, f"终止形态不诚实:{v.terminated}"
    if expect_terminated is not None:
        assert v.terminated == expect_terminated, (
            f"归因错了:期望 {expect_terminated},实际 {v.terminated}")
    rate, lost = retention(res, injector)
    assert rate == 1.0, (
        f"Verdict 保留率 {rate:.0%} < 100% —— 故障发生前已经花钱买到的 "
        f"{len(lost)} 条结果跟着故障一起丢了:{lost}")
    before = injector.bought_before_fault
    denominator = len(before if before is not None else injector.bought)
    if denominator == 0 and not allow_empty_denominator:
        raise AssertionError(
            "④' 分母为 0:故障打在第一次成功之前,这个 100% 是空转的 —— "
            "什么都没买到就谈不上保留。把 at 往后挪(先让系统买到点东西再打),"
            "或者你确实只想验诚实收口,就显式传 allow_empty_denominator=True。")
    return v
