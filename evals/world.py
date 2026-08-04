"""评测世界：把"假大脑/假工具"或"真大脑+假后端"喂给真 run_loop。

两种跑法：
- ScriptedWorld：大脑和工具都写死（免费，验证评测机器本身）。
- LiveWorld / 多轮会话：真 Gemini 当大脑，后端全换成"评测假后端"（EvalBackend）——
  假数据库（进程内、每次跑重新灌种子）、假播放签名、记忆替身（不碰真 GCS）、
  "看画面"回放件（假片库没有真视频文件，analyze_video 拦下来按清单回答）。
  这样真跑不碰生产数据、结果可复现，上传/入库这类用户动作也真的能落进假库。
"""
from __future__ import annotations

import io
import json
import math
import os

from pipeline.loop_driver import Call, ExecResult, run_loop  # noqa: F401  (Call re-exported for policies)


def _fake_gemini_generate(gcs_uri: str, prompt: str = "", time_range=None) -> str:
    """站在【真 Gemini 那一次调用】的位置,返回事先写好的画面事实清单(原始 JSON 串)。

    它上面的所有生产逻辑照跑:配额闸、成本护栏的 admit/settle、缓存、重试循环、
    AnalyzeResult 解析与字段矫正、失败归因、NodeResult 形状、预览裁剪。
    这是 install() 里唯一替换掉的一环 —— 假的只有"模型看见了什么"。

    只拿得到 gcs_uri,所以 video_id 反查假库(upload() 会在跑的过程中插新行,
    所以【每次现查】而不是开场缓存一张表);查不到再退回按文件名猜。
    """
    import repl._mock_db as mock
    from evals.fixtures.analyze_answers import analyze_answer

    vid = ""
    try:
        safe = str(gcs_uri or "").replace("'", "''")
        rows = mock.mock_run_sql(
            f"SELECT video_id FROM video_metadata WHERE gcs_uri = '{safe}' LIMIT 1")
        vid = str((rows or [{}])[0].get("video_id") or "")
    except Exception:
        vid = ""
    if not vid:
        vid = str(gcs_uri or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]

    env = analyze_answer(vid, prompt or "")
    # evidence_ts 生产是【单个 float 或 null】(AnalyzeResult 的字段类型),回放件历来给的是
    # 空列表 —— 类型都不对。这里给 null:回放件本来就没有逐条时间戳,给 null 是如实说"不知道",
    # 编一个假秒数会让 T2 的跳播判分测到一个我们自己编的数。
    return json.dumps({"answer": env["answer"], "enough": env["enough"],
                       "confidence": env["confidence"], "evidence_ts": None},
                      ensure_ascii=False)


def _cosine(a, b) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


def build_cosine_search(index, weak_threshold: float = 0.6):
    """内存语义检索：query 向量 vs 索引里每条文档向量算 cosine，取 top-k。
    index = [(video_id, snippet, start, end, vector)]。返回值与真 semantic_index.search
    同格式,relevance 三档口径也与真实现对齐(D3:strong/borderline/weak)——
    评测世界的语义判定必须和生产同一套,否则调的是两个系统。"""
    from pipeline import semantic_index as _si

    def search(vec_lit, k, video_ids=None):
        # P0-5:参数契约与真 semantic_index.search 对齐 —— 否则三臂实验(EVAL_SEMANTIC=1
        # + USE_IN_VIDEO_SEARCH=1)里大脑一带 video_ids 就裸 TypeError,下钻臂调一次挂一次,
        # 实验测到的是坏工具不是下钻收益(review 实测确认)。
        try:
            qv = json.loads(vec_lit)
        except Exception:
            return []
        pool = index if not video_ids else [e for e in index if e[0] in set(video_ids)]
        scored = sorted(((_cosine(qv, e[4]), e) for e in pool), key=lambda x: -x[0])[:int(k)]
        return [{"n": i + 1, "video_id": e[0], "source": "eval", "snippet": e[1],
                 "start_ts": e[2], "end_ts": e[3], "score": round(sc, 3),
                 "relevance": ("strong" if sc >= _si.T_HI
                               else "borderline" if sc >= _si.T_LO else "weak"),
                 "label": (e[1] or "")[:40]}
                for i, (sc, e) in enumerate(scored)]
    return search


# ── 脚本车道（免费）──────────────────────────────────────────────────
class ScriptedConv:
    """按脚本依次返回 (calls, text)，忽略发来的 msg —— 就是把"大脑的决定"写死。"""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return self.script.pop(0)


def make_exec(values=None, fail=(), *, faults=None, spend_per_call: float = 0.0):
    """stub 工具执行器：按工具名返回固定结果（把"工具/数据库的输出"写死）。

    `fail=("sql_query",)` 只能让某个工具返回一个 `stderr="boom"` 的空壳 ——
    生产里不存在"boom"这种故障,所以它测不出任何真实韧性问题。

    批次 3.5 的新能力走【新的关键字参数】(既有签名与行为逐字节不变,全仓一堆调用点在用它):
      faults          = `evals.faults.FaultInjector`,把真实形状的故障(429/5xx/超时 /
                        SQLSTATE / 进程杀点)注到【工具执行接缝】上,并顺带记 Verdict 账;
      spend_per_call  = 每次工具调用真烧多少美元(经 usage.add_usage 落进真账),
                        供③"预算在特定时刻耗尽"用 —— 钱在调用【结束后】才落账,与生产同序。
    两个都不传 → 一个字节都不变。
    """
    seen = []

    def execute(cid, name, inputs, upstream, uses):
        seen.append({"cid": cid, "name": name, "inputs": inputs})
        if name in fail:
            return ExecResult(ok=False, stderr="boom")
        val = (values or {}).get(name, [{"v": 1}])
        return ExecResult(ok=True, value=val, preview=val[:1], n=len(val))

    execute.seen = seen
    if faults is None and not spend_per_call:
        return execute
    # spend_per_call 单独传也要生效:docstring 把它和 faults 列成两项【独立】新能力,
    # "只烧钱不注故障"(测预算耗尽)是合法用法 —— 以前 faults=None 时它被静默丢弃,
    # 那种用例会得到一个永远不触闸、而且是绿的结果。
    from evals.faults import FaultInjector, wrap_exec
    return wrap_exec(execute, faults if faults is not None else FaultInjector(),
                     spend_per_call=spend_per_call)


class ScriptedWorld:
    """一道题的最小考场：脚本大脑 + 固定工具结果，跑一次真 run_loop。"""

    def __init__(self, script, tool_results=None, fail=()):
        self.script = script
        self.tool_results = tool_results or {}
        self.fail = fail

    def run(self, user_query, max_steps: int = 16):
        conv = ScriptedConv(self.script)
        execute = make_exec(values=self.tool_results, fail=self.fail)
        res = run_loop(user_query, conv, execute, max_steps=max_steps)
        if res.terminated != "text":      # 同 EvalBackend 那条:占位文案不是 agent 的回答
            res.answer = ""
        return res


# ── 评测假后端（真跑用）──────────────────────────────────────────────
class EvalBackend:
    """真跑时的"考场后勤"：把所有会碰真服务/真数据的口子换成假的，
    并让用户动作（上传/入库/记偏好）真的落进假世界，判分时能查证。

    world_state 是判分用的"账本"：uploads / enriched / memory 都如实记在这。
    """

    def __init__(self, owner: str = "eval", world: str = "A"):
        self.owner = owner
        self.world = world                    # GD-2:按题选考场(A=冻结的16视频 / B=新20视频)
        self.world_state: dict = {"uploads": [], "enriched": [], "memory": ""}

    # 打补丁：全部是"换掉模块里的函数"，进程内幂等，只影响评测进程
    def install(self):
        os.environ["REPL_USE_MOCK_DB"] = "1"
        # 考场 id 形状(v001/sky01/b001/c001/d001)与生产(v_长串/GX/纯数长串)不同,
        # 生产清洗器对它们空转 → 把假世界 id 纳入清洗模式(幂等),背带在两边同样勒紧
        from pipeline import answer_guard as _ag
        import re as _re
        if r"sky\d{2}" not in _ag.ID_PAT.pattern:
            _ag.ID_PAT = _re.compile(
                _ag.ID_PAT.pattern
                + r"|(?<![0-9A-Za-z_-])(?:[vbcd]\d{3}|sky\d{2})(?![0-9A-Za-z_-])")
        os.environ["MOCK_WORLD"] = self.world  # GD-2:重灌种子时按此切世界
        from pipeline import config, mcp_client, user_memory, video_url
        import repl._mock_db as mock

        mock._conn = None                     # 每次跑重新灌种子：上一场的上传不会串场；世界按 env 切换
        mcp_client.query_db = mock.mock_run_sql          # 数据库查询走进程内假库（不再开子进程）
        mcp_client.get_schema = mock.mock_fetch_schema
        video_url.sign_gcs_uri = lambda uri, **kw: (f"https://eval.local/{uri}" if uri else None)
        # 「看画面」的假件【下沉到感知层】,换掉真 Gemini 那一次调用,其余全走生产。
        # 以前是在 executor 外面包一层、见 analyze_video 就直接返回假结果 —— 那等于
        # 把 _do 到感知层之间的一整段生产逻辑全部跳过,评测因此少了四样东西:
        #   ① MAX_VIDEOS_PER_REQUEST 配额闸(实测:调 15 次 0 次被拦,计数器恒 0;生产第 13 次就拒)
        #   ② 结果形状 —— 生产是 {"video_id": vid, **dump},假件回的是 [env]:
        #      【没有 video_id】。一步并行看 5 个视频时,评测的大脑根本分不清哪条对应哪个视频。
        #   ③ ANALYZE_PREVIEW_CELL 那档预览裁剪(假件自带 preview,绕过了 _preview)
        #   ④ 子 agent 那条路 —— 它拿到的是 _make_executor 的【内层】闭包,外面包的那层它看不见,
        #      于是子 agent 的 analyze 会绕过假件去打真感知层(USE_SUBAGENTS=1 时)。
        # 换在这里,以上四样自动全对,而且 pipeline/ 一行都不用改:analyze_with_outcome 的
        # generate 默认 None → 【调用时】才取模块属性,这个注入点是它自己文档里写明留给离线用的。
        from perception import analyze_video_contextual as _avc
        _avc._gemini_generate = _fake_gemini_generate

        backend = self

        def _mem_update(owner, text, mode="append"):     # 记忆替身：写进账本，不碰真 GCS
            if mode == "rewrite":
                backend.world_state["memory"] = text
            else:
                backend.world_state["memory"] = (backend.world_state["memory"] + "\n" + text).strip()
            return backend.world_state["memory"]

        user_memory.update = _mem_update
        user_memory.load = lambda owner: backend.world_state["memory"]
        user_memory.render_section = lambda owner: backend.world_state["memory"]

        config.USE_USER_MEMORY = True         # 记偏好工具要对大脑可见（写入走上面的替身）
        # 语义检索：默认关（它连的是生产库）。要测你自己改的 semantic_search，用 --semantic 打开——
        # 会用你【真实的 embed 函数】把假片库(标题+活动+事实)嵌进内存索引，语义搜跑你的代码、吃假数据。
        if os.environ.get("EVAL_SEMANTIC") == "1" and self._install_semantic():
            config.USE_SEMANTIC_SEARCH = True
        else:
            config.USE_SEMANTIC_SEARCH = False
        return self

    def _install_semantic(self) -> bool:
        """给假片库建内存语义索引（真 embed），并把 semantic_index.search 换成内存 cosine 检索。
        成功返回 True；embed 失败（没凭证/离线）返回 False → 语义保持关闭。
        注意：这测的是你的【embed 模型 + 查询构造 + 排序阈值】，不测你那句 pgvector SQL 本身
        （那需要一个真 pgvector 库，不在这套假世界里）。"""
        try:
            from pipeline import embeddings, semantic_index
            import repl._mock_db as mock

            conn = mock._get_conn()
            docs = []   # (video_id, snippet, start, end)
            for v in mock.VIDEOS:
                vid, title, _gcs, dur = v[0], v[1], v[2], v[3]
                docs.append((vid, f"{title}. activities: {', '.join(v[4])}", 0.0, float(dur)))
            for r in conn.execute("SELECT video_id, predicate, start_ts, end_ts "
                                  "FROM video_facts WHERE matched=1").fetchall():
                docs.append((r[0], r[1], float(r[2] or 0), float(r[3] or 0)))
            vecs = embeddings.embed_texts([d[1] for d in docs], task_type="RETRIEVAL_DOCUMENT")
            if not vecs:
                return False
            index = [(docs[i][0], docs[i][1], docs[i][2], docs[i][3], vecs[i]) for i in range(len(docs))]
            semantic_index.search = build_cosine_search(index, semantic_index.WEAK_THRESHOLD)
            return True
        except Exception:
            return False

    # 「看画面」的假件已经下沉到 install() 里的 _fake_gemini_generate ——
    # 这里【刻意不再留 wrap_execute 那层包装】。它当初拦 analyze_video 直接返回假结果,
    # 副作用是把 _make_executor 挂在闭包上的 tree_guard / tree_nodes / analyze_quota
    # 一起弄丢了(函数属性不会跟着包装走),而 run_loop 的 C4 余额回灌正是
    # getattr(execute, "analyze_quota", None) —— 评测里恒为 None,整段"你还剩几个配额、
    # 这次请求花了多少钱"从来没进过 prompt。包装层没了,这个问题就不存在了,
    # 不需要再写一行"记得把属性拷过去"。谁想再包一层:请先读这段。

    # ── 用户动作：真的落进假世界 ──
    def upload(self, video_id: str, title: str = "", activities=None, duration: float = 30.0):
        """上传新视频 = 假库里真插一行（元数据+活动词+基础事实），agent 查库就能看见。"""
        import repl._mock_db as mock

        acts = list(activities or [])
        conn = mock._get_conn()
        conn.execute("INSERT OR REPLACE INTO video_metadata(video_id,title,gcs_uri,duration_sec) "
                     "VALUES (?,?,?,?)", (video_id, title, f"gs://eval/{video_id}.mp4", duration))
        conn.execute("INSERT OR REPLACE INTO video_discovery(video_id,all_activities) VALUES (?,?)",
                     (video_id, json.dumps(acts, ensure_ascii=False)))
        for a in acts:
            conn.execute("INSERT INTO video_facts(video_id,predicate,matched,confidence,rationale,"
                         "start_ts,end_ts) VALUES (?,?,1,0.9,?,0,?)",
                         (video_id, a, "用户上传时自带的活动标签", duration))
        conn.commit()
        self.world_state["uploads"].append(video_id)

    def enrich(self, video_id: str):
        """内容入库 = 记进账本（uploads 时事实已入假库，这里确认索引这一步发生了）。"""
        self.world_state["enriched"].append(video_id)


def make_note_image(text: str) -> tuple[bytes, str]:
    """造一张写着说明文字的小图（贴图动作用）。
    注意：这测的是"图有没有真送到大脑手里 + 大脑接没接住"，
    不是真实视觉识别（那需要真图片素材，属于以后的活）。"""
    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (480, 200), "white")
        d = ImageDraw.Draw(img)
        d.rectangle([4, 4, 475, 195], outline="black", width=3)
        d.text((20, 80), text, fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    except Exception:
        # 没装 PIL 就退回一张 1x1 白点（图照样送达，只是没内容）
        tiny = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
                b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02"
                b"\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
        return tiny, "image/png"


# ── 真跑（单轮）──────────────────────────────────────────────────────
def live_preflight():
    """检查能不能真跑。能跑返回 None；否则返回一段"缺什么、怎么配"的说明。"""
    from pipeline import config

    proj = os.environ.get("GCP_PROJECT") or getattr(config, "GCP_PROJECT", "")
    if not proj or proj == "your-gcp-project-id":
        return (
            "没配 GCP 凭证 —— 真跑要真 Gemini。请先设：\n"
            "  set GCP_PROJECT=<你的项目>\n"
            "  set GENAI_LOCATION=global\n"
            "  set GOOGLE_APPLICATION_CREDENTIALS=<service-account.json>   (或配好 gcloud ADC)\n"
            "  set REPL_USE_MOCK_DB=1                                      (评测跑假库，不碰生产数据)\n"
            "然后： python -m evals.runner --live --n 1     # 先每题 1 次冒烟（会花 token）"
        )
    return None


class LiveWorld:
    """真跑：真 Gemini 当大脑，其余全是评测假后端（见 EvalBackend）。"""

    def __init__(self, owner: str = "eval", world: str = "A"):
        self.backend = EvalBackend(owner, world=world).install()
        self.owner = owner

    def run(self, user_query, max_steps: int = 16):
        from pipeline import config, loop_driver, mcp_client
        from pipeline.agentops.trace import Trace
        from sandbox.client import SandboxClient

        schema = mcp_client.get_schema()
        # GD-0:runtime_facts 对齐生产 —— orchestrator 每请求都注入「运行时状态」(模型档/语言指令等,
        # orchestrator.py 的 runtime_facts_line 调用),eval 此前传 None → 评测的 prompt 比生产少一节,
        # 语言指令等段在 eval 里成了死代码。usage_cum 传 None(单题无会话累计),与生产新会话首轮一致。
        # 【生产传的每一个参都照传】,哪怕值就是默认值 —— 少传一个是看不见的漂移
        # (test_eval_prompt_parity 现在按"评测传的 ⊇ 生产传的"逐入口锁死)。
        # 单轮车道没有贴图 → has_image 恒 False;模型就是 config.LOOP_MODEL(与 make_conversation 同一个)。
        rt = loop_driver.runtime_facts_line(None, nl=user_query,
                                            has_image=False, model=config.LOOP_MODEL)
        # C1 连带(同一个保真问题又长了一次):生产每请求都注入库存快照,eval 不传就又比
        # 生产少一节 —— 上面 GD-0 那条注释记的正是同形漂移。这里走的是假库(本文件
        # 把 mcp_client.query_db 换成了 mock_run_sql),所以拿到的是假库的快照,正确。
        # 【别再手动对齐下一节了】:test_eval_prompt_parity 会在 _loop_system 新增
        # 任何注入段而这里没跟上时变红。
        # 【每一节都要传】。保真检查在 tests/evals/test_eval_prompt_parity.py:
        # _loop_system 新增任何注入段而这里没跟上,那条测试立刻红。
        # 少一节 = 评测在量一个和生产不一样的系统,这个坑已经长过两次
        # (GD-0 的 runtime_facts、C1 的 library_state),就在上面那条注释底下。
        from pipeline import library_state as _lib
        from pipeline import user_memory as _um
        lib = _lib.library_state_line()            # 与 orchestrator 同一入口,自带 TTL + fail-open
        mem = _um.render_section(self.owner)       # 假世界装了记忆替身,读的是 world_state
        notice, _ = loop_driver.task_done_notice(self.owner)   # USE_TASKS=0 时短路成 ""
        conv = loop_driver.make_conversation(
            config.LOOP_MODEL,
            loop_driver.loop_function_declarations(),
            loop_driver._loop_system(schema, None, rt, notice,
                                     library_state=lib, user_memory=mem),
        )
        # 装配照抄生产 run_query_loop 那一段(loop_driver.py 里 guard/execute/run_loop 三行):
        # 一次请求 = 一棵树 = 一本账,【同一个】guard 同时喂给工具闸(挂点①)和每步大脑闸(挂点②)。
        # 以前这里不传 guard= → 挂点② 在评测里是关的,而 gate 跑机把 MAX_TREE_COST_USD 设成 0.80,
        # 闸是开着的:触闸之后评测不会干净收口、答案里也没有成本披露,分数被自己压低。
        # critic 同理照传:默认 USE_SELF_CHECK_CRITIC=0 时它是 None(今天的数字一个都不变),
        # 但不传的话"开自检 vs 不开自检"的 A/B 两臂会跑出逐字节相同的结果 —— 花真钱得零信息。
        from pipeline.agentops.treeguard import TreeGuard
        tr = Trace()
        guard = TreeGuard(trace=tr)
        execute = loop_driver._make_executor(SandboxClient(), tr, schema, None,
                                             owner=self.owner, guard=guard)
        critic = loop_driver.make_self_check_critic() if config.USE_SELF_CHECK_CRITIC else None
        # req_short:生产每请求发一个短前缀拼进 result_id(A2)。单轮车道一题一请求、
        # 不会跨请求合并台账,所以给个常量就够 —— 照传是为了让 result_id 的【形状】
        # 和生产一致(大脑看到的是 r_q_c0_0 而不是裸 c0_0),也让上面那条 ⊇ 规则不用开例外。
        res = run_loop(user_query, conv, execute, max_steps=max_steps,
                       guard=guard, critic=critic, req_short="q")
        # A1 连带(保真):terminated != "text" 时 run_loop 交的是【系统占位文案】,不是 agent
        # 的回答 —— 步数耗尽的那段兜底话术里带"没能",正好命中 scorers._NEG_WORDS,于是
        # expect_refusal / expect_honest_disclaimer 这类题会【白拿 1.0】(实测 42 道 0→1),
        # 而抬得最狠的正是"该说没有"那一类,尺子被我们自己的兜底文案骗过去了。
        # 置空 = 恢复 A1 之前的判分口径,历史基线仍可比。不靠改文案措辞躲:把"没能"换成
        # "未能"只会让 expect_positive 那一支从 0.0 翻成 1.0,更糟。
        if res.terminated != "text":
            res.answer = ""
        # 保真:生产在 run_loop 外层还有一道终清洗(loop_driver 收口处 scrub_ids),用户看到的
        # 是清洗后的答案;评测此前直连 run_loop 绕过了它 → 尺子在看用户看不到的裸文本。
        # (selfknow-links 实录:模型手滑列裸 id,生产会被兜住、考场却记 0 —— 考的不是同一个系统)
        from pipeline.answer_guard import scrub_ids
        if res.answer:
            res.answer, _ = scrub_ids(res.answer, (er.value for er in res.ledger.values()))
        return res
