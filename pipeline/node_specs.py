"""
每种节点类型的元数据登记表。

一处定义,两处复用:
    1. Planner   —— catalog_for_planner() 拼进 system prompt,告诉 LLM 有哪些工具、
                    每个工具要什么 inputs(让它生成合法 DAG)
    2. CodeGen   —— codegen_hint(tool) 拼进代码生成 prompt,告诉 LLM 这个节点
                    该写什么样的 Python

新增一种分析能力 = 在这里加一条 NodeSpec(+ 在 dag_schema 的 ToolName 里登记)。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeSpec:
    tool: str
    needs_sandbox: bool        # True → CodeGen + 沙箱;False → 主进程经 MCP
    planner_desc: str          # 给 Planner 看的工具说明
    codegen_hint: str = ""     # 给 CodeGen 看的实现提示(仅 needs_sandbox 用)


SPECS: dict[str, NodeSpec] = {
    # ── 数据获取(主进程 / MCP)──────────────────
    "sql_query": NodeSpec(
        tool="sql_query",
        needs_sandbox=False,
        planner_desc=(
            "执行一条只读 SELECT,返回行。inputs.sql = 完整 SQL 字符串。"
            "关系类操作(筛选/聚合/join/排序/分组)都用这一个节点直接写 SQL 表达,"
            "不要拆成多个节点。"
        ),
    ),
    "threshold_sweep": NodeSpec(
        tool="threshold_sweep",
        needs_sandbox=False,
        planner_desc=(
            "动态阈值扫描(Stage 9)。inputs.sql_template = 含 {threshold} 占位符的 SQL,"
            "inputs.thresholds = 数值列表(如 [0.5,0.6,0.7,0.8,0.9])。"
            "主进程对每个阈值代入模板、经 MCP 查询,汇总为一张 "
            "[{threshold, <聚合列>}] 表返回。"
        ),
    ),

    # ── 数据科学(沙箱 / CodeGen)────────────────
    "load_sensor_csv": NodeSpec(
        tool="load_sensor_csv",
        needs_sandbox=True,
        planner_desc=(
            "生成模拟传感器数据(Stage 7)。inputs.rows=行数(默认1000),"
            "inputs.columns=字段列表(如 ['timestamp','heart_rate']),"
            "inputs.jitter_ms=时间戳抖动毫秒。返回 list[dict]。无上游依赖。"
        ),
        codegen_hint=(
            "用 numpy 生成 inputs['rows'] 行传感器数据。timestamp 从 0 递增(秒),"
            "按 inputs['jitter_ms'] 加随机毫秒抖动;heart_rate 在 60~160 之间合理波动。"
            "用 random 种子保证可复现。最后 print(json.dumps(records))。"
        ),
    ),
    "merge_asof": NodeSpec(
        tool="merge_asof",
        needs_sandbox=True,
        planner_desc=(
            "近似时间匹配合并两张表(Stage 7)。依赖两个上游节点:第一个是左表(视频侧),"
            "第二个是右表(传感器侧)。inputs.left_on / right_on = 时间列名,"
            "inputs.tolerance_ms = 容差毫秒。返回合并后的 list[dict]。"
        ),
        codegen_hint=(
            "用 pandas.merge_asof 按时间列近似合并。两个上游 DataFrame 都要先按时间列排序;"
            "把时间列转成 pd.to_timedelta(seconds, unit='s') 再用 "
            "tolerance=pd.Timedelta(f\"{inputs['tolerance_ms']}ms\"), direction='nearest'。"
            "dropna 掉没匹配上的行。print(json.dumps(merged_records))。"
        ),
    ),
    "interpolate": NodeSpec(
        tool="interpolate",
        needs_sandbox=True,
        planner_desc=(
            "用 scipy 把不同采样率的数据重采样到统一时间轴(Stage 8)。依赖一个上游节点。"
            "inputs.target_hz = 目标频率(如 10),inputs.columns = 要插值的数值列。"
            "返回统一时间轴上的 list[dict]。"
        ),
        codegen_hint=(
            "用 scipy.interpolate.interp1d 对 inputs['columns'] 每列做线性插值。"
            "以上游数据的时间列为 x,生成 np.arange(t_min, t_max, 1/inputs['target_hz']) 新时间轴。"
            "interp1d(kind='linear', bounds_error=False, fill_value='extrapolate')。"
            "注意:某分组样本不足 2 个时 interp1d 会抛错,需跳过或保护。"
            "print(json.dumps(resampled_records))。"
        ),
    ),
    "ols_regress": NodeSpec(
        tool="ols_regress",
        needs_sandbox=True,
        planner_desc=(
            "OLS 线性回归(Stage 9)。依赖一个上游节点(含自变量与因变量列)。"
            "inputs.y = 因变量列名,inputs.x = 自变量列名列表。"
            "返回 {coef, r_squared, p_values, n} 这类回归摘要。"
        ),
        codegen_hint=(
            "用 statsmodels.api。X = sm.add_constant(df[inputs['x']]);"
            "model = sm.OLS(df[inputs['y']], X).fit()。"
            "print(json.dumps({'params': model.params.to_dict(), "
            "'r_squared': float(model.rsquared), 'pvalues': model.pvalues.to_dict(), "
            "'n': int(model.nobs)}))。所有数值转成 python float/int 再 json。"
        ),
    ),
    "plot": NodeSpec(
        tool="plot",
        needs_sandbox=True,
        planner_desc=(
            "出图(Stage 10)。依赖一个上游节点。inputs.kind = 'scatter'|'line',"
            "inputs.x / inputs.y = 列名,inputs.title = 标题。"
            "返回 {svg} —— 主进程拿到后写回 GCS/本地。"
        ),
        codegen_hint=(
            "用**纯 Python 生成 SVG 字符串**(沙箱没装 matplotlib,不要 import 它)。"
            "读取上游每行的 inputs['x'] / inputs['y'] 两个数值列,线性映射到 640x420 画布"
            "(留出 50px 边距);inputs['kind']=='scatter' 画 <circle>,'line' 画 <polyline>;"
            "再画 x/y 坐标轴线和标题 inputs['title']。"
            "print(json.dumps({'svg': svg_string, 'n_points': len(rows)}))。"
            "不要写文件系统、不要 import matplotlib。"
        ),
    ),
    "python": NodeSpec(
        tool="python",
        needs_sandbox=True,
        planner_desc=(
            "通用分析逃生舱:无法用上述专用工具表达时使用。"
            "inputs.instruction = 用自然语言描述要对上游数据做的分析。依赖上游节点。"
        ),
        codegen_hint=(
            "按 inputs['instruction'] 的自然语言要求,对上游数据写分析代码,"
            "用 print() 输出结论(优先 print(json.dumps(...)) 便于下游解析)。"
        ),
    ),
}


def needs_sandbox(tool: str) -> bool:
    return SPECS[tool].needs_sandbox


def codegen_hint(tool: str) -> str:
    return SPECS[tool].codegen_hint


def catalog_for_planner() -> str:
    """拼成 Planner system prompt 里的"可用工具"清单。"""
    lines = []
    for spec in SPECS.values():
        where = "主进程/MCP" if not spec.needs_sandbox else "沙箱"
        lines.append(f"- {spec.tool} [{where}]: {spec.planner_desc}")
    return "\n".join(lines)
