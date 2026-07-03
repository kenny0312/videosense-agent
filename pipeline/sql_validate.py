"""
规划期 SQL 静态校验(保守、fail-open)—— 风险点 1 的快速兜底。

parse_dag 只验结构,不看 inputs.sql 的内容;这里在规划期补一道**表名**校验:
SQL 引用的表必须在 schema(= 业务表白名单)里。明确引用未知表 → 抛错 →
被 planner 现有重规划循环捕获 → 带错重规划。

设计原则 —— fail-open(拿不准就放行):
    只在"确信引用了 schema 之外的表"时才拒绝;别名 / 子查询 / CTE / 函数 /
    字符串里的 from / 解析不确定的,一律放行。**误拒一个合法计划比不校验更糟**,
    真正的列级正确性交给执行期自愈(SqlFixer)。

本期(A1)只做表名级;列级校验(A2,需 sqlglot)留作后续,函数名沿用 validate_sql_columns
以便将来升级时调用点不变。
"""
from __future__ import annotations

import re

from pipeline.dag_schema import DAG

# FROM / JOIN 后第一个标识符(可能带 schema. 前缀)= 表名
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
# CTE 名:WITH x AS ( / , y AS (  —— 临时表名,不算未知表
_CTE_RE = re.compile(r"\b(?:WITH|,)\s+([A-Za-z_]\w*)\s+AS\s*\(", re.IGNORECASE)
# 字符串字面量(先剥掉,避免 ILIKE '%from foo%' 之类误判)
_STRINGS = re.compile(r"'[^']*'")


def _referenced_tables(sql: str) -> set[str]:
    sql = _STRINGS.sub("''", sql)               # 剥字符串再抓表名
    return {m.group(1).split(".")[-1].lower()    # schema.table → table
            for m in _TABLE_RE.finditer(sql)}


def _cte_names(sql: str) -> set[str]:
    return {m.group(1).lower() for m in _CTE_RE.finditer(sql)}


def validate_sql_columns(dag: DAG, schema: dict) -> None:
    """保守表名校验。明确引用未知表 → raise ValueError(交给 planner 重规划)。"""
    known = {t.lower() for t in (schema or {}).keys()}
    if not known:
        return   # 没有 schema 信息 → fail-open,不校验

    for node in dag.nodes:
        if node.tool != "sql_query":
            continue
        sql = node.inputs.get("sql") or ""
        if not sql:
            continue
        ctes = _cte_names(sql)
        for tbl in _referenced_tables(sql):
            if tbl in known or tbl in ctes:
                continue
            raise ValueError(
                f"node {node.id}: SQL 引用了未知表 '{tbl}'，可用表: {sorted(known)}"
            )
