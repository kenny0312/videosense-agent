# 设计:单 loop 主路 —— 把 Router 从【终局门】降为可选提示 / 移除(CC 式)

> 状态:Design(评审后即动手) · 范围:`pipeline/orchestrator.py`(run_query)、`pipeline/loop_driver.py`(_LOOP_SYSTEM)、`pipeline/router.py`、`api/server.py` + `web/index.html`(turn_type 徽章) · 关联:① follow-up 门序修复(PR #48,本设计的"首付")、`memory-simplification.md`(方案 A)

## 1. 背景 / 问题
实测反复暴露**同一类** bug:一个【不看上下文】的前置分类器(Router),却对【只有结合上下文才看得懂】的话做【终局裁决】,把本该进 loop 的轮挡在门外。
- ① 最严重:助手问「你想看这个视频吗?」后,「ok」被判 `smalltalk` 回了句寒暄、「我想看」被判 `refuse` 回「太模糊」—— 两者都没进 loop(只有 loop 带回放才解得出"这个视频")。PR #48 把这两道门改成"有上文就不终结"是**补丁**,但病根是 **Router 作为前置门这个结构本身**。
- 用户洞察(成立):应学 **CC —— 主用一条 loop**,模型带【完整历史】在 loop 内自己决定 答 / 澄清 / 拒 / 调工具,不要前置分类门。

## 2. 目标 / 非目标
**目标**
1. **loop 成唯一主路**:每轮直接进 loop;smalltalk / refuse / clarify 由 loop 带完整 transcript 回放【自己】判,不再有能【终结】一轮的前置门。
2. **更省**:去掉每轮的 Router 预调用 —— 现在每条真实查询是 `router(1 次 flash)+ loop(…)`,之后只 `loop`。
3. **不回归**:寒暄、超范围拒答、追问、指代、「第 N 个」回指 —— 行为不差于现在,且更稳。

**非目标**
- **不做【树 / leaf-node】对话结构**(§4.4 决策):CC 本身是【线性 + 完整历史】,不是树;"回到之前的问题"靠完整回放即可,VS 已有。树是另一套更重的东西,本期不做。
- 不动 transcript 记忆层(回放 / 压缩不改)。
- 不做强安全网关(VS 的拒答是"超范围",loop 能处理;硬安全另议,见开放问题)。

## 3. 现状(据代码核实)
`orchestrator.run_query`:
1. `Router().judge(nl, schema=schema, tools=catalog_for_planner())` → `RouterVerdict{decision, confidence, reason, intent, turn_type, route}` —— **一次 flash 调用**(`CRITIC_MODEL`)。
2. 消费点:
   - `decision == "smalltalk"` → `smalltalk_reply(nl)` 早返回(PR #48 后:**仅当 `not replay_ctx`**)。
   - `should_refuse`(`decision=="refuse" and confidence ≥ REFUSE_MIN_CONFIDENCE`)→ 拒答早返回(PR #48 后:**仅当 `not replay_ctx`**)。
   - `turn_type`(new | followup | meta)→ 回前端做【徽章】。
   - `route` → `skills.handler_for(route)` 分派自定义 handler —— **现阶段四大类全是 "planner" → 全落 loop,实际是死代码**(orchestrator 注释自陈)。
   - `intent` → 仅日志。
   - Router 自身抛错 → **fail-open,照常进 loop**(已有"loop 兜底"先例)。
3. 之后:建 `replay_ctx`(PR #48 已提前到门【之前】)→ `run_query_loop(..., replay_context=replay_ctx)`。

→ **Router 真正还在做的只剩三件**:① 寒暄快路;② 超范围快拒;③ turn_type 徽章。①② 都能被 loop 接管,③ 可廉价派生。其余(route/intent)已是死代码 / 仅日志。

## 4. 设计

### 4.1 主干:run_query 去掉 Router 门
`run_query` 收敛成:`reset_usage → 设 Pro 覆盖 → 建 replay_ctx → run_query_loop → 记 transcript`。
删:Router 调用、smalltalk/refuse 早返回、route→handler 分派(死代码)。**loop 是唯一执行路径。**

### 4.2 smalltalk / refuse / clarify 交给 loop(principle,不是枚举)
`_LOOP_SYSTEM` 加一小段(principle,贴合"别套固定流程"):
- 纯寒暄 / 身份问题 → 用人设一句话答,**别调工具**。
- 明显超出"视频数据问答"范围、或确实做不到的 → **简短说明做不了**(别硬试)。
- 问题不清 → 先反问(指代 / clarify 指引已有)。

loop 本就能输出纯文本收口、已有 clarify 指引 → 这几类天然落得下,且**带完整上下文判,比 context-blind 的 Router 更准**。

### 4.3 turn_type 徽章:廉价派生,不再靠模型
- **new vs followup**:据 `replay_ctx` 是否非空派生(零模型调用)。最简 `followup if replay_ctx else new`;要更准可看 loop 是否实际引用了上文。
- **meta(方法回放)**:取消独立徽章 —— loop 直接答"怎么算的"(它有回放),不需要徽章标识。
- (可选)若坚持要更准的 meta 徽章,留一个【纯标注、绝不终结、与执行解耦】的极薄分类。**倾向先派生,不够再说。**

### 4.4 【决策】保持线性 + 完整回放,不做树
用户提到"树叶节点 / leaf-to-node 回到之前的问题"。澄清 + 决策:
- **CC 是一条线性 loop**(模型看完整历史自己决定),**没有**树 / leaf node。"回到之前的问题"靠的是**完整历史就在上下文里**。
- VS 的 transcript 回放已把整段对话喂给 loop → loop 已能引用 / 回到任意先前轮(① 刚把挡路的门拆了,"看第 2 个"已能回指)。
- 真做成【树】= 分支管理 + "当前活跃节点" + 导航 UI + "哪条分支进上下文"的取舍 —— **重,且不带来"完整回放"之外的新能力**。据 [[architecture-prefer-simplicity]] "别加层":**本期保持线性,不做树**。将来若要【可见的分支 UX】(产品功能)再单独立项。

### 4.5 安全 / 滥用
VS 的"拒"主要是【超范围】(不是视频数据问答)→ loop 用 §4.2 的 principle 处理即可;沙箱 + 业务表白名单 + "潘多拉"隔离仍在,边界没松。若将来要【硬安全门】(内容审核之类),加一道**极薄、非终结、只在命中明确红线时才拒**的检查 —— 那是 loop 之外的安全层,不是退回 Router 那种"功能分类门"。(开放问题)

## 5. 改动点 + 受影响测试
| 文件 | 改动 |
|---|---|
| `pipeline/orchestrator.py`(run_query)| 删 Router 调用 + smalltalk/refuse 早返回 + route→handler 分派;turn_type 改派生;只留 建回放 → loop |
| `pipeline/loop_driver.py`(_LOOP_SYSTEM)| 加 principle:寒暄一句答 / 超范围简短拒 / 不清先问 |
| `pipeline/router.py` | 先【停用】不删(便于回退);后续标弃用或只留纯标注 |
| `api/server.py` + `web/index.html` | turn_type 用派生值;meta 徽章下线 |
| `pipeline/skills/handlers.py` | handler 分派下线(死代码);未来自定义 workflow 走 loop 的工具 / skill |

**受影响测试**
- `test_multiturn.py`:smalltalk/refuse【门】相关用例重写 —— 没有终结门了,断言改成"一律进 loop、由 loop 处理"。**注意:我在 PR #48 加的 3 条门测试(`test_smalltalk_with_history_falls_through` 等)也要随之调整**(门没了 → 直接验 loop 收到回放并自处理)。
- 新增:寒暄("你好")→ loop 一步纯文本收口、不调工具;超范围("帮我写首诗")→ loop 简短拒;首轮无回放仍正常。
- `test_router.py`(若有)→ 迁移 / 标记。

## 6. 里程碑(小步可回退)
- **L0 影子(零风险)**:先给 loop 加 §4.2 principle + 备好派生 turn_type,**但还不删 Router**(env 开关 `USE_ROUTER_GATE=1` 默认开)。本地验证 loop 能接住寒暄 / 拒答 / clarify。
- **L1 切换(可灰度回退)**:`USE_ROUTER_GATE=0` → run_query 走单 loop 主路(Router 不再当门)。观察寒暄 / 拒答 / 追问质量 + 成本(应降:每轮少一次 flash)。一键回退 `=1`。
- **L2 清理**:稳定后删死代码(route 分派、smalltalk/refuse 早返回),Router 模块标弃用或只留纯标注。

## 7. 开放问题(评审定夺)
1. **turn_type 徽章**:派生(new/followup,零成本)够不够?还是保留一个极薄标注分类给更准的 meta 徽章?倾向先派生。
2. **要不要留一道极薄安全门**(非终结、只拦明确红线)?VS 现在不需要;若产品要内容审核,放哪层?
3. **Router 模块**:停用后【删】还是【留作纯标注 / 未来分流】?倾向先留(标弃用),L2 再定。
4. **寒暄成本**:greeting 现在走 loop(带完整 system + schema + tools + 回放)→ 比小 router 调用 token 多一点。要不要给"明显寒暄"留一个超轻判断省 token?倾向不要(每轮省下的 router 调用已抵)。

---

### 已核实事实锚点
- `run_query` 现状:Router → smalltalk/refuse 早返回(PR #48 后受 `not replay_ctx` 守)→ replay 建好 → loop;route→handler 全 "planner"(死代码);Router 出错已 fail-open 进 loop。
- Router = 一次 `CRITIC_MODEL`(flash)调用;loop 大脑也是 flash(`LOOP_MODEL=CRITIC_MODEL`)→ 去掉 Router = 每轮少一次同档调用。
- loop 已能纯文本收口 + 已有 clarify / 指代回放指引(`_LOOP_SYSTEM` "# 指代与追问")→ 接管 smalltalk/refuse/clarify 无新机制。
