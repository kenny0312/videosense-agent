# Stage 6 — Agentic REPL · 复现指南

> **一句话**:LLM 写 SQL → 主进程执行 → 拿到数据 → LLM 写 Python 分析代码 → Sandbox 执行 → 失败时把 traceback 回喂 LLM 重试,最多 3 次。这是自愈式 NL→Code agent 的最小可用实现。
>
> **设计原理**:见同目录 [stage6-repl-design.pdf](stage6-repl-design.pdf)(任务 / 实现 / 改进三章)
>
> **本文档目的**:任何人(包括未来的您)拿到这个仓库,**15 分钟内能复现一次成功的 demo**。

---

## 1. 前置(一次性,~5 分钟)

| 必须项 | 验证命令 | 备注 |
|---|---|---|
| Python 3.11+ | `python --version` 或 `C:\Users\User\anaconda3\python.exe --version` | Anaconda 也可 |
| gcloud CLI(已 login) | `gcloud auth application-default print-access-token` | 出 token 就 OK,出错就 `gcloud auth application-default login` |
| Sandbox(Cloud Run 部署好) | `gcloud run services describe sandbox --region=us-central1` | 已在 `https://your-sandbox.run.app` |
| 仓库依赖 | `pip install psycopg2-binary google-cloud-aiplatform vertexai` | 一次性装 |

---

## 2. 两种模式(任选其一)

### 模式 A —— Mock 模式($0 / 即装即用 / 演示首选)

内存 SQLite + 12 个内置视频 + 48 条 facts,数据贴近 ActivityNet 风格。**完全不需要数据库,不花钱**。

```cmd
set REPL_USE_MOCK_DB=1
set SANDBOX_URL=https://your-sandbox.run.app
C:\Users\User\anaconda3\python.exe -m repl.main
```

### 模式 B —— 真 DB 模式(AlloyDB / Cloud SQL Postgres)

```cmd
:: 不设 REPL_USE_MOCK_DB
set ALLOYDB_PASSWORD=<您的密码>
set SANDBOX_URL=https://your-sandbox.run.app
C:\Users\User\anaconda3\python.exe -m repl.main
```

切换方式:`set REPL_USE_MOCK_DB=` (空字符串等同未设) → 真 DB;`set REPL_USE_MOCK_DB=1` → mock。

---

## 3. 跑 demo(4 道标准测试题)

启动后您会看到提示符 `你的问题 > `。依次粘下面四题:

### Q1 —— 总数(冒烟,验证全链路通)
```
数据库里一共有多少个视频?
```
**预期**:答案 = `12`(mock 模式)或您 DB 里的真实数,trace 全绿 `[+][+][+][+]`。

### Q2 —— 关键词查询(测中英翻译)
```
找出包含滑雪活动的所有视频
```
**预期**:LLM 把"滑雪"翻成 `'%skiing%' OR '%snowboarding%'`,返回 `v001/v002/v003`。
**注意**:如果 prompt 里 *没有* "中文翻译成英文"的规则提示,LLM 会用 `'%滑雪%'` 直接搜,返回空结果。这条规则在 [`repl/generator.py`](../repl/generator.py) 的 `_sql_prompt` 里。

### Q3 —— 聚合(测自愈循环,首次易触发 SQL 重试)
```
哪个视频里出现的活动种类最多?各列出前 5
```
**预期**:答案 = `Backcountry Snowboarding Run`(3 种活动)排第 1。
**自愈剧场**:LLM 会用 Postgres 的 `array_length(arr, 1)` 或 `cardinality(arr)`,这在 SQLite 上不存在——`_mock_db.py` 的翻译器把它们静默转成 `json_array_length`,所以单次过。如果想故意触发 `[~]` 重试,删掉翻译器里的这两条规则。

### Q4 —— 统计(测 LLM 写 CASE WHEN 桶)
```
按置信度区间统计 video_facts 的分布,从 0.5 到 1.0 每 0.1 一档
```
**预期**(mock 模式):`0.5-0.6:4 | 0.6-0.7:6 | 0.7-0.8:7 | 0.8-0.9:12 | 0.9-1.0:16`,总 45。

### 跑完应该看到的总结表
| 题 | 期望 | 总耗时(参考) |
|---|---|---|
| Q1 | ✅ 4/4 全绿 | ~20s |
| Q2 | ✅ 4/4 全绿,3 个视频 | ~14s |
| Q3 | ✅ 4/4 全绿,v003 排第 1 | ~26s |
| Q4 | ✅ 4/4 全绿,5 个桶 | ~32s |

---

## 4. 文件分工(代码地图)

| 文件 | 行 | 干什么 |
|---|---|---|
| [`repl/main.py`](../repl/main.py) | ~80 | CLI 入口、UTF-8 兜底、warning 静默 |
| [`repl/loop.py`](../repl/loop.py) | ~170 | `run()` 主控:SQL phase(带 1 次自愈)+ Code phase(带 3 次自愈)|
| [`repl/generator.py`](../repl/generator.py) | ~200 | `CodeGenerator` 类:Gemini 调用 + prompt 模板 + history 上限 + SQL repair |
| [`repl/trace.py`](../repl/trace.py) | ~95 | 结构化 trace 事件 + 实时打印 |
| [`repl/_mock_db.py`](../repl/_mock_db.py) | ~310 | 内存 SQLite + 12 视频样本 + PG→SQLite 小翻译器 |
| [`sandbox/client.py`](../sandbox/client.py) | ~100 | HTTP client 调 Stage 5 sandbox(独立可复用) |

---

## 5. 自愈循环原理(2 分钟看懂)

**两阶段,各自独立重试**:

```
            用户问题
                │
                ▼
    ┌──────────────────────┐
    │  SQL phase           │
    │   try 1: 生成 SQL    │  失败 → repair_sql() 重试 1 次
    │   try 2: 修复 SQL    │  仍失败 → 整体失败,fail_phase=sql_exec
    └──────────┬───────────┘
               │  成功:拿到 data: list[dict]
               ▼
    ┌──────────────────────┐
    │  Code phase          │
    │   try 1: 生成 Python │  注入 data 为 JSON 字面量
    │   sandbox.execute()  │  失败 → 把 stderr 回喂 LLM
    │   try 2: 修复代码    │  最多重试 3 次(共 4 次尝试)
    │   ...                │
    │   try 4: 最后一次    │  仍失败 → fail_phase=code_exec
    └──────────────────────┘
```

**关键设计点**:
- 每一步都有 `TraceStep`,实时打印 `[+]/[~]/[x]`(成功/重试中/最终失败)
- `code_history` 上限 5 条(generator 内部),防止 prompt 无界增长
- LLM 历史在 SQL phase 是无状态的(失败就独立重试),只在 Code phase 维护

---

## 6. 故障排查表

| 症状 | 原因 | 修法 |
|---|---|---|
| `UnicodeEncodeError: 'charmap' codec can't encode...` | Windows cmd 默认 cp1252 | `main.py` 顶部已经 `sys.stdout.reconfigure("utf-8")` 兜底,如果不生效 → `set PYTHONIOENCODING=utf-8` |
| `Reauthentication needed` / Gemini 调用 401 | gcloud token 过期 | `gcloud auth application-default login` |
| `connection refused` 或 `Connection timed out` 连 sandbox | `SANDBOX_URL` 没设或本地 sandbox 没起 | `set SANDBOX_URL=https://your-sandbox.run.app` |
| `connection to server at "your-db-host" failed` | AlloyDB 已 trial 到期被停 | 切 mock 模式:`set REPL_USE_MOCK_DB=1` |
| `[失败] 阶段=sql_exec, no such function: XXX` | LLM 写了 Postgres 函数,mock 翻译器没覆盖 | 在 `_mock_db.py` 的 `_TRANSLATIONS` 加一条正则 |
| `policy_violation=True` in trace | LLM 写了被禁的 import(`requests` / `subprocess`) | sandbox 故意拒,再问一次让 LLM 重写 |
| trace 不打印 `[+]`,只看到日志 | logging level 太低 | `main.py` 里改 `logging.basicConfig(level=logging.WARNING)` |

---

## 7. 已知限制 & 改进路线

### 当前能力边界
- **SQL phase 只重试 1 次**(`SQL_MAX_RETRIES=1`),Code phase 重试 3 次(`CODE_MAX_RETRIES=3`)
- **没有跨 session 上下文**——每次 `run()` 起一个新 `CodeGenerator`,上次问什么不知道
- **没有 streaming UI**——trace 是 stdout 实时打印,不是 SSE 推前端
- **没有 result artifact 持久化**——每次结果只在屏幕上,关掉就没了
- **没有 long-running kernel**——每次 sandbox 执行都是 fresh subprocess(Stage 5 隔离模型决定)

### 已规划改进(详见 PDF §3)
| 优先级 | 改进 | 估时 |
|---|---|---|
| P1 | 错误分类(syntax/runtime/semantic 各走不同 prompt) | 1d |
| P1 | Schema as tool(让 LLM 主动查 schema,不一开始塞 prompt) | 1d |
| P1 | Artifact 持久化(每次 run 写 `run_<hash>.json`) | 0.5d |
| P2 | Verifier pass(成功后 LLM 二次审,防 silently wrong) | 1d |
| P2 | Long-running kernel(对标 Pandora,跨轮 df 复用) | 1-2w |
| P2 | Streaming UI(FastAPI + SSE,接 Stage 10) | 1w |

### 已知技术债
- **vertexai SDK 将于 2026/06/24 deprecated** —— 已在 `main.py` 用 `warnings.filterwarnings` 静默 warning,但要迁移到 `google-genai` 新 SDK 才是长期方案
- **`_mock_db.py` 翻译器只覆盖了 5 条规则**(ILIKE / cast / NOW / array_length / cardinality),遇到新的 Postgres 专有函数还会死。LLM 报错 → 看 trace stderr → 加正则
- **mock 数据 12 视频偏小** —— 想跑更复杂的查询,要么扩 seed,要么走真 DB

---

## 8. 备份与重生数据(可选)

如果今天用 mock 模式发现想要"真数据"再次 demo:

```cmd
:: 用 Stage 2 重新生成 facts,~30 min,< $5
python perception/gemini_predicates.py --videos 20 --output data/video_facts.csv
```
(`--videos 20 --output` 是规划中的 flag,目前未实装。要实装见 PDF §3 P1 "重生脚本"。)

生成完 CSV 之后,只要 `_mock_db.py` 实装了"有 CSV 就用 CSV"的回退逻辑(规划中),mock 模式会自动从 `data/*.csv` 加载真数据。

---

## 9. 设计文档(读这个之前先跑一遍 demo)

[stage6-repl-design.pdf](stage6-repl-design.pdf) —— 三章,~5 页:
1. **任务、内容、为什么** —— Stage 6 在 pipeline 里干什么、为什么自愈循环不是可选
2. **现有实现细节** —— 数据流图、文件分工、关键设计决策(为什么 SQL/Python 分两步、为什么 JSON 注入、为什么 history 分开)
3. **可改进方向** —— P0/P1/P2 三级清单,带估时和判断"什么时候上 P1/P2"的信号

---

**写于 2026-06-07**——Stage 6 demo 在 cmd / Windows / Anaconda Python 3.13.5 上 4/4 题通过。
