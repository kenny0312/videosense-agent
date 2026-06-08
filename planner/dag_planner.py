#!/usr/bin/env python3
"""
第4阶段：声明式计划生成器（Planner）—— [已被 pipeline/ 取代,保留作参考]

⚠️ DEPRECATED:
    本文件是 Stage 4 的早期独立实现:DAG 词表只有 5 个关系工具,且 execute_dag()
    直接解释执行,既没接 Code Generator / Sandbox,也用 `*_via_mcp` 假 MCP(直连 psycopg2)。

    生产路径请用对齐大纲的新流水线:
        python -m pipeline.main          # 完整 Planner→CodeGen→Sandbox 流水线 CLI
        uvicorn api.server:app           # Stage 10 编排 API
    其中:
        pipeline/planner.py        —— 真·Planner(经真 MCP 取 schema,DAG 含科学节点)
        pipeline/code_generator.py —— 逐节点代码生成
        pipeline/node_executor.py  —— 单节点自愈执行
        pipeline/orchestrator.py   —— DAG 拓扑编排

    本文件仅留作教学对照(展示"DAG 解释器"形态),不再维护。
"""

import json
import logging
import os
import psycopg2
import psycopg2.extras
import vertexai
from vertexai.generative_models import GenerativeModel
from collections import defaultdict, deque

# ══════════════════════════════════════════
#  配置
# ══════════════════════════════════════════
PROJECT_ID       = "your-gcp-project-id"
REGION           = "us-central1"
ALLOYDB_IP       = "your-db-host"
ALLOYDB_PASSWORD = os.environ.get("ALLOYDB_PASSWORD", "")
ALLOYDB_DB       = "your_database"
ALLOYDB_USER     = "postgres"
# ══════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("planner")

# ── 数据库连接 ────────────────────────────

def get_conn():
    return psycopg2.connect(
        host=ALLOYDB_IP, port=5432,
        dbname=ALLOYDB_DB, user=ALLOYDB_USER,
        password=ALLOYDB_PASSWORD, sslmode="require"
    )

def get_schema_via_mcp() -> dict:
    """模拟 MCP get_schema()：获取真实表结构"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                AND table_name IN (
                    'video_metadata',
                    'video_discovery',
                    'video_facts',
                    'video_fact_instances'
                )
                ORDER BY table_name, ordinal_position
            """)
            rows = cur.fetchall()
        schema = {}
        for table, column, dtype in rows:
            schema.setdefault(table, []).append({"column": column, "type": dtype})
        return schema
    finally:
        conn.close()

def query_db_via_mcp(sql: str) -> list:
    """模拟 MCP query_db()：执行只读 SQL"""
    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("只允许 SELECT 查询")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

# ── System Prompt 构造 ────────────────────

def build_system_prompt(schema: dict) -> str:
    return f"""你是一个视频分析 SQL 规划器。

# 数据库结构
{json.dumps(schema, ensure_ascii=False, indent=2)}

# 关键数据说明
- video_discovery.all_activities 是 PostgreSQL 数组类型（TEXT[]），搜索时用：
    'keyword' = ANY(all_activities)  ← 精确匹配数组元素
    all_activities::text ILIKE '%keyword%'  ← 模糊匹配（推荐）
- video_discovery.primary_activity 是普通文本，搜索时用 ILIKE '%keyword%' 做大小写不敏感匹配
- video_facts.predicate 存储的是英文活动描述字符串，搜索时同样用 ILIKE
- video_facts.matched 是布尔值，查询已确认的活动时加 AND matched = true
- 只使用上面 schema 中存在的表和列，不要创建不存在的表名或列名

# 你的任务
将用户的自然语言查询编译为 JSON DAG 执行计划。

# DAG 格式
{{
  "nodes": [
    {{
      "id": "n1",
      "tool": "<工具名>",
      "inputs": {{...}},
      "depends_on": ["<前置节点id>"]
    }}
  ]
}}

# 可用工具
- db_select:  从表中筛选，inputs 需要 table 和 condition
- db_join:    跨表合并，inputs 需要 right(表名) 和 on(关联键)
- sort:       排序，inputs 需要 by(列名) 和 order(asc/desc)
- aggregate:  聚合，inputs 需要 op(count/avg/sum) 和 label
- filter:     二次过滤，inputs 需要 condition

# 规则
- 只输出 JSON，不加任何说明文字或 markdown
- 列名必须和上面数据库结构完全一致
- 每个节点必须有唯一 id（n1,n2,n3...）
- depends_on 列出所有前置节点 id
"""

# ── DAG 执行引擎 ──────────────────────────

def execute_dag(dag: dict) -> list:
    """按拓扑顺序执行 DAG"""
    nodes = {n["id"]: n for n in dag["nodes"]}
    results = {}

    in_degree = defaultdict(int)
    dependents = defaultdict(list)
    for node in dag["nodes"]:
        for dep in node.get("depends_on", []):
            in_degree[node["id"]] += 1
            dependents[dep].append(node["id"])

    queue = deque([nid for nid in nodes if in_degree[nid] == 0])

    while queue:
        nid = queue.popleft()
        node = nodes[nid]
        tool = node["tool"]
        inputs = node.get("inputs", {})
        deps = node.get("depends_on", [])

        log.info("执行节点 %s (%s)", nid, tool)

        if tool == "db_select":
            table = inputs.get("table", "")
            condition = inputs.get("condition", "")
            sql = f"SELECT * FROM {table}"
            if condition:
                sql += f" WHERE {condition}"
            results[nid] = query_db_via_mcp(sql)

        elif tool == "db_join":
            left_data = results.get(deps[0], [])
            right_table = inputs.get("right", "")
            on_key = inputs.get("on", "video_id")
            if left_data:
                ids = [f"'{r[on_key]}'" for r in left_data if on_key in r]
                sql = f"SELECT * FROM {right_table} WHERE {on_key} IN ({','.join(ids)})"
                right_data = query_db_via_mcp(sql)
                right_map = {r[on_key]: r for r in right_data}
                results[nid] = [{**row, **right_map.get(row.get(on_key), {})} for row in left_data]
            else:
                results[nid] = []

        elif tool == "sort":
            data = results.get(deps[0], []) if deps else []
            by = inputs.get("by", "")
            reverse = inputs.get("order", "asc").lower() == "desc"
            results[nid] = sorted(data, key=lambda x: x.get(by, 0) or 0, reverse=reverse)

        elif tool == "aggregate":
            data = results.get(deps[0], []) if deps else []
            op = inputs.get("op", "count")
            label = inputs.get("label", "result")
            if op == "count":
                results[nid] = [{label: len(data)}]
            elif op == "avg":
                field = inputs.get("field", "confidence")
                vals = [r.get(field, 0) for r in data if r.get(field) is not None]
                results[nid] = [{label: round(sum(vals) / len(vals), 4) if vals else 0}]
            elif op == "sum":
                field = inputs.get("field", "")
                results[nid] = [{label: sum(r.get(field, 0) or 0 for r in data)}]

        elif tool == "filter":
            results[nid] = results.get(deps[0], []) if deps else []

        else:
            log.warning("未知工具: %s", tool)
            results[nid] = []

        for child in dependents[nid]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    last_node = dag["nodes"][-1]["id"]
    return results.get(last_node, [])

# ── Planner 主逻辑 ────────────────────────

def plan(user_query: str) -> dict:
    """自然语言 → DAG"""
    log.info("获取数据库 Schema...")
    schema = get_schema_via_mcp()
    system_prompt = build_system_prompt(schema)

    log.info("调用 Gemini 生成 DAG...")
    vertexai.init(project=PROJECT_ID, location=REGION)
    model = GenerativeModel("gemini-2.5-pro")

    full_prompt = f"{system_prompt}\n\n用户问题：{user_query}"
    response = model.generate_content(
        full_prompt,
        generation_config={"response_mime_type": "application/json"}
    )

    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("```")

    return json.loads(raw)

# ── 主入口 ────────────────────────────────

def main():
    print("\n" + "="*55)
    print("  第4阶段：Planner 自然语言查询系统")
    print("  LLM: Gemini 2.5 Pro + AlloyDB")
    print("  输入 'quit' 退出")
    print("="*55 + "\n")

    while True:
        try:
            user_input = input("你的问题：").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input or user_input.lower() in ("quit", "exit", "q"):
            break

        try:
            print("\n[1] 生成执行计划...")
            dag = plan(user_input)
            print("DAG:")
            print(json.dumps(dag, indent=2, ensure_ascii=False))

            print("\n[2] 执行计划...")
            result = execute_dag(dag)

            print(f"\n[3] 结果（共 {len(result)} 条）:")
            for i, row in enumerate(result[:10]):
                print(f"  {i+1}. {json.dumps(row, ensure_ascii=False, default=str)}")
            if len(result) > 10:
                print(f"  ... 还有 {len(result)-10} 条")

        except Exception as e:
            log.error("执行失败: %s", e)
            import traceback
            traceback.print_exc()
        print()

if __name__ == "__main__":
    main()
