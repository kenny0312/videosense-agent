# VideoSense：DAG → probe-and-step 主循环 · 迁移设计 (v2)

> **状态：提案,待审查。** 当前 `main` 干净,`orchestrator.run_query` 仍是 DAG plan-then-execute(`pipeline/orchestrator.py:194-243`)。
> **本版基于两条已确认要求:** ① 不在意成本/延迟变化;② 结构必须改成 CC 式(全保真 append-only transcript)。
> 据此,v1 的「有界 step-trace + 成本熔断」升级为「**无界 transcript + 窗口压缩**」,成本熔断降为可选。

---

## 1. 一句话

把 `Planner → 整张 DAG → 拓扑执行` 换成一个**手写的 Gemini function-calling 主循环**:模型每步发工具调用 → 复用现有 `execute_node` 执行 → 结果喂回 → 直到模型返回纯文本即收敛。**大脑仍是 Gemini,Agent SDK 不当引擎**(它只跑 Claude)。记忆从「覆盖式有界 blob」换成「**append-only transcript**」,`recipe` 概念随之消失。

```
现在:  Router → resolve → Planner.plan → DAG → topo → for node: execute_node → 答案=最后节点值
之后:  Router → resolve → [循环: 模型 → function_call → execute_node → 结果喂回 ↺ 直到纯文本] → 答案=该文本
       └──── 前半段两路共享 ────┘   └────────────── 唯一被替换的一段 ──────────────┘
```

---

## 2. 保留 / 删除 / 改造(总览)

| 子系统 | 保留 | 删除 | 改造 |
|---|---|---|---|
| **编排** | `run_query` 前置段(Router/smalltalk/refuse/meta/skill)、`_result`、`_remember` | `orchestrator.py:194-243`(Plan+topo+for-node)、`Planner.plan` 作为「一次性出整图」 | 新 `pipeline/loop_driver.py:run_loop()` |
| **工具执行** | **整个 `node_executor`**(`execute_node`/各 `_run_*`/`NodeResult`)、`CodeGenerator`、`SqlFixer`、`mcp_client`、`node_specs.SPECS` | DAG 的 `depends_on` 边语义、`_inject` 的 `data_<dep>` 约定 | 上游改**显式命名句柄**(`result_id`),大值传句柄不回灌 |
| **会话/记忆** | 存储层(`_scoped` 等)、`resolve_references`、value 仓 + `_live_cached` 活探针 | `_derive_recipe`、`recipe` 字段、`catalog` 作为独立存储结构 | blob→transcript(§3);catalog→派生索引(§4) |
| **沙箱** | **全部逐字保留**(`_check_policy`+隔离+`timeout=30`) | — | — |
| **可观测** | `usage`、`trace.py`、`_audit` 发射机制 | 「首个节点失败=整轮 abort」语义 | transcript 即审计+复现凭据,**必须落库** |

> **沙箱回答:在,且不动。** 它与编排正交——循环里模型调沙箱类工具(ols/plot/python)时,路径仍是 `CodeGenerator → sandbox/client → executor`。**唯一不变量:所有代码执行仍只走 `_run_sandbox_node` 一条路**,别让新循环开第二条绕过 `_check_policy`。

---

## 3. transcript 存哪

**先更正现状**(一个常见误解):

| 数据 | 今天实际在哪 |
|---|---|
| history / rolling / **catalog** / recipe(**同一个 blob**) | **Redis**(生产)/ SQLite(本地),每轮 UPSERT 覆盖 |
| value 仓(完整可复用值) | Redis / 内存 LRU |
| GCS | **只有** plot 图片(`plots/…`)+ 签名视频 |

→ **catalog 在 Redis,不在 GCS**;GCS 只放渲染好的图片文件。

**目标:三层存储,全部 `owner:session_id` 命名,全部对业务 Neon 不可见。**

| 层 | 存什么 | 放哪 | 说明 |
|---|---|---|---|
| **热 transcript**(喂 prompt) | 近窗事件 | **Redis Stream**(`XADD`/`XREVRANGE`,带 `MAXLEN`) | 沿用现有隔离域,数据模型从 blob 换成 append |
| **耐久全量**(CC 同款留全部) | 全部事件行 | **GCS,一会话一个 `.jsonl`** | 最贴 CC 字面形;GCS 无便宜行追加 → 整对象重写,或一轮一对象 |
| **大负载溢出**(= CC 的 `tool-results/`) | 大表/图/帧 | **GCS `tool-results/{sid}/`** | transcript 行只存 `gs://` 指针 + 预览;复用现有「沙箱产物→主进程传 GCS」管线 |

> **铁律(潘多拉):transcript 绝不进业务 Neon**——否则 planner 的 MCP-SQL 能查到它,隔离即破。Redis / GCS 天然在 MCP 查询路径之外;若坚持用 Postgres,必须是**独立 database/角色**。

**数据流一览:**

```
事件 ─▶ 写入器(确定性·按 type+size)─┬─▶ Redis 热尾 (XADD,MAXLEN) ──取尾─▶ 下一轮 prompt(尾+旧摘要)
                                    ├─▶ GCS 全量 .jsonl (append,耐久真相)
                                    └─▶ GCS tool-results (size>阈值才落;行里留 指针+预览)
════════════ 潘多拉边界(SQL 够不到上面) ════════════
planner SQL ─▶ Neon 业务库(5 张视频表,只读)
```

### 3.1 谁判断「往哪存」——确定性代码,不是模型

模型只决定「做什么」(调哪个工具);**「存哪」由写入器按 `事件类型 + 大小` 机械判断**(零 token、零延迟、可复现、无幻觉):

```python
def append_event(ev, owner, sid):
    line = serialize(ev)
    if ev.type == "tool_result" and size(ev.value) > OVERFLOW_BYTES:  # 轴②:大 → GCS
        ref  = gcs_put(f"tool-results/{sid}/{ev.id}", ev.value)
        line = {**line, "result_ref": ref, "preview": cap_preview(ev.value), "n": n}
    gcs_append(f"transcripts/{owner}/{sid}.jsonl", line)              # 全量真相(冷)
    redis.xadd(f"vs:tx:{owner}:{sid}", line, maxlen=HOT_WINDOW)       # 热尾(轴①自动淘汰)
```

- **轴①(热/冷)不是逐条选**:每条小行都进 GCS 全量 + Redis 热尾;Redis 靠 `MAXLEN` 自动只留近窗,「冷」= 滑出热窗但仍在 GCS 的部分。
- **轴②(小/大)= 一次大小判断**:`tool_result` 本体超阈值或二进制 → 落 GCS,行里只留指针+预览(即 CC 的 overflow)。

### 3.2 保留与清理(TTL)

| 对象 | 规则 | 说明 |
|---|---|---|
| GCS `transcripts/` | Lifecycle `age>30d`(可选先转 Coldline) | server 端自动删,零 cron |
| GCS `tool-results/` | Lifecycle `age>7d` | 大本体可重算,短 TTL |
| Redis 热尾 | `MAXLEN` + key `TTL`(=SESSION_TTL) | 自动 |
| value 仓 | `ARTIFACT_VALUE_TTL_SECONDS`(已有) | 不变 |

> 定向删除(用户删会话 / GDPR)走显式 `gcs_delete(prefix=transcripts/{owner}/{sid})` + `redis.delete`。

---

## 4. 上下文:从「投影」改成「回放 + 压缩」

今天靠三个非对称视图投影 blob(`catalog_view`/`history_view`/`planner_context`)。之后:

- **轮内**:`contents` 随每步增长(模型输出 + `function_response` 追加)——这就是工作上下文,跟 CC 一轮一样。**大结果用 `result_id` 句柄 + 预览传**(替掉 DAG 的 `upstream` dict)。
- **轮间**:回放过去几轮 transcript 尾。

⚠️ **唯一一个「不在意成本」也免不掉的硬约束:模型上下文窗口仍有界。** 不在意成本 ≠ 不限长度——token 上限还在,所以**仍需 compaction**(CC 也如此:盘无界、窗口靠摘要压)。区别:现在**能用 LLM 语义摘要**取代旧的字符截断,压得更准。

三个简化:
- **recipe 删除。** transcript 自己就是复用底料:跨轮复用 = 模型在回放里看到上一轮调用,自己决定复算或 `load_artifact`。→ `_derive_recipe`、`recipe` 字段全去;`_explain_meta` 改成「从 transcript 回放回答」;`_context_block` 并入回放。
- **catalog 降级成派生索引。** 扫 transcript 里「产出 artifact」的事件**现推**句柄表,只为 Router 解析「那个表」。**`resolve_references` 抗幻觉过滤 + Router 前置门 保留**(多租户 + 模型可能幻觉 id,留着更稳)。
- **value 仓保留**当优化;`_live_cached` 活探针不变;`load_artifact` miss 从硬失败放宽为软回喂(模型据此重算)。

---

## 5. loop 驱动(伪代码)

替换 `orchestrator.py:194-243`;前置段与 `_result`/`_remember` 不动。

```python
# pipeline/loop_driver.py
def run_loop(nl, *, schema, session, context, sandbox, trace, session_id, value_store):
    model = GenerativeModel(LOOP_MODEL, tools=[build_function_declarations(node_specs.SPECS)])
    history = [seed_prompt(system_text, context)]   # context = 回放 + 压缩后的上下文
    ledger, seen, answer = {}, {}, None

    for step in range(MAX_STEPS):                    # 硬上限:保证停 + 防死循环(与成本无关)
        resp = model.generate_content(history, generation_config={"temperature": 0.0})
        usage.add_usage(resp, LOOP_MODEL)
        calls = function_calls(resp)
        if not calls:                                # 收敛:纯文本即答案
            answer = resp.text; break
        history.append(resp.candidates[0].content)
        for k, fc in enumerate(calls):
            sig = hash((fc.name, normalize(fc.args)))
            if seen.get(sig, 0) >= REPEAT_LIMIT:     # 重复失败 → 强制终止
                return fail_open(history, "no_progress")
            cid = f"c{step}_{k}"
            upstream = {u: ledger[u].value for u in fc.args.get("_uses", []) if u in ledger}
            res = execute_node(Node(cid, fc.name, dict(fc.args)), upstream, sandbox, trace,
                               schema=schema, session_id=session_id, value_store=value_store)  # 逐字复用
            ledger[cid] = res
            payload = {"error": res.stderr[:300]} if not res.ok \
                      else {"result_id": cid, "preview": cap_preview(res.value)}  # 大值只给句柄+预览
            seen[sig] = seen.get(sig, 0) + (0 if res.ok else 1)
            history.append(function_response_part(fc.name, payload))
        history = compact(history, keep_last_k=K)    # 守 token 窗口(LLM 摘要老步)
    else:
        return fail_open(history, "step_budget_hit")
    return harvest(answer, ledger, trace)            # 收 plot/videos 旁路 + 组 transcript 落库
```

**每个事件落一条 transcript 行**:`{seq, ts, type: user|model|tool_call|tool_result, payload(截断), result_ref?}`。

---

## 6. 护栏

1. **`MAX_STEPS`(主 backstop)**——保证循环终止、防死循环。**这是终止性,不是成本**,即使不在意成本也必须有。
2. **重复失败检测**——`(tool, args)` 哈希 + 连续失败计数,越阈终止(节点级重试不再天然界住整轮)。
3. **沙箱不变**——`_check_policy` + 隔离 + `timeout=30`;补一条测试断言新循环路径仍触发 policy gate。
4. **潘多拉不变**——数据路径恒 `mcp_client.query_db`;循环绝不把会话/transcript 暴露成可查工具目标。
5. **(可选)成本天花板**——`usage.over_budget()`;req 1 下非必须,但建议留个上限防 runaway。

---

## 7. 分阶段迁移(可灰度并存)

`run_query` 内用 env 开关 `VS_EXECUTOR=dag|loop` 选择;前置段两路共享,并存代价低。

1. **`node_specs.SPECS` 加结构化参数 schema**(每工具一个 OpenAPI 形 `parameters`)。**纯加法,不碰 DAG 路径,可独立合入。**
2. **function-calling spike**——这是**全新调用形态**(今天所有 `generate_content` 都是文本 prompt + JSON-mode,无 `tools=`),先跑通最小 demo。
3. **写 `loop_driver.py`**,复用 `execute_node`;只接灰度流量。
4. **存储换形(§3)**:blob-UPSERT → Redis Stream + GCS 落盘。
5. **删 recipe、catalog 改派生、`_explain_meta` 改回放(§4)**;加 compaction。
6. **加护栏(§6)+ transcript 耐久落库**。
7. **灰度观测对比**(cost/latency/正确率),确认不劣 → **拆 Planner/DAG**(`orchestrator.py:194-243`、`dag_schema.py:topo_order/_validate_graph/parse_dag`)。

---

## 8. 需要你拍板的开放问题

| # | 决策 | 取舍 |
|---|---|---|
| ① | **`Node`/`topo_order` 留不留** | 让循环产「合成 DAG 对象」→ 旧消费方几乎不改但多一层;vs 纯 transcript 更干净但改动大 |
| ② | **多输入工具句柄是否够稳**(`merge_asof` 的 `left/right_result_id`) | 模型能否可靠产出正确句柄**未经实测** → **必须先 spike**,否则静默换表的数据正确性风险 |
| ③ | **耐久真相用 GCS-JSONL 还是隔离 Neon 表** | GCS 贴 CC/便宜/无行追加;Neon 表可 SQL 查审计/有真追加但要独立实例 |
| ④ | **compaction 策略** | LLM 语义摘要(更准、req 1 可承受)vs 确定性截断(可复现);保留哪些锚点 |
| ⑤ | **API/前端 + 流式** | `/v1` 的 `dag` 字段没了 → 换 transcript;`web/index.html` 渲染要改;要不要上 SSE 流式 |

---

## 9. 风险(已验证)

- **延迟 ↑**:循环=多次串行模型往返 → 单轮墙钟时间大涨;Cloud Run `--timeout 120` 可能不够 → **调高**;`_session_lock` 持锁更久(多轮串行,可接受)。
- **确定性降级**:无可缓存计划 → **持久化 transcript 成为唯一复现/审计凭据,必须耐久落盘**(原「Trace 不落服务端」缺口从可选变承重)。
- **`planner.plan` 不是纯发射器**:内含 `validate→repair` 环,且 `execute_node` 还吃 `planner.schema` → 这套逻辑必须**迁移**(进循环或下沉到工具),**不能净删**。
- **存量会话硬切**:旧 blob 格式读不成 transcript;会话有 24h TTL、本就易失 → 直接切,不迁移。
- **测试大改**:`test_multiturn`(断言 `recipe['type']=='sql'`)、`test_artifact_value_reuse`、整个 `dag_schema`/`planner` 测试面全要重写。

---

## 关键文件锚点

`orchestrator.py:194-243`(待替换的 DAG 核) · `planner.py:plan/_system_prompt/_context_block`(待改造/salvage) · `session.py:125 _derive_recipe / 212 register_artifact / 304 resolve_references / 330 _live_cached`(记忆深水区) · `node_executor.py:273 execute_node / 51 _inject`(复用面 + 上游重接) · `node_specs.py:SPECS`(加结构化 schema) · `artifacts.py:_persist`(GCS 溢出层) · 新建 `pipeline/loop_driver.py`、transcript 存储层。
