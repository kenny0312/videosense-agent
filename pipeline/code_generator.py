"""
Code Generator —— 把单个 DAG 节点翻译成 Python(Stage 6 的"生成"半边)。

与旧 repl/generator.py 的区别:
    旧:入参是"用户问题 + 整表 data",一把生成端到端分析代码
    新:入参是"单个节点 + 它的上游结果",只生成这一个节点的代码

每个节点一个独立的 CodeGenerator 实例,内部维护 code_history,
失败时把 sandbox 的 stderr 回喂、基于报错重写(自愈),history 有上限防膨胀。
"""
from __future__ import annotations

import json
import logging

import vertexai
from vertexai.generative_models import GenerativeModel

from pipeline import config, usage
from pipeline.dag_schema import Node
from pipeline.node_specs import codegen_hint

log = logging.getLogger("pipeline.code_generator")

# 沙箱白名单(与 Stage 5 AST policy gate 一致)
_ALLOWED_LIBS = "pandas, numpy, scipy, statsmodels, matplotlib, json, math, statistics, collections, itertools, functools, datetime, io, base64, random"
_FORBIDDEN = "socket, requests, urllib, subprocess, importlib, ctypes, eval, exec, open, __import__"


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        for prefix in ("python", "sql"):
            if t.lower().startswith(prefix):
                t = t[len(prefix):]
                break
        t = t.strip().rstrip("`").strip()
    return t


def _upstream_preview(upstream: dict[str, list], max_rows: int = 3) -> str:
    """给 LLM 看的上游变量预览(注入的是 data_<id> 变量)。"""
    if not upstream:
        return "（无上游数据：本节点是数据源）"
    parts = []
    for nid, rows in upstream.items():
        var = f"data_{nid}"
        n = len(rows) if isinstance(rows, list) else 1
        sample = rows[:max_rows] if isinstance(rows, list) else rows
        parts.append(
            f"- 变量 `{var}` (来自节点 {nid}, 共 {n} 行) 预览:\n"
            f"  {json.dumps(sample, ensure_ascii=False, default=str)}"
        )
    return "\n".join(parts)


def _node_prompt(node: Node, upstream: dict[str, list]) -> str:
    return f"""你是一个数据科学 Python 代码生成器,负责实现执行计划(DAG)中的**单个节点**。

# 本节点
tool: {node.tool}
inputs: {json.dumps(node.inputs, ensure_ascii=False)}

# 实现要求
{codegen_hint(node.tool)}

# 已注入的上游变量(可直接使用,无需自己查库/读文件)
{_upstream_preview(upstream)}
节点的 inputs 也已注入为变量 `inputs`(dict)。

# 可用库
{_ALLOWED_LIBS}

# 严禁(会被沙箱拒绝)
{_FORBIDDEN}

# 输出约定
- 只输出 Python 代码,不要 markdown 围栏,不要解释
- **代码里的所有注释、字符串字面量、标签一律用英文(English only)。禁止出现任何中文字符。**
- 计算结果必须用 print(json.dumps(...)) 输出,以便下游节点解析
- 若上游数据为空,print(json.dumps([]))
"""


class CodeGenerator:
    """单节点代码生成器(含自愈 history)。"""

    HISTORY_TAIL = 4   # 保留首条(节点说明)+ 最近 4 条

    def __init__(self):
        vertexai.init(project=config.GCP_PROJECT, location=config.GCP_REGION)
        self.model = GenerativeModel(config.CODEGEN_MODEL)
        self.history: list[dict] = []

    def generate(self, node: Node, upstream: dict[str, list]) -> str:
        """首轮生成:基于节点定义 + 上游预览。"""
        self.history = [{"role": "user", "text": _node_prompt(node, upstream)}]
        return self._call()

    def repair(self, stderr: str, exit_code: int) -> str:
        """自愈:把沙箱报错回喂,基于 traceback 重写。"""
        self.history.append({
            "role": "user",
            "text": (
                f"上一次代码在沙箱执行失败,请基于报错修复后重新输出完整代码:\n"
                f"--- stderr ---\n{stderr}\n--- exit_code: {exit_code} ---"
            ),
        })
        # 截断历史:首条 + 最近若干条
        if len(self.history) > self.HISTORY_TAIL + 1:
            self.history = [self.history[0]] + self.history[-self.HISTORY_TAIL:]
        return self._call()

    def _call(self) -> str:
        text = ""
        for turn in self.history:
            tag = "用户" if turn["role"] == "user" else "助手"
            text += f"\n[{tag}]\n{turn['text']}\n"
        log.info("生成节点代码 (history=%d)", len(self.history))
        resp = self.model.generate_content(
            text, generation_config={"temperature": 0.2},
        )
        usage.add_usage(resp, config.CODEGEN_MODEL)
        code = _strip_fence(resp.text)
        self.history.append({"role": "model", "text": code})
        return code
