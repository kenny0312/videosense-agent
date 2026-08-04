"""批次 3.5:故障注入 seam 的验收(合并任务书 v1.1 §9 + §13.4)。

三层验收,缺一层这批就没交付:

  ① **seam 自身**：注入了却没生效 = 最坏的情况(测试全绿、韧性为零)。
     所以每种故障形状都要证明【生产的判据代码认得它】,而不是我们自己说它像。
  ② **五种真实事故的复现**：仓里有记录的那五次,能不能在离线用例里重放出来。
  ③ **Verdict 保留率**：每一种注入下,故障【之前】已经花钱买到的东西一件都没丢。
     这条串起批次 0 的一串修复(A1 部分交付 / 残值回收 / A4 失败不伪装 /
     B3 不误进 SqlFixer),让它们可回归,而不是每次靠人记得。

`evals/faults.py` 的模块头写了每种形状对应生产里的哪一行判据。
"""
from __future__ import annotations

import json

import pytest

from evals import faults as F
from evals.world import make_exec
from pipeline import loop_driver as LD
from pipeline.loop_driver import Call, ExecResult, run_loop


# ══════════════════════════════════════════════════════════════════
#  公用小件
# ══════════════════════════════════════════════════════════════════
class Conv:
    """脚本大脑:按脚本依次返回 (calls, text)。脚本用完 = 一直不收敛(撞 max_steps)。"""

    def __init__(self, script, tail=None):
        self.script = list(script)
        self.tail = tail            # 脚本用完之后每轮返回什么(None = 空调用列表 → 收敛)
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        if self.script:
            return self.script.pop(0)
        return self.tail if self.tail is not None else ([], "脚本用完")


class FakeTrace:
    """最小 trace 替身(node_executor/TreeGuard 只用 step().ok/fail/soft/bill)。"""
    steps: list = []

    def step(self, *_a, **_kw):
        class _S:
            def ok(self, **_k): pass

            def fail(self, **_k): pass

            def soft(self, *_a, **_k): pass

            def bill(self, **_k): pass
        return _S()


@pytest.fixture()
def money():
    """③ 用真 usage 账落钱 —— 没 reset 过 add_usage 会静默丢弃(spend_usd 里有响亮的检查)。"""
    from pipeline.agentops import usage
    usage.reset_usage()
    yield usage
    usage.reset_usage()


def _tool(name, **inputs):
    return ([Call(name, inputs, [])], None)


# ══════════════════════════════════════════════════════════════════
#  一、seam 自身:注入了到底有没有生效
# ══════════════════════════════════════════════════════════════════

def test_make_exec_old_signature_and_behavior_are_untouched():
    """既有签名与行为逐字节兼容 —— 全仓一堆调用点在用它(evals/runner、
    tests/pipeline/test_e_batch_guards 等)。新能力只许走新的关键字参数。"""
    ex = make_exec({"sql_query": [{"a": 1}, {"a": 2}]}, ("python",))   # 两个位置参数,照旧
    r = ex("c0_0", "sql_query", {}, {}, [])
    assert r.ok and r.value == [{"a": 1}, {"a": 2}] and r.n == 2 and r.preview == [{"a": 1}]
    bad = ex("c0_1", "python", {}, {}, [])
    assert bad.ok is False and bad.stderr == "boom"
    assert [s["name"] for s in ex.seen] == ["sql_query", "python"]     # .seen 还在
    # 默认值也不许变
    assert make_exec()("c0_0", "x", {}, {}, []).value == [{"v": 1}]


def test_injector_screams_when_a_declared_fault_never_fired():
    """**本文件最重要的一条**。注入没生效的用例会【绿】,而它证明的东西是零。

    典型成因:where 写成了不存在的工具名、或被测路径根本没走到。
    verify() 让这种情况当场炸,而不是变成一条自我感觉良好的绿灯。
    """
    inj = F.FaultInjector(F.Fault(F.transient("429"), where="没有这个工具", label="打偏了"))
    ex = make_exec(faults=inj)
    ex("c0_0", "sql_query", {}, {}, [])
    assert inj.fired == []
    with pytest.raises(F.FaultNeverFired) as e:
        inj.verify()
    assert "打偏了" in str(e.value)


def test_injector_fires_exactly_at_the_nth_call_and_for_times_calls():
    """at/times 的语义:按【每个 key 各自的】调用序号计数。

    times 不是装饰:analyze 的重试循环要连挂 RETRY_LIMIT+1 次才走到 ANALYZE_FAILED,
    只挂一次测到的是"重试把它救回来了"—— 那是另一回事。
    """
    inj = F.FaultInjector(F.Fault(F.transient("503"), where="sql_query", at=2, times=2))
    ex = make_exec(faults=inj)
    ok_calls, boom_calls = [], []
    for i in range(5):
        try:
            ex(f"c0_{i}", "sql_query", {}, {}, [])
            ok_calls.append(i)
        except Exception:
            boom_calls.append(i)
    assert boom_calls == [1, 2] and ok_calls == [0, 3, 4]
    # 别的工具有自己的计数器,不受影响
    assert ex("c1_0", "analyze_video", {}, {}, []).ok
    inj.verify()


# ── ① 瞬时错误:必须被【生产的】分类器认出来 ──────────────────────────
@pytest.mark.parametrize("kind", F.TRANSIENT_KINDS)
def test_injected_transients_are_what_production_calls_transient(kind):
    """注入的异常必须让 `loop_driver._is_transient` 判 True。

    否则注入的"429"根本不会走重试路径,测出来的韧性是假的。
    两条路都要盖住:带 .code / .response.status_code 的走数值,其余靠【类名】——
    改一个类名,路 B 就静默失效,所以这条按 kind 参数化,一个都不许漏。
    """
    assert LD._is_transient(F.transient(kind)) is True, f"{kind} 没被认成瞬时错误"


def test_deterministic_error_is_not_transient():
    """阴性对照。没有它,上面那条可能只是因为分类器恒真 ——
    那样 400(参数非法)也会被重试三遍,白烧两次钱。"""
    assert LD._is_transient(F.deterministic(400)) is False


def test_a_handmade_429_is_not_recognized_which_is_why_the_factory_exists():
    """反向锁:随手搓一个"看起来像 429"的异常,生产分类器【不认】。

    这正是 transient() 必须存在的理由 —— 也是"注入的故障必须长得跟生产一样"
    这条设计约束最容易被绕过的地方(文本里有 429,判据里没有)。
    """
    assert LD._is_transient(RuntimeError("429 Too Many Requests")) is False


def test_transient_injection_walks_the_real_retry_path(monkeypatch):
    """接到【真】`_send_with_retry` 上:前两次瞬时错 → 第三次成功;
    确定性错误 → 第一次就上抛(不许白烧两次重试)。"""
    monkeypatch.setattr(LD.time, "sleep", lambda _s: None)

    inj = F.FaultInjector(F.Fault(F.transient("503"), where="send", at=1, times=2))
    n = {"i": 0}

    def send():
        n["i"] += 1
        return inj.apply("send", lambda: "成功")

    assert LD._send_with_retry(send) == "成功" and n["i"] == 3
    inj.verify()

    inj2 = F.FaultInjector(F.Fault(F.deterministic(400), where="send", at=1, times=9))
    m = {"i": 0}

    def send2():
        m["i"] += 1
        return inj2.apply("send", lambda: "成功")

    with pytest.raises(Exception):
        LD._send_with_retry(send2)
    assert m["i"] == 1, "确定性错误被重试了 —— 每多转一圈白烧一次钱"


# ── ② SQL 故障:复用 _mock_db.MockSqlError,别两边各写一套 ─────────────
def test_sql_faults_reuse_the_mock_db_error_class():
    """任务书 §9 原话:"复用批次 1 给 _mock_db.py 加的伪造能力,别两边各写一套"。"""
    from repl._mock_db import MockSqlError
    assert isinstance(F.sql_fault("timeout"), MockSqlError)


@pytest.mark.parametrize("kind,code", [("timeout", "57014"), ("lock", "55P03"),
                                       ("syntax", "42601"), ("column", "42703"),
                                       ("table", "42P01"), ("transport", None)])
def test_sql_fault_codes_line_up_with_the_production_classifier(kind, code):
    """码不是随便填的:上游 `node_executor` 按【这两张表】决定该不该进 SqlFixer。
    seam 里的码要是跟那两张表对不上,注入的就是一种生产里不存在的故障。"""
    from pipeline import node_executor as ne
    e = F.sql_fault(kind)
    assert getattr(e, "pgcode", None) == code
    if code in ("42601", "42703", "42P01"):
        assert code in ne._REPAIRABLE_SQLSTATES
    elif code in ("57014", "55P03"):
        assert code in ne._OVERLOAD_SQLSTATES and code not in ne._REPAIRABLE_SQLSTATES


# ── ⑤ 进程杀点:必须【不可被 except Exception 接住】───────────────────
def test_worker_killed_is_not_catchable_by_except_exception():
    """WorkerKilled 继承 BaseException 是整个 ⑤ 的要点。

    `node_executor.execute_node` 和 `subagents._run_one` 都有 `except Exception`
    兜底 —— 用普通 Exception 模拟"实例被回收",会被它们吞成一条软失败回喂大脑,
    测出来的是"报错处理得挺好",跟"进程没了"毫无关系。
    """
    assert not issubclass(F.WorkerKilled, Exception)
    try:
        raise F.WorkerKilled("cloud run 回收实例")
    except Exception:                                       # noqa: BLE001
        pytest.fail("WorkerKilled 被 except Exception 接住了 —— 那不是进程杀点")
    except BaseException:
        pass


# ══════════════════════════════════════════════════════════════════
#  二、五种真实事故的复现 + 三、Verdict 保留率
# ══════════════════════════════════════════════════════════════════

# ── 真实事故①:沙箱缺失的 AttributeError(三份 gate jsonl 共 12 条)────────
def test_incident_1_sandbox_absent_crash_tears_down_the_whole_verdict():
    """复现【修复前】那 12 条崩溃的形状,并把它的代价钉成数字。

    见 tests/pipeline/test_sandbox_absent.py 文件头:
    `AttributeError("'NoneType' object has no attribute 'execute'")`。
    它不是软失败,是从工具里【抛】出来的 —— 一路掀掉 run_loop,ledger 里
    已经付过费的结果跟着一起没。这条测的就是"跟着一起没"到底是多少:0%。
    """
    inj = F.FaultInjector(F.Fault(F.sandbox_absent_error(), where="python", label="沙箱缺失"))
    ex = make_exec(values={"sql_query": [{"n": 3}]}, faults=inj)
    conv = Conv([_tool("sql_query", sql="SELECT 1"), _tool("python", instruction="画图")])

    with pytest.raises(AttributeError, match="NoneType"):
        run_loop("q", conv, ex, max_steps=6)

    inj.verify()
    # 崩之前已经买到了 sql 结果,而 run_loop 连 LoopResult 都没交出来 → 那份 ledger 无处可取
    assert list(inj.bought_before_fault) == ["c0_0"]


def test_incident_1_todays_code_soft_fails_and_keeps_the_verdict(monkeypatch):
    """同一个场景走【今天的】代码:B0-1 防线②把它变成软失败,Verdict 全额保留。

    这条是防线②的回归锁:哪天有人把 `_run_sandbox_node` 的守卫挪到生成代码之后,
    崩溃会回来,这里立刻红。
    """
    from pipeline import mcp_client
    from pipeline.agentops.trace import Trace

    inj = F.FaultInjector()
    monkeypatch.setattr(mcp_client, "query_db", F.wrap_query_db(inj))
    base = LD._make_executor(sandbox=None, trace=Trace(quiet=True), schema={}, session_id=None)
    ex = F.wrap_exec(base, inj)
    conv = Conv([_tool("sql_query", sql="SELECT COUNT(*) AS n FROM video_metadata"),
                 _tool("python", instruction="画图"),
                 ([], "库里有 16 个视频(画图这一步没有沙箱,已跳过)")])

    r = run_loop("q", conv, ex, max_steps=6)

    assert r.terminated == "text"
    py = [er for cid, er in r.ledger.items() if cid.endswith("c1_0")][0]
    assert py.ok is False and "沙箱" in py.stderr        # 软失败,不是崩溃
    assert F.verdict_of(r).bought, "sql 那一份必须还在"
    # 这条的"故障"是【沙箱缺席】本身,不是某次调用被换掉 —— 跟 ③④⑤ 一样走 mark 登记,
    # 否则零声明的注入器会被 assert_verdict_preserved 的新闸当场揭穿(那个闸是对的)。
    inj.mark("沙箱缺席")
    F.assert_verdict_preserved(r, inj, expect_terminated="text")


# ── 真实事故②:analyze 坏 JSON → ANALYZE_FAILED ────────────────────────
def test_incident_2_truncated_analyze_json_becomes_analyze_failed():
    """复现:pro 档 max_output_tokens 把带 evidence 的 JSON 从中间截断,
    `_parse` 抛 "Unterminated string starting at line 6" → 连试 3 次 → ANALYZE_FAILED。

    (`pipeline/config.py:230` 与 `analyze_video_contextual.py:171` 两处注释都记着
     "实测 pro 档两次标注全部 ANALYZE_FAILED / Unterminated string"。)
    """
    import perception.analyze_video_contextual as AV

    inj = F.FaultInjector(F.Fault(F.truncated_analyze_json(), where="generate",
                                  at=1, times=AV.RETRY_LIMIT + 1, label="坏JSON"))
    out = AV.analyze_with_outcome(AV.AnalyzeRequest(question="在干嘛"),
                                  "gs://eval/v001.mp4", generate=F.wrap_generate(inj))

    inj.verify()
    assert out.ok is False and out.error_code == AV.ERROR_ANALYZE_FAILED
    assert out.attempts == AV.RETRY_LIMIT + 1, "重试次数要如实交代(A4:不虚报)"
    assert "Unterminated string" in out.error and "line 6" in out.error


def test_incident_2_verdict_survives_the_failed_analyze():
    """A4 的另一半:analyze 失败【不许伪装成成功】,而它失败【也不许】把之前买到的丢掉。

    A1 之前 loop 在这条路上回 None → orchestrator 谎报"服务波动"+ 丢整份 ledger。
    """
    inj = F.FaultInjector(F.Fault(
        ExecResult(ok=False, stderr="没有被分析过(ANALYZE_FAILED)", error_code="ANALYZE_FAILED"),
        where="analyze_video", at=1, times=9, label="analyze全失败"))
    ex = make_exec(values={"sql_query": [{"video_id": "v001"}]}, faults=inj)
    conv = Conv([_tool("sql_query", sql="SELECT 1"),
                 _tool("analyze_video", video_id="v001", question="q"),
                 _tool("analyze_video", video_id="v001", question="q")],   # 同签名连撞 → repeat
                tail=_tool("analyze_video", video_id="v001", question="q"))

    r = run_loop("q", conv, ex, max_steps=8, repeat_limit=2)

    assert r.terminated == "repeat"
    assert r.answer and "已经查到、已经展示出来的内容都是真实结果" in r.answer
    F.assert_verdict_preserved(r, inj, expect_terminated="repeat")


# ── 真实事故③:57014 之后【不该】进 SqlFixer ───────────────────────────
def test_incident_3_statement_timeout_never_reaches_sql_fixer(monkeypatch):
    """B3:57014 进 SqlFixer 就是"超时 → 改SQL → 再超时"的烧钱循环。

    锁三件:① SqlFixer 一次都没被造出来;② attempts=1(没白发第二条查询);
    ③ 回喂大脑的话【说死】这不是 SQL 写错了 —— 少了这句,大脑自己重发一遍,
    等价于把刚从代码里删掉的循环原封不动搬进模型脑子里。
    """
    from pipeline import node_executor as ne
    from pipeline.dag_schema import Node

    built = {"n": 0}

    class _NeverFixer:
        def __init__(self):
            built["n"] += 1

        def repair(self, *_a):
            return "SELECT 1"

    inj = F.FaultInjector(F.Fault(F.sql_fault("timeout"), where="sql", at=1, times=9,
                                  label="57014"))
    monkeypatch.setattr(ne, "SqlFixer", _NeverFixer)
    monkeypatch.setattr(ne, "_query_db", lambda sql, meta: F.wrap_query_db(inj)(sql, meta))

    node = Node(id="c0_0", tool="sql_query", inputs={"sql": "SELECT 1"}, depends_on=[])
    res = ne._run_sql_query(node, {}, FakeTrace())

    inj.verify()
    assert built["n"] == 0, "超时被当成【SQL 写错了】送进了 SqlFixer —— 烧钱循环回来了"
    assert res.ok is False and res.attempts == 1
    assert "57014" in res.stderr and "【这不是 SQL 写错了】" in res.stderr


def test_incident_3_repairable_code_does_reach_the_fixer(monkeypatch):
    """阴性对照:42601(真写错了)必须进 SqlFixer。

    没这条,上面那条把 SqlFixer 整个删掉也能过 —— 测的就成了"永远不自愈"。
    """
    from pipeline import node_executor as ne
    from pipeline.dag_schema import Node

    built = {"n": 0}

    class _Fixer:
        def __init__(self):
            built["n"] += 1

        def repair(self, *_a):
            return "SELECT 1"

    inj = F.FaultInjector(F.Fault(F.sql_fault("syntax"), where="sql", at=1, times=9,
                                  label="42601"))
    monkeypatch.setattr(ne, "SqlFixer", _Fixer)
    monkeypatch.setattr(ne, "_query_db", lambda sql, meta: F.wrap_query_db(inj)(sql, meta))

    res = ne._run_sql_query(Node(id="c0_0", tool="sql_query", inputs={"sql": "SELCT 1"},
                                 depends_on=[]), {}, FakeTrace())
    inj.verify()
    assert built["n"] == 1 and res.attempts == ne.SQL_MAX_RETRIES + 1


def test_incident_3_verdict_survives_a_statement_timeout(monkeypatch):
    """整轮跑批:第 2 条 SQL 超时,第 1 条查到的结果一条都不许丢。"""
    from pipeline import mcp_client
    from pipeline.agentops.trace import Trace

    inj = F.FaultInjector(F.Fault(F.sql_fault("timeout"), where="sql", at=2, times=9,
                                  label="57014"))
    monkeypatch.setattr(mcp_client, "query_db", F.wrap_query_db(inj))
    base = LD._make_executor(sandbox=None, trace=Trace(quiet=True), schema={}, session_id=None)
    ex = F.wrap_exec(base, inj)
    conv = Conv([_tool("sql_query", sql="SELECT COUNT(*) AS n FROM video_metadata"),
                 _tool("sql_query", sql="SELECT * FROM video_facts"),
                 ([], "库里有 16 个视频;第二条明细查询超时了,这部分【未核查】")])

    r = run_loop("q", conv, ex, max_steps=6)

    inj.verify()
    assert r.ledger["c1_0"].ok is False and "57014" in r.ledger["c1_0"].stderr
    F.assert_verdict_preserved(r, inj, expect_terminated="text")


# ── 真实事故④:护栏在 analyze 的【重试中途】触闸 ────────────────────────
def test_incident_4_guard_trips_between_analyze_retries(money):
    """A7+ B 方案下沉之后,admit/settle 挂在【每一次真实 generate】上。

    时序:第 1 次 generate 发出去 → 429 → 那笔钱**已经落账** → 第 2 次 generate 的
    admit 看见这笔钱 → 触闸 → 这次 generate 根本不发。
    要害是 `attempts == 1`:护栏拦下的那次【不算一次分析】,记账笔数必须等于真实
    LLM 调用次数(下沉之前是记 1 笔实发 3 次,账错 3 倍)。
    """
    import perception.analyze_video_contextual as AV
    from pipeline import node_executor as ne

    inj = F.FaultInjector(F.Fault(F.transient("429"), where="generate", at=1, times=9,
                                  label="429"))
    bf = F.budget_trips_at(2, cost_cap=0.20, call_estimate=0.05,
                           trace=F.trip_marker(inj, "护栏触闸"))

    def gen(_uri, _prompt, _tr=None):
        try:
            return inj.apply("generate", lambda: "{}")
        finally:
            bf.burn()                     # 这一次真花了钱,落账在调用【之后】(与生产同序)

    admit, settle = ne._guard_hooks(bf.guard, "gemini-2.5-flash", "v001")
    out = AV.analyze_with_outcome(AV.AnalyzeRequest(question="x"), "gs://eval/v001.mp4",
                                  generate=gen, admit=admit, settle=settle)

    inj.verify()
    assert [f["label"] for f in inj.fired] == ["429", "护栏触闸"], "触闸没发生在重试中途"
    assert out.error_code == AV.ERROR_GUARD_BLOCKED
    assert out.attempts == 1, "拦下的那次被算成了一次分析 —— 记账笔数≠LLM 调用次数"
    assert bf.guard.tripped == "budget"
    assert "成本护栏" in out.error and out.error.index("成本护栏") < 20   # 信封在最前,别被截掉
    # 前几次的钱已经落账,披露必须带上它(现算,不是触闸瞬间的快照)
    assert f"${bf.per_call:.4f}" in bf.guard.final_note(mark_claim=False)


def test_incident_4_verdict_survives_the_mid_flight_trip(money):
    """整轮跑批:护栏在第 3 次 admit(第 3 轮思考)触闸,前两轮买到的一件不丢,
    且答案里对用户【透明】(为什么停、花了多少)。"""
    inj = F.FaultInjector()
    bf = F.budget_trips_at(3, cost_cap=0.60, call_estimate=0.05,
                           trace=F.trip_marker(inj, "护栏触闸"))
    ex = make_exec(values={"sql_query": [{"a": 1}]}, faults=inj, spend_per_call=bf.per_call)
    conv = Conv([_tool("sql_query", sql=f"SELECT {i}") for i in range(3)],
                tail=([], "就已有证据收口:查到 3 组数据,其余【未核查】"))

    r = run_loop("q", conv, ex, guard=bf.guard, max_steps=8)

    assert bf.guard.tripped == "budget"
    assert "第 3 轮思考" in bf.guard.trip_detail, "触闸时机不是第 3 次 admit"
    assert [f["label"] for f in inj.fired] == ["护栏触闸"]
    assert "本次触发成本护栏" in (r.answer or "")
    F.assert_verdict_preserved(r, inj, expect_terminated="text")
    assert len(inj.bought_before_fault) == 2, "触闸前该买到 2 份"


# ── 真实事故⑤:子 agent 撞 max_steps 不收敛(真机 10/14 = 71%)────────────
def _analyze_env(vid, answer):
    """一次成功 analyze 的结果形状(同 node_executor._run_analyze_video 的 value)。"""
    v = {"video_id": vid, "answer": answer, "enough": "yes", "confidence": 0.9}
    return ExecResult(ok=True, value=v, preview=[v], n=1)


_QUOTA_NOTE = ("已达本请求视频分析上限(12 个),这个【没分析】。"
               "请【就已分析过的那些视频】给出结论:不要再调 analyze_video。")


def test_incident_5_unconverged_subagent_still_hands_back_what_it_bought():
    """子 agent 烧光步数没给结论时,ledger 里【花钱买来的】analyze 结论必须回到主脑。

    子 agent 复用父 execute 闭包 —— 它每 analyze 一个视频都从全树共享的
    MAX_VIDEOS_PER_REQUEST 里实扣一个:钱花了、配额没了,都不可逆。
    此前只回一句"(子 agent 未收敛:max_steps)",那几份结论被直接丢弃。

    同时注两种【生产真实存在】的非结论形状,验证它们不许被当成证据捞回去:
      · 第 3 次 = 配额闸信封(loop_driver._soft_note 的形状:**ok=True** 但一帧都没看,
        靠 gate="blocked" 识别)。把一句"已达上限"当成看过的证据回流给主脑是灾难 ——
        主脑会拿它去下结论,而它连视频都没打开。
      · 第 4 次 = 真失败(A4:ok=False、value=None)。

    【变异验证记录 · 一条如实的负结果】把 `_salvage_analyses` 里的 `not st.get("ok")`
    删掉,本条测试**不会**变红 —— 因为失败的 analyze 在生产里 value 恒为 None
    (`NodeResult.value` 默认 None,`_run_analyze_video` 的四条失败 return 都不带 value),
    前一行的 `isinstance(v, dict)` 已经先把它挡掉了。也就是说那个 `ok` 判据是
    **当前不可达的第二道锁**。不为它编一条用人造 fixture 撑起来的测试:
    那样测的是我们自己造的形状,不是生产的形状 —— 正是本批次要禁的事。
    """
    from pipeline import subagents

    inj = F.FaultInjector(
        F.Fault(LD._soft_note(_QUOTA_NOTE), where="analyze_video", at=3, times=1,
                label="配额闸信封"),
        F.Fault(ExecResult(ok=False, error_code="ANALYZE_FAILED",
                           stderr="video_id=v004 这个视频【没有被分析过】"),
                where="analyze_video", at=4, times=1, label="第4次analyze挂了"))

    def base(cid, name, inputs, _up, _uses):
        return _analyze_env(inputs.get("video_id", "?"), f"{inputs.get('video_id')} 里有人在滑雪")

    ex = F.wrap_exec(base, inj)
    conv = Conv([_tool("analyze_video", video_id=v, question="q")
                 for v in ("v001", "v002", "v003", "v004", "v005")])

    r = run_loop("看看这几个", conv, ex, max_steps=5)          # 步数用光,永不收敛
    inj.mark("子agent撞墙")                                    # ⑤ 的"故障"就是步数烧光
    assert r.terminated == "max_steps"

    out = subagents._no_answer_output(f"子 agent 未收敛:{r.terminated}", r)

    inj.verify()
    assert "未收敛:max_steps" in out                           # 诚实:没说这是它的结论
    assert "v001" in out and "v002" in out and "v005" in out   # 买到的结论回到主脑
    assert "已达本请求视频分析上限" not in out and "v003" not in out, \
        "配额闸信封被当成【看过的证据】回流给主脑了 —— 它连视频都没打开"
    assert "v004" not in out, "失败的那次被当成【看过的证据】捞回去了(A4 反了)"
    F.assert_verdict_preserved(r, inj, expect_terminated="max_steps")


def test_incident_5_answer_is_not_none_so_the_ledger_is_not_dropped():
    """A1 的本体:max_steps 是【交付点】不是故障,绝不能回 None。

    实证(loop_driver 的 MAX_STEPS_ANSWER 注释):78 次跑里 7 次 terminated=max_steps
    且答案长度 0,其中 4 次屏幕上明明已经摆出了 2/6/7/8 个视频 ——
    用户看得见视频、系统却说"服务波动"。
    """
    inj = F.FaultInjector()
    ex = make_exec(values={"sql_query": [{"a": 1}]}, faults=inj)
    conv = Conv([], tail=_tool("sql_query", sql="SELECT 1"))
    r = run_loop("q", conv, ex, max_steps=3)
    assert r.terminated == "max_steps"
    assert r.answer is not None and "服务波动" not in r.answer
    inj.mark("步数烧光")     # 故障 = 撞墙本身(同 ⑤ 的口径),不是某次调用被换掉
    F.assert_verdict_preserved(r, inj, expect_terminated="max_steps")


# ══════════════════════════════════════════════════════════════════
#  四、④ 租约过期回传:CAS 影响 0 行
# ══════════════════════════════════════════════════════════════════
class TaskDB:
    """最小 `agent_tasks` 替身(只认 taskstate 的规范语句,不解析 SQL)。

    与 tests/pipeline/test_task_runner.py 的 FakeDB 同一套判据 —— 那边测的是
    波次心脏本身,这边测的是"CAS 空结果"这一种注入下战果保不保得住。
    """

    def __init__(self, row):
        self.row = row
        self.events = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        r = self.row
        if s.startswith("UPDATE agent_tasks SET status='running', lease_until"):   # CLAIM
            if (r["status"] in ("pending", "running") and r["wave_n"] == params["wave_n"]
                    and not r["lease_live"]):
                r["status"], r["lease_token"], r["lease_live"] = "running", params["token"], True
                return [(r["task_id"], r["owner"], r["goal"], json.dumps(r["plan"]),
                         r["status"], r["wave_n"], r["lease_token"], r["budget_cap"],
                         r["spent_usd"], r["wasted_usd"], r["precharged_usd"], None)]
            return []
        if s.startswith("SELECT status, wave_n"):
            return [(r["status"], r["wave_n"], bool(r["lease_live"]), json.dumps(r["plan"]))]
        if "spent_usd = spent_usd + %(est)s" in s:                                 # PRECHARGE
            if r["lease_token"] == params["token"] and r["status"] == "running":
                r["spent_usd"] += params["est"]
                r["wasted_usd"] += r["precharged_usd"]
                r["precharged_usd"] = params["est"]
                return [(r["spent_usd"], r["wasted_usd"])]
            return []
        if "SET plan=" in s:                                                       # CHECKPOINT
            if (r["lease_token"] == params["token"] and r["wave_n"] == params["wave_n"]
                    and r["status"] == "running"):
                p = params["plan"]
                r["plan"] = getattr(p, "adapted", p)
                r["spent_usd"], r["wasted_usd"] = params["spent_usd"], params["wasted_usd"]
                r["precharged_usd"], r["wave_n"] = 0.0, r["wave_n"] + 1
                r["lease_token"], r["lease_live"] = None, False
                return [(r["wave_n"],)]
            return []
        if "wasted_usd = wasted_usd + %(actual)s" in s:                            # SETTLE_FAILED
            if r["lease_token"] == params["token"]:
                r["spent_usd"] += params["actual"] - r["precharged_usd"]
                r["wasted_usd"] += params["actual"]
                r["precharged_usd"] = 0.0
                return [(r["spent_usd"], r["wasted_usd"])]
            return []
        if s.startswith("UPDATE agent_tasks SET status="):                         # TERMINAL
            if r["status"] in params["from_statuses"]:
                r["status"] = params["to_status"]
                r["lease_token"], r["lease_live"] = None, False
                return [(r["status"],)]
            return []
        if "INSERT INTO agent_task_events" in s:
            self.events.append((params["kind"], json.loads(params["payload"])))
            return []
        if "kind='user_note'" in s:
            return []
        raise AssertionError(f"TaskDB 不认识的 SQL:{s[:80]}")


@pytest.fixture()
def task_bed(monkeypatch):
    """接线:task_runner/_task_store 的 DB 接缝 → TaskDB;LLM/波执行/队列/限流全打桩。"""
    from pipeline import task_queue, task_runner as TR, task_store
    from pipeline.agentops import ratelimit, usage

    db = TaskDB(dict(task_id="tk_1", owner="kenny", goal="整理滑雪视频",
                     plan={"remaining": [{"id": 1, "instruction": "看 v1", "video_ids": ["v1"]},
                                         {"id": 2, "instruction": "看 v2", "video_ids": ["v2"]}],
                           "done": {"0": {"answer": "上一波已经买到的战果", "wave": 0}}},
                     status="running", wave_n=1, lease_token=None, lease_live=False,
                     budget_cap=5.0, spent_usd=0.0, wasted_usd=0.0, precharged_usd=0.0))
    calls = {"enq": []}
    monkeypatch.setattr(task_store, "_execute", db.execute)
    monkeypatch.setattr(TR, "run_wave",
                        lambda task, batch: {str(b["id"]): {"answer": f"done-{b['id']}"}
                                             for b in batch})
    monkeypatch.setattr(TR, "finalize_report", lambda goal, done: "最终报告")
    monkeypatch.setattr(task_queue, "enqueue_advance",
                        lambda tid, w: calls["enq"].append((tid, w)))
    monkeypatch.setattr(usage, "reset_usage", lambda: None)
    monkeypatch.setattr(usage, "summarize", lambda: {"cost_usd": 0.033})
    monkeypatch.setattr(ratelimit, "record", lambda *a, **k: None)
    return db, calls, TR


def test_lease_lost_at_checkpoint_drops_the_wave_but_keeps_the_ledger(task_bed, monkeypatch):
    """④ 租约过期回传:落检查点那一刻租约已被别人抢走 → CAS 影响 0 行。

    Verdict 在任务这条路上的载体是【任务账本】,不是进程内 ledger。要保的三件:
      ① 上一波已经买到的战果(plan.done["0"])一个字不动;
      ② 本波真花的钱结进账本并记成 wasted(不结算 = resume 免费重跑,cap 被突破 N 倍);
      ③ 任务【不许】被写成 done —— 那是把丢掉的战果谎报成交付。
    """
    db, calls, TR = task_bed
    inj = F.FaultInjector(F.Fault(F.CAS_EMPTY, where="checkpoint", label="租约易主"))
    monkeypatch.setattr(TR, "_execute", F.wrap_db_execute(db.execute, inj))

    out = TR.advance("tk_1", 1)

    inj.verify()
    assert out["dispatch"] == "zombie_dropped"
    assert db.row["plan"]["done"]["0"]["answer"] == "上一波已经买到的战果"   # ①
    assert db.row["wasted_usd"] >= 0.033 and db.row["spent_usd"] >= 0.033   # ②
    assert ("wasted", {"wave": 1, "usd": 0.033, "why": "cas_lost"}) in db.events
    assert db.row["status"] != "done" and calls["enq"] == []                # ③


def test_lease_lost_at_claim_is_a_retry_not_a_state_change(task_bed, monkeypatch):
    """认领就 0 行(租约还活着)→ 503 退避,【绝不动状态】。

    动了状态就会把别人正持租在跑的那一波打成僵尸 —— 那是"注入一个故障,
    结果自己造出第二个故障"。
    """
    db, _calls, TR = task_bed
    db.row["lease_live"] = True                        # 别人正持租
    inj = F.FaultInjector(F.Fault(F.CAS_EMPTY, where="claim", label="认领落空"))
    monkeypatch.setattr(TR, "_execute", F.wrap_db_execute(db.execute, inj))

    out = TR.advance("tk_1", 1)

    inj.verify()
    assert out == {"result": TR.RETRY, "dispatch": "lease_busy"}
    assert db.row["status"] == "running" and db.row["wave_n"] == 1


def test_sql_kind_recognises_every_canonical_statement():
    """`where=` 认的是语句种类 —— 认错了整条注入就打偏(而 verify() 会替我们喊出来)。
    这条把 taskstate 的六条规范 SQL 逐条钉住,改名/改写法时立刻红。"""
    from pipeline import taskstate as TS
    assert F.sql_kind(TS.CLAIM_SQL) == "claim"
    assert F.sql_kind(TS.PRECHARGE_SQL) == "precharge"
    assert F.sql_kind(TS.CHECKPOINT_SQL) == "checkpoint"
    assert F.sql_kind(TS.SETTLE_FAILED_SQL) == "settle_failed"
    assert F.sql_kind(TS.RESUME_SQL) == "resume"
    assert F.sql_kind(TS.TERMINAL_SQL) == "terminal"


# ══════════════════════════════════════════════════════════════════
#  五、⑤ 进程杀点:Verdict 保留率的【边界】
# ══════════════════════════════════════════════════════════════════

def test_kill_point_loses_the_whole_in_request_verdict():
    """**这条不是"保留住了"的证明,是它的边界 —— 而且是这批最值钱的发现。**

    单次请求(run_query_loop)这条路上【没有任何检查点】:实例被回收 = 那一次请求里
    已经花钱买到的全部证据 100% 蒸发,没有残值回收、没有断点续跑。
    后台任务那条路有(taskstate 的 CHECKPOINT_SQL,见上面 ④ 的用例)。

    【哪句是棘轮,说清楚】真正钉住生产行为的是下面那句 pytest.raises:
    BaseException 原样掀出 run_loop、不产出任何 LoopResult —— 哪天单请求路径
    有了残值回收(比如 run_loop 学会在被杀前把 ledger 落盘再重抛),raises 这一半
    就会变红,那是好消息。末尾那两行 retention 算的是【手搓的空 ledger】上的算术
    (崩溃之后调用方手里就是什么都没有,这是对"没有任何东西幸存"的演算,
    不是对生产代码的探测)—— 别把它当棘轮引用,它对 pipeline/ 的改动不敏感。
    """
    inj = F.FaultInjector(F.Fault(F.WorkerKilled, where="analyze_video", at=2,
                                  label="实例被回收"))
    ex = make_exec(values={"sql_query": [{"a": 1}], "analyze_video": [{"v": 1}]}, faults=inj)
    conv = Conv([_tool("sql_query", sql="SELECT 1"),
                 _tool("analyze_video", video_id="v001", question="q"),
                 _tool("analyze_video", video_id="v002", question="q")])

    with pytest.raises(F.WorkerKilled):
        run_loop("q", conv, ex, max_steps=6)

    inj.verify()
    assert len(inj.bought_before_fault) == 2, "被杀之前已经买到 2 份(sql + 第一个 analyze)"
    dead = type("R", (), {"ledger": {}, "answer": None, "terminated": "?"})()
    rate, lost = F.retention(dead, inj)
    assert rate == 0.0 and len(lost) == 2


def test_kill_point_spares_the_waves_already_checkpointed(task_bed, monkeypatch):
    """同一个杀点打在后台任务那条路上:上一波【已落检查点】的战果活下来。

    这就是 ④ 那套 CAS 围栏的价值 —— 也是上一条那个 0.0 的解药长什么样。
    """
    db, _calls, TR = task_bed
    monkeypatch.setattr(TR, "run_wave", lambda task, batch: (_ for _ in ()).throw(
        F.WorkerKilled("cloud run 回收实例")))
    inj = F.FaultInjector()
    monkeypatch.setattr(TR, "_execute", F.wrap_db_execute(db.execute, inj))

    with pytest.raises(F.WorkerKilled):
        TR.advance("tk_1", 1)                 # BaseException:没有 except Exception 接得住

    assert db.row["plan"]["done"]["0"]["answer"] == "上一波已经买到的战果"
    assert db.row["status"] != "done"


# ══════════════════════════════════════════════════════════════════
#  尺子自身的卫生(对抗性 review 揪出的五条,每条都有过真实的骗分路径)
# ══════════════════════════════════════════════════════════════════

def test_gate_envelope_is_not_counted_as_bought():
    """闸门信封(ok=True 但一帧没看、一分没花)不许算"买到"。

    生产的配额闸/成本闸拦下的调用就是 ok=True + value.gate=="blocked"。
    把它记进分子分母,保留率量的就成了"信封还在不在" —— 恒真的那种。
    判据必须复用生产的 _is_gate_envelope,不另写一套。
    """
    real = ExecResult(ok=True, value={"video_id": "v1", "answer": "看到了"}, n=1)
    gate = ExecResult(ok=True, value={"answer": "已达上限", "enough": "no",
                                      "gate": "blocked"}, n=1)
    # 真成功也可能 enough="no"(如"视频里没有狗"),不能按 enough 判
    honest_no = ExecResult(ok=True, value={"video_id": "v2", "answer": "没有狗",
                                           "enough": "no"}, n=1)
    inj = F.FaultInjector(F.Fault(F.transient("429"), where="x", at=99))
    ex = F.wrap_exec(lambda cid, *a: {"c0": real, "c1": gate, "c2": honest_no}[cid], inj)
    for cid in ("c0", "c1", "c2"):
        ex(cid, "analyze_video", {}, {}, [])
    assert set(inj.bought) == {"c0", "c2"}, (
        f"记账错了:{sorted(inj.bought)} —— 闸门信封混进了'买到'的账本")


def test_fingerprint_sees_the_preview_not_just_the_value():
    """preview 才是真正回喂给大脑的字段(value 不进 prompt)。

    只按 value 算指纹的话,"证据还在、但大脑读到的那份被腰斩了"判成保留率 100% ——
    而这个仓刚为护栏指令被 _preview 砍掉改过两次代码。
    """
    a = ExecResult(ok=True, value={"k": 1}, preview=[{"note": "完整的收口指令,一个字没少"}], n=1)
    b = ExecResult(ok=True, value={"k": 1}, preview=[{"note": "完整的收口指"}], n=1)
    assert F._fingerprint(a) != F._fingerprint(b), (
        "value 相同、preview 被腰斩,指纹却一样 —— 尺子看不见大脑真正读到的那一维")


def test_zero_fault_injector_cannot_pass_the_verdict_gate():
    """忘了把 Fault(...) 传进 FaultInjector(...),整条用例会退化成"无故障跑批也能过"。
    这正是本模块自称要消灭的那种绿灯,所以门槛断言第一句就要把它揭穿。"""
    fake = type("R", (), {"ledger": {}, "answer": "x", "terminated": "text"})()
    with pytest.raises(F.FaultNeverFired, match="一条 Fault 都没有"):
        F.assert_verdict_preserved(fake, F.FaultInjector(), expect_terminated="text")


def test_empty_denominator_is_exposed_not_silently_perfect():
    """故障打在第一次成功之前 → 分母 0 → 那个 100% 是空转的。

    最容易写出来的注入(默认 at=1)恰好就是这种,所以必须响,
    除非调用方显式表态只想验诚实收口。
    """
    # 工具接缝的注入用 ok=False 信封(生产的 executor 把异常兜成软失败回喂,
    # 裸抛异常会直接掀掉 run_loop —— 那是 ⑤ 杀点的形状,不是这条要的):
    inj = F.FaultInjector(F.Fault(
        ExecResult(ok=False, stderr="429 RESOURCE_EXHAUSTED"), where="sql_query", at=1))
    ex = make_exec(values={"sql_query": [{"a": 1}]}, faults=inj)
    conv = Conv([_tool("sql_query", sql="SELECT 1")],
                tail=([], "第一步就被限流,什么都没查到 —— 如实说没有数据"))
    r = run_loop("q", conv, ex, max_steps=4)
    with pytest.raises(AssertionError, match="分母为 0"):
        F.assert_verdict_preserved(r, inj, expect_terminated="text")
    v = F.assert_verdict_preserved(r, inj, expect_terminated="text",
                                   allow_empty_denominator=True)
    assert v.bought == {}, "前提检查:这条用例的分母必须真的是空,否则上面测的不是它"


def test_spend_without_faults_still_burns_money(money):
    """spend_per_call 单独传也要落账 —— docstring 把它和 faults 列成两项独立能力。

    以前 faults=None 时它被静默丢弃:想单独测"预算耗尽"(只烧钱不注故障)的用例
    会得到一个永远不触闸、而且是绿的结果。
    """
    from pipeline.agentops import usage

    ex = make_exec(values={"sql_query": [{"a": 1}]}, spend_per_call=0.25)
    for i in range(4):
        ex(f"c{i}", "sql_query", {"sql": f"SELECT {i}"}, {}, [])
    # TreeGuard.spent() 读的就是 summarize()["cost_usd"] —— 用同一个口径验
    assert usage.summarize()["cost_usd"] == pytest.approx(1.0, rel=0.02), (
        "烧了 4 × $0.25 却没落账 —— spend_per_call 不带 faults 时又被静默丢了")
