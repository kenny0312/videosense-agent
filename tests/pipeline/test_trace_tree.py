"""P0-2 / T-1 / T-2:trace 树结构 + 因由码纪律 + 落盘 + triage 定位。

验收目标(总任务书 Part T):测试挂了,一条命令能指认【哪一层设计】出问题。
所以本文件既测 trace 本身,也测 scripts/trace_report.py 的定位能力(故障注入)。
离线、零 API。
"""
import json
import threading

import pytest

from pipeline.agentops import trace as T
from pipeline.agentops import trace_report as TR


def test_backward_compatible_api_unchanged():
    """升级不能碰坏既有调用方(node_executor 等用的就是这三个方法)。"""
    tr = T.Trace(quiet=True)
    s = tr.step("Planning DAG")
    s.ok(nodes=3)
    s2 = tr.step("sql retry")
    s2.fail(error="boom", will_retry=True)
    assert [x["status"] for x in tr.as_list()] == ["ok", "retry"]
    assert tr.as_list()[0]["meta"]["nodes"] == 3
    assert "trace:" in tr.summary_line()


def test_span_ids_unique_under_parallel_appends():
    """span_id 用模块计数器而非 len(steps) —— 并行子 agent 下 len 会撞号(红队 A5)。"""
    tr = T.Trace(quiet=True)
    def worker():
        for _ in range(40):
            tr.step("x").ok()
    ths = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    ids = [s["span_id"] for s in tr.as_list()]
    assert len(ids) == 240 and len(set(ids)) == 240      # 一个都不许重复
    assert len(tr.steps) == 240                          # append 也没丢


def test_child_trace_carries_parent_and_depth_sharing_one_list():
    """子 agent 用 child() 记步:同一条 steps 列表,但带父子关系与深度。"""
    tr = T.Trace(quiet=True)
    root = tr.step("spawn_agents")
    sub = tr.child(parent_span_id=root.span_id, component="exec")
    a = sub.step("subagent A")
    a.ok()
    grand = sub.child(parent_span_id=a.span_id, component="exec")
    grand.step("depth-2 agent").ok()
    root.ok()
    rows = {s["name"]: s for s in tr.as_list()}
    assert rows["subagent A"]["parent_id"] == root.span_id
    assert rows["subagent A"]["depth"] == 1 and rows["subagent A"]["component"] == "exec"
    assert rows["depth-2 agent"]["depth"] == 2
    assert rows["depth-2 agent"]["parent_id"] == rows["subagent A"]["span_id"]
    assert len(tr.steps) == 3            # 全在一份可序列化的树里


def test_cause_enum_hygiene_three_ways():
    """因由码纪律:漏打/表外/挂错层,三种都留痕(fail-open,绝不抛)。"""
    tr = T.Trace(quiet=True, component="exec")
    tr.step("no cause").fail(error="x")                       # 漏打
    tr.step("bogus").fail(error="x", cause="NOT_A_REAL_CODE")  # 表外
    tr.step("wrong layer").fail(error="x", cause="GUARD_BUDGET")   # 码属 guard 层
    metas = [s["meta"] for s in tr.as_list()]
    assert metas[0].get("cause_missing") is True
    assert metas[1].get("cause_unknown") is True
    assert metas[2].get("cause_component_mismatch") == "exec"


def test_main_component_may_omit_cause():
    """主 loop 层允许无码(它就是兜底层),不刷 cause_missing 噪声。"""
    tr = T.Trace(quiet=True)          # component 默认 main
    tr.step("whatever").fail(error="x")
    assert "cause_missing" not in tr.as_list()[0]["meta"]


def test_soft_and_refused_statuses_and_billing():
    tr = T.Trace(quiet=True, component="guard")
    s = tr.step("budget check")
    s.bill(tok={"in": 100, "out": 20, "thought": 30}, cost_usd=0.0123)
    s.soft("GUARD_BUDGET", error="cap reached")
    r = T.Trace(quiet=True, component="main").step("second parallel call")
    r.refuse("MAIN_TOOL_ERROR")
    assert tr.as_list()[0]["status"] == "softfail"
    assert tr.as_list()[0]["cause"] == "GUARD_BUDGET"
    assert tr.as_list()[0]["tok"]["thought"] == 30
    assert tr.total_cost() == pytest.approx(0.0123)
    assert r.status == "refused"
    assert tr.failures() and tr.cause_counts() == {"GUARD_BUDGET": 1}


def test_all_enum_causes_belong_to_declared_component():
    """枚举表自身的一致性:没有码同时属于两层,层名都在 COMPONENTS 里。"""
    seen = {}
    for comp, codes in T.CAUSES.items():
        assert comp in T.COMPONENTS
        for c in codes:
            assert c not in seen, f"{c} 同时属于 {seen.get(c)} 与 {comp}"
            seen[c] = comp
    assert seen and T.ALL_CAUSES == frozenset(seen)


def test_dump_trace_atomic_and_reloadable(tmp_path):
    tr = T.Trace(quiet=True)
    tr.step("a").ok()
    p = T.dump_trace(tr, str(tmp_path / "sub" / "t1.json"), trace_id="req-1")
    payload = json.loads(open(p, encoding="utf-8").read())
    assert payload["trace_id"] == "req-1" and len(payload["steps"]) == 1
    assert not list(tmp_path.glob("**/*.tmp"))       # 临时文件已清
    assert "total_cost_usd" in payload and "cause_counts" in payload


# ── T-3 故障注入验收:四种故障,triage 必须指认正确的设计层 ──
def _mk(tmp_path, name, component, cause, status="error"):
    tr = T.Trace(quiet=True, component=component)
    s = tr.step(name)
    (s.soft(cause) if status == "softfail" else s.fail(error="x", cause=cause))
    return T.dump_trace(tr, str(tmp_path / f"{name}.json"), trace_id=name)


def test_triage_pinpoints_each_layer_synthetic(tmp_path):
    _mk(tmp_path, "subagent_blew_up", "exec", "EXEC_TOOL_ERROR")
    _mk(tmp_path, "budget_cut", "guard", "GUARD_BUDGET", status="softfail")
    _mk(tmp_path, "wave_timeout", "sub", "SUB_LEASE_TIMEOUT")
    _mk(tmp_path, "enqueue_died", "sub", "SUB_ENQUEUE_FAIL")
    report = TR.triage(sorted(str(p) for p in tmp_path.glob("*.json")))
    assert "4 trace" in report and "4 failures" in report
    assert "sub" in report and "exec" in report and "guard" in report
    for code in ("EXEC_TOOL_ERROR", "GUARD_BUDGET", "SUB_LEASE_TIMEOUT", "SUB_ENQUEUE_FAIL"):
        assert code in report
    assert "hygiene" not in report          # 四条都规范打点,不该报卫生问题


def test_triage_surfaces_hygiene_problems(tmp_path):
    """漏打因由码本身是 bug —— triage 必须把它顶到脸上,而不是让失败静默。"""
    tr = T.Trace(quiet=True, component="exec")
    tr.step("silent").fail(error="x")               # 漏打
    T.dump_trace(tr, str(tmp_path / "s.json"), trace_id="s")
    report = TR.triage([str(tmp_path / "s.json")])
    assert "cause_missing" in report and "NO_CAUSE" in report


def test_triage_tolerates_broken_file(tmp_path):
    (tmp_path / "bad.json").write_text("{trunca", encoding="utf-8")
    tr = T.Trace(quiet=True, component="exec")
    tr.step("ok one").ok()
    T.dump_trace(tr, str(tmp_path / "good.json"))
    report = TR.triage([str(tmp_path / "bad.json"), str(tmp_path / "good.json")])
    assert "unreadable_trace_file" in report and "1 trace" in report


def test_render_tree_shows_hierarchy_and_orphan(tmp_path):
    tr = T.Trace(quiet=True)
    root = tr.step("spawn")
    sub = tr.child(parent_span_id=root.span_id, component="exec")
    sub.step("child A").ok()
    root.ok()
    orphan = T.TraceStep(name="lost", span_id="sZZZ", parent_id="s-not-here",
                         component="exec", status="ok")
    tr.steps.append(orphan)
    p = T.dump_trace(tr, str(tmp_path / "t.json"), trace_id="req")
    out = TR.render_tree(json.loads(open(p, encoding="utf-8").read()))
    lines = out.splitlines()
    assert any("spawn" in l for l in lines)
    child_line = next(l for l in lines if "child A" in l)
    assert child_line.startswith("  ")              # 缩进体现层级
    assert any("orphan_span" in l for l in lines)    # 父不在本 trace → 标注出来


# ── 审查补测:CLI 真走 main()(T-3 验收说的"一条命令",此前只测函数直调)──
def test_cli_triage_via_main(tmp_path, capsys):
    _mk(tmp_path, "boom", "exec", "EXEC_TOOL_ERROR")
    assert TR.main(["--triage", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "EXEC_TOOL_ERROR" in out and "1 trace" in out


def test_cli_trace_via_main(tmp_path, capsys):
    tr = T.Trace(quiet=True)
    tr.step("a").ok()
    p = T.dump_trace(tr, str(tmp_path / "t.json"), trace_id="req-9")
    assert TR.main(["--trace", p]) == 0
    assert "req-9" in capsys.readouterr().out


def test_cli_missing_arg_and_bad_path_are_friendly(tmp_path, capsys):
    assert TR.main(["--triage"]) == 2                     # 缺参 → usage,不裸崩
    assert "需要一个路径参数" in capsys.readouterr().out
    assert TR.main(["--trace", "req-abc-not-a-file"]) == 1   # 传 id 而非路径 → 人话
    assert "不是 request_id" in capsys.readouterr().out
    assert TR.main([]) == 2                                # 无参 → 打文档
    assert TR.main(["--triage", str(tmp_path)]) == 1       # 空目录 → 明说没有
    assert "没有 trace json" in capsys.readouterr().out


def test_triage_ignores_non_trace_json(tmp_path):
    """结果目录里混进别的 json(eval 数组/裸 trace list)→ 统计而非整批崩。"""
    (tmp_path / "list.json").write_text("[1,2,3]", encoding="utf-8")
    (tmp_path / "cfg.json").write_text('{"a":1}', encoding="utf-8")
    _mk(tmp_path, "real", "exec", "EXEC_TOOL_ERROR")
    report = TR.triage(sorted(str(p) for p in tmp_path.glob("*.json")))
    assert "not_a_trace_file: 2" in report and "1 trace" in report


def test_triage_flags_unattributed_main_failure(tmp_path):
    """main 层豁免 cause_missing,但"零归因的失败"必须响亮 —— 否则报成体检合格。"""
    tr = T.Trace(quiet=True)              # component=main
    tr.step("sql died").fail(error="boom")
    T.dump_trace(tr, str(tmp_path / "m.json"))
    report = TR.triage([str(tmp_path / "m.json")])
    assert "unattributed_main_failure" in report


def test_bill_accumulates_and_guards_nonfinite():
    tr = T.Trace(quiet=True, component="exec")
    s = tr.step("two calls")
    s.bill(tok={"in": 100, "out": 10, "thought": 5}, cost_usd=0.01)
    s.bill(tok={"in": 50, "thought": 5}, cost_usd=0.02)      # 自愈重试的第二次计费
    assert s.cost_usd == pytest.approx(0.03)                 # 累加,不是覆盖
    assert s.tok["in"] == 150 and s.tok["thought"] == 10
    s.bill(tok={"in": 1})                                    # 只传 tok → 不清零已记成本
    assert s.cost_usd == pytest.approx(0.03)
    s.bill(cost_usd=float("nan"))                            # NaN → 归零并留痕
    assert s.cost_usd == pytest.approx(0.03)
    assert s.meta.get("cost_nonfinite") is True


def test_render_tree_survives_cyclic_parent(tmp_path):
    """人造环形 parent_id(a→b→a)不该让渲染无限递归。"""
    tr = T.Trace(quiet=True)
    a = T.TraceStep(name="A", span_id="sA", parent_id="sB", component="exec", status="ok")
    b = T.TraceStep(name="B", span_id="sB", parent_id="sA", component="exec", status="ok")
    tr.steps.extend([a, b])
    out = TR.render_tree({"steps": tr.as_list()})
    assert "cycle at" in out


def test_render_tree_reports_top_cost_branch(tmp_path):
    """Part T:"钱花在树的哪个枝上必须可见"。"""
    tr = T.Trace(quiet=True)
    root = tr.step("spawn")
    sub = tr.child(parent_span_id=root.span_id, component="exec")
    sub.step("cheap").bill(cost_usd=0.001).ok()
    sub.step("expensive").bill(cost_usd=0.42).ok()
    root.ok()
    out = TR.render_tree({"steps": tr.as_list()})
    assert "top cost:" in out and "expensive" in out.split("top cost:")[1]
