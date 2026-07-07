"""python -m evals —— 评测一键入口（跑完自动打开本地仪表盘）。

    python -m evals            # 快跑（脚本车道，免费）+ 打开仪表盘
    python -m evals live       # 真跑（真 Gemini，花 token）+ 打开仪表盘
    python -m evals view       # 只打开仪表盘（看历史/趋势）
    python -m evals list       # 数据集清单
    python -m evals check      # 校验金标 grounding

仓库根目录还有 eval.bat：`eval live` 等价于 `python -m evals live`。
"""
from __future__ import annotations

import os
import sys


def _open_dashboard():
    from evals import dashboard

    dashboard.rebuild()
    try:
        os.startfile(dashboard.DASH_PATH)  # noqa: S606  Windows 本地打开浏览器
    except OSError:
        print(f"仪表盘：{dashboard.DASH_PATH}")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "quick"
    rest = sys.argv[2:]
    if cmd == "view":
        _open_dashboard()
        return 0
    if cmd == "list":
        from evals import runner
        return runner.main(["--list", *rest])
    if cmd == "check":
        from evals import validate_tasks
        return validate_tasks.main()
    if cmd == "live":
        from evals import runner
        rc = runner.main(["--live", "--out", "evals/report_live.html", *rest])
        _open_dashboard()
        return rc
    if cmd == "quick":
        from evals import runner
        rc = runner.main(rest)
        _open_dashboard()
        return rc
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
