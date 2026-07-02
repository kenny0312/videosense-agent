# 设计:前端 UI Refresh —— 精致化 + 英文化 + 去掉"每轮宣告"

> 状态:Design(mockup 已给 owner 过目) · 范围:`web/index.html`(单文件,626 行) · 原则:信息密度不降、操作路径不变,只动观感与措辞;英文为主的 UI chrome,回答内容语言不受影响。

## 1. 诊断(现状)

功能完备但工程味重:
1. **每轮宣告元信息**:`follow-up · 复用了上文` 徽章 + `trace: 2/2 steps ok, total 10542ms · 12302ms` 长串常驻每条回答头部 —— 把"正常状态"当新闻播报,视觉噪音大(owner 原话:小 low)。
2. **UI 文案中英混杂**:新对话/历史对话/无法回答/上传/trace(N 步)…
3. **组件糙**:按钮无过渡动效、发送键方块、徽章配色平、视频卡无 hover 状态、表格无斑马纹、空状态平淡。

## 2. 设计语言

- 保持暗色基调,但表面分层加深(bg #0b0d10 / card #12151a / hover #1a1f26),0.5px 边框;
- 圆角统一(card 14px / 控件 10px / pill 999px);间距 8px 网格;
- 动效:150-200ms ease(hover/按下 scale .98/消息淡入上移 8px);思考态用呼吸点(Thinking…),弃用 spinner;
- 图标:Tabler outline webfont(替代 Unicode 符号 ⬆/☰/▶);
- 数字一律 tabular-nums。

## 3. 轮次元信息重设计(核心痛点)

**原则:常态不宣告,异常才标记;细节按需展开。**
- 删除 `follow-up/new/meta` 徽章 —— 上下文复用是常态;`turn_type` 仍在响应里,只是不再渲染成徽章。
- trace 摘要行从头部移到卡片**底部安静页脚**:`> Steps 4 · sql ×1 · watch ×2 · 12.4s`(muted 色,11.5px),点击展开完整 timeline(工具图标 + 状态点 + 耗时);
- 仅保留异常徽章:`Declined`(refused);
- 页脚右侧薄 action:copy / retry 图标(hover 才亮)。

## 4. 英文化清单(chrome only,回答语言不动)

| 现状 | 改为 |
|---|---|
| ＋ 新对话 / 历史对话 | New chat / Recents |
| follow-up · 复用了上文 / meta · 方法回放 / 无法回答 | (删) / (删) / Declined |
| ⌄ trace (N 步) | Steps N(页脚) |
| ⬆ 上传 | Upload(图标+进度环) |
| 🎬 为你准备了 N 个视频 / 📋 已为你列出 N 条 | N videos ready / N rows |
| 暂时无法播放 | Preview unavailable |
| 输入占位/空状态示例 | "Ask about your videos…" + 英文示例 chips |
| Pro 开关 | Pro(sparkles 图标) |

## 5. 组件精致化

- **Composer**:悬浮卡片,focus 时 accent 描边;上传图标进输入框左侧(PUT 时圆形进度环);发送键 32px 圆角方块、箭头图标、hover 提亮、streaming 时变 Stop。
- **视频卡**:16:9 网格,左上 `#N · 时长` chip,中央 play 图标,hover 提亮 + 轻放大;不可播时蒙层 "Preview unavailable"。
- **表格**:sticky 表头、斑马纹、hover 行高亮、数字右对齐。
- **侧栏**:会话按日期分组(Today/Yesterday/Earlier),active 项左侧 accent 竖条,标题自动取首问。
- **头部**:会话标题(首问截断)居中位;成本环改为 `$0.04` 微型计数(hover tooltip 展开明细)。
- **空状态**:居中 hero + 3 个英文示例 chips。

## 6. 实施与验收

- 单文件改造,1 个 PR(纯前端,零后端改动,revert 即回滚);Tabler webfont 从 CDN 引入(~30KB woff2)。
- 验收:① 视觉走查(新旧截图对比);② 功能回归 —— 视频侧信道/表格/上传/Pro/成本环/SSE 流式全部照常;③ 无任何后端字段依赖变化(turn_type 仍返回,只是不渲染)。
- 后续候选(不在本 PR):消息 markdown 渲染增强、视频卡时间戳跳转 chips、亮色主题。
