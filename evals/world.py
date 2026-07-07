"""ScriptedWorld —— 离线脚本车道：把"假大脑"和"假工具结果"喂给真 run_loop。

Offline scripted harness. No Gemini, no network, no DB. Wraps pipeline.run_loop with a
scripted conversation (ScriptedConv) + a stubbed tool executor (make_exec), copied from
the shape already used by pipeline/test_loop_driver.py so it stays faithful to prod.

写任务用的 snapshot / state_diff 先留占位（下一步接真执行器时再填）。
"""
from __future__ import annotations

import os

from pipeline.loop_driver import Call, ExecResult, run_loop  # noqa: F401  (Call re-exported for policies)


class ScriptedConv:
    """按脚本依次返回 (calls, text)，忽略发来的 msg —— 就是把"大脑的决定"写死。"""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return self.script.pop(0)


def make_exec(values=None, fail=()):
    """stub 工具执行器：按工具名返回固定结果（把"工具/DB 的输出"写死）。"""
    seen = []

    def execute(cid, name, inputs, upstream, uses):
        seen.append({"cid": cid, "name": name, "inputs": inputs})
        if name in fail:
            return ExecResult(ok=False, stderr="boom")
        val = (values or {}).get(name, [{"v": 1}])
        return ExecResult(ok=True, value=val, preview=val[:1], n=len(val))

    execute.seen = seen
    return execute


class ScriptedWorld:
    """一道题的最小测试环境：给定脚本策略 + 固定工具结果，跑一次真 run_loop。"""

    def __init__(self, script, tool_results=None, fail=()):
        self.script = script
        self.tool_results = tool_results or {}
        self.fail = fail

    def run(self, user_query, max_steps: int = 16):
        conv = ScriptedConv(self.script)
        execute = make_exec(values=self.tool_results, fail=self.fail)
        return run_loop(user_query, conv, execute, max_steps=max_steps)

    # ── 占位：写任务（update_memory / 建索引）接真执行器后再填 ──
    def snapshot(self) -> dict:  # pragma: no cover - placeholder for Mode B
        raise NotImplementedError("写任务 state-diff 属于下一步（接真执行器）")

    def state_diff(self, before, after, target):  # pragma: no cover - placeholder
        raise NotImplementedError("写任务 state-diff 属于下一步（接真执行器）")


# ── Mode B：真 Gemini 大脑进循环 ──────────────────────────────────────
def live_preflight():
    """检查能不能跑 Mode B。能跑返回 None；否则返回一段"缺什么、怎么配"的说明。"""
    from pipeline import config

    proj = os.environ.get("GCP_PROJECT") or getattr(config, "GCP_PROJECT", "")
    if not proj or proj == "your-gcp-project-id":
        return (
            "没配 GCP 凭证 —— Mode B 要真 Gemini。请先设：\n"
            "  set GCP_PROJECT=<你的项目>\n"
            "  set GENAI_LOCATION=global\n"
            "  set GOOGLE_APPLICATION_CREDENTIALS=<service-account.json>   (或配好 gcloud ADC)\n"
            "  set REPL_USE_MOCK_DB=1     # 用 mock DB，不碰生产数据\n"
            "然后： python -m evals.runner --live --n 1     # 先用 n=1 冒烟（真 Gemini 会花 token）"
        )
    return None


class LiveWorld:
    """Mode B：真 Gemini 大脑进循环。工具走真执行器；默认 mock DB（不碰生产 + 省钱），
    analyze_video 走缓存（record-replay）。需要 GCP 凭证 —— 见 live_preflight()。

    装配复刻 pipeline.run_query_loop（loop_driver.py:579-583），但直接调 run_loop，
    这样拿到的 LoopResult（trace/ledger/answer）能被同一套判分器复用。
    """

    def __init__(self, owner: str = "eval", use_mock_db: bool = True):
        if use_mock_db:
            os.environ.setdefault("REPL_USE_MOCK_DB", "1")
        self.owner = owner

    def run(self, user_query, max_steps: int = 16):
        from pipeline import config, loop_driver, mcp_client
        from pipeline.trace import Trace
        from sandbox.client import SandboxClient

        schema = mcp_client.get_schema()
        conv = loop_driver.make_conversation(
            config.LOOP_MODEL,
            loop_driver.loop_function_declarations(),
            loop_driver._loop_system(schema, None, None),
        )
        execute = loop_driver._make_executor(SandboxClient(), Trace(), schema, None, owner=self.owner)
        return loop_driver.run_loop(user_query, conv, execute, max_steps=max_steps)
