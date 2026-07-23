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

## 选片关 · 3 条 LVBench 下载/入库/富化 — 2026-07-18
- [x] 3 条视频齐:魔笛手动画 30.4min / 大卫·布莱恩今夜秀 32.9min / 伦敦奥运高低杠决赛 33.3min
      (+自库 GX010533/GX010534 两条短片对照,共 5 条)
- [x] GCS: gs://activitynet/lvbench/ 3 个对象;DB: 3 行全部 source='lvbench-dvd'
- [x] 基线B索引:每条 caption 1 + video 1 + transcript 123/200/200 行(共新增 529 行)
- [x] **污染对账全 ✅**:四表存量 = 基线(511/50/2218/4957)一行未动
- [x] 官方 52 题已存 questions/lvbench_official.jsonl
- [x] 闸门总账:$0.86(含全部失败重试),单次最高 $0.04,三闸零触发
- 基建变更(迁移计划内):activitynet 桶授 videosense2 Vertex 服务代理 objectViewer
  (撤销:gcloud storage buckets remove-iam-policy-binding 同参数)
- ⚠ 已知限制(如实入册):transcript 每视频截断 200 段(pipeline MAX_SEGMENTS,方案§8
  已知短板)——布莱恩/体操原始段 1097/308,索引只收前 200 → 基线B 对长视频尾部欠索引,
  这正是"VS 现状"的诚实呈现,对照实验特性而非缺陷
- 排障弹道(9 跑):AV1转码→Neon掐闲置连接→8192截断→分块→字符串控制符→约束解码→
  思考token吃输出预算(关thinking)→复读机拆段钻空子→prompt并段+括号扫描打捞+劈窗递归
- [ ] 用户验收签字: ____
