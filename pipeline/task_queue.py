"""S-2/S-3(任务底座):波次投递薄驱动(设计 docs/longhorizon-task-substrate-plan.md §0 D1)。

TASKS_DRIVER=inline|cloudtasks,同一代码路径:
  · inline(默认,本地/单测/降级):daemon 线程直接调 task_runner.advance —— 零云依赖;
  · cloudtasks(生产):命名任务 name={task_id}-w{N}(同名 ~1h ALREADY_EXISTS 去重 →
    补投天然幂等);队列的 maxAttempts/dispatchDeadline 属基础设施配置(不吃默认 ~100 次
    重试 —— 成本护栏,见任务书 D1),本驱动只负责投。
enqueue 失败一律【上抛】:调用方(S-2 立项 / S-3 续投)按 fail-closed 处置
(paused_error + event + 5xx),绝不吞 —— 本库三处 except-log-continue 惯性是反面教材。
"""
from __future__ import annotations

import logging
import threading

from pipeline import config

log = logging.getLogger("pipeline.task_queue")


def _inline(task_id: str, wave_n: int) -> None:
    """本地驱动:daemon 线程跑 advance(同一代码路径;task_runner 由 S-3 提供)。"""
    from pipeline import task_runner                     # 惰性:S-3 落地前 import 即炸 → fail-closed

    def run():
        try:
            task_runner.advance(task_id, wave_n)
        except Exception:                                # 线程内兜底:advance 自己负责落 paused_error
            log.warning("inline advance 崩溃(advance 内部应已 fail-closed)", exc_info=True)
    threading.Thread(target=run, daemon=True, name=f"task-{task_id}-w{wave_n}").start()


def task_name(task_id: str, wave_n: int, salt: str = "") -> str:
    """命名任务的名字(纯函数,单测钉)。salt 非空 = resume/救援重投:执行过的名字有
    ~1h 墓碑期,裸重投会 ALREADY_EXISTS 静默丢投(假活)—— 必须带后缀绕开。"""
    return f"{task_id}-w{wave_n}" + (f"-{salt}" if salt else "")


def _cloudtasks(task_id: str, wave_n: int, salt: str = "") -> None:
    """生产驱动:命名 HTTP 任务(OIDC 由队列侧配置注入,鉴权在 advance 端点验)。"""
    from google.cloud import tasks_v2                    # 惰性:本地不装也能跑 inline
    from google.api_core.exceptions import AlreadyExists
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(config.GCP_PROJECT, config.TASKS_REGION, config.TASKS_QUEUE)
    name = f"{parent}/tasks/{task_name(task_id, wave_n, salt)}"
    task = {
        "name": name,
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": config.TASKS_ADVANCE_URL,
            "headers": {"Content-Type": "application/json"},
            "body": (f'{{"task_id": "{task_id}", "wave_n": {int(wave_n)}}}').encode(),
            "oidc_token": {"service_account_email": config.TASKS_INVOKER_SA},
        },
    }
    try:
        client.create_task(request={"parent": parent, "task": task})
    except AlreadyExists:
        # 【契约注意】(review 确认):吞成功只对"任务还在队列里的重复投递"成立。
        # 命名任务执行完/删除后名字有 ~1h 墓碑期,期间 ALREADY_EXISTS = 什么都没投 ——
        # S-4 的 resume 重投若撞墓碑会假活(状态回 running 却永远等不来波)。
        # S-4 落地时 resume 路径的任务名必须带重试后缀(如 -r{n})绕开墓碑,别复用本函数裸投。
        log.info("命名任务已存在(%s-w%s)= 排队去重,视为成功", task_id, wave_n)


def enqueue_advance(task_id: str, wave_n: int, salt: str = "") -> None:
    """投递下一波。失败上抛(fail-closed 归调用方)。
    salt:resume/救援重投必须传非空(绕命名任务墓碑,见 task_name);常规续投留空。"""
    if config.TASKS_DRIVER == "cloudtasks":
        _cloudtasks(task_id, wave_n, salt)
    else:
        _inline(task_id, wave_n)
