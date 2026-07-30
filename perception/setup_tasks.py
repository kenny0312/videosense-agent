"""S-1(任务底座):agent_tasks / agent_task_events 两表(设计 docs/longhorizon-task-substrate-plan.md)。

家规:CREATE TABLE IF NOT EXISTS 幂等(setup_schema.py 同款);DDL 是模块常量 ——
测试离线校验文本,应用函数注入 conn(不在 import 时连库)。
运行方式(手动一次):PYTHONUTF8=1 python perception/setup_tasks.py
"""
from __future__ import annotations

import os

# 状态机的唯一事实源在 pipeline/taskstate.py;这里的 CHECK 约束只是 DB 侧兜底
# (裸 SQL 也写不进非法状态)。
DDL_TASKS = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id      TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    goal         TEXT NOT NULL,
    goal_hash    TEXT NOT NULL,
    plan         JSONB NOT NULL DEFAULT '{"remaining":[],"done":{}}',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','running','paused_budget','paused_error',
                                   'done','cancelled')),
    wave_n       INT  NOT NULL DEFAULT 0,
    lease_until  TIMESTAMPTZ,
    lease_token  TEXT,
    budget_cap   NUMERIC NOT NULL,
    spent_usd    NUMERIC NOT NULL DEFAULT 0,
    wasted_usd   NUMERIC NOT NULL DEFAULT 0,
    precharged_usd NUMERIC NOT NULL DEFAULT 0,  -- 本 attempt 未结算的预估(重试时转 wasted)
    notified_at  TIMESTAMPTZ,
    parent_task_id TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ
);
"""

# 立项幂等(S-2):同 owner 同 goal_hash 的【活跃】任务唯一(部分唯一索引;终态不占坑)。
DDL_TASKS_IDEMPOTENCY = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_active_goal
ON agent_tasks (owner, goal_hash)
WHERE status IN ('pending','running','paused_budget','paused_error');
"""

DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS agent_task_events (
    id         BIGSERIAL PRIMARY KEY,
    task_id    TEXT NOT NULL,
    kind       TEXT NOT NULL
               CHECK (kind IN ('planned','wave_attempt','wave_done','user_note',
                               'paused','resumed','cancelled','done','error',
                               'wasted','enqueue_failed')),
    payload    JSONB,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_task_events_task ON agent_task_events (task_id, id);
"""

ALL_DDL = (DDL_TASKS, DDL_TASKS_IDEMPOTENCY, DDL_EVENTS)


def setup_tasks(conn) -> None:
    """幂等建表(重复跑无害)。conn 由调用方注入(Neon 即用即连家规)。"""
    with conn.cursor() as cur:
        for ddl in ALL_DDL:
            cur.execute(ddl)


if __name__ == "__main__":
    import psycopg2
    from pipeline import config as _cfg                      # .env 装载副作用
    conn = psycopg2.connect(
        host=os.environ.get("ALLOYDB_HOST", "localhost"), port=5432,
        dbname=os.environ.get("ALLOYDB_DB", "your_database"),
        user=os.environ.get("ALLOYDB_USER", "postgres"),
        password=os.environ.get("ALLOYDB_PASSWORD") or input("DB 密码: "),
        sslmode="require", connect_timeout=10)
    conn.autocommit = True
    setup_tasks(conn)
    print("[OK] agent_tasks / agent_task_events 就绪(幂等)")
