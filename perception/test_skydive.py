"""
跳伞专栏 null-safety 测试 —— 不依赖 GCP/DB(schema 用纯 Python,端到端用 mock SQLite)。
    python -m perception.test_skydive

重点验证"空 feature 不崩":缺席阶段 → 真 NULL(不是 0.0)、派生指标缺端 → None、
mock 端到端可建表/可查/聚合自动忽略 NULL。
"""
from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from perception.skydive_schema import (
    COLUMNS, PHASE_KEYS, PhaseSpan, SkydiveExtraction, create_table_sql, to_row,
)


# ── schema 层:缺席 = None,不是 0.0 ──────────────────────────────
def test_empty_extraction_all_none():
    row = to_row("x", SkydiveExtraction())            # 模型啥都没返回(全缺)
    for k in PHASE_KEYS:
        assert row[f"{k}_start_ts"] is None, k        # ★ 关键:缺席是 None,不是 0.0
        assert row[f"{k}_end_ts"] is None
        assert row[f"{k}_confidence"] is None
    assert row["jump_type"] is None and row["is_wingsuit"] is None
    assert row["freefall_sec"] is None                # 派生缺端 → None,不抛


def test_partial_only_freefall():
    ext = SkydiveExtraction(freefall=PhaseSpan(start_ts=2.0, end_ts=46.0, confidence=0.9))
    row = to_row("x", ext)
    assert row["freefall_start_ts"] == 2.0 and row["freefall_sec"] == 44.0
    assert row["exit_start_ts"] is None and row["landing_start_ts"] is None   # 其余仍 NULL


def test_freefall_sec_needs_both_ends():
    # 只有起点没有终点 → 不能算时长 → None(不能崩、不能瞎算)
    row = to_row("x", SkydiveExtraction(freefall=PhaseSpan(start_ts=2.0)))
    assert row["freefall_sec"] is None


def test_zero_timestamp_is_real_not_missing():
    # start_ts=0.0 是合法时间戳(第0秒),必须保留为 0.0 而非被当成缺失
    row = to_row("x", SkydiveExtraction(exit=PhaseSpan(start_ts=0.0, end_ts=2.0, confidence=0.9)))
    assert row["exit_start_ts"] == 0.0


def test_extra_keys_ignored():
    # Gemini 多返回个没定义的阶段也不应崩(Pydantic 忽略多余键)
    ext = SkydiveExtraction.model_validate({"freefall": {"start_ts": 1, "end_ts": 5},
                                            "some_future_phase": {"start_ts": 9}})
    assert ext.freefall is not None


# ── 端到端:mock SQLite 建表 + 查询 + NULL 聚合(证明"空 feature 不崩 SQL")──────
def test_mock_end_to_end_nullsafe():
    import os
    os.environ.setdefault("REPL_USE_MOCK_DB", "1")
    from repl._mock_db import mock_run_sql, mock_fetch_schema

    # 表进了 schema(planner 看得见)
    assert "skydive_segments" in mock_fetch_schema()

    # 缺阶段的行确实是 NULL(sky02 只有 freefall)
    rows = mock_run_sql("SELECT video_id, deploy_start_ts, landing_start_ts "
                        "FROM skydive_segments WHERE video_id='sky02'")
    assert rows and rows[0]["deploy_start_ts"] is None and rows[0]["landing_start_ts"] is None

    # 典型 null-safe 查询:找"只有自由落体、没拍到开伞"的视频(Pandora 式空 feature 的正确查法)
    only_ff = mock_run_sql("SELECT video_id FROM skydive_segments "
                           "WHERE freefall_start_ts IS NOT NULL AND deploy_start_ts IS NULL")
    assert any(r["video_id"] == "sky02" for r in only_ff)

    # 聚合自动忽略 NULL,不崩:平均自由落体时长只对有值的行算
    agg = mock_run_sql("SELECT COUNT(freefall_sec) AS n, AVG(freefall_sec) AS avg_ff "
                       "FROM skydive_segments")
    assert agg[0]["n"] >= 3 and agg[0]["avg_ff"] is not None

    # 全 4 个 sky 视频都在(含缺阶段的),没有因 NULL 掉行
    assert mock_run_sql("SELECT COUNT(*) AS n FROM skydive_segments")[0]["n"] == 4


# ── DDL 自检 ──────────────────────────────────────────────────
def test_ddl_has_all_phase_cols_and_no_now():
    ddl = create_table_sql()
    for k in PHASE_KEYS:
        assert f"{k}_start_ts" in ddl
    assert "NOW()" not in ddl and "CURRENT_TIMESTAMP" in ddl   # SQLite 兼容
    assert len(COLUMNS) == 1 + 3 * len(PHASE_KEYS) + 4         # video_id + 阶段*3 + 4 内容/派生列


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t(); print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1; print(f"  FAIL  {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
