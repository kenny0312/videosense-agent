# 使用审计与看板(Usage Monitoring)

部署版每处理一个问题,就往 **Cloud Logging** 写一行结构化 JSON(`api/server.py` 的 `_audit`),
记录:**谁(app_user)/ 从哪(ip)/ 何时(ts)/ 问了什么(query)/ 用了多少 token(tokens_*、cost_usd)**
以及 status、turn_type、latency_ms。

- **只想随手查** → 直接用 Logs Explorer(下面"0. 即时查询")。零配置。
- **想要图表看板**(人均 token / 成本趋势)→ 把日志 sink 到 **BigQuery**,再用 **Looker Studio** 画图(本文 1–5 步)。

> 字段映射在 `pipeline/usage.py`(token 累加 + 估算单价 `_PRICE`)和 `api/server.py:_audit`(落日志)。
> 成本是**估算**(`usage_metadata × 单价`),绝对花费以 GCP 账单为准。

---

## 0. 即时查询(无需任何配置)

GCP Console → **Logging → Logs Explorer**,粘贴:

```
resource.type="cloud_run_revision"
jsonPayload.logType="usage_audit"
```

常用筛法:

```
jsonPayload.logType="usage_audit" AND jsonPayload.app_user="alice"
jsonPayload.logType="usage_audit" AND jsonPayload.tokens_total>5000
jsonPayload.logType="usage_audit" AND jsonPayload.status="refused"
```

---

## 图表看板:Cloud Logging → BigQuery → Looker Studio

> ⚠️ 前提:**先用带审计代码的版本重新部署**(`gcloud run deploy videosense ...`,见 `DEPLOY.md`)。
> 日志 sink 是**向前生效**的 —— 只收 sink 建好之后产生的日志,历史日志不会回填。
> 下面命令用了本项目 id `primeval-camera-494521-u6`,换项目自行替换。

### 1. 建 BigQuery 数据集

```bash
bq --location=US mk --dataset primeval-camera-494521-u6:videosense_audit
```

### 2. 建日志 sink(把审计日志路由进 BigQuery)

```bash
gcloud logging sinks create videosense-audit-sink \
  bigquery.googleapis.com/projects/primeval-camera-494521-u6/datasets/videosense_audit \
  --log-filter='resource.type="cloud_run_revision" AND jsonPayload.logType="usage_audit"' \
  --use-partitioned-tables
```

`--use-partitioned-tables` 让它写进**一张按天分区的表**(而非每天一张碎表),更好查。

### 3. 给 sink 的写入身份授 BigQuery 写权限

sink 会自带一个"写入身份"服务账号,需要授权它写这个数据集:

```bash
WRITER=$(gcloud logging sinks describe videosense-audit-sink --format='value(writerIdentity)')
gcloud projects add-iam-policy-binding primeval-camera-494521-u6 \
  --member="$WRITER" \
  --role="roles/bigquery.dataEditor"
```

> 想更小权限:把上面的 project 级授权换成只在 `videosense_audit` 数据集上授 `dataEditor`
> (BigQuery 控制台 → 数据集 → SHARING → Permissions)。

### 4. 等几条流量进来,再建一个"拍平"视图

sink 把日志写进表 `run_googleapis_com_stdout`(stdout 日志的固定表名),原始结构是嵌套的。
建一个视图把常用字段拉平,Looker Studio 直接连它即可。在 **BigQuery 控制台**跑:

```sql
CREATE OR REPLACE VIEW `primeval-camera-494521-u6.videosense_audit.usage` AS
SELECT
  timestamp                                   AS ts,
  jsonPayload.app_user                        AS app_user,
  jsonPayload.ip                              AS ip,
  jsonPayload.session_id                      AS session_id,
  jsonPayload.query                           AS query,
  jsonPayload.status                          AS status,
  jsonPayload.turn_type                       AS turn_type,
  CAST(jsonPayload.tokens_in    AS INT64)     AS tokens_in,
  CAST(jsonPayload.tokens_out   AS INT64)     AS tokens_out,
  CAST(jsonPayload.tokens_total AS INT64)     AS tokens_total,
  CAST(jsonPayload.llm_calls    AS INT64)     AS llm_calls,
  CAST(jsonPayload.cost_usd     AS FLOAT64)   AS cost_usd,
  CAST(jsonPayload.latency_ms   AS INT64)     AS latency_ms,
  jsonPayload.by_model                        AS by_model    -- JSON 字符串:每模型 in/out/total/calls
FROM `primeval-camera-494521-u6.videosense_audit.run_googleapis_com_stdout`
WHERE jsonPayload.logType = "usage_audit";
```

> 若你的项目里 `jsonPayload` 落成的是 **JSON 类型**单列(较新行为)而非嵌套 RECORD,
> 上面 `jsonPayload.app_user` 改成 `JSON_VALUE(jsonPayload, '$.app_user')`、
> 数字用 `CAST(JSON_VALUE(jsonPayload,'$.tokens_total') AS INT64)`。先 `SELECT * ... LIMIT 1` 看一眼表结构即可判断。

### 5. Looker Studio 看板

1. 打开 **lookerstudio.google.com** → Create → **Data source** → **BigQuery**。
2. 选 project `primeval-camera-494521-u6` → dataset `videosense_audit` → 表/视图 **`usage`** → Connect。
3. 把 `ts` 设为日期维度,`Add to report`。建议几张图:

| 图表类型 | 维度 / 指标 | 看什么 |
|---|---|---|
| Scorecard ×3 | `COUNT()`、`SUM(tokens_total)`、`SUM(cost_usd)` | 总请求 / 总 token / 总估算成本 |
| Time series | 维度 `ts`(按天)· 指标 `SUM(cost_usd)`、`SUM(tokens_total)` | 每日成本 / token 趋势 |
| Bar chart | 维度 `app_user` · 指标 `SUM(tokens_total)` | **谁烧得最多** |
| Pie | 维度 `status` · 指标 `COUNT()` | ok / refused / error 占比 |
| Table | `ts, app_user, ip, query, status, tokens_total, cost_usd` 按 `ts` 倒序 | 最近问了什么 |

4. 右上 Share 设为私有(只你自己),完工。

---

## 成本与清理

- **BigQuery 存储**:审计行很小(每行几百字节),费用基本可忽略;长期可在数据集设**表过期**自动清旧分区。
- **Cloud Logging**:审计日志走 `_Default` 日志桶(默认留 30 天),要长留就调日志桶保留期。
- **拆除**(全部可逆):
  ```bash
  gcloud logging sinks delete videosense-audit-sink
  bq rm -r -d primeval-camera-494521-u6:videosense_audit
  ```

## 隐私

`ip` 与原始 `query` 属于 PII。审计走 Cloud Logging / BigQuery,**完全不进** planner 能生成 SQL 触及的业务库
(与会话存储同样的"物理隔离、免疫潘多拉"原则)。给日志桶/表设合理保留期。
