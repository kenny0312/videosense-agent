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

    def search(vec_lit, k):
        try:
            qv = json.loads(vec_lit)
        except Exception:
            return []
        scored = sorted(((_cosine(qv, e[4]), e) for e in index), key=lambda x: -x[0])[:int(k)]
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


def make_exec(values=None, fail=()):
    """stub 工具执行器：按工具名返回固定结果（把"工具/数据库的输出"写死）。"""
    seen = []

    def execute(cid, name, inputs, upstream, uses):
        seen.append({"cid": cid, "name": name, "inputs": inputs})
        if name in fail:
            return ExecResult(ok=False, stderr="boom")
        val = (values or {}).get(name, [{"v": 1}])
        return ExecResult(ok=True, value=val, preview=val[:1], n=len(val))

    execute.seen = seen
    return execute


class ScriptedWorld:
    """一道题的最小考场：脚本大脑 + 固定工具结果，跑一次真 run_loop。"""

    def __init__(self, script, tool_results=None, fail=()):
        self.script = script
        self.tool_results = tool_results or {}
        self.fail = fail

    def run(self, user_query, max_steps: int = 16):
        conv = ScriptedConv(self.script)
        execute = make_exec(values=self.tool_results, fail=self.fail)
        return run_loop(user_query, conv, execute, max_steps=max_steps)


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

    # "看画面"回放件：假片库没有真视频，analyze_video 拦下来按事先写好的清单回答
    def wrap_execute(self, execute):
        from evals.fixtures.analyze_answers import analyze_answer

        def wrapped(cid, name, inputs, upstream, uses):
            if name == "analyze_video":
                env = analyze_answer(str(inputs.get("video_id", "")), str(inputs.get("question", "")))
                return ExecResult(ok=True, value=[env], preview=[env], n=1)
            return execute(cid, name, inputs, upstream, uses)

        return wrapped

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
        rt = loop_driver.runtime_facts_line(None, nl=user_query)
        conv = loop_driver.make_conversation(
            config.LOOP_MODEL,
            loop_driver.loop_function_declarations(),
            loop_driver._loop_system(schema, None, rt),
        )
        execute = loop_driver._make_executor(SandboxClient(), Trace(), schema, None, owner=self.owner)
        res = run_loop(user_query, conv, self.backend.wrap_execute(execute), max_steps=max_steps)
        # 保真:生产在 run_loop 外层还有一道终清洗(loop_driver 收口处 scrub_ids),用户看到的
        # 是清洗后的答案;评测此前直连 run_loop 绕过了它 → 尺子在看用户看不到的裸文本。
        # (selfknow-links 实录:模型手滑列裸 id,生产会被兜住、考场却记 0 —— 考的不是同一个系统)
        from pipeline.answer_guard import scrub_ids
        if res.answer:
            res.answer, _ = scrub_ids(res.answer, (er.value for er in res.ledger.values()))
        return res
