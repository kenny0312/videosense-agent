"""
DAG 中间表示(IR)的 Pydantic 定义 + 校验。

Planner 的产物必须先过这里:
    - tool 必须是已知类型
    - id 唯一、非空
    - depends_on 引用的节点必须存在
    - 无环(拓扑可排)

校验通过才交给 orchestrator 执行;校验失败 → 退回 Planner 重新规划
(这正是"DAG 错"与"代码错"分离的关键:坏 DAG 在执行前就被挡住)。
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ── 节点类型(tool)────────────────────────────
#
# 两类:
#   数据获取类(在主进程经 MCP 执行,不进沙箱)
#       sql_query        Planner 直接写 SQL,经 MCP query_db 执行
#       threshold_sweep  阈值扫描:主进程循环拼 SQL 经 MCP 查,做"动态探针"(Stage 9)
#
#   数据科学类(Code Generator 生成 Python,进 Stage 5 沙箱执行 + Stage 6 自愈)
#       load_sensor_csv  生成/加载传感器 CSV(Stage 7 mock 数据)
#       merge_asof       近似时间匹配,跨模态合并(Stage 7)
#       interpolate      scipy 插值重采样到统一时间轴(Stage 8)
#       ols_regress      statsmodels OLS 回归(Stage 9)
#       plot             matplotlib 出图(Stage 10,产物存回 GCS/本地)
#       python           通用逃生舱:任意 NL 描述的分析
DATA_TOOLS = {"sql_query", "threshold_sweep", "show_video", "show_table", "analyze_video",
              "web_search", "update_memory", "semantic_search", "spawn_agents"}
SANDBOX_TOOLS = {
    "load_sensor_csv", "merge_asof", "interpolate",
    "ols_regress", "plot", "python",
}
ALL_TOOLS = DATA_TOOLS | SANDBOX_TOOLS

ToolName = Literal[
    "sql_query", "threshold_sweep", "show_video", "show_table", "analyze_video",
    "web_search", "update_memory", "semantic_search", "spawn_agents",
    "load_sensor_csv", "merge_asof", "interpolate",
    "ols_regress", "plot", "python",
]


class Node(BaseModel):
    id: str = Field(..., min_length=1)
    tool: ToolName
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_no_space(cls, v: str) -> str:
        if any(c.isspace() for c in v):
            raise ValueError(f"节点 id 不能含空白: {v!r}")
        return v


class DAG(BaseModel):
    nodes: list[Node]

    @model_validator(mode="after")
    def _validate_graph(self) -> "DAG":
        ids = [n.id for n in self.nodes]
        if not ids:
            raise ValueError("DAG 至少要有一个节点")
        if len(ids) != len(set(ids)):
            raise ValueError(f"节点 id 重复: {ids}")

        idset = set(ids)
        for n in self.nodes:
            for dep in n.depends_on:
                if dep not in idset:
                    raise ValueError(f"节点 {n.id} 依赖了不存在的节点 {dep}")
                if dep == n.id:
                    raise ValueError(f"节点 {n.id} 不能依赖自己")

        # 检测环:拓扑排序若排不完即有环
        self.topo_order()
        return self

    def topo_order(self) -> list[Node]:
        """Kahn 算法返回拓扑序;有环则抛错。"""
        by_id = {n.id: n for n in self.nodes}
        in_deg: dict[str, int] = defaultdict(int)
        children: dict[str, list[str]] = defaultdict(list)
        for n in self.nodes:
            in_deg[n.id]  # touch
            for dep in n.depends_on:
                in_deg[n.id] += 1
                children[dep].append(n.id)

        queue = deque([nid for nid in by_id if in_deg[nid] == 0])
        order: list[Node] = []
        while queue:
            nid = queue.popleft()
            order.append(by_id[nid])
            for child in children[nid]:
                in_deg[child] -= 1
                if in_deg[child] == 0:
                    queue.append(child)

        if len(order) != len(self.nodes):
            raise ValueError("DAG 存在环,无法拓扑排序")
        return order


def parse_dag(raw: dict) -> DAG:
    """把 Planner 输出的 dict 解析+校验为 DAG;失败抛 pydantic/值错误。"""
    return DAG.model_validate(raw)
