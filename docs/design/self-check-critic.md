# 设计:自检 B —— 收口前的显式 critic 回路(opt-in)

> 状态:Design+Build · 范围:`pipeline/loop_driver.py`(run_loop + critic)、`pipeline/orchestrator.py`、`pipeline/config.py` · 关联:自检 A(已上线的提示级 `_LOOP_SYSTEM` 自检)、`prompt-keep-adaptive`

## 1. 背景
已上线【自检 A】= prompt 里一句"收口前自问做到位没"。够用大多数场景,但靠模型自觉,偶尔仍早停。
【自检 B】= 在 loop【真的要收口】(吐纯文本)那一刻,插一个【独立 critic 调用】判"这答案真满足用户没",
没满足且有办法就把意见喂回 loop 再来一轮。比 A 硬。**代价**:每个收口轮多一次 flash + 可能多一轮 → 故 **opt-in**。

## 2. 设计
- **注入式**(保持 run_loop 纯、可测):run_loop 新增 `critic` 形参(默认 None=关)。loop 收敛(`not calls`)时:
  - critic is None → 照常返回(行为不变)。
  - 有 critic 且未超 `max_critic` 次 → 调 `critic(user_query, answer) -> (satisfied, hint)`:
    - satisfied → 返回。
    - 不满足且 hint 非空 → 计数 +1,把 hint 当一条消息喂回 conversation(`[自检] …`),`continue`(再来一轮)。
- **护栏**:`max_critic` 默认 1(至多一次 critic 驱动的再来 → 防空转/控成本);critic 自身异常 → 视为 satisfied(fail-open,绝不卡收口);critic prompt 明确"问有无/数量/简单事实、或已诚实说做不到 → 算 satisfied"(别强求)。
- **真 critic**(orchestrator 侧):`make_self_check_critic()` 用 `CRITIC_MODEL`(flash)发一句判断 prompt → 解析 `{satisfied, missing}`。`USE_SELF_CHECK_CRITIC=1` 才启用并传给 run_loop。

## 3. 改动点 + 测试
| 文件 | 改动 |
|---|---|
| `loop_driver.run_loop` | +`critic`/`max_critic` 形参;收敛处插 critic 回路 |
| `loop_driver` | +`make_self_check_critic()`(真 flash critic,解析 JSON,fail-open) |
| `loop_driver.run_query_loop` / `orchestrator` | `USE_SELF_CHECK_CRITIC` 时建 critic 传进去 |
| `config` | `USE_SELF_CHECK_CRITIC`(默认 0)、`SELF_CHECK_MAX_ROUNDS`(默认 1) |

**测试**(注入 fake critic,离线):① critic 说 satisfied → 直接返回;② 说不满足+hint → 多走一轮、hint 被喂回、最终返回;③ 超 max_critic 不再触发;④ critic 抛异常 → fail-open 直接返回。

## 4. 非目标 / 开放
- 不默认开(成本 + 过度迭代风险);先灰度观察 A 不够的场景再决定是否默认。
- 不做多 critic 投票(单 critic 够;要更强是后话)。
