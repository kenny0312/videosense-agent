"""
SQL 自愈器 —— sql_query 节点执行失败时,把 DB 报错回喂、基于报错重写 SQL。

与 code_generator 的关系(两者同形):
    code_generator 修"生成的 Python"(沙箱节点);
    sql_fixer     修"planner 写的 SQL"(数据节点)。
    都是"出错 → 回喂报错 → 重写",各自带 history 防止重复同一个错误。

本类只负责"重写 SQL 字符串";要不要重试、试几次,由 node_executor 控制
(对称 _run_sandbox_node 的 CodeGenerator.repair)。
"""
from __future__ import annotations

import json
import logging

import vertexai
from vertexai.generative_models import GenerativeModel

from pipeline import config
from pipeline.code_generator import _strip_fence   # 复用同一套去围栏逻辑

log = logging.getLogger("pipeline.sql_fixer")


def _prompt(bad_sql: str, db_error: str, schema: dict, prior: list[tuple[str, str]]) -> str:
    prior_block = ""
    if prior:
        lines = "\n".join(f"- 试过: {s}\n  仍报错: {e}" for s, e in prior)
        prior_block = f"\n# 之前失败过的尝试(别再重复这些写法)\n{lines}\n"
    return f"""你是一个 SQL 修复器。下面这条只读 SELECT 在 AlloyDB / Postgres 上执行失败了。
请**只**输出修好的 SQL,严格遵守:
- 只用下面 schema 里**真实存在**的表名和列名(列名必须完全一致)
- 必须仍是只读查询(SELECT 或 WITH ... SELECT),不得有任何写操作
- 不要任何解释、不要 markdown 围栏,只输出 SQL 本身

# 数据库结构(列名必须严格一致)
{json.dumps(schema, ensure_ascii=False, indent=2)}

# 出错的 SQL
{bad_sql}

# 数据库报错
{db_error}
{prior_block}"""


class SqlFixer:
    """单个 sql_query 节点的 SQL 自愈器(含失败 history,防重复)。"""

    def __init__(self) -> None:
        vertexai.init(project=config.GCP_PROJECT, location=config.GCP_REGION)
        self.model = GenerativeModel(config.CODEGEN_MODEL)
        self._prior: list[tuple[str, str]] = []

    def repair(self, bad_sql: str, db_error: str, schema: dict) -> str:
        log.info("修复 SQL (第 %d 次)", len(self._prior) + 1)
        resp = self.model.generate_content(
            _prompt(bad_sql, db_error, schema, self._prior),
            generation_config={"temperature": 0.0},
        )
        fixed = _strip_fence(resp.text)
        self._prior.append((bad_sql, db_error))   # 记下这次失败,下一轮回喂
        return fixed
