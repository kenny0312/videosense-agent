"""google-genai 客户端单例(global 端点)。

U5 的 loop 后端(GenAIConversation)与 U6 的 web_search grounding 共用 —— gemini-3.x
及 Google Search grounding 都走新 SDK;放中立模块避免 node_executor ↔ loop_driver 循环引用。
"""
from __future__ import annotations

import threading

from pipeline import config

_CLIENT = None
_LOCK = threading.Lock()


def get_client():
    """惰性双检锁单例(与 analyze_cache._redis 同款)。"""
    global _CLIENT
    if _CLIENT is None:
        with _LOCK:
            if _CLIENT is None:
                from google import genai
                _CLIENT = genai.Client(vertexai=True, project=config.GCP_PROJECT,
                                       location=config.GENAI_LOCATION)
    return _CLIENT
