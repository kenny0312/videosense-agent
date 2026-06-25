# v1.4 · 会话存储可插拔 + Redis(跨实例共享)
日期:2026-06-25 ｜ 类型:feature

主题:把会话记忆从【本地单节点 SQLite】抽成【可换后端】,新增 Redis 后端,让多副本 / Cloud Run **跨实例共享多轮会话**。默认仍 SQLite,本地零改动。

## 更新内容

### 可插拔后端
- 抽出 `BaseSessionStore` 接口(`get_or_create / save / reset`)。持久化与 pipeline 解耦:请求开头读、结尾写,中间 router/planner/orchestrator **一行不改**。
- `SessionStore`(SQLite + 进程内 L0 缓存)保持原样;新增 `RedisSessionStore`。
- 工厂 `_make_store()` 按 `SESSION_BACKEND` 选;默认 `sqlite`。

### Redis 后端
- 形状与 SQLite 版一致:`vs:session:<id> → JSON blob`(一次 GET / 一次 SET)。
- 连接二选一(TCP 优先):`REDIS_URL`(redis-py)或 `UPSTASH_REDIS_REST_URL`/`_TOKEN`(upstash-redis REST,Cloud Run 同样可用)。两种客户端 get/set(ex=)/delete 等价,本类与具体库解耦。
- **刻意不留 L0 缓存**:Redis 为唯一真相源,每请求重读 → 副本间不会读到脏缓存。
- **TTL 交给 Redis**(`SET ... EX`),省掉 SQLite 版的懒清理。
- 读写异常一律 **fail-open**(退化为新会话 / 跳过写),不让记忆层拖垮主请求。
- 仍守"潘多拉"隔离:会话在独立服务,planner 的 SQL(MCP 查 Neon)够不着。

### 并发加固(对抗审查后修复)
- 端点是 sync `def`,FastAPI 放线程池并发;请求是 read→mutate→write 非原子序列。移除 L0 缓存后,**同会话并发请求即便在单副本也会"后写覆盖"丢整轮**。
- API 层加**每会话锁**(`WeakValueDictionary`,锁自动回收)串行化这段 → 单副本安全。跨副本仍 LWW:部署建议开 **session affinity**;要严格再上 WATCH/MULTI 或 append-only。

## 影响 / 注意
- API 契约不变。默认后端仍 `sqlite`,本地开发零改动。
- 新增本地文件 `neon.env` 字段:`SESSION_BACKEND` / `REDIS_URL` / `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`(gitignored)。
- 依赖:运行期 `redis` + `upstash-redis`(按连接方式二选一即可);测试期 `fakeredis`(`requirements-dev.txt`)。
- 测试:离线 **session 19/19 + redis 19/19**(`fakeredis`,缺失则优雅 SKIP)。真 Upstash 验证:**跨副本续聊**(两进程共享一库,replica B 复述 replica A 的上一轮 SQL)+ **并发同会话不丢轮**。
- ⚠️ 已知项(超本次范围,见 follow-up):client 传入的 `session_id` 无归属校验(IDOR),被共享 Redis 放大 —— 接入鉴权层后应绑定到认证身份。

## 下一个更新方向
- 部署:Cloud Run 上 `SESSION_BACKEND=redis` + session affinity;把会话 IDOR 绑定到鉴权身份。
- 跨副本严格原子(可选):WATCH/MULTI(TCP)或 history/catalog 改 append-only(RPUSH/INCR)。
