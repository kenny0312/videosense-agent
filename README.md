# Vibe Coding for Video Understanding

10阶段端到端 AI 视频理解系统。

## 技术栈
- **GCS** — 视频存储 (`gs://activitynet`)
- **AlloyDB** — 结构化事实存储
- **Gemini 2.5** — 多模态视频理解
- **MCP Python SDK** — 标准化数据库访问协议
- **FastAPI** — API 交付（第10阶段）

## 进度

| 阶段 | 内容 | 状态 |
|------|------|------|
| 第1阶段 | GCP Foundation — 数据下载/转码/上传 | ✅ 完成 |
| 第2阶段 | Gemini Predicates — 视频理解写入 DB | ✅ 完成 |
| 第3阶段 | MCP Server — get_schema / query_db | ✅ 完成 |
| 第4阶段 | Planner — 自然语言→DAG执行 | ✅ 完成 |
| 第5阶段 | Sandbox Engine — 安全代码执行 | 🔜 进行中 |
| 第6阶段 | Agentic REPL — AI自愈调试闭环 | ⏳ 待开始 |
| 第7阶段 | Data Engineering — 跨模态ETL | ⏳ 待开始 |
| 第8阶段 | Temporal Alignment — 时序插值 | ⏳ 待开始 |
| 第9阶段 | Dynamic Simulation — 高阶统计 | ⏳ 待开始 |
| 第10阶段 | Orchestration — 全栈API交付 | ⏳ 待开始 |

## 项目结构
```
scripts/
├── stage1_upload_metadata.py   # 视频下载→转码→GCS→AlloyDB
├── stage2_gemini_predicates.py # Gemini 视频分析
├── stage3_mcp_server.py        # MCP stdio Server
└── stage4_planner.py           # 自然语言 DAG 规划器
```

## GCP 配置
- Project: `your-gcp-project-id`
- Region: `us-central1`
- AlloyDB: `your-db-host` / `your_database`
