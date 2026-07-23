"""DVD 复现的唯一旋钮点(docs/dvd-repro-plan.md §4.1)。改参数只改这里,不改代码。"""
import os

# ── 论文忠实参数(Sec 3.1/3.2/3.3)──
CLIP_SECONDS = 5          # 均匀切段长度
FPS = 2                   # 每段抽帧帧率(≈10 帧/段)
FRAME_HEIGHT = 720        # 抽帧短边
MAX_INSPECT_FRAMES = 50   # Frame Inspect 上限,超限均匀下采样
TOP_K = 16                # Clip Search 默认 top-k(declaration 里开放给模型改)
MAX_STEPS = 15            # agent 循环步数上限(超限强制作答)
KEEP_FRAMES = True        # 首轮忠实论文留帧;False = Frame Inspect 只走方案B
GLOBAL_BROWSE_FRAMES = 32 # event-centric 现算摘要的全片均匀采样帧数(论文未给,可调)

# ── 模型(消融纪律:省 captioner,别省 orchestrator)──
CAPTION_MODEL  = os.environ.get("DVD_CAPTION_MODEL", "gemini-2.5-flash")
ORCH_MODEL     = os.environ.get("DVD_ORCH_MODEL", "gemini-3.5-flash")   # 起步档
ORCH_MODEL_ALT = os.environ.get("DVD_ORCH_MODEL_ALT", "gemini-2.5-pro") # 对照档
VQA_MODEL      = os.environ.get("DVD_VQA_MODEL", "gemini-2.5-flash")    # Frame Inspect 内部

# ── 路径(产物全部圈在 dvd_repro/ 内,gitignore)──
ROOT        = os.path.dirname(os.path.abspath(__file__))
DB_DIR      = os.path.join(ROOT, "db")
VIDEOS_DIR  = os.path.join(ROOT, "videos")
LOGS_DIR    = os.path.join(ROOT, "logs")
RESULTS_DIR = os.path.join(ROOT, "results")

# ── 费用闸门(用户制度 2026-07-18:触闸=保进度→写 PAUSED→停下等审查)──
GUARD_SINGLE_CALL_USD = float(os.environ.get("DVD_GUARD_SINGLE", "0.50"))  # 单次调用异常闸
GUARD_RUN_USD         = float(os.environ.get("DVD_GUARD_RUN", "5.00"))     # 单场运行闸($5)
GUARD_TOTAL_USD       = float(os.environ.get("DVD_GUARD_TOTAL", "45.00"))  # 项目总闸

# ── 隔离契约(README 第一节;cleanup.py 按此撤销)──
INGEST_SOURCE_TAG = "lvbench-dvd"   # 入 VS 库的行一律打此 source 标签
GCS_PREFIX        = "lvbench/"      # GCS 上独立前缀 gs://<bucket>/lvbench/
