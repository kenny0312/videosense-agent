# 部署到 Cloud Run(B 方案:应用层鉴权)

把 API + 聊天前端部到 Cloud Run,**只有持口令的人能访问**。鉴权在应用层(HTTP Basic
Auth):设了 `APP_ACCESS_KEYS`(逗号分隔的口令)才生效,不设则无鉴权(本地开发不受影响)。

> 从源码部署用 Cloud Build 在云端打镜像 —— **你本地不需要装 Docker**。

## 0. 一次性前置
```bash
gcloud config set project <YOUR_GCP_PROJECT>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# 给 Cloud Run 运行时服务账号 Vertex AI 权限(Router/Planner 要调 Gemini)
PN=$(gcloud projects describe <YOUR_GCP_PROJECT> --format='value(projectNumber)')
gcloud projects add-iam-policy-binding <YOUR_GCP_PROJECT> \
  --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

## 1. 选访问口令
每个可信的人一个,逗号分隔,例如:`alice-9f3k2,bob-7x2qd`

## 2. 部署(连接值从你本地 `neon.env` 取)
`APP_ACCESS_KEYS` 含逗号,单独放在一个带 `^@^` 自定义分隔符的 flag 里(否则 gcloud 会把逗号当分隔)。
```bash
gcloud run deploy videosense \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 1Gi --cpu 1 --timeout 120 \
  --min-instances 0 --max-instances 1 \
  --set-env-vars "GCP_PROJECT=<YOUR_GCP_PROJECT>,GCP_REGION=us-central1,ALLOYDB_HOST=<NEON_POOLER_HOST>,ALLOYDB_DB=neondb,ALLOYDB_USER=neondb_owner,ALLOYDB_PASSWORD=<NEON_PASSWORD>,GCS_BUCKET=activitynet,SESSION_DB_PATH=" \
  --set-env-vars "^@^APP_ACCESS_KEYS=alice-9f3k2,bob-7x2qd"
```
部署完会输出一个 `https://videosense-xxxx-uc.a.run.app` 网址。

要点:
- `--allow-unauthenticated`:服务公开,但鉴权在 app 里(B 方案)。
- `--max-instances 1`:多轮会话落在同一实例(内存会话一致)。
- `SESSION_DB_PATH=`(置空):会话纯内存(Cloud Run 文件系统易失,演示够用)。
- 本次只演示 **SQL 类问题**,未部沙箱(`SANDBOX_URL` 不设);画图/科学题要再单独部 `sandbox/`(它已有自己的 Dockerfile),再把 `SANDBOX_URL` 指过去。

## 3. 发给朋友
把**网址 + 他的口令**发过去。浏览器会弹登录框:**用户名随便填,密码填口令**。

## 撤销 / 改口令
改 `APP_ACCESS_KEYS` 去掉某人的 key,重新部署:
```bash
gcloud run services update videosense --region us-central1 \
  --set-env-vars "^@^APP_ACCESS_KEYS=alice-9f3k2"
```

## 更安全(可选):密码/口令走 Secret Manager,不放明文 env
```bash
printf '%s' '<NEON_PASSWORD>'        | gcloud secrets create neon-password   --data-file=-
printf '%s' 'alice-9f3k2,bob-7x2qd'  | gcloud secrets create app-access-keys --data-file=-
gcloud secrets add-iam-policy-binding neon-password   --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding app-access-keys --member="serviceAccount:${PN}-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor"
# 部署时改用(并从 --set-env-vars 里删掉这两项):
#   --set-secrets "ALLOYDB_PASSWORD=neon-password:latest,APP_ACCESS_KEYS=app-access-keys:latest"
```

## 顺带
数据已全在 Neon → 可去 GCP 控制台删掉 AlloyDB 实例,止住月账单。
