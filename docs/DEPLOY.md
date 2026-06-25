# 部署到 Cloud Run(B 方案:应用层鉴权)

把 API + 聊天前端部到 Cloud Run,**只有持口令的人能访问**。鉴权在应用层(HTTP Basic
Auth):设了 `APP_ACCESS_KEYS` 才生效,不设则无鉴权(本地开发不受影响)。

> 从源码部署用 Cloud Build 在云端打镜像 —— **你本地不需要装 Docker**。

> **每条命令都给了 bash 和 PowerShell 两版。** bash 的 `\` 换行在 PowerShell 里会报错
> (PowerShell 续行是反引号 `` ` ``),Windows 用户直接用 PowerShell 版。把 `<...>` 占位符换成你的值。

---

## 0. 一次性前置

**bash / macOS / Linux / Cloud Shell**
```bash
gcloud config set project <YOUR_GCP_PROJECT>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# 给 Cloud Run 运行时服务账号 Vertex AI 权限(Router/Planner 要调 Gemini)
PN=$(gcloud projects describe <YOUR_GCP_PROJECT> --format='value(projectNumber)')
gcloud projects add-iam-policy-binding <YOUR_GCP_PROJECT> \
  --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

**PowerShell / Windows**
```powershell
gcloud config set project <YOUR_GCP_PROJECT>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# 给 Cloud Run 运行时服务账号 Vertex AI 权限(Router/Planner 要调 Gemini)
$PN = (gcloud projects describe <YOUR_GCP_PROJECT> --format="value(projectNumber)")
gcloud projects add-iam-policy-binding <YOUR_GCP_PROJECT> --member="serviceAccount:$PN-compute@developer.gserviceaccount.com" --role="roles/aiplatform.user"
```

---

## 1. 选访问口令(`name:key` 格式)

每个可信的人一个。格式 **`name:key`** —— **冒号前是审计/用量监控里显示的人名,冒号后才是真正的登录密码**。
多人用逗号分隔,例如:`alice:9f3k2x7q,bob:7x2qd8m4`。

> 也兼容老的裸 key(`alice-9f3k2,bob-7x2qd`),但那样审计里只能看到不可逆短标签 `u_xxxxxx`,看不到人名。
> 谁用了多少 token / 问了什么,见 [MONITORING.md](MONITORING.md)。

---

## 2. 部署(连接值自动从本地 `neon.env` 读)

所有 env 合进**一个** `--set-env-vars`,用 `^@^` 自定义分隔符(`@` 隔开各项),
这样 `APP_ACCESS_KEYS` 里就算有逗号也不会被 gcloud 误拆。下面脚本自动从 `neon.env`
读连接值(host/密码等),你只需定一下访问口令(脚本会随机生成一个并打印出来)。

**bash / macOS / Linux**
```bash
set -a; source neon.env; set +a          # 载入 GCP_PROJECT / ALLOYDB_* / GCS_BUCKET / UPSTASH_REDIS_REST_*
APP_ACCESS_KEYS="kenny:$(openssl rand -hex 6)"
echo "登录口令(密码填冒号后那段): $APP_ACCESS_KEYS"

gcloud run deploy videosense \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 1Gi --cpu 1 --timeout 120 \
  --min-instances 0 --max-instances 5 \
  --session-affinity \
  --set-env-vars "^@^GCP_PROJECT=$GCP_PROJECT@GCP_REGION=us-central1@ALLOYDB_HOST=$ALLOYDB_HOST@ALLOYDB_DB=$ALLOYDB_DB@ALLOYDB_USER=$ALLOYDB_USER@ALLOYDB_PASSWORD=$ALLOYDB_PASSWORD@GCS_BUCKET=$GCS_BUCKET@SESSION_BACKEND=redis@UPSTASH_REDIS_REST_URL=$UPSTASH_REDIS_REST_URL@UPSTASH_REDIS_REST_TOKEN=$UPSTASH_REDIS_REST_TOKEN@APP_ACCESS_KEYS=$APP_ACCESS_KEYS"
```

**PowerShell / Windows**(gcloud 那行是**完整一行**,粘进去不会断)
```powershell
$neon = @{}
Get-Content neon.env | ForEach-Object { if ($_ -match '^\s*([A-Z_]+)=(.+)$') { $neon[$matches[1]] = $matches[2].Trim() } }
$APP_ACCESS_KEYS = "kenny:" + [guid]::NewGuid().ToString('N').Substring(0,12)
Write-Host "登录口令(密码填冒号后那段): $APP_ACCESS_KEYS"
$pairs = @("GCP_PROJECT=$($neon['GCP_PROJECT'])","GCP_REGION=us-central1","ALLOYDB_HOST=$($neon['ALLOYDB_HOST'])","ALLOYDB_DB=$($neon['ALLOYDB_DB'])","ALLOYDB_USER=$($neon['ALLOYDB_USER'])","ALLOYDB_PASSWORD=$($neon['ALLOYDB_PASSWORD'])","GCS_BUCKET=$($neon['GCS_BUCKET'])","SESSION_BACKEND=redis","UPSTASH_REDIS_REST_URL=$($neon['UPSTASH_REDIS_REST_URL'])","UPSTASH_REDIS_REST_TOKEN=$($neon['UPSTASH_REDIS_REST_TOKEN'])","APP_ACCESS_KEYS=$APP_ACCESS_KEYS") -join "@"
gcloud run deploy videosense --source . --region us-central1 --allow-unauthenticated --memory 1Gi --cpu 1 --timeout 120 --min-instances 0 --max-instances 5 --session-affinity --set-env-vars "^@^$pairs"
```

部署完会输出一个 `https://videosense-xxxx-uc.a.run.app` 网址。

> ⚠️ **必须 `--source .` 从源码重建镜像**:会话后端是新代码,只改 env 不重建不会生效(旧镜像里没有 `RedisSessionStore`)。

要点:
- `--allow-unauthenticated`:服务公开,但鉴权在 app 里(B 方案);口令门是唯一拦未授权访问的闸。
- **`SESSION_BACKEND=redis` + Upstash**:会话存共享 Redis —— 重启续得上、**多副本跨实例共享**,不再靠单实例内存。
- **`--max-instances 5`**:会话共享后可横向扩(原先锁 `1` 是因为内存会话只在单实例一致);**`--session-affinity`** 让同一会话尽量落同一副本(配合 app 内每会话锁,防跨副本"后写覆盖")。
- 想让**画图/科学题**也能用:再加一项 `SANDBOX_URL=<你的 sandbox 服务地址>`(`sandbox/` 已可单独部署,有自己的 Dockerfile);只问 SQL 类问题则可不设。

---

## 3. 发给朋友
把**网址 + 他的口令**发过去。浏览器会弹登录框:**用户名随便填,密码填 `name:key` 里冒号后那段**。

---

## 撤销 / 改口令
改 `APP_ACCESS_KEYS`(去掉某人、或加人),用 **`--update-env-vars`**(只改这一个、保留其余):

> ⚠️ **改单个变量千万别用 `--set-env-vars`** —— 它会「先删光所有环境变量再设」,
> 会把 `GCP_PROJECT` / `ALLOYDB_*` 等全冲掉,服务直接坏。只更新某个变量一律用 `--update-env-vars`。

**bash**
```bash
gcloud run services update videosense --region us-central1 \
  --update-env-vars APP_ACCESS_KEYS=alice:9f3k2x7q
```

**PowerShell**
```powershell
gcloud run services update videosense --region us-central1 --update-env-vars APP_ACCESS_KEYS=alice:9f3k2x7q
```

> 多个用户(值里有逗号)才需要 `^@^` 分隔符:`--update-env-vars "^@^APP_ACCESS_KEYS=alice:k1,bob:k2"`

---

## 更安全(可选):密码/口令走 Secret Manager,不放明文 env

**bash**
```bash
printf '%s' '<NEON_PASSWORD>'          | gcloud secrets create neon-password   --data-file=-
printf '%s' 'alice:9f3k2x7q,bob:7x2qd' | gcloud secrets create app-access-keys --data-file=-
gcloud secrets add-iam-policy-binding neon-password   --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding app-access-keys --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
# 部署时改用(并从 --set-env-vars 里删掉这两项):
#   --set-secrets "ALLOYDB_PASSWORD=neon-password:latest,APP_ACCESS_KEYS=app-access-keys:latest"
```

**PowerShell**
```powershell
"<NEON_PASSWORD>"          | gcloud secrets create neon-password   --data-file=-
"alice:9f3k2x7q,bob:7x2qd" | gcloud secrets create app-access-keys --data-file=-
gcloud secrets add-iam-policy-binding neon-password   --member="serviceAccount:$PN-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding app-access-keys --member="serviceAccount:$PN-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
# 部署时改用(并从 --set-env-vars 里删掉这两项):
#   --set-secrets "ALLOYDB_PASSWORD=neon-password:latest,APP_ACCESS_KEYS=app-access-keys:latest"
```

---

## 用量监控
部署后,每个请求会落一条审计日志(谁/从哪/何时/问了什么/用了多少 token+成本)→ Cloud Logging →(可选)BigQuery + Looker Studio。完整方案见 [MONITORING.md](MONITORING.md)。

## 顺带
数据已全在 Neon → 可去 GCP 控制台删掉 AlloyDB 实例,止住月账单。
