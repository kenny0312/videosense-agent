"""V1 语义检索:embedding 通道(设计 docs/design/semantic-retrieval.md §7)。

text-multilingual-embedding-002(768 维)—— 必须用多语言型号:005 是英文模型,S2 实测
中文查询向量退化(四个不同问题命中同一批结果、分数几乎相同),英文查询则精准;查询是中文、
snippet 是英文,跨语对齐是硬需求。走共享 genai client。
全程 fail-open:embed 失败返回 None —— 调用方(写钩子/回填/检索)自行降级,绝不影响作答。
成本:~$0.025/1M tokens,单条 snippet 几十 token → 整库回填 < $0.01,不单独进 usage 记账
(embed 响应不带 usage_metadata;量级可忽略,诚实标注于此)。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("pipeline.embeddings")

EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-multilingual-embedding-002")
EMBED_DIM = 768
_BATCH = 100                                   # 单次 API 上限内的保守批量


def embed_texts(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> "list[list[float]] | None":
    """批量 embed(入库用 RETRIEVAL_DOCUMENT)。任何失败 → None(fail-open)。"""
    if not texts:
        return []
    try:
        from google.genai import types
        from pipeline.genai_client import get_client
        out: list[list[float]] = []
        client = get_client()
        for i in range(0, len(texts), _BATCH):
            r = client.models.embed_content(
                model=EMBED_MODEL, contents=texts[i:i + _BATCH],
                config=types.EmbedContentConfig(task_type=task_type))
            out.extend([list(e.values) for e in r.embeddings])
        if any(len(v) != EMBED_DIM for v in out):
            log.warning("embedding 维度异常(期望 %d)", EMBED_DIM)
            return None
        return out
    except Exception as e:
        log.warning("embed 失败(fail-open): %r", e)
        return None


def embed_query(text: str) -> "list[float] | None":
    """查询侧 embed(RETRIEVAL_QUERY 任务类型 —— 与文档侧不对称,官方推荐)。"""
    r = embed_texts([text], task_type="RETRIEVAL_QUERY")
    return r[0] if r else None


def vec_literal(vec: list[float]) -> str:
    """向量 → pgvector 字面量 '[0.1,0.2,…]'(psycopg2 传参后接 ::vector 转型)。"""
    return "[" + ",".join(f"{x:.6g}" for x in vec) + "]"
