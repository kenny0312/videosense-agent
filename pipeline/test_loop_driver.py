"""M3:loop 驱动器【控制流】离线单测(注入 stub conversation + stub execute)。

不调 Gemini、不碰 DB/沙箱 —— live 路径已由 M2 spike(spikes/loop_spike.py)验过。
这里只验:收敛、句柄→upstream 解析、max_steps、重复失败终止、声明叠加、合成 DAG。
"""
from pipeline import loop_driver as ld
from pipeline.loop_driver import Call, ExecResult, run_loop


class ScriptedConv:
    """按脚本依次返回 (calls, text);忽略发来的 msg。"""
    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return self.script.pop(0)


def make_exec(values=None, fail=()):
    seen = []

    def execute(cid, name, inputs, upstream, uses):
        seen.append({"cid": cid, "name": name, "inputs": inputs,
                     "uses": list(uses), "upstream": dict(upstream)})
        if name in fail:
            return ExecResult(ok=False, stderr="boom")
        val = (values or {}).get(name, [{"v": 1}])
        return ExecResult(ok=True, value=val, preview=val[:1], n=len(val))

    execute.seen = seen
    return execute


def test_converges_on_text():
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "SELECT 1"}, [])], None),
        ([], "答案在此"),
    ])
    r = run_loop("q", conv, make_exec(), max_steps=8)
    assert r.terminated == "text" and r.answer == "答案在此"
    assert r.steps == 1 and len(r.ledger) == 1 and r.llm_calls == 2


def test_handle_resolution_passes_upstream_in_order():
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "video"}, []), Call("sql_query", {"sql": "sensor"}, [])], None),
        ([Call("merge_asof", {"left_on": "ts", "right_on": "t", "tolerance_ms": 500},
               ["c0_0", "c0_1"])], None),
        ([], "merged"),
    ])
    ex = make_exec(values={"sql_query": [{"x": 1}], "merge_asof": [{"m": 1}]})
    r = run_loop("q", conv, ex, max_steps=8)
    assert r.answer == "merged"
    merge = [c for c in ex.seen if c["name"] == "merge_asof"][0]
    assert merge["uses"] == ["c0_0", "c0_1"]                 # 顺序保留(左、右)
    assert set(merge["upstream"]) == {"c0_0", "c0_1"}        # upstream 由 ledger 解析得到


def test_max_steps_termination():
    conv = ScriptedConv([([Call("sql_query", {"sql": "x"}, [])], None)] * 10)
    r = run_loop("q", conv, make_exec(), max_steps=3)
    assert r.terminated == "max_steps" and r.answer is None and r.steps == 3


def test_repeat_failure_termination():
    conv = ScriptedConv([([Call("sql_query", {"sql": "bad"}, [])], None)] * 10)
    ex = make_exec(fail={"sql_query"})
    r = run_loop("q", conv, ex, max_steps=8, repeat_limit=2)
    assert r.terminated == "repeat" and r.answer is None
    # 重复上限=2:执行了 2 次后第 3 次循环前被拦
    assert sum(1 for c in ex.seen if c["name"] == "sql_query") == 2


def test_failed_step_feeds_error_not_crash():
    conv = ScriptedConv([
        ([Call("sql_query", {"sql": "bad"}, [])], None),     # 失败一次
        ([Call("sql_query", {"sql": "good"}, [])], None),    # 模型改正(不同参数 → 不算重复)
        ([], "好了"),
    ])
    ex = make_exec(fail={})                                   # 都成功;上面靠不同参数区分
    # 让第一次失败:用一个按 inputs 决定成败的执行器
    def exec2(cid, name, inputs, upstream, uses):
        ex.seen.append({"cid": cid, "name": name, "inputs": inputs})
        if inputs.get("sql") == "bad":
            return ExecResult(ok=False, stderr="syntax error")
        return ExecResult(ok=True, value=[{"v": 1}], preview=[{"v": 1}], n=1)
    r = run_loop("q", conv, exec2, max_steps=8)
    assert r.answer == "好了" and r.terminated == "text"
    assert r.trace[0]["ok"] is False and r.trace[1]["ok"] is True


def test_declarations_have_handles_without_mutating_specs():
    decls = ld.loop_function_declarations()
    merge = next(d for d in decls if d["name"] == "merge_asof")
    assert "left_result_id" in merge["parameters"]["properties"]
    assert "right_result_id" in merge["parameters"]["required"]
    plot = next(d for d in decls if d["name"] == "plot")
    assert "data_result_id" in plot["parameters"]["required"]
    show = next(d for d in decls if d["name"] == "show_video")
    assert "data_result_id" in show["parameters"]["properties"]
    assert "data_result_id" not in show["parameters"]["required"]   # show_video 句柄可选
    # SPECS 未被污染
    from pipeline.node_specs import SPECS
    assert "data_result_id" not in SPECS["plot"].parameters["properties"]


def test_synthesize_dag_skips_failed_and_links_deps():
    trace = [
        {"cid": "c0_0", "tool": "sql_query", "inputs": {"sql": "x"}, "uses": [], "ok": True},
        {"cid": "c1_0", "tool": "plot", "inputs": {"kind": "scatter", "x": "a", "y": "b"},
         "uses": ["c0_0"], "ok": True},
    ]
    dag = ld.synthesize_dag(trace)
    assert dag is not None and len(dag.nodes) == 2
    assert dag.nodes[1].depends_on == ["c0_0"]
    bad = trace + [{"cid": "c2_0", "tool": "ols_regress", "inputs": {"y": "a", "x": ["b"]},
                    "uses": ["c1_0"], "ok": False}]
    assert len(ld.synthesize_dag(bad).nodes) == 2            # 失败步不进
    assert ld.synthesize_dag([]) is None


def test_loop_metrics():                                     # M6 审计指标
    lo = ld.LoopOutcome(answer="x", steps=3, terminated="text", dag=None, node_values={},
                        results={}, trace=[{"tool": "sql_query"}, {"tool": "plot"},
                                           {"tool": "sql_query"}])
    assert ld.loop_metrics(lo) == {"steps": 3, "terminated": "text",
                                   "tool_calls": {"sql_query": 2, "plot": 1}}
