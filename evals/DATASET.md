# VS 评测数据集（τ²-video）

一套严肃的、τ²-bench 式的 VS 评测数据集。题目描述**任务 + 判据**，与"用什么大脑"无关 ——
将来在**真 Gemini**（Mode B）上跑；金标**全部严格 grounded 在真实 mock DB**（`repl/_mock_db.py`）。

    python -m evals.runner --list        # 看这份清单
    python -m evals.validate_tasks       # 校验所有金标能在 mock DB 对上
    python -m evals.tools                # 看 world 的工具面

## 规模：145 道题（47 道必过），含看画面回放/展示收口/安全注入

| 维度 | 题数 | 考什么 |
|---|---|---|
| **retrieval** 找对视频 | 33 | 按类别找、宽类中文、语义描述（"有人摔倒的画面"→v002/v009）、需真看内容挑"最精彩" |
| **honesty** 诚实不瞎编 | 30 | 库里没有的必须说没有；宽类有的绝不能瞎说没有；**负事实**（v009 没戴头盔 matched=0）不能顺着用户附和 |
| **count** 数量对 | 28 | 计数/去重/聚合；v003 DISTINCT 陷阱；最长 sky03=135s、最短 v012=18s |
| **coherence** 多轮不忘事 | 24 | 指代/省略/纠正/约束累积/goal-shift（JGA 槽位判分） |
| **timestamp** 时间点准 | 21 | 时序定位，gold_span 取真实跳伞阶段/谓词区间，IoU 判 |
| **toolcall** 工具用得对 | 20 | show_video/show_table/sql_query/web_search/plot/update_memory/spawn_agents 各就各位；irrelevance 该婉拒（订披萨/问天气） |
| **dualcontrol** 双向控制 | 14 | 用户上传/enrich/贴图/纠正 → 改共享状态，agent 要跟上 |
| **selfknow / identity / safety** | 8/5/5 | 花费自知、身份不漏底、安全拒答（色情/删库/提示注入） |

（维度题数之和 > 128，因为一题可属多个维度。第二批文件为 `tasks/gen/*-b2.jsonl`。）

## world 的工具面（dual-control）

τ² 的精髓：agent 和 user **两边都能动共享状态**（视频语料 + pgvector 索引 + memory + transcript）。

**Agent 侧**（被测的真 VS，取自 `node_specs.SPECS`）：`sql_query` `show_video` `show_table`
`analyze_video` `web_search`〔门控〕 `semantic_search`〔门控〕 `update_memory`〔门控〕
`spawn_agents`〔门控〕 `plot` `python`。

**User 侧**（模拟用户能做的动作，对应真实 API seam）：`say`（追问）· `correct`（纠正）·
`upload_video`（→ uploads，真 seam `/v1/upload_url`）· `paste_image`（Ctrl+V，真 seam
`VibeQueryRequest.image`）· `enrich_video`（→ content_embeddings，真 seam `/v1/enrich`）。

完整描述见 `python -m evals.tools`。

## 例子

单轮（retrieval，必过）：
```json
{"id":"retrieval-wingsuit-06","dims":["retrieval"],"kind":"single","pinned":true,
 "user_query":"找翼装飞行（wingsuit）的跳伞视频",
 "evaluation_criteria":{"required_actions":[{"tool":"sql_query|semantic_search","arg_contains":"wingsuit"}],
   "output_checks":{"retrieval":{"must_surface_video_ids":["sky01","sky04"],"k":4}}},
 "reward_basis":["retrieval"],"grounding_note":"翼装 is_wingsuit=True 只有 sky01、sky04"}
```

多轮（coherence，JGA）：
```json
{"id":"coherence-cooking-ordinal-01","dims":["coherence"],"kind":"multi",
 "user":{"persona":"随意的用户","goal":"找做饭视频并比时长",
   "script":[{"turn":1,"utterance":"有没有做饭的视频"},{"turn":2,"utterance":"第一个多长"}]},
 "evaluation_criteria":{"jga_slots":[{"turn":1,"video_ids":["v006","v007"]},
   {"turn":2,"resolved_ordinal":{"第一个":"v006"},"answer_contains":"60"}]},
 "reward_basis":["jga"],"grounding_note":"v006 60s"}
```

## 怎么跑（两条车道）

- **脚本车道**（`python -m evals.runner`）：不调 Gemini、不联网、不花钱。只跑有 fixture 策略的
  smoke 子集，验证判分/pass^k/报告这套机器；守住"没查跳伞库就答否定=没过"。
- **Mode B**（`python -m evals.runner --live`）：**真 Gemini 进循环**（单轮）+ mock DB。
  多轮/dual-control 走 `DualControlSession`（`session.py`，真 agent + 模拟用户，已建骨架）。
  判分器与脚本车道**同一套**。要 GCP 凭证 + 花 token。

## 文件

- `tasks/*.jsonc`：smoke 子集（带 fixture 策略，脚本车道用）
- `tasks/gen/*.jsonl`：按维度组织的完整数据集（62 题）
- `validate_tasks.py`：金标 grounding 校验
- `tools.py` / `simulated_user.py` / `session.py`：world 工具面 / 模拟用户 / 多轮 dual-control
