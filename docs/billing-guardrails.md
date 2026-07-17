# 账单护栏 · P0-1（你亲自执行的三道底线）

> 这一块 Claude **不替你点**——它动的是你的**计费账户**，且 Gemini 花费上限只能在网页界面设。
> 下面每步都能复制粘贴。做完这三步 = 「睡觉也不会被打出天价账单」。

背景：应用层限流（P0-2，已随代码上线）是软护栏，Redis 一抖就会 fail-open 放行。**真正不依赖任何东西的硬底线，是 provider 侧的花费上限。** 两层叠加才是纵深防御。

---

## 底线一：Gemini API 月度花费上限（最重要，网页界面，~3 分钟）

这是唯一「到顶直接停服、不依赖你的 Redis/代码」的硬闸。

1. 打开 **https://aistudio.google.com/app/billing** （或 AI Studio → 左下 Settings → Billing）。
2. 确认计费项目是 `primeval-camera-494521-u6`（VS 用的项目）。
3. 找 **"Set up a monthly spend limit"** / **"Spending limit"**，设一个你能承受的月上限（例如 `$50`）。
4. 保存。到顶后该项目的 Gemini 调用返回 `429 RESOURCE_EXHAUSTED`，应用会走已有的退避逻辑、不再烧钱。

> ⚠️ 两个已知边界（Google 官方）：账单数据有最长 ~10 分钟延迟，可能小幅超顶；Batch 任务和长
> agent 会话可能在跨过上限的那一刻继续跑完。所以上限要设得比「绝对红线」略低一点留缓冲。
> 另外 Gemini 各 tier 自带一个 **10 分钟滚动花费熔断**（如 Tier 1 = $10/10min，超了直接 429）——
> 这是免费送的第二层，不用配置。

---

## 底线二：GCP 预算告警 50 / 80 / 100%（邮件预警，~5 分钟）

花费上限是「闸」，预算告警是「烟雾报警器」——烧到一半就发邮件，让你在撞闸前就知道。
（Sysdig 2026 报告：针对 AI 服务的凭据窃取一年涨 376%，key 泄漏后就靠这个早发现。）

先启用 API（之前特意没替你开——现在你确认要做就开）：

```powershell
gcloud services enable billingbudgets.googleapis.com
```

拿到计费账户号（已查过是 `011926-5D02D6-B6060D`），建一个带三档告警的预算：

```powershell
gcloud billing budgets create `
  --billing-account=011926-5D02D6-B6060D `
  --display-name="VideoSense 月度预算" `
  --budget-amount=50USD `
  --threshold-rule=percent=0.5 `
  --threshold-rule=percent=0.8 `
  --threshold-rule=percent=1.0
```

默认告警发到计费管理员邮箱（你的 `kennyqiu0312@gmail.com`）。想发到别处或接 Pub/Sub 自动化，再加 `--notifications-rule-*` 参数。

> 想要「烧穿自动断电」的极端版（预算通知 → Pub/Sub → Cloud Function 解绑 billing）？Google 官方
> 明确警告这是**毁灭性且非实时**操作（可能连带删资源、有延迟窗口）。有了底线一的 spend cap 后，
> 个人项目**不需要**这一步——别做。

---

## 底线三：确认限流已生效（代码侧，已上线，你只需部署时带上 Redis）

P0-2 的应用层限流随这次代码改动已经进仓。它**需要 Redis 才工作**——你现在的部署已经配了
Upstash（`SESSION_BACKEND=redis`），所以限流会自动复用同一个 Redis，无需额外配置。默认额度：

| 维度 | 默认上限 | env 覆盖 |
|---|---|---|
| 具名用户 每分钟请求 | 30 | `RL_REQ_PER_MIN` |
| 匿名/guest 每分钟请求 | 8 | `RL_REQ_PER_MIN_GUEST` |
| 每 IP 每分钟（小额度档） | 40 | `RL_IP_REQ_PER_MIN` |
| 具名用户 每日成本 | $2.0 | `RL_DAILY_COST_USD` |
| 匿名/guest 每日成本 | $0.20 | `RL_DAILY_COST_USD_GUEST` |
| 单会话 累计成本 | $0.75 | `RL_SESSION_COST_USD` |
| **全站 每日成本熔断** | $15.0 | `RL_GLOBAL_DAILY_COST_USD` |
| 单条 query 字符 | 8000 | `QUERY_MAX_CHARS` |

超额返回 `429` + 中文理由。这些是**起步值**：上线跑几天后，去 MONITORING 看真实 cost 分布，
把上限收到「正常用户够用、失控者撞墙」的位置。要临时关掉限流：`USE_RATE_LIMIT=0`。

验证生效：部署后连着快速发十几条消息，应看到 `429`；或看审计日志里某用户 `cost_usd` 累计触顶后被拦。

---

## 一句话总结

- **底线一（spend cap）** = 硬闸，不依赖任何东西 → 先做，最重要。
- **底线二（预算告警）** = 烟雾报警器，撞闸前预警。
- **底线三（应用层限流）** = 软护栏，省得频繁打到底线一；代码已上线，带 Redis 部署即生效。

三层叠加，才是商业 agent 产品的标准「纵深防御」。
