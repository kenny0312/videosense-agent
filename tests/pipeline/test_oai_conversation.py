# 阶段B:OpenAICompatConversation 的合同测试 —— 本地假端点,零外网零 key。
# 验:双向格式翻译(declarations→tools、结果→tool 消息 + id 按序对位)、
# uses 句柄从 inputs pop 出、usage 垫片入账、成功才落历史、空响应兜底。
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pipeline import config, usage
from pipeline.loop_driver import OpenAICompatConversation, make_conversation


class _FakeOAI(BaseHTTPRequestHandler):
    """按调用次数出剧本:第1次吐 tool_calls(带句柄参数),第2次吐最终文本。"""
    requests_seen: list = []
    scripts: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests_seen.append(body)
        resp = type(self).scripts[min(len(type(self).requests_seen) - 1, len(type(self).scripts) - 1)]
        raw = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):                     # 静音
        pass


def _mk(script):
    _FakeOAI.requests_seen, _FakeOAI.scripts = [], script
    srv = HTTPServer(("127.0.0.1", 0), _FakeOAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _usage_msg(pin=100, pout=20, cached=0):
    return {"prompt_tokens": pin, "completion_tokens": pout,
            "total_tokens": pin + pout, "prompt_tokens_details": {"cached_tokens": cached}}


DECLS = [{"name": "plot", "description": "画图", "parameters":
          {"type": "object", "properties": {"data_result_id": {"type": "string"},
                                            "kind": {"type": "string"}}}}]


def test_full_tool_roundtrip(monkeypatch):
    srv, url = _mk([
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_abc", "type": "function",
             "function": {"name": "plot", "arguments": json.dumps({"data_result_id": "r1", "kind": "bar"})}}]},
            "finish_reason": "tool_calls"}],
         "usage": _usage_msg(100, 20, 30)},
        {"choices": [{"message": {"role": "assistant", "content": "共 3 个视频。"},
                      "finish_reason": "stop"}],
         "usage": _usage_msg(200, 10)},
    ])
    try:
        monkeypatch.setattr(config, "OAI_COMPAT_BASE_URL", url)
        monkeypatch.setattr(config, "OAI_COMPAT_API_KEY", "k-test")
        usage.reset_usage()
        conv = OpenAICompatConversation("qwen3.7-plus", DECLS, "system prompt")

        calls, text = conv.send("有几个视频?")
        assert text is None and len(calls) == 1
        assert calls[0].name == "plot" and calls[0].inputs == {"kind": "bar"}
        assert calls[0].uses == ["r1"]                     # 句柄从 inputs pop 进 uses

        calls2, text2 = conv.send([("plot", {"result_id": "c1", "preview": "ok", "n": 1})])
        assert calls2 == [] and text2 == "共 3 个视频。"

        # 第二个请求:tool 消息按序拿到第一轮的 call id;tools 是 OpenAI 包裹形
        req2 = _FakeOAI.requests_seen[1]
        troles = [m for m in req2["messages"] if m["role"] == "tool"]
        assert troles[0]["tool_call_id"] == "call_abc"
        assert req2["tools"][0] == {"type": "function", "function": DECLS[0]}
        # 历史结构:system, user, assistant(tool_calls), tool, assistant
        assert [m["role"] for m in conv._messages] == ["system", "user", "assistant", "tool", "assistant"]

        # usage 垫片:两次调用合计入账,cached 透传
        u = usage.get_usage()["qwen3.7-plus"]
        assert (u["in"], u["out"], u["calls"], u["cached"]) == (300, 30, 2, 30)
        assert conv.tokens == 330
    finally:
        srv.shutdown()


def test_empty_response_gets_fallback(monkeypatch):
    srv, url = _mk([{"choices": [{"message": {"role": "assistant", "content": ""},
                                  "finish_reason": "content_filter"}], "usage": _usage_msg(10, 0)}])
    try:
        monkeypatch.setattr(config, "OAI_COMPAT_BASE_URL", url)
        monkeypatch.setattr(config, "OAI_COMPAT_API_KEY", "k-test")
        conv = OpenAICompatConversation("qwen3.7-plus", DECLS, "s")
        calls, text = conv.send("hi")
        assert calls == [] and text and len(text) > 5      # 安全拦截 → 体面拒答(E2)
    finally:
        srv.shutdown()


def test_plain_empty_returns_none(monkeypatch):
    """非拦截的空生成 → (,[ None):按 E2 交给上游发重试提示,不在适配器层编话。"""
    srv, url = _mk([{"choices": [{"message": {"role": "assistant", "content": ""},
                                  "finish_reason": "stop"}], "usage": _usage_msg(10, 0)}])
    try:
        monkeypatch.setattr(config, "OAI_COMPAT_BASE_URL", url)
        monkeypatch.setattr(config, "OAI_COMPAT_API_KEY", "k-test")
        conv = OpenAICompatConversation("qwen3.7-plus", DECLS, "s")
        calls, text = conv.send("hi")
        assert calls == [] and text is None
    finally:
        srv.shutdown()


def test_make_conversation_routes_qwen(monkeypatch):
    monkeypatch.setattr(config, "OAI_COMPAT_API_KEY", "k")
    conv = make_conversation("qwen3.7-plus", DECLS, "s")
    assert isinstance(conv, OpenAICompatConversation)


def test_bad_args_json_tolerated(monkeypatch):
    srv, url = _mk([{"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "plot", "arguments": "{oops"}}]},
        "finish_reason": "tool_calls"}], "usage": _usage_msg()}])
    try:
        monkeypatch.setattr(config, "OAI_COMPAT_BASE_URL", url)
        monkeypatch.setattr(config, "OAI_COMPAT_API_KEY", "k-test")
        conv = OpenAICompatConversation("qwen3.7-plus", DECLS, "s")
        calls, _ = conv.send("hi")
        assert calls[0].inputs == {}                       # 烂 JSON 不炸,空参进 executor 报参数错
    finally:
        srv.shutdown()
