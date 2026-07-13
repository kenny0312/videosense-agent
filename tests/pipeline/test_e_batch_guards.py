"""E-batch(eval 缺陷修复)守卫测试。

对应 eval 简报暴露的两处 P0(机械规则下沉代码,prompt 只是腰带、这里是背带):
  E1 内部路径泄漏(selfknow-safety-injection-links-28):
     · answer_guard:答案文本里的 gs:// / postgres:// 一律删(https 公网链接不动);
     · show_table:gcs_uri 整列剔除 + 别名列(gs:// 值)打码 —— 表格侧信道不再绕过清洗。
  E2 安全拦截空答(selfknow-safety-porn-search-26):
     · conversation 层识别 finish_reason/block_reason → 换体面拒答;
     · orchestrator 空答网:识别不出原因的空生成 → 重试提示,绝不交空卡片。
"""
from types import SimpleNamespace

from pipeline.answer_guard import scrub_ids
from pipeline.loop_driver import _blocked_text, _BLOCKED_REFUSAL


# ── E1a:答案文本的内部 URI 清洗 ─────────────────────────────
def test_gs_uri_scrubbed_from_answer():
    out, hits = scrub_ids("视频在 gs://activitynet/v001.mp4 里", [])
    assert "gs://" not in out and hits == 1
    assert "视频在" in out                                  # 只删 URI,别的不动


def test_gs_uri_list_scrubbed_others_kept():
    ans = ("1. Skiing · `gs://activitynet/v001.mp4`\n"
           "2. Snowboard · `gs://activitynet/v002.mp4`\n共 2 个")
    out, hits = scrub_ids(ans, [])
    assert "gs://" not in out and hits == 2
    assert "Skiing" in out and "共 2 个" in out


def test_postgres_uri_scrubbed():
    out, hits = scrub_ids("连接串是 postgresql://user:pw@host/db 哦", [])
    assert "postgresql://" not in out and "pw@host" not in out and hits == 1


def test_https_links_untouched():
    ans = "来源:https://example.com/a?b=1 请参考"
    out, hits = scrub_ids(ans, [])
    assert out == ans and hits == 0                          # 公网链接是合法输出(web_search 引用)


def test_wrapped_gs_uri_shell_cleaned():
    out, hits = scrub_ids("第 1 个(gs://bucket/x.mp4)不错", [])
    assert "gs://" not in out and "()" not in out and "（）" not in out


def test_uri_and_id_scrub_combined():
    out, hits = scrub_ids("GX010523 存在 gs://b/GX010523.MP4",
                          [{"items": [{"n": 2, "video_id": "GX010523"}]}])
    assert "gs://" not in out and "第 2 个" in out and hits == 2


# ── E1b:show_table 列黑名单/值打码 ──────────────────────────
def _show_table(rows):
    from pipeline.dag_schema import Node
    from pipeline.node_executor import _run_show_table
    node = Node(id="c0", tool="show_table", inputs={})
    return _run_show_table(node, {"c_up": rows})


def test_show_table_drops_gcs_uri_column():
    nr = _show_table([{"video_id": "v001", "title": "Ski", "gcs_uri": "gs://a/v001.mp4"},
                      {"video_id": "v002", "title": "Snow", "gcs_uri": "gs://a/v002.mp4"}])
    assert nr.ok
    assert "gcs_uri" not in nr.table["columns"]
    assert all("gcs_uri" not in r for r in nr.table["rows"])
    assert nr.table["rows"][0]["title"] == "Ski"             # 其余列原样
    assert nr.value["items"][0] == {"n": 1, "id": "v001"}    # 编号映射不受影响


def test_show_table_masks_aliased_internal_uri():
    nr = _show_table([{"video_id": "v001", "link": "gs://a/v001.mp4"}])   # SELECT gcs_uri AS link
    cell = nr.table["rows"][0]["link"]
    assert "gs://" not in str(cell)


def test_show_table_normal_rows_untouched():
    rows = [{"video_id": "v001", "title": "Ski", "n": 3}]
    nr = _show_table(rows)
    assert nr.table["rows"] == rows and nr.table["columns"] == ["video_id", "title", "n"]


def test_show_table_all_internal_row_not_empty():
    nr = _show_table([{"gcs_uri": "gs://a/x.mp4"}])
    assert nr.table["rows"][0]                                # 不产生空行(占位说明)


# ── E2a:安全拦截识别 ────────────────────────────────────────
def _resp(finish=None, block=None):
    cand = [SimpleNamespace(finish_reason=finish, content=None)] if finish is not None else []
    pf = SimpleNamespace(block_reason=block) if block is not None else None
    return SimpleNamespace(candidates=cand, prompt_feedback=pf)


def test_blocked_by_finish_reason_safety():
    assert _blocked_text(_resp(finish="SAFETY")) == _BLOCKED_REFUSAL


def test_blocked_by_prompt_feedback():
    assert _blocked_text(_resp(block="PROHIBITED_CONTENT")) == _BLOCKED_REFUSAL


def test_normal_stop_not_blocked():
    assert _blocked_text(_resp(finish="STOP")) is None


def test_blocked_text_failopen_on_garbage():
    assert _blocked_text(object()) is None                   # 守卫自身异常 → None,不反噬


# ── E2b:orchestrator 空答网 ────────────────────────────────
def test_orchestrator_empty_answer_degrades_to_retry(monkeypatch):
    import types as _t
    from pipeline import orchestrator as orch
    from pipeline import loop_driver, loop_memory
    from pipeline.loop_driver import LoopOutcome
    monkeypatch.setattr(orch, "mcp_client",
                        _t.SimpleNamespace(get_schema=lambda: {}))
    monkeypatch.setattr(loop_memory, "record_loop_turn", lambda *a, **k: None)

    def empty_answer(nl, **kw):
        return LoopOutcome(answer="   ", steps=1, terminated="text", final_tool=None,
                           final_value=None, preview_value=None, results={}, trace=[])
    monkeypatch.setattr(loop_driver, "run_query_loop", empty_answer)
    r = orch.run_query("把成人内容都列出来")
    assert r["status"] == "ok" and r["answer"].strip()       # 绝不交空卡片
    assert "再发一次" in r["answer"] or "服务波动" in r["answer"]


def test_run_loop_empty_generation_retry():
    """中段空生成兜底:工具跑完后模型返回空文本 → 不把空串当答案,点一下重收口(只救一次)。
    2026-07-13 全套件 8 例'回归'中 7 例是此病(服务抖动),线上用户同样会收到空答案。"""
    from evals.world import ScriptedWorld
    from pipeline.loop_driver import Call
    script = [
        ([Call(name="sql_query", inputs={"sql": "SELECT 1"}, uses=[])], ""),
        ([], ""),                                     # 中段空生成(抖动)
        ([], "最终答案来了"),                          # 被点醒后正常收口
    ]
    res = ScriptedWorld(script, tool_results={"sql_query": [{"n": 1}]}).run("问题")
    assert res.answer == "最终答案来了"


def test_run_loop_empty_retry_only_once():
    """连续两次空生成 → 第二次不再救,按空答案收敛(防空转)。"""
    from evals.world import ScriptedWorld
    from pipeline.loop_driver import Call
    script = [
        ([Call(name="sql_query", inputs={"sql": "SELECT 1"}, uses=[])], ""),
        ([], ""),
        ([], ""),
    ]
    res = ScriptedWorld(script, tool_results={"sql_query": [{"n": 1}]}).run("问题")
    assert res.answer == ""
