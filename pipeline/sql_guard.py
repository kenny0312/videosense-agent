"""
只读 SQL 守门 —— 一处定义,MCP server 与 mock DB 共用。

旧检查是 `sql.upper().startswith("SELECT")`,会误杀合法的 CTE(`WITH ... SELECT`)。
这里改成:
    - 首关键字必须是 SELECT 或 WITH
    - 剥掉字符串字面量后,不得出现任何写关键字(INSERT/UPDATE/DELETE/DROP/...)
      (Postgres 允许 `WITH x AS (DELETE ... RETURNING)`,所以 WITH 也要查写关键字)
"""
from __future__ import annotations

import re

_WRITE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"REPLACE|MERGE|COPY|VACUUM|ATTACH|DETACH|PRAGMA|INTO)\b",
    re.IGNORECASE,
)
_STRINGS = re.compile(r"'[^']*'")


def is_read_only(sql: str) -> bool:
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False
    first = s.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH"):
        return False
    # 剥掉字符串字面量,避免 ILIKE '%update%' 这类内容误判
    no_strings = _STRINGS.sub("''", s)
    return _WRITE.search(no_strings) is None


def assert_read_only(sql: str) -> None:
    if not is_read_only(sql):
        raise ValueError("只允许只读查询(SELECT / WITH ... SELECT),不允许写操作")
