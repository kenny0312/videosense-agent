# dvd_repro — DVD 论文本地复现(Deep Video Discovery, arXiv 2505.18079)

按 `docs/dvd-repro-plan.md` 执行。目标不是刷榜:跑通 建库→三工具→agent 循环→答题,
在 5 条视频 × 20 题上产出 `准确率 × 成本 × 延迟` 三方对照表(DVD-agent vs 全喂 vs 冻结索引),
作为 agency-Δ 的第一台实验装置。

## 隔离契约(第一原则)

| 资源 | 承诺 | 撤销 |
|---|---|---|
| VS 核心代码(pipeline/api/perception/evals/web) | **零修改**,只 import 复用 | — |
| Git | 所有 PR 的 diff 只出现 `dvd_repro/**` 路径 | 不合并即无痕 |
| GCS | 只新增 `gs://activitynet/lvbench/` 前缀下对象 | `python -m dvd_repro.cleanup --execute` |
| Neon 库 | 只新增行,全部 `source='lvbench-dvd'` 打标,不改任何存量行 | 同上,按标签删 |
| 本地 | 产物全在 `dvd_repro/{db,videos,logs,results}/`(gitignore) | 同上/删目录 |

**污染基线**(2026-07-18 开工前,验收时逐关核对):video_metadata=511,
video_discovery=50, video_facts=2218, content_embeddings=4957(存量行数不许变)。

## 费用闸门(任何 Gemini 调用后必须过 costguard.charge)

单次 >$0.50 / 单场运行 >$5 / 项目总 >$45 → 立即:进度已增量落盘 → 写
`results/PAUSED.json`(含恢复令牌)→ 停下等审查。审查后携 `approve_token` 续跑,断点续、不重花。

## 快速上手(三条命令)

```
python -m dvd_repro.build_db <video_id|mp4路径>     # Stage 1: 建库(可 --inspect N 抽查)
python -m dvd_repro.agent "<问题>" --vid <video_id>  # Stage 3: 单题跑 agent(调试)
python -m dvd_repro.run_eval                         # Stage 4: 全量对照表 → results/summary.md
```

## 目录地图(精读顺序)

```
config.py      所有旋钮(参数/模型/闸门阈值/标签)     ← 先读
costguard.py   费用三级闸门(离线单测 5 条)
cleanup.py     隔离契约撤销器(默认干跑)
segmenter.py → captioner.py → registry.py → embedder.py → store.py → build_db.py   (S1 建库六件套)
tools/{global_browse,clip_search,frame_inspect}.py   (S2 三工具,纯函数)
agent.py       (S3 循环,≤15 轮,全轨迹落 logs/)
baselines.py + run_eval.py + questions/              (S4 对照实验)
prompts.py     4 个 prompt;每个上方注释标官方原文出处,下面是 Gemini 改写版
ACCEPTANCE.md  每 Stage 验收证据存档
```
