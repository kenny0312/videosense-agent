"""
Stage 4 —— 声明式计划生成器(Planner)。

自然语言 → JSON DAG。**只推理,不生成代码**:
    - 通过 MCP get_schema() 拿真实表结构(防列名幻觉)
    - 通过 node_specs 注入"可用工具"清单
    - 让 Gemini 输出 DAG(做哪几步、什么顺序、每步什么 inputs)
    - 用 dag_schema 校验;不合法(坏列名/坏依赖/环)则把错误回喂,重规划一次

产物是给 orchestrator 执行的蓝图,可缓存、可审计、可 dry-run。
"""
from __future__ import annotations

import json
import logging

from pipeline import config, mcp_client, usage
from pipeline.dag_schema import DAG, parse_dag
from pipeline.node_specs import catalog_for_planner
from pipeline.sql_validate import validate_sql_columns

log = logging.getLogger("pipeline.planner")

PLAN_MAX_REPAIRS = 1   # 校验失败后重规划次数


def _context_block(context: dict) -> str:
    """多轮 follow-up:把已解析的上一轮"配方+预览"拼成提示,让 Planner 重建自洽 DAG。"""
    history = context.get("history") or []
    arts = context.get("resolved_artifacts") or []
    lines = ["# 多轮上下文(这是一个 follow-up)"]
    if history:
        lines.append("## 最近对话")
        for h in history:
            lines.append(f"- 第{h.get('turn')}轮[{h.get('turn_type')}/{h.get('intent')}]:"
                         f"{h.get('question')} → {h.get('answer_summary')}")
    lines.append("## 已解析的上一轮结果(请复用/扩展它)")
    for a in arts:
        recipe = a.get("recipe") or {}
        cached = " [value_cached]" if a.get("value_cached") else ""
        lines.append(f"### {a.get('id')} · {a.get('label')} (kind={a.get('kind')}){cached}")
        if a.get("value_cached"):
            lines.append(f"已缓存其真实值:【仅当】你要把这【同一份刚算出的结果】原样重新呈现/"
                         f"重渲染(如再画一张图、换种展示)时,才用 load_artifact"
                         f"(artifact_id='{a.get('id')}') 直接载入,免重跑下面的配方。"
                         f"数据/筛选/范围/时间有任何变化 → 别用,照配方重算。")
        if recipe.get("type") == "sql":
            lines.append("上一轮 SQL(可直接复用或包成子查询):")
            lines.append(recipe.get("sql", ""))
        elif recipe.get("type") == "dag":
            if recipe.get("truncated"):
                lines.append(f"上一轮步骤链:{recipe.get('chain', '')}")
                for nid, sql in (recipe.get("sqls") or {}).items():
                    lines.append(f"  {nid} SQL:{sql}")
            else:
                lines.append("上一轮 DAG(可复用其 SQL/步骤):")
                lines.append(json.dumps(recipe.get("dag", {}), ensure_ascii=False))
        if a.get("preview"):
            lines.append(f"结果预览:{json.dumps(a['preview'], ensure_ascii=False)}")
    lines.append(
        "\n# 重要:请【重建一个完整、自洽的新 DAG】,节点 id 用全新的 n1..nk("
        "不要照抄旧 id、也不要假设旧节点还在)。复用上面配方里的 SQL/步骤(重跑数据),"
        "再加上用户这一轮新要求的步骤。"
        "\n# 复用 vs 重算(强默认=重算):load_artifact 只用于一种情形 ——【把某条刚算出的结果"
        "原样重新呈现/重渲染】(如再画一张图、换种排版),且该结果标了 [value_cached]。"
        "判据很严:用户这一轮对【数据 / 筛选 / 范围 / 聚合 / 时间】有任何改动,哪怕只是换个活动、"
        "改个阈值、换段时间窗,都【不是】重新呈现 → 别用 load_artifact,照配方里的 SQL/步骤重算。"
        "没标 [value_cached] 的结果一律走重算(其缓存值已不在场,load_artifact 会失败)。拿不准就重算。"
    )
    return "\n".join(lines)


def _system_prompt(schema: dict) -> str:
    return f"""你是一个视频分析查询规划器。把用户的自然语言问题编译成 JSON DAG 执行计划。

# 数据库结构(来自 MCP get_schema,列名必须严格一致)
{json.dumps(schema, ensure_ascii=False, indent=2)}

# 关键数据说明
- video_facts.predicate 是**英文**活动描述,用 ILIKE 模糊匹配。
  用户用中文/其它语言时,先翻译成英文:
    "滑雪" → ILIKE '%skiing%' OR ILIKE '%snowboarding%'
    "做饭" → ILIKE '%cooking%' OR ILIKE '%baking%'
- video_facts.matched 是布尔,查已确认事实加 AND matched = true
- video_discovery.all_activities 是数组(TEXT[]),用 all_activities::text ILIKE '%kw%'
- 求数组长度用 array_length(arr, 1)

# 可用工具(每个 DAG 节点的 tool 必须取自下表)
{catalog_for_planner()}

# DAG 格式(只输出这个 JSON,不要任何解释或 markdown 围栏)
{{
  "nodes": [
    {{"id": "n1", "tool": "<工具名>", "inputs": {{...}}, "depends_on": []}},
    {{"id": "n2", "tool": "<工具名>", "inputs": {{...}}, "depends_on": ["n1"]}}
  ]
}}

# 规则
- 关系类查询(筛选/聚合/join/排序)优先用单个 sql_query 节点,直接写完整 SQL
- 需要分析/对齐/插值/回归/出图时,才追加 sandbox 类节点
- depends_on 必须引用已定义的节点 id,不能成环
- 节点 id 用 n1,n2,n3...
- inputs 字段名严格按工具说明
- **会出现在结果里的文本一律用英文:SQL 不要写注释、plot 的 title 用英文。禁止在 DAG 里出现中文。**
- 只输出 JSON
"""


class Planner:
    def __init__(self, schema: dict | None = None):
        # 惰性导入(与 CodeGenerator/SqlFixer/Router 同):import pipeline.orchestrator
        # (离线测试的传递依赖)不该把 vertexai 拉进来 —— 留到真正构造 Planner 时再 import。
        import vertexai
        from vertexai.generative_models import GenerativeModel
        vertexai.init(project=config.GCP_PROJECT, location=config.GCP_REGION)
        self.model = GenerativeModel(config.PLANNER_MODEL)
        self.schema = schema if schema is not None else mcp_client.get_schema()

    def _gen(self, prompt: str) -> dict:
        resp = self.model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json", "temperature": 0.0},
        )
        usage.add_usage(resp, config.PLANNER_MODEL)
        raw = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip().rstrip("`").strip()
        return json.loads(raw)

    def plan(self, user_query: str, *, context: dict | None = None) -> DAG:
        sys_prompt = _system_prompt(self.schema)
        # follow-up:把已解析的上一轮配方+预览拼进 system 段(无解析结果则为空,行为同单轮)
        ctx = f"\n\n{_context_block(context)}" if (context and context.get("resolved_artifacts")) else ""

        last_err = ""
        for attempt in range(PLAN_MAX_REPAIRS + 1):
            prompt = f"{sys_prompt}{ctx}\n\n用户问题：{user_query}"
            if attempt > 0:
                prompt = (
                    f"{sys_prompt}{ctx}\n\n用户问题：{user_query}\n\n"
                    f"上一次生成的 DAG 校验失败,错误如下,请修正后重新输出完整 DAG：\n{last_err}"
                )
            raw = self._gen(prompt)
            try:
                dag = parse_dag(raw)
                validate_sql_columns(dag, self.schema)   # 表名校验:失败 → 走下面重规划
                log.info("DAG 校验通过 (%d 节点)", len(dag.nodes))
                return dag
            except Exception as e:
                last_err = str(e)
                log.warning("DAG 校验失败 (try %d): %s", attempt + 1, last_err)

        raise ValueError(f"Planner 无法生成合法 DAG: {last_err}")
