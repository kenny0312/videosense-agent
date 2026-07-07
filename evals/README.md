# evals —— VS 评测系统（τ²-video）

VS「先有可信评测，再谈微调」的地基。τ²-bench 式：以**真 Gemini agent** 为被测主体，
**62 道 grounded 题**（见 [DATASET.md](DATASET.md)），dual-control 的 world 工具面（`python -m evals.tools`）。
设计见 [../docs/eval-system-and-layer0-plan.md](../docs/eval-system-and-layer0-plan.md) 和
[../docs/tau2-video-eval-design.md](../docs/tau2-video-eval-design.md)。

## 怎么跑

在项目 anaconda 环境、仓库根目录下：

```bash
# 看数据集清单 / 校验金标能对上 mock DB / 看 world 工具面
python -m evals.runner --list
python -m evals.validate_tasks
python -m evals.tools

# 单测（含核心断言：没查跳伞库就答否定 = 没过）
python -m pytest evals/ -q

# 跑一遍标准题，生成首次基线报告
python -m evals.runner
# -> 打印每题结果 + 生成 evals/report.html

# 演示"变差"：好策略(旧版) vs 回归策略(新版)，跳伞题失守 -> 打回
python -m evals.runner --compare

# Mode B：真 Gemini 进循环（要 GCP 凭证 + 花 token；用 mock DB 不碰生产数据）
set GCP_PROJECT=<你的项目>
set GENAI_LOCATION=global
set REPL_USE_MOCK_DB=1
python -m evals.runner --live --n 1     # 先 n=1 冒烟，稳了再加大 n
```

用浏览器打开 `evals/report.html` 看大白话报告（整体通过率 / 各方面变化 / 每题×各方面）。

## 这版是什么、覆盖到哪

**离线脚本车道**：用 `ScriptedConv` 把「大脑的动作」写死、`make_exec` 把「工具/DB 的结果」写死，
喂给**真的** `run_loop`（`pipeline/loop_driver.py:133`）。所以它**不调 Gemini、不联网、不碰 DB，零花费**。

它证明整套机器跑得通：出题 → 跑 n 次 → 判分（工具用得对 / 诚实不瞎编 / 找对视频）→
连做 k 次都对的比例 → 结论（变好 / 变差·打回 / 有得有失·待人看 / 没明显变化）→ 报告。
并守住那条关键断言：**没查 `skydive_segments` 就答否定 = 没过**。

> 脚本车道是确定的，所以「连做 k 次的比例」现在非 0 即 1；这个指标要等接了真 Gemini
> （有随机性）才真正发挥作用。脚本车道测的是**机器和判分逻辑**，不是模型的真实能力。

## 文件

| 文件 | 干什么 |
|---|---|
| `scorers.py` | 判分函数（纯函数、确定性）：`toolseq_match` / `refusal_ok` / `recall_at_k` / `timestamp_iou` / `answer_count` / `no_id_leak` / `no_provider_leak` / `passk` |
| `world.py` | `ScriptedWorld`（脚本车道）+ `LiveWorld`（Mode B 真 Gemini + mock DB）+ `live_preflight` |
| `session.py` | `DualControlSession`：τ² 多轮 dual-control（真 agent + 模拟用户，已建骨架） |
| `simulated_user.py` | 模拟用户（pinned 跨家族 Claude，persona+goal，能用 USER_TOOLS） |
| `tools.py` | world 工具面：agent 侧（取自 node_specs）+ user 侧 5 动作 |
| `runner.py` | 跑批 + 结论 + 命令行（`--list` / `--compare` / `--live`） |
| `report.py` | 大白话 HTML 报告 |
| `validate_tasks.py` | 校验金标能在真 mock DB 对上 |
| `fixtures/policies.py` | smoke 子集的脚本策略（假大脑）+ 固定工具结果 |
| `tasks/*.jsonc` | smoke 子集（带策略，脚本车道用） |
| `tasks/gen/*.jsonl` | 完整数据集 62 题（按维度） |
| `DATASET.md` | 数据集清单 |
| `test_*.py` | 单测 |

## 下一步

- **Mode B（真 Gemini 进循环）已接好**：`world.py` 的 `LiveWorld` + `runner.py --live`，用 mock DB 不碰生产、analyze_video 走缓存。**只差配 GCP 凭证 + 预算**就能真跑（见上面命令）。判分器和脚本车道完全同一套。
- **Claude 当模拟用户**测多轮（nightly）——要 Anthropic API key。
- **真执行器 + state-diff 写任务**（`update_memory` / 建索引）——`world.py` 已留占位。
- **多轮 JGA**（走 `run_query` 真 replay）。
- 接进 GitHub Actions 当合并前的门。
- 继续把 probe 21 轮里剩下的缺陷抽成题（已抽：跳伞诚实、做饭诚实、id 泄漏、花费自知、身份不漏底）。
