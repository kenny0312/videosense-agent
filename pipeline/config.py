"""
中央配置 —— 消除 planner / repl / mcp_server 三处重复的 DB 与 GCP 常量。

所有取值优先读环境变量,给出和 .env.example 一致的默认值。
任何模块需要 DB / GCP / Sandbox 配置,都从这里 import,不再各写一份。
"""
from __future__ import annotations

import logging
import os


def _load_local_env() -> None:
    """本地便利:把仓库根 .env(gitignored,密钥统一放这)里的 KEY=VALUE 载入环境
    (不覆盖已显式设置的)。这样直接 uvicorn / 跑脚本都自动带上配置,无需先手动 source。
    neon.env 是老名字,2026-07-08 已并入 .env——这里还认它只为兜底,新密钥别再往里加。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in (".env", "neon.env"):
        try:
            with open(os.path.join(root, name), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except OSError:
            pass


_load_local_env()

# ── GCP / Vertex AI ───────────────────────────
GCP_PROJECT = os.environ.get("GCP_PROJECT", "your-gcp-project-id")
GCP_REGION  = os.environ.get("GCP_REGION", "us-central1")
GCS_BUCKET  = os.environ.get("GCS_BUCKET", "your-gcs-bucket")

# M5 实时上传:用户直传的临时视频。前缀单独(配 GCS lifecycle 自动删);临时 video_id 形如 up_<hex>,
# 注册在 Redis(TTL 到期自删),【不进 video_metadata】(免污染正式语料)。每用户每天有上传配额。
UPLOAD_PREFIX        = os.environ.get("UPLOAD_PREFIX", "uploads")          # gs://<bucket>/uploads/<owner>/<vid>.mp4
UPLOAD_TTL_SECONDS   = int(os.environ.get("UPLOAD_TTL_SECONDS", str(24 * 3600)))   # 临时注册 TTL(≈ lifecycle)
MAX_UPLOADS_PER_DAY  = int(os.environ.get("MAX_UPLOADS_PER_DAY", "20"))    # 每用户每天上传数上限
MAX_UPLOAD_BYTES     = int(os.environ.get("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))   # 单个上传大小上限(签进 PUT URL)
UPLOAD_CONTENT_TYPES = ("video/mp4", "video/quicktime", "video/webm")     # 允许的上传类型(端点白名单)

# Planner 与 Code Generator 用的模型(可分别覆盖,默认同一个)
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "gemini-2.5-pro")
CODEGEN_MODEL = os.environ.get("CODEGEN_MODEL", "gemini-2.5-pro")
# 前置 Router(可答性/意图判定)用的小模型 —— 评判任务,小模型够用且便宜
CRITIC_MODEL  = os.environ.get("CRITIC_MODEL", "gemini-2.5-flash")

# ── 执行器:probe-and-step 主循环(M7b 起【唯一】路径;旧 Planner→DAG 仅 dev CLI main.py 保留)──
# loop 大脑模型(U5):默认 gemini-3.5-flash(2026-05 GA;agentic/coding 强、~4x 快;$1.5/$9 per 1M
# ≈ 2.5-flash 的 5 倍价,但 loop 每轮 ~5-10k tok → 单轮 <$0.01,收益>成本;视频分析仍 2.5-flash 不变)。
# gemini-3.x 起【只】在新 google-genai SDK + global 端点可用(us-central1 404,已实测)——
# 后端由 loop_driver.make_conversation 按模型代际自动选。回滚:LOOP_MODEL=gemini-2.5-flash(旧 SDK 路径)。
LOOP_MODEL         = os.environ.get("LOOP_MODEL", "gemini-3.5-flash")
# 阶段A(CC 式切换):每请求可选大脑模型。服务端白名单 —— 绝不信任客户端字符串,
# 防任意模型名烧钱/打崩;guest* 账号锁便宜档(2.5-pro 输出单价 4x 于 2.5-flash)。
LOOP_MODEL_CHOICES = [m.strip() for m in os.environ.get(
    "LOOP_MODEL_CHOICES", "gemini-3.5-flash,gemini-2.5-flash,gemini-2.5-pro").split(",") if m.strip()]
LOOP_MODEL_GUEST_CHOICES = [m.strip() for m in os.environ.get(
    "LOOP_MODEL_GUEST_CHOICES", "gemini-3.5-flash,gemini-2.5-flash").split(",") if m.strip()]
# 阶段B:OpenAI 兼容大脑端点(Qwen/DashScope、OpenRouter、vLLM/Ollama 自托管同一套)。
# 默认阿里云美东(us-central1 后端 ~30-40ms RTT)。key 为空 = 功能不可用;
# qwen 模型【故意不在】默认白名单——过了 evals live 门再手动加进 LOOP_MODEL_CHOICES。
OAI_COMPAT_BASE_URL = os.environ.get(
    "OAI_COMPAT_BASE_URL", "https://dashscope-us.aliyuncs.com/compatible-mode/v1")
OAI_COMPAT_API_KEY  = os.environ.get("OAI_COMPAT_API_KEY", "")
GENAI_LOCATION     = os.environ.get("GENAI_LOCATION", "global")     # genai 后端端点(3.x 需 global)

# U6:联网搜索工具(Gemini Google-Search grounding;spike 已验)。
#   USE_WEB_SEARCH=0 → 工具从大脑的声明里消失(零残留);模型用 2.5-flash(grounding 够用且比 3.5 省 ~9x)。
USE_WEB_SEARCH   = os.environ.get("USE_WEB_SEARCH", "1").lower() in ("1", "true", "yes")
WEB_SEARCH_MODEL = os.environ.get("WEB_SEARCH_MODEL", "gemini-2.5-flash")

# L2:跨会话用户记忆(每 owner 一块,GCS user-memory/;update_memory 工具写入)。
USE_USER_MEMORY       = os.environ.get("USE_USER_MEMORY", "1").lower() in ("1", "true", "yes")
USER_MEMORY_MAX_CHARS = int(os.environ.get("USER_MEMORY_MAX_CHARS", "6000"))   # ≈2k tokens

# V1:语义检索(pgvector 内容级;semantic_search 工具 + analyze 随用写钩子)。
#   0 = 工具从声明消失且写钩子停用(零残留)。S4 验收通过(2026-07-02)→ 默认开。
USE_SEMANTIC_SEARCH = os.environ.get("USE_SEMANTIC_SEARCH", "1").lower() in ("1", "true", "yes")
# P0-5 长程引擎:semantic_search 的 video_ids 过滤(视频内下钻)。关 = 参数对大脑不可见、
# 行为与升级前逐字节一致;深度 2 三臂实验【同开】(控制变量)。
USE_IN_VIDEO_SEARCH = os.environ.get("USE_IN_VIDEO_SEARCH", "0").lower() in ("1", "true", "yes")
SEMANTIC_SEARCH_K   = int(os.environ.get("SEMANTIC_SEARCH_K", "8"))
MAX_LOOP_STEPS     = int(os.environ.get("MAX_LOOP_STEPS", "16"))    # 终止护栏:防死循环
LOOP_REPEAT_LIMIT  = int(os.environ.get("LOOP_REPEAT_LIMIT", "2"))  # 同一(工具,参数)连续失败上限

# ── P0-3 长程引擎护栏:per-tree(=per-request)美元熔断 + 墙钟 ────────────────
# 与 RL_* 的分工:RL_* 是【跨请求】的日/会话顶(事后 record);这两条是【请求内】的实时闸,
# 治的是"一次请求里一棵树把钱烧穿"—— DVD 实测 Trace 税 12× 成本方差正是这个形状。
# 两处挂点(缺一不可,红队 B1):工具执行前 + 主循环每步 generate 前 —— 只闸工具挡不住
# "进入 Trap 循环只思考不调工具"的烧钱。
# 默认 0 = 关(Part 0 不变量①:开关全关时行为与升级前逐字节等价)。
# 【下限约束,启动时校验,见本文件末 _validate_tree_guard_budget()】开启(>0)时必须
#   MAX_TREE_COST_USD >= TREE_ANALYZE_ESTIMATE_USD × MAX_ANALYZE_PARALLEL(默认 0.30×3 = 0.90),
#   否则一步内的并行 analyze 会把闸在【实花几乎为零】时顶掉(admit 一旦拦下就 _trip 整棵树,
#   之后每个工具调用全被拦 —— 不是"这次不看",是整次请求瘫痪)。
#   低于单次预留(< TREE_ANALYZE_ESTIMATE_USD)时 pro 档 analyze 永远进不来 → 直接 raise 拒绝启动;
#   够单次但不够满并行时 → 启动告警(别再靠人肉发现"怎么第三个 analyze 就熄火了")。
# 0=关;开启的实验值建议 0.90(旧注释写的 0.80 在 pro 档不满足上式,只在 flash 档安全)。
MAX_TREE_COST_USD  = float(os.environ.get("MAX_TREE_COST_USD", "0"))
MAX_TREE_WALL_S    = float(os.environ.get("MAX_TREE_WALL_S", "0"))     # 0=关;生产建议 900
# 触闸判据是"预估后比 + 在飞预留":spent + pending + 本次估价 > cap 即拦(而非事后发现超了),
# 否则最后一次调用总能越线、K 个并行调用在钱落账前互相看不见(超冲 K×,review 变异验证)。
TREE_CALL_ESTIMATE_USD = float(os.environ.get("TREE_CALL_ESTIMATE_USD", "0.05"))
# analyze_video 单独给悲观口径:pro/长视频单次 $0.10~0.30(60k tok × pro 价),按 $0.05 预留
# 会让并行 analyze 把 cap 冲穿 80%+(review 验算)。代价是缓存命中(免费)也按此预留 ——
# 保守方向,与"宁可早触闸"的设计一致。
TREE_ANALYZE_ESTIMATE_USD = float(os.environ.get("TREE_ANALYZE_ESTIMATE_USD", "0.30"))

# 自检 B(设计 self-check-critic.md):收口前插一个显式 critic 判"满足用户没",没满足喂回再来一轮。
#   2026-07-16 判决:12 争议题×两臂×n=3,成功数 20 vs 20 完全打平(无功也无害)→ 默认关,
#   不为零收益付每次收口的额外调用;保留为【请求级模式】(API critic=true / UI 开关)与本 env
#   (评测/全局实验用)。大脑换代或题库上难度后值得重测。MAX_ROUNDS = "再来"上限(防空转)。
USE_SELF_CHECK_CRITIC = os.environ.get("USE_SELF_CHECK_CRITIC", "0").lower() in ("1", "true", "yes")
SELF_CHECK_MAX_ROUNDS = int(os.environ.get("SELF_CHECK_MAX_ROUNDS", "1"))

# ── 子 agent 编排(spawn_agents;设计 docs/design/subagent-fanout.md §3)──
#   主脑把一个【能拆成几个彼此独立、各自多步】的大任务,当场为每个子 agent 写不同 instruction、
#   并行跑受限工具集的 mini-loop,收集各 output 自己综合。opt-in(默认 0;开 = 工具对大脑可见,
#   关 = 从声明消失,零残留,同 web_search)。
#   FANOUT = 一次最多并行几个子 agent(扇出/成本护栏);MAX_STEPS = 每个子 agent 的循环步上限(防子循环空转)。
USE_SUBAGENTS       = os.environ.get("USE_SUBAGENTS", "0").lower() in ("1", "true", "yes")
SUBAGENT_MAX_FANOUT = int(os.environ.get("SUBAGENT_MAX_FANOUT", "6"))
#   基线 4 → 6(2026-08-02 实测改):真机 14 个子 agent,拿 4~5 步的 10 个【无一收敛】、
#   拿 6 步的 4 个【全部收敛】。且与"派了几个视频"无关 —— 同一步内 analyze 是并行的
#   (loop_driver 线程池),N 个视频本来就能一步看完;卡死的是固定开销:定位 1~2 步 + 看 1 步
#   + 汇总 1 步 ≈ 4,4 步等于零余量。实测有子 agent 拿 2 视频/4 步,一个视频都没看成就撞墙。
SUBAGENT_MAX_STEPS  = int(os.environ.get("SUBAGENT_MAX_STEPS", "6"))
#   MAX_STEPS 是【基线】,实际步数按这个子任务要看几个视频动态给(subagents._steps_for):
#   4 步装不下「读任务 + 逐个看 N 个视频 + 汇总成文」—— 点名 3 个视频的子 agent 会在看完最后一个
#   那步被掐断,钱花了、结论没有。公式 min(CAP, max(MAX_STEPS, len(video_ids)+2));
#   没点名 video_ids 时恒等于 MAX_STEPS(与动态化之前逐字节一致)。CAP = 硬顶(更多步 = 更多钱)。
SUBAGENT_MAX_STEPS_CAP = int(os.environ.get("SUBAGENT_MAX_STEPS_CAP", "8"))
SUBAGENT_MODEL      = os.environ.get("SUBAGENT_MODEL", LOOP_MODEL)   # 默认同主脑;可单独覆盖(如子 agent 用更强/更省档,见 SA-0 spike)
# ── P0-6 长程引擎:裸 depth-2(实验对象,默认关;依赖 USE_SUBAGENTS=1)──────────
# 只做深度穿透,不带 DAG/蒸馏/分层(红队 C2:实验测单变量)。关 = 全部路径与现状逐字节一致。
USE_DEPTH2          = os.environ.get("USE_DEPTH2", "0").lower() in ("1", "true", "yes")
# ── 线2 任务底座(S-1 起;默认全关,行为与升级前等价)────────────────────────
USE_TASKS           = os.environ.get("USE_TASKS", "0").lower() in ("1", "true", "yes")
RL_TASK_DAILY_COST_USD = float(os.environ.get("RL_TASK_DAILY_COST_USD", "2.0"))  # 任务自己的日顶(独立于对话 $2 日顶)
TASK_MAX_CAP_USD    = float(os.environ.get("TASK_MAX_CAP_USD", "2.0"))           # 单任务 cap 硬顶(resume 提额也不越)
TASK_DEFAULT_CAP_USD = float(os.environ.get("TASK_DEFAULT_CAP_USD", "0.5"))      # 立项不填 cap 时的默认
# 收尾额度:已花钱买到的战果必须能变成交付物 —— 收口波(一次 LLM 调用)在 cap 之上额外
# 允许这一点点,否则花满预算的任务会因差几分钱的收尾费永远出不了报告(review 实测的死锁)。
TASK_FINALIZE_GRACE_USD = float(os.environ.get("TASK_FINALIZE_GRACE_USD", "0.10"))
USE_TASK_TOOL       = os.environ.get("USE_TASK_TOOL", "0").lower() in ("1", "true", "yes")  # S-6 主脑立项工具位(独立开关)
TASKS_DRIVER        = os.environ.get("TASKS_DRIVER", "inline")                   # inline|cloudtasks(同一代码路径)
TASKS_QUEUE         = os.environ.get("TASKS_QUEUE", "agent-tasks")
TASKS_REGION        = os.environ.get("TASKS_REGION", "us-central1")
TASKS_ADVANCE_URL   = os.environ.get("TASKS_ADVANCE_URL", "")                    # advance 完整 URL(OIDC audience 同值)
TASKS_INVOKER_SA    = os.environ.get("TASKS_INVOKER_SA", "")                     # Cloud Tasks 注入 OIDC 的 SA
SUBAGENT_L2_FANOUT  = int(os.environ.get("SUBAGENT_L2_FANOUT", "3"))    # depth-1 再拆时的扇出顶
MAX_TREE_NODES      = int(os.environ.get("MAX_TREE_NODES", "13"))       # 全树节点硬顶(防 6×6 乘法)
# M5 记忆:loop 路径 transcript 回放 + 压缩(决策④)
# CC 式「全量注入 + 临窗压缩」:默认把整段回放原文喂 loop,只在【逼近 context window】时才压缩。
# 预算跟 LOOP_MODEL 的窗口挂钩(flash=1M),留头寸(FRACTION)给 system+schema+tools+本轮步骤+输出,
# 故压在 ~0.6 而非填满 → 回放高水位 ≈ 600k(旧值 3000 等于只用窗口 0.3%,过早摘要丢精度)。
LOOP_KEEP_TURNS              = int(os.environ.get("LOOP_KEEP_TURNS", "4"))            # 压缩时保最近 N 轮原文
LOOP_CONTEXT_WINDOW          = int(os.environ.get("LOOP_CONTEXT_WINDOW", "1000000"))  # LOOP_MODEL 的 context window
LOOP_CONTEXT_BUDGET_FRACTION = float(os.environ.get("LOOP_CONTEXT_BUDGET_FRACTION", "0.6"))
LOOP_CONTEXT_TOKEN_BUDGET    = int(os.environ.get(                                   # 回放压缩高水位(默认 = 窗口×FRACTION)
    "LOOP_CONTEXT_TOKEN_BUDGET", str(int(LOOP_CONTEXT_WINDOW * LOOP_CONTEXT_BUDGET_FRACTION))))

# 方向一:单请求最多【现场分析】的视频数(配额护栏;成本闸)。
# M4.4:并行(MAX_ANALYZE_PARALLEL)落地后从 5 提到 12 —— 直接覆盖设计的动机场景(「比 12 个翼装视频」),
# 大脑仍被引导「先 sql_query 缩到最相关的几个」,12 只是上限不是常态。要回退设环境变量即可。
MAX_VIDEOS_PER_REQUEST    = int(os.environ.get("MAX_VIDEOS_PER_REQUEST", "12"))

# M4.1:analyze_video 内容缓存(视频离线投递后静态 → 重复不重看,省一次 Gemini 多模态调用)。
#   memory = 进程内 LRU(默认,零基建、跨副本不共享、重启清空);off = 关闭(一键退回)。
#   后续 M4 可叠加 redis 跨副本共享(见设计 §4.3 / 开放问题)。键含【实际生效模型】→ Pro/Flash 不串味。
ANALYZE_CACHE_BACKEND = os.environ.get("ANALYZE_CACHE_BACKEND", "memory").lower()  # memory | redis | off
ANALYZE_CACHE_MAX     = int(os.environ.get("ANALYZE_CACHE_MAX", "512"))            # 进程内 L1 LRU 上限条数
# redis 后端:L1 进程内 LRU 之上加一层 L2 共享 Redis(跨 Cloud Run 副本命中)。TTL 秒;视频静态 → 默认 7 天。
ANALYZE_CACHE_TTL_SECONDS = int(os.environ.get("ANALYZE_CACHE_TTL_SECONDS", str(7 * 24 * 3600)))

# M4.3:同一步内多个 analyze_video 调用的并发上限(I/O 密集 = 等 Gemini 多模态)。
#   =1 → 退回纯串行(秒级回退开关);起步 3,压测看 Gemini 429/限流再调。
MAX_ANALYZE_PARALLEL = int(os.environ.get("MAX_ANALYZE_PARALLEL", "3"))

# ── P0-2:滥用/账单护栏(限流,见 pipeline/agentops/ratelimit.py)──────────────────
# 按【成本 $】+【请求速率】双口径,纵深四维(IP/用户/会话/全局)。默认值给得宽松:正常用户
# 打不到,失控/白嫖才会撞墙 —— 起步值,按实际用量与 MONITORING 里的 cost 分布再调紧。
# 无 Redis(本地/测试)时限流本就 no-op;真正的硬底线是 provider spend cap(见 docs/billing-guardrails.md)。
USE_RATE_LIMIT           = os.environ.get("USE_RATE_LIMIT", "1").lower() in ("1", "true", "yes")
RL_REQ_PER_MIN           = int(os.environ.get("RL_REQ_PER_MIN", "30"))            # 具名用户每分钟请求数
RL_REQ_PER_MIN_GUEST     = int(os.environ.get("RL_REQ_PER_MIN_GUEST", "8"))       # 匿名/guest 每分钟(更紧)
RL_IP_REQ_PER_MIN        = int(os.environ.get("RL_IP_REQ_PER_MIN", "40"))         # 每 IP 每分钟(仅小额度档查;同 IP 可能多设备)
RL_DAILY_COST_USD        = float(os.environ.get("RL_DAILY_COST_USD", "2.0"))      # 具名用户每日成本顶 $
RL_DAILY_COST_USD_GUEST  = float(os.environ.get("RL_DAILY_COST_USD_GUEST", "0.20"))  # 匿名/guest 每日 $
RL_SESSION_COST_USD      = float(os.environ.get("RL_SESSION_COST_USD", "0.75"))   # 单会话累计成本顶 $
RL_GLOBAL_DAILY_COST_USD = float(os.environ.get("RL_GLOBAL_DAILY_COST_USD", "15.0"))  # 全站每日成本熔断 $
QUERY_MAX_CHARS          = int(os.environ.get("QUERY_MAX_CHARS", "8000"))         # 单条问题字符上限(挡超大 query 灌 token)

# ── AlloyDB ───────────────────────────────────
ALLOYDB_HOST     = os.environ.get("ALLOYDB_HOST", "localhost")
ALLOYDB_PORT     = int(os.environ.get("ALLOYDB_PORT", "5432"))
ALLOYDB_DB       = os.environ.get("ALLOYDB_DB", "your_database")
ALLOYDB_USER     = os.environ.get("ALLOYDB_USER", "postgres")
ALLOYDB_PASSWORD = os.environ.get("ALLOYDB_PASSWORD", "")

# ── B1 有界读取:SQL 结果的行/字节/时间上界 ────────────────────────
# 现状(改之前)是 `cur.execute(sql)` + 裸 `fetchall()`:没有超时、没有行数上界、
# 没有字节上界。一条 `SELECT * FROM video_fact_instances` 就能把 MCP 子进程的内存
# 和大脑的 context 一起顶穿,而且没人知道发生过。
#
# 【这几个常量是安全项,恒生效,不受 USE_BOUNDED_SQL 控制】——
# 见 §12「不允许普通 flag 关掉(回滚只回滚展示,不回滚安全)」。
# 【截断这件事本身也恒报,同样不受开关控制】(§12 规则 3:正确性字段永不受开关控制)——
# 上界既然恒生效,关掉开关并不会让行回来,只会让上游【不知道行被扔了】,
# 那比不加上界更隐蔽,等于把安全项做成了静默丢数据。
SQL_MAX_ROWS   = int(os.environ.get("SQL_MAX_ROWS", "2000"))              # 最多保存 2000 行;第 2001 行只用于确认截断
SQL_MAX_BYTES  = int(os.environ.get("SQL_MAX_BYTES", str(1024 * 1024)))   # 最终 JSON 的 UTF-8 字节上界(1 MiB)
SQL_FETCH_BATCH = int(os.environ.get("SQL_FETCH_BATCH", "128"))           # fetchmany 批大小
SQL_STATEMENT_TIMEOUT_MS = int(os.environ.get("SQL_STATEMENT_TIMEOUT_MS", "10000"))  # SET LOCAL statement_timeout
SQL_LOCK_TIMEOUT_MS      = int(os.environ.get("SQL_LOCK_TIMEOUT_MS", "2000"))        # SET LOCAL lock_timeout

# USE_BOUNDED_SQL —— 只管一件纯展示的事:【零行时报不报列名】。
# 上界恒生效、截断恒报,两者都不受它控制(见上)。所以:
#   关(默认)+ 没截断 → wire 与升级前逐字节等价(裸 JSON 数组);
#   关(默认)+ 截断了 → wire 仍是信封 —— 别指望把它翻回 0 来"恢复旧 wire"。
# 验证后生产强制开(§12 规则 2 的启动校验尚未落地,见交付报告)。
USE_BOUNDED_SQL = os.environ.get("USE_BOUNDED_SQL", "0").lower() in ("1", "true", "yes")

# B2 客户端超时 —— 【顺序依赖:必须先有上面的 statement_timeout,再收紧这里】。
# 反过来做会造出"客户端已经放弃、服务端 SQL 还在跑"的悬挂查询:连接不归还、
# 锁不释放,而且上游拿到超时后会去重试 → 一条慢查询变成 N 条并发慢查询。
# 因此下界用 max() 焊死在 statement_timeout + 5s:哪怕有人把 env 设成 3,
# 也不会出现"客户端比服务端先放弃"。5s 是留给 stdio 往返 + JSON 序列化的余量。
MCP_CALL_TIMEOUT_S = max(
    float(os.environ.get("MCP_CALL_TIMEOUT_S", "15")),
    SQL_STATEMENT_TIMEOUT_MS / 1000.0 + 5.0,
)

# 业务表白名单 —— get_schema 只暴露这些表
BUSINESS_TABLES = [
    "video_metadata",
    "video_discovery",
    "video_facts",
    "video_fact_instances",
    "skydive_segments",          # 跳伞专栏:受控阶段元数据(每视频一行,阶段列可为 NULL)
]

# ── Sandbox (Stage 5) ─────────────────────────
SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://localhost:8080")

# ── 运行模式开关 ──────────────────────────────
# REPL_USE_MOCK_DB=1  → 用内存 SQLite mock,零成本、不需要 AlloyDB
USE_MOCK_DB = os.environ.get("REPL_USE_MOCK_DB", "").lower() in ("1", "true", "yes")

# ── 会话持久化(多轮记忆)──────────────────────
# 独立 SQLite 文件:与 MCP 查的库【物理隔离】,planner 生成的 SQL 够不着 → 免疫"潘多拉"。
# 设 SESSION_DB_PATH="" 关闭持久化(纯内存,测试/CI 用)。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_DB_PATH = os.environ.get(
    "SESSION_DB_PATH", os.path.join(_REPO_ROOT, ".session_store.sqlite"))
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(24 * 3600)))  # 闲置超此秒数的会话懒清理

# 跨轮 artifact【值】仓的 TTL(秒);默认随会话 TTL。Redis 值仓用 SET ... EX,到期自动删(无需定时任务)。
# 想"只保留三天" → 设 SESSION_TTL_SECONDS=259200(连带值仓),或单独设 ARTIFACT_VALUE_TTL_SECONDS。
ARTIFACT_VALUE_TTL_SECONDS = int(os.environ.get("ARTIFACT_VALUE_TTL_SECONDS", str(SESSION_TTL_SECONDS)))

# 会话后端:sqlite(默认,本机单节点)| redis(共享外部存储,多实例/Cloud Run 跨副本续聊)。
# 选 redis 仍守"潘多拉"隔离 —— 会话存在独立服务,planner 的 SQL(MCP 查 Neon)够不着。
# redis 后端的连接二选一(工厂里 TCP 优先):
#   · REDIS_URL —— TCP RESP 协议(redis-py),如 rediss://default:<pwd>@<host>:6379
#   · UPSTASH_REDIS_REST_URL + _TOKEN —— Upstash 的 HTTP REST(upstash-redis),Cloud Run 同样可用
SESSION_BACKEND = os.environ.get("SESSION_BACKEND", "sqlite").lower()
REDIS_URL = os.environ.get("REDIS_URL", "")
UPSTASH_REDIS_REST_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")


# ── A7+ 启动校验:成本护栏别被配置配死 ──────────────────────────
def _validate_tree_guard_budget(cost_cap: float, analyze_est: float, parallel: int) -> str:
    """校验 per-tree 熔断线容不容得下 analyze。返回告警文本(空串 = 没问题);致命配置直接 raise。

    为什么用 raise 而不是 assert:这是【安全约束】,而 `python -O` 会把 assert 整条优化掉 ——
    偏偏生产更可能带 -O 跑,等于"最需要这条检查的场景恰好没有这条检查"。

    分两档(见 MAX_TREE_COST_USD 上方注释):
      · cap < 单次预留        → pro 档 analyze 【一次都进不来】,工具等于不存在 → raise,拒绝启动;
      · cap < 单次预留 × 并行 → 满并行的一步会顶闸,而 admit 一旦拦下就 _trip 整棵树 → 告警。
    cap<=0 是"熔断关闭",不受本约束管(不设闸 ≠ 把闸设死)。
    """
    if cost_cap <= 0 or analyze_est <= 0:
        return ""
    if cost_cap < analyze_est:
        raise ValueError(
            f"MAX_TREE_COST_USD={cost_cap:g} 小于单次 analyze 预留 "
            f"TREE_ANALYZE_ESTIMATE_USD={analyze_est:g} —— pro 档 analyze_video 会被【静默】"
            f"拦死(且第一次拦下就触闸,整棵树后续工具全被拦)。请把 MAX_TREE_COST_USD 提到 "
            f">= {analyze_est * max(1, parallel):g}(= 单次预留 × MAX_ANALYZE_PARALLEL)。"
            f"只跑 flash 档的部署,也可以把 TREE_ANALYZE_ESTIMATE_USD 调【小】到实际单次成本"
            f"(禁令只禁调大,调小到真实值是对的)。"
            f"【不要】用 MAX_TREE_COST_USD=0 绕过本报错:那是把请求内唯一的美元熔断整个关掉,"
            f"不是本报错的补救方案。")
    need = analyze_est * max(1, parallel)
    if cost_cap < need:
        return (f"MAX_TREE_COST_USD={cost_cap:g} < 单次预留 {analyze_est:g} × "
                f"MAX_ANALYZE_PARALLEL={parallel} = {need:g}:pro 档下一步内并行 analyze "
                f"会在实花接近 $0 时顶掉熔断线并触闸(整棵树后续工具全被拦)。"
                f"建议提到 >= {need:g}。")
    return ""


TREE_GUARD_CONFIG_WARNING = _validate_tree_guard_budget(
    MAX_TREE_COST_USD, TREE_ANALYZE_ESTIMATE_USD, MAX_ANALYZE_PARALLEL)
if TREE_GUARD_CONFIG_WARNING:
    logging.getLogger("pipeline.config").warning(TREE_GUARD_CONFIG_WARNING)


def alloydb_dsn() -> dict:
    """psycopg2.connect(**alloydb_dsn()) 用的连接参数。"""
    return {
        "host": ALLOYDB_HOST,
        "port": ALLOYDB_PORT,
        "dbname": ALLOYDB_DB,
        "user": ALLOYDB_USER,
        "password": ALLOYDB_PASSWORD,
        "sslmode": "require",
    }
