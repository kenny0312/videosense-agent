# 验收台账(每 Stage 过关证据,用户拍板后进下一关)

## Stage 0 · 环境与安全地基 — 2026-07-18
- [x] 费用闸门 costguard.py:三级闸(单次$0.50/单场$5/总$45),离线单测 **5/5 过**
      (单次闸/累计闸/跨场总闸/暂停单拦截重启+令牌续跑/正常流不误伤)
- [x] 撤销器 cleanup.py 干跑演习通过:DB 打标行=0,GCS lvbench/ 前缀=0 对象,本地=空
      (同时证明 DB/GCS 连通)
- [x] ffmpeg 8.1.1 / ffprobe 8.1.1 在位
- [x] numpy 2.1.3 已有;yt-dlp 2026.03.17 已装;不装 opencv(抽帧走 ffmpeg)
- [x] **污染基线**:video_metadata=511, video_discovery=50, video_facts=2218,
      content_embeddings=4957 —— 此后每关核对存量不变
- [ ] 用户验收签字: ____
