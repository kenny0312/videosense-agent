"""
第6阶段 — Agentic REPL: 命令行入口

用法:
    python -m repl.main

需要环境变量:
    ALLOYDB_PASSWORD       — AlloyDB 密码(必需)
    SANDBOX_URL (可选)     — 默认 http://localhost:8080
                              生产请用 https://your-sandbox.run.app
    SANDBOX_TOKEN (可选)   — Cloud Run 时自动通过 gcloud 取
"""

import logging
import os
import sys
import warnings

# Windows cmd 默认 cp1252 — 强制 UTF-8 输出,避免中文字符串崩
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass  # 3.6 以下或非 tty 时 silently skip

# 压掉 vertexai SDK 那条 deprecation warning(2026/06/24 才真正失效,先静默)
warnings.filterwarnings("ignore", category=UserWarning, module="vertexai.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="vertexai.*")

from repl.loop import run

logging.basicConfig(
    level=logging.WARNING,   # 默认 WARN,避免淹没 trace 输出
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


HEADER = """
======================================================
  第6阶段:Agentic REPL  (自愈版)
  自然语言 → SQL → Python → Sandbox → 答案
  输入 'quit' / 'exit' / 'q' 退出
  按 Ctrl-C 中断当前查询
======================================================
"""


def _check_env():
    missing = []
    if not os.environ.get("ALLOYDB_PASSWORD"):
        missing.append("ALLOYDB_PASSWORD")
    if missing:
        print(f"[!] 缺少环境变量:{', '.join(missing)}")
        print("    示例(PowerShell):")
        print(r'      $env:ALLOYDB_PASSWORD = "your_password"')
        print(r'      $env:SANDBOX_URL = "https://your-sandbox.run.app"')
        return False
    return True


def _print_result(result: dict):
    print()
    print(f"  SQL:")
    for line in result["sql"].splitlines():
        print(f"    {line}")
    print(f"  代码尝试次数: {result['attempts']}")
    print(f"  {result['trace_summary']}")

    if result["ok"]:
        print(f"  答案:")
        for line in result["answer"].splitlines():
            print(f"    {line}")
    else:
        print(f"  [失败] 阶段={result['fail_phase']}")
        print(f"  最后报错:")
        for line in (result["last_stderr"] or "").splitlines()[-15:]:
            print(f"    {line}")
    print()


def main():
    print(HEADER)
    if not _check_env():
        return 1

    while True:
        try:
            q = input("你的问题 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not q or q.lower() in ("quit", "exit", "q"):
            break

        print("\n  --- trace ---")
        try:
            result = run(q)
        except KeyboardInterrupt:
            print("\n  (已中断)\n")
            continue
        except Exception as e:
            print(f"\n  [REPL 出错] {e}\n")
            continue

        _print_result(result)

    print("再见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
