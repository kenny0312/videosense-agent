"""
完整流水线 CLI 入口(Stage 4 → 6 → 10 合流)。

    python -m pipeline.main

环境变量:
    REPL_USE_MOCK_DB=1     用内存 SQLite mock(零成本,不需要 AlloyDB)
    ALLOYDB_PASSWORD       真 DB 模式必需
    SANDBOX_URL            默认 http://localhost:8080,生产用 Cloud Run URL
    SANDBOX_TOKEN          Cloud Run 时自动经 gcloud 取

本入口:自然语言 → Planner 规划 DAG → 逐节点执行(数据走 MCP / 科学节点进沙箱,失败自愈)→ 答案。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import warnings

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

warnings.filterwarnings("ignore", category=UserWarning, module="vertexai.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="vertexai.*")

from pipeline.orchestrator import run_query
from pipeline import config

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

HEADER = """
======================================================
  完整流水线:Planner → CodeGen → Sandbox(自愈)
  自然语言 → DAG → 逐节点执行 → 答案
  输入 'quit' / 'exit' / 'q' 退出
======================================================
"""


def _check_env() -> bool:
    if not config.USE_MOCK_DB and not config.ALLOYDB_PASSWORD:
        print("[!] 真 DB 模式需要 ALLOYDB_PASSWORD;或设 REPL_USE_MOCK_DB=1 走 mock。")
        print(r'    PowerShell: $env:REPL_USE_MOCK_DB = "1"')
        return False
    return True


def _print_result(r: dict):
    print()
    if r.get("status") == "smalltalk":
        print(f"  {r.get('answer', '')}")
        print(f"  {r['trace_summary']}")
        print()
        return
    if r.get("status") == "refused":
        print(f"  🛑 无法回答: {r.get('reason', '')}")
        print(f"  {r['trace_summary']}")
        print()
        return
    if r.get("dag"):
        tools = " → ".join(n["tool"] for n in r["dag"]["nodes"])
        print(f"  DAG: {len(r['dag']['nodes'])} 节点 [{tools}]")
    print(f"  {r['trace_summary']}")

    if r["ok"]:
        print("  答案:")
        ans = r["answer"]
        text = json.dumps(ans, ensure_ascii=False, indent=2, default=str)
        for line in text.splitlines()[:30]:
            print(f"    {line}")
        if r.get("plot", {}).get("png_base64"):
            print(f"  [图表] png_base64 ({len(r['plot']['png_base64'])} chars) —— 可写回 GCS")
        if r.get("generated_code"):
            print(f"  生成代码节点: {', '.join(r['generated_code'].keys())}")
    else:
        print(f"  [失败] 节点={r.get('fail_node')}")
        print(f"  {r.get('error','')}")
    print()


def main() -> int:
    print(HEADER)
    print(f"  模式: {'MOCK (内存 SQLite)' if config.USE_MOCK_DB else 'AlloyDB'}"
          f"  |  Sandbox: {config.SANDBOX_URL}")
    if not _check_env():
        return 1

    while True:
        try:
            q = input("\n你的问题 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q or q.lower() in ("quit", "exit", "q"):
            break

        print("\n  --- trace ---")
        try:
            r = run_query(q)
        except KeyboardInterrupt:
            print("\n  (已中断)\n")
            continue
        except Exception as e:
            print(f"\n  [流水线出错] {e}\n")
            continue
        _print_result(r)

    print("再见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
