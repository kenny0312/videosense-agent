"""批次 1 联合审查的六条必修 —— 每条都是【关于数字的诚实】。

共同的病根:服务端加了上界之后,"我们取回了多少"和"库里到底有多少"变成两个数,
而链路上有六处还在把前者当后者说给下游听。每一处的后果都是**自信的假数字**:

  1. `_truncated_shell` 的话术说"还有 N 行"(读成"另外还有")→ 大脑推出的总数虚报一倍
  2. `_sql_error_note` 对"拿到码但不在白名单"的错误说"连 SQLSTATE 都没给出"+"别改 SQL"
  3. show_table 侧信道的 `n` 语义变了(取回数),前端仍当确切总数渲染
  4. 薄壳原样进 transcript → 下一轮回放说"结果(共1行)"(实际 2000 行)
  5. `data_<id>_meta` 只注入不进 prompt → 生成模型看不见,拿子集算总数/均值
  6. 四处注释描述的是被推翻的旧契约(截断受开关控制)

这些都不是"少了个功能",是**说了假话**。所以测试直接钉话术里的关键子串。
"""
from __future__ import annotations

import pytest

from pipeline import code_generator as CG
from pipeline import loop_memory as LM
from pipeline import node_executor as ne


# ── 必修 1:total 是【含】带回那些的下界,不是"另外还有" ──────────────
def test_truncation_note_does_not_double_count_the_total():
    """服务端多读一行确认截断,所以 total ≡ returned+1。

    说"带回 2000 行,还有 2001 行"会被读成"另外还有" → 总数 ≥4001,
    而真实保证只有 ≥2001。截断话术上多说一行都是假陈述。
    """
    shell = ne._truncated_shell([{"id": i} for i in range(2000)],
                                {"returned": 2000, "total_seen": 2001, "reason": "row_cap"})
    note = shell["_note"]
    # 断言要钉【有害的那个搭配】,不是"还有"两个字本身 ——
    # 澄清句"不是另外还有这么多"里也带这两个字,一刀切会把正确的写法也判红。
    assert "至少还有" not in note, f"【至少还有 N 行】会被读成【另外还有】,总数虚报一倍:{note}"
    assert "库里至少还有" not in note
    assert "总共至少 2001 行" in note
    assert "已经包含带回的 2000 行" in note, "没说清 total 含不含带回的那些,读者只能猜"
    assert shell["_total"] == 2001 and shell["_returned"] == 2000


# ── 必修 2:拿到码但不在白名单 → 别说"没给码",也别堵死改写 ────────────
@pytest.mark.parametrize("code,err", [
    ("42803", 'column "v.id" must appear in the GROUP BY clause'),
    ("42883", "function date_trunc(unknown, text) does not exist"),
    ("22P02", "invalid input syntax for type integer"),
])
def test_unlisted_sqlstate_is_not_reported_as_missing(code, err):
    note = ne._sql_error_note(err, code, 0)
    assert "SQLSTATE 都没给出" not in note, (
        f"码明明是 {code},却对大脑说没拿到码 —— 同一步的 trace 里还记着它,自相矛盾")
    assert "这不是 SQL 写错了" not in note, f"{code} 就是 SQL 写错了"
    assert "别把 SQL 改来改去" not in note, "堵死了唯一能救的动作"
    assert code in note and "自己改一版" in note


def test_transport_error_without_code_still_says_so():
    """反向锁:真的没拿到码时,那句话必须还在。"""
    note = ne._sql_error_note("server closed the connection unexpectedly", None, 0)
    assert "SQLSTATE 都没给出" in note and "这不是 SQL 写错了" in note


@pytest.mark.parametrize("code", ["42601", "42703", "42P01"])
def test_whitelisted_codes_still_take_the_repair_path(code):
    assert "SQL 写错了" in ne._sql_error_note("boom", code, 2)


@pytest.mark.parametrize("code", ["57014", "55P03"])
def test_overload_codes_still_say_dont_rewrite(code):
    note = ne._sql_error_note("boom", code, 0)
    assert "这不是 SQL 写错了" in note and "变轻" in note


# ── 必修 3:侧信道要带 at_least,前端才有的可渲染 ─────────────────────
def test_show_table_side_channel_carries_the_lower_bound():
    """`n` 的语义在本批次变了(从"真实总数"变成"取回了多少")。

    唯一的消费者是前端表头,它比答案区更醒目 —— 只改答案文案不改侧信道,
    等于让用户看着一个自信的假总数。
    """
    from pipeline.dag_schema import Node

    rows = [{"video_id": f"v{i}"} for i in range(50)]
    shell = ne._truncated_shell(rows, {"returned": 50, "total_seen": 51, "reason": "byte_cap"})
    node = Node(id="c1_0", tool="show_table", inputs={}, depends_on=["c0_0"])
    res = ne._run_show_table(node, {"c0_0": shell})

    assert res.table["truncated"] is True
    assert res.table["at_least"] == 51, "侧信道没带下界,前端只能拿 n 当总数"
    assert res.table["n"] == 50
    assert "至少 51 条" in (res.value or "") or "至少 51 条" in str(res.value)


def test_frontend_renders_the_bound_not_the_bare_count():
    """前端那一行是本批次唯一的用户可见回归点,单独钉住它。"""
    src = (__import__("pathlib").Path(__file__).resolve().parents[2]
           / "web" / "index.html").read_text(encoding="utf-8")
    assert "t.truncated" in src, "前端还没消费 truncated,表头会把取回数当确切总数"
    assert "t.at_least" in src
    assert "+t.n+' rows'+(t.shown" not in src, "旧的裸 t.n 渲染还在"


# ── 必修 4:截断结果进 transcript,回放不许说"共 1 行" ────────────────
def test_truncated_result_does_not_replay_as_one_row():
    rows = [{"video_id": f"v{i}"} for i in range(2000)]
    shell = ne._truncated_shell(rows, {"returned": 2000, "total_seen": 2001, "reason": "row_cap"})

    events: list = []

    class _Res:
        ok = True
        value = shell
        stderr = ""

    LM.record_loop_turn(
        _Store(events), "o", "s1", 1, "问题",
        [{"cid": "c0_0", "tool": "sql_query", "inputs": {"sql": "SELECT 1"},
          "uses": [], "ok": True}],
        {"c0_0": _Res()}, "答案", blob_put=None)

    ev = next(e for e in events if e.get("type") == "tool_result")
    # 大本体被 append_event 溢出走了(pop("value")),留下的是预览 + 行数。
    # 关键就是这个 n:薄壳原样落盘时 _preview 走 dict 分支,n 恒为 1 —— 那正是
    # 下一轮回放里"结果(共1行)"的来源,而实际是 2000 行。
    assert ev.get("n") == 2000, (
        f"落盘行数是 {ev.get('n')},应为 2000 —— 薄壳没被拆开,_preview 走了 dict 分支")
    assert ev.get("truncated") is True and ev.get("total") == 2001, (
        "截断信息没跟着存下来,下一轮回放就说不出'未取全'")

    line = LM._render_turn(1, [ev])
    assert "共1行" not in line, f"回放对大脑说了假计数:{line}"
    assert "至少2001行" in line.replace(" ", ""), f"回放没说这是下界:{line}"


class _Store:
    """最小 transcript store 替身:只接住 append。"""

    def __init__(self, sink):
        self.sink = sink

    def append(self, key, line):          # 与 transcript_store 的 append(key, line) 同签名
        self.sink.append(line)

    def read(self, *a, **k):
        return list(self.sink)


# ── 必修 5:截断信息必须进 codegen 的 prompt,不能只注入变量 ───────────
def test_codegen_prompt_tells_the_model_the_upstream_was_capped():
    up = {"c0_0": [{"id": i} for i in range(2000)]}
    meta = {"c0_0": {"truncated": True, "returned": 2000, "total": 2001}}

    plain = CG._upstream_preview(up)
    capped = CG._upstream_preview(up, up_meta=meta)

    assert "共 2000 行" in plain, "没截断时的措辞变了(应与升级前逐字节一致)"
    assert "共 2000 行" not in capped, "截断了还在说【共 N 行】 —— 模型会拿子集当全集"
    assert "上游已被截断" in capped and "总共至少 2001 行" in capped
    assert "data_c0_0_meta" in capped, "没告诉模型那个变量的存在 = 变量是死的"
    assert "不要把 len(data_c0_0) 当成总数" in capped


def test_codegen_prompt_unchanged_when_not_truncated():
    """反向锁:没截断时 prompt 逐字节不变(不给未截断的路增加噪声)。"""
    up = {"c0_0": [{"id": 1}]}
    assert CG._upstream_preview(up) == CG._upstream_preview(up, up_meta={})
    assert CG._upstream_preview(up) == CG._upstream_preview(
        up, up_meta={"c0_0": {"truncated": False}})


# ── 必修 6:注释不许再描述被推翻的旧契约 ──────────────────────────────
def test_no_comment_still_claims_the_flag_gates_truncation():
    """注释是决策存档。§12 规则 3 之后,【开关决定要不要报截断】这句话是反的。

    on-call 把 USE_BOUNDED_SQL 翻回 0 想【恢复旧 wire】时,截断路径仍是信封 ——
    门面文档说这不可能,那就会浪费一次故障排查。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    bad = []
    for rel in ("pipeline/config.py", "pipeline/sql_bounds.py",
                "pipeline/mcp_client.py", "mcp_server/server.py"):
        for i, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith(("#", '"""', "'''", "*")) and '"""' not in line:
                continue
            if "开关" in line and "截断" in line and "不受" not in line and "恒报" not in line:
                bad.append(f"{rel}:{i}  {line.strip()[:70]}")
    assert not bad, ("这些注释还在说开关管截断(§12 规则 3 之后是反的):\n  "
                     + "\n  ".join(bad))
