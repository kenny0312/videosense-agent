# M2 Spike 结论:Gemini 原生 function-calling 主循环

> 跑 `spikes/loop_spike.py`(桩工具,隔离纯机制,无 DB/沙箱)。**10 次 live 运行**:flash + pro × {merge×3, plot×2}。

## 验证项与结论

### 机制可行 ✅
M1 的 `build_function_declarations()` OpenAPI schema **直接喂** vertexai `FunctionDeclaration` 即可;模型稳定返回 `function_call`,`function_response` 喂回后正常迭代到「纯文本即收敛」。`model.start_chat()` 自动管 Content/role,驱动器很薄。

### 开放问题 ② 多输入句柄 —— 解决,可靠 ✅
2-input `merge_asof` 的 `left_result_id` / `right_result_id` 句柄:**10/10 全部填对**(视频侧→left,传感器侧→right),flash 和 pro 都 100%。单句柄(plot 的 `data_result_id`)也 100%。
→ **句柄约定成立**:命名参数 + 结果回带 `result_id` + 描述里说清左右语义,模型就能可靠映射。M3 按此实现。

### 开放问题 ③ 模型选型 —— 倾向 flash ✅(真实场景待复核)

| 模型 | 收敛 | 句柄正确 | merge 延迟 / tok | plot 延迟 / tok |
|---|---|---|---|---|
| **flash** (CRITIC) | 100% | **100%** | 8.0s / 4949 | 5.6s / 2628 |
| **pro** (PLANNER) | 100% | **100%** | 12.1s / 4653 | 10.9s / 3452 |

质量无差,flash 快 ~33–50%、token 相当 → **loop 大脑用 `CRITIC_MODEL`(flash)即可**,`pro` 留给沙箱 codegen。
⚠️ 桩场景语义清晰;真实库 / 歧义 schema 上需在 **M3 / M7** 用真实查询复核。

### 步数 = 预期,无浪费
merge 恰 **3 步**(2 查询 + 合并),plot 恰 **2 步**(查询 + 出图),无多余调用、无自纠错触发(场景干净)。

## 附带发现(影响 M3)
- `vertexai.generative_models` SDK **已弃用**(deprecated 2025-06-24,2026-06-24 移除),后继是 **`google-genai`**(Gen AI SDK)。全仓现都用前者。
  → **M3 写新 loop 时直接上 `google-genai`**,顺带把 router/planner/codegen 的旧调用逐步迁移。
