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

import vertexai
from vertexai.generative_models import GenerativeModel

from pipeline import config, mcp_client
from pipeline.dag_schema import DAG, parse_dag
from pipeline.node_specs import catalog_for_planner

log = logging.getLogger("pipeline.planner")

PLAN_MAX_REPAIRS = 1   # 校验失败后重规划次数


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
- 只输出 JSON
"""


class Planner:
    def __init__(self, schema: dict | None = None):
        vertexai.init(project=config.GCP_PROJECT, location=config.GCP_REGION)
        self.model = GenerativeModel(config.PLANNER_MODEL)
        self.schema = schema if schema is not None else mcp_client.get_schema()

    def _gen(self, prompt: str) -> dict:
        resp = self.model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json", "temperature": 0.0},
        )
        raw = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip().rstrip("`").strip()
        return json.loads(raw)

    def plan(self, user_query: str) -> DAG:
        sys_prompt = _system_prompt(self.schema)
        prompt = f"{sys_prompt}\n\n用户问题：{user_query}"

        last_err = ""
        for attempt in range(PLAN_MAX_REPAIRS + 1):
            if attempt > 0:
                prompt = (
                    f"{sys_prompt}\n\n用户问题：{user_query}\n\n"
                    f"上一次生成的 DAG 校验失败,错误如下,请修正后重新输出完整 DAG：\n{last_err}"
                )
            raw = self._gen(prompt)
            try:
                dag = parse_dag(raw)
                log.info("DAG 校验通过 (%d 节点)", len(dag.nodes))
                return dag
            except Exception as e:
                last_err = str(e)
                log.warning("DAG 校验失败 (try %d): %s", attempt + 1, last_err)

        raise ValueError(f"Planner 无法生成合法 DAG: {last_err}")
