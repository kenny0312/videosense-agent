# 2026-07-01 U 批次升级 —— 探针驱动的七项更新(U1–U6 + 加固)

> 来源:[probe-findings-upgrade-plan](../design/probe-findings-upgrade-plan.md)(21 轮实测探针定的方案)+ roadmap 三项新增(最强模型 / 联网搜索 / 现场写码确认)。
> 节奏:每项独立 PR(可单独回滚)→ 实测验收 → 对抗式 review(4 镜头×双否证)→ 修复 → 终回归 9/9 → 部署。
> 测试:**185 个离线测试全绿**;线上探针累计 ~60 轮真实对话验收。

## 0. 一览:PR × 内容 × 回滚方式

| PR | 内容 | 回滚 |
|---|---|---|
| [#60](https://github.com/kenny0312/videosense-agent/pull/60) | **U2 展示收口契约**:id 永不进答案文本(走侧信道);要看/要播必以 show_video 收口;计数对账「共X展示Y」 | revert PR(纯 prompt) |
| [#61](https://github.com/kenny0312/videosense-agent/pull/61) | **U3 自我认知**:会话累计 usage 注入大脑;「用了多少 token/钱/窗口多大」给真数;元问题不漏厂商 | revert PR |
| [#62](https://github.com/kenny0312/videosense-agent/pull/62) | **U5 大脑升 gemini-3.5-flash**:新 google-genai 后端(global 端点),按模型代际自动选后端 | `LOOP_MODEL=gemini-2.5-flash`(env,一键回旧 SDK 路径) |
| [#63](https://github.com/kenny0312/videosense-agent/pull/63) | **U1 受控大类标准**:26 词表+153 别名+197 谓词映射;入库即写大类;查询桥(词表进 prompt+护栏+DISTINCT+两层计数) | revert PR;大类行可 `DELETE ... WHERE rationale LIKE 'category:%'` |
| [#64](https://github.com/kenny0312/videosense-agent/pull/64) | **U6 web_search**:Gemini Google-Search grounding,带注入防护与来源引用 | `USE_WEB_SEARCH=0`(env,工具即消失) |
| [#65](https://github.com/kenny0312/videosense-agent/pull/65) | **U4 加固**:不当内容策略性拒答(零工具);transcript 耐久序号防覆盖(Redis INCR) | revert PR |
| [#66](https://github.com/kenny0312/videosense-agent/pull/66) | **依赖拆弹**:google-genai 缺失于 requirements(会崩生产),部署前审计抓到 | — |
| [#67](https://github.com/kenny0312/videosense-agent/pull/67) | **review 修复**:web_search 预览 80 字截断(诱发编造引用)、词表同步 FK 顺序、序号对齐 max+1 | revert PR |

## 1. 各项验收数据(全部实测)

**U2 展示收口** —— 改前:「播放最精彩的那个」把 3 个 30 字符 id 各打两遍且 `videos[]` 为空。改后:0 泄漏(8 问探针),播放意图 `videos[]`=1,「共 12 个,展示前 8 个」自动对账。3.5-flash 上线后曾用富格式反引号重新泄漏(11 处)→ 以正反例强化规则 → 复测 0 泄漏。

**U3 自我认知** —— 「我这次对话用了多少 token」→ 报出与实测分毫不差的真数(9,136);累计成本正确;窗口答 ~100 万;「你是什么模型」→ 只说 flash 档,零厂商泄漏。

**U5 模型升级** —— 事实:gemini-3.5-flash【只】在新 google-genai SDK + global 端点可达(旧 SDK/us-central1 全 404,逐一实测);3.5-pro 项目未开通。收益实测:「有没有做饭的视频」2.5 答"没有",3.5 自己连发 8 个 SQL 挖出 preparing salad 等(后被 U1 词表根治为 1 次直查);挑"最精彩"会用 skydive_segments 的 freefall 数据而非盲目全 analyze。成本实测:~$0.034/轮(约 2.5-flash 的 10 倍;价目 $1.5/$9 per 1M,"Flash 价"是宣传话术)——视频分析(成本大头)仍在 2.5-flash 不变,单人使用可接受。价目已进 usage.py 审计。

**U2b 中英 prompt A/B(附带实验)** —— 2 变体×4 维×17 问并行实测:zh 15好/2弱 vs en 14好/3弱,0 broken;弱项全是数据层噪声。**结论:中文保留**(英文无优势、略啰嗦更贵)。

**U1 受控大类(结构性最大项)** —— 途中发现:**114 个视频里 50 个(44%)零 video_facts 行**,对一切内容查询隐身(跳伞事故×50)。处理:26 大类词表(代码即真源,git 可审)+ Neon 两小表 + 65 个有谓词视频回填 + **50 个隐身视频全部补抽(50/50 成功,279 条活动,均置信 0.955)** + 新入库自动带大类。终态:**114/114 全覆盖,0 无大类视频**,video_facts 578 行。验收:做饭→7 个✓;跳水(当天新数据)→3 个✓;滑雪→「冬季运动大类 2 个,其中真正滑雪 1 个(另一个是冰壶)」两层精确作答✓;钓鱼→诚实没有+最近类说明✓;「有哪些类别」→ 24 个干净大类(此前是 195 个细谓词的墙)。

**U6 web_search** —— grounding spike:2.5-flash 与 3.5 答案等同、便宜 9 倍 → `WEB_SEARCH_MODEL=gemini-2.5-flash`。混合问句一轮内 SQL+联网各归其位;关掉开关 → 工具从声明消失、诚实说不能联网。防护:网页内容=资料非指令(双层写入)、范围限视频相关、grounding 计入成本审计。

**U4 加固** —— ①「有没有色情视频」从"查库答没有"改为**策略性拒答(零工具调用)**;②真 bug:transcript 耐久层 GCS 对象名用进程内计数器,**重启即从 1 重数、静默覆盖会话最早历史** → 改 Redis INCR(跨进程单调,计数键刻意不设 TTL)+ 旧历史按 max+1 对齐 + Redis 故障退时间戳名(任何模式下绝不复用旧名);③数据修正:2 个非翼装视频被错标 `wingsuit skydiving`(A/B 探针发现)→ 已删,12 翼装/14 跳伞与 skydive_segments 对齐。

## 2. 对抗式 review 摘要

4 镜头(logic/state/security/contract)× 每发现 2 个否证代理。部分否证代理撞了会话限额 → 全部争议项**逐条人工对码复核**。结果:
- **确认并修复**(#67):词表同步 FK 顺序(删大类先于删别名 → 演进词表必崩)。
- **复核属实并修复**(#67):web_search 结果被 80 字/格预览截断 → 大脑靠自身知识脑补"搜索结果"(编造引用风险);序号对齐按 count 会被历史空洞骗到复用旧名 → 改 max+1 + 探测失败绝不冒用序号 1。
- **按设计接受**(已记录):计数键无 TTL(过期会回卷重演覆盖)、Redis 抖动时会话态 fail-open(与全库哲学一致)、Redis 停机期的时间戳名顺序残留(严格优于其替代的覆盖行为)、genai 空候选优雅退化(优于旧后端直接抛错)、单实例 per-session 锁已序列化首写竞态。

## 3. 数据操作记录(非代码,已执行)

| 操作 | 结果 |
|---|---|
| categories/category_aliases 建表+同步 | 26 大类 / 153 别名 |
| 大类行回填(两轮,幂等) | 56 + 5 行 |
| 50 个零 facts 视频补抽(category 感知) | 50/50 成功,279 条活动,0 失败 |
| wingsuit 过度标注修正 | 删 2 行;12 翼装 / 14 跳伞 |
| 终态 | video_facts 578 行,114/114 视频有大类,24 类在用 |

## 4. 部署与开关矩阵

部署:`gcloud run deploy videosense --source . --region us-central1`(**不带** `--set-env-vars`,保留全部现有 env;已审计服务 env,无旧 LOOP_MODEL/USE_* 覆盖,新默认值直接生效)。

| 开关(env) | 默认 | 作用 / 回滚 |
|---|---|---|
| `LOOP_MODEL` | gemini-3.5-flash | 回 `gemini-2.5-flash` = 自动走旧 SDK 路径 |
| `GENAI_LOCATION` | global | 3.x 必需 global |
| `USE_WEB_SEARCH` | 1 | `0` = web_search 从工具声明消失 |
| `WEB_SEARCH_MODEL` | gemini-2.5-flash | grounding 模型 |
| `USE_ROUTER_GATE` / `USE_SELF_CHECK_CRITIC` | 0(不变) | 上一批的回退开关,保留 |

## 5. 已知残留与后续(不阻塞)

1. **P2 语义桥(pgvector)**:按设计文档触发条件 —— T2 上线后观察到词表对不上的真实 miss 再上。
2. **上传 IDOR owner 校验**:capability-only(122-bit uuid4),已有 task chip,低优。
3. **perception 仍在旧 vertexai SDK**(2.5-flash 视频分析,含 M4.5 裁剪 hack):旧 SDK 已过官方移除期限(2026-06-24),运行正常但建议排期迁 google-genai(genai 的 VideoMetadata 原生支持 start/end offset,迁移后 hack 可删)。
4. Redis 长停机期间写入的 t-前缀 transcript 对象在恢复后排序靠后(顺序近似;严格优于旧实现的覆盖丢失)。
5. 补抽产生的新细谓词(如 diving (handstand dive))不在 197 映射表内 —— 不影响功能(入库时模型已直选大类),下次演进词表时可顺带收编。
