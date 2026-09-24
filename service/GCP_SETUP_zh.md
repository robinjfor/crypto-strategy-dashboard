# GCP 設定清單（Binance Demo 下單服務）

> 給 Emily｜預計費用約 **$0／月**（Free tier 內；請保留 $5 預算警報）  
> 地區一律用 **asia-east1（臺灣）**｜專案建議 ID：`crypto-demo-trader`  
> **請勿把 API Key／Secret 貼到任何聊天。** 只在 Cloud Shell 或 Console 輸入。  
> 全部完成後回覆「**設定好了**」，我們再幫你部署。

## 約定名稱（之後部署會用到，請照抄）

| 項目 | 值 |
|------|-----|
| 專案 ID | `crypto-demo-trader`（若已用別的 ID，以下全部改成你的） |
| 地區 | `asia-east1` |
| Artifact Registry 倉庫 | `trader` |
| Cloud Run Job | `crypto-trader` |
| Cloud Scheduler（每小時） | `crypto-trader-hourly`（`1 * * * *` UTC） |
| Cloud Scheduler（SOL 日線） | `crypto-trader-daily-sol`（`7 0 * * *` UTC＝台北 08:07） |
| GCS 狀態桶 | `crypto-demo-trader-state`（全域唯一；撞名就加後綴） |
| 執行用 SA | `trader-runtime@PROJECT_ID.iam.gserviceaccount.com` |
| 部署用 SA | `trader-deploy@PROJECT_ID.iam.gserviceaccount.com` |
| WIF Pool | `github` |
| WIF Provider | `github-provider` |
| Secret 名稱 | `BINANCE_DEMO_API_KEY`、`BINANCE_DEMO_API_SECRET` |
| GitHub Variables | `GCP_PROJECT_ID`、`GCP_PROJECT_NUMBER`、`WIF_PROVIDER`、`DEPLOY_SA_EMAIL`、`GCS_BUCKET`、`TRADER_MODE`、`TRADER_ENABLED` |

可用瀏覽器開 **Cloud Shell**（右上角終端圖示），不用本機安裝 gcloud。

---

## 1. 建立專案、連結帳單、設 $5 預算警報

**Console**

1. 開啟 [Google Cloud Console](https://console.cloud.google.com/) → 頂部專案選單 → **New Project**／**新增專案**  
   - Project name：例如 `Crypto Demo Trader`  
   - Project ID：建議 `crypto-demo-trader` → **Create**
2. 左側選單 **Billing**／**帳單** → **Link a billing account**／連結帳單帳戶（你自己啟用帳單；助手不會替你操作）。
3. **Billing** → 左側 **Budgets & alerts**／**預算與警報** → **Create budget**  
   - Name：`crypto-demo-5usd`  
   - Projects：選本專案  
   - Budget type：**Specified amount** = **$5**／月  
   - Thresholds：**50%**、**90%**、**100%**（Email 通知開給你自己）  
   → **Finish**

**Cloud Shell（可選，對齊同一專案）**

```bash
gcloud config set project crypto-demo-trader
# 記下專案編號（後面 WIF／GitHub 會用）：
gcloud projects describe crypto-demo-trader --format='value(projectNumber)'
```

預算若要以 CLI 建立，需先有 Billing Account ID（Console → Billing → 帳戶名稱旁），再：

```bash
# 將 BILLING_ACCOUNT_ID 換成你的；金額單位為幣別最小單位語意請以 Console 為準
gcloud billing budgets create \
  --billing-account=BILLING_ACCOUNT_ID \
  --display-name=crypto-demo-5usd \
  --budget-amount=5 \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0 \
  --filter-projects=projects/PROJECT_NUMBER
```

（預算警報用 Console 建立最穩；CLI 介面偶有版本差異。）

---

## 2. 啟用 API

**Console：** **APIs & Services** → **API Library**／**API 和服務** → **API 程式庫**，分別搜尋並 **Enable**：

- Cloud Run API  
- Cloud Scheduler API  
- Secret Manager API  
- Artifact Registry API  
- Cloud Build API  
- IAM Service Account Credentials API（搜尋 *IAM Credentials*）  
- Cloud Storage API（狀態 JSON 用）

**Cloud Shell（一次開齊）：**

```bash
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  iamcredentials.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com \
  cloudresourcemanager.googleapis.com
```

---

## 3. Binance Demo 金鑰 → Secret Manager

### 3a. 在 Binance Demo 建立金鑰

1. 登入 [https://demo.binance.com](https://demo.binance.com)（確認頂部是 Demo，不是 Live）。  
2. 找 **API Management**／API 管理。常見路徑：個人頭像 → **Demo Trading API**，或直接開  
   `https://demo.binance.com/en/my/settings/api-management`  
   （若選單名稱不同，在 Demo 站內搜尋「API」即可，**不要**用正式站的 live 金鑰。）  
3. 建立 **System generated** 金鑰；權限開 **Reading** + **Spot** 交易即可；**不要**開提幣。  
4. 立刻複製 API Key 與 Secret（Secret 只顯示一次）。  
5. **絕對不要**把金鑰貼到聊天、Issue、或 GitHub。

### 3b. 寫入 Secret Manager

**Console：** **Security** → **Secret Manager** → **Create secret**  
- Name：`BINANCE_DEMO_API_KEY` → 貼上 Key → **Create secret**  
- 再建立：`BINANCE_DEMO_API_SECRET`

**Cloud Shell（推薦；把 KEY／SECRET 換成你的，勿貼聊天）：**

```bash
printf '%s' '在此貼上你的API_KEY' | gcloud secrets create BINANCE_DEMO_API_KEY \
  --data-file=- --replication-policy=automatic

printf '%s' '在此貼上你的API_SECRET' | gcloud secrets create BINANCE_DEMO_API_SECRET \
  --data-file=- --replication-policy=automatic
```

若 secret 已存在要換新版本：

```bash
printf '%s' '新值' | gcloud secrets versions add BINANCE_DEMO_API_KEY --data-file=-
```

---

## 4. 建立服務帳戶（執行用＋部署用）

```bash
PROJECT_ID=$(gcloud config get-value project)
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# 執行用：Cloud Run Job 讀 Secret、讀寫 GCS
gcloud iam service-accounts create trader-runtime \
  --display-name="Crypto trader runtime"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:trader-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:trader-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

# 部署用：GitHub Actions 建映像、部署 Job、改 Scheduler
gcloud iam service-accounts create trader-deploy \
  --display-name="Crypto trader deploy (GitHub Actions)"

for ROLE in \
  roles/run.admin \
  roles/artifactregistry.writer \
  roles/cloudbuild.builds.editor \
  roles/iam.serviceAccountUser \
  roles/cloudscheduler.admin \
  roles/secretmanager.secretAccessor \
  roles/storage.admin
do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:trader-deploy@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$ROLE"
done

# 讓 deploy SA 可以模擬／指定 runtime SA 給 Cloud Run Job
gcloud iam service-accounts add-iam-policy-binding \
  "trader-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --member="serviceAccount:trader-deploy@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

---

## 5. Workload Identity Federation（GitHub Actions 免 JSON 金鑰）

限制：**僅** repo `robinjfor/crypto-strategy-dashboard` 可換取權杖。

```bash
PROJECT_ID=$(gcloud config get-value project)
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
REPO="robinjfor/crypto-strategy-dashboard"

gcloud iam workload-identity-pools create github \
  --location=global \
  --display-name="GitHub Actions Pool"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global \
  --workload-identity-pool=github \
  --display-name="GitHub provider (crypto-strategy-dashboard)" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository == '${REPO}'"

gcloud iam service-accounts add-iam-policy-binding \
  "trader-deploy@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"
```

記下這三個值（稍後貼到 GitHub **Variables**，不是 Secrets）：

```bash
echo "GCP_PROJECT_ID=$PROJECT_ID"
echo "GCP_PROJECT_NUMBER=$PROJECT_NUMBER"
echo "WIF_PROVIDER=projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/github-provider"
echo "DEPLOY_SA_EMAIL=trader-deploy@${PROJECT_ID}.iam.gserviceaccount.com"
```

### 加到 GitHub

1. 開 repo → **Settings** → **Secrets and variables** → **Actions**  
2. 選上方 **Variables** 分頁 → **New repository variable**，新增：

| Name | Value |
|------|--------|
| `GCP_PROJECT_ID` | 你的專案 ID |
| `GCP_PROJECT_NUMBER` | 專案數字編號 |
| `WIF_PROVIDER` | 上面印出的完整 provider 路徑 |
| `DEPLOY_SA_EMAIL` | `trader-deploy@….iam.gserviceaccount.com` |
| `GCS_BUCKET` | `crypto-demo-trader-state`（或你實際建的桶名） |
| `TRADER_MODE` | `dry-run`（之後要實單再改成 `live`） |
| `TRADER_ENABLED` | `true`（緊急停機改 `false`） |

這些是路徑／名稱，**不是**金鑰，放 Variables 即可。

---

## 6. 建立 GCS 桶（狀態＋儀表板 JSON）

Job 無狀態，持倉／冪等狀態寫一個小 JSON；跑完後也匯出儀表板可讀的 feed。

```bash
PROJECT_ID=$(gcloud config get-value project)
BUCKET="${PROJECT_ID}-state"   # 若撞名：改成 crypto-demo-trader-state-你的暱稱
# 建議固定用清單約定名（全域唯一）：
BUCKET="crypto-demo-trader-state"

gcloud storage buckets create "gs://${BUCKET}" \
  --location=asia-east1 \
  --uniform-bucket-level-access

# 狀態物件僅 runtime SA 讀寫（預設私有即可）
# 儀表板 feed：公開讀單一物件（可選；也可用之後再開）
# gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
#   --member=allUsers --role=roles/storage.objectViewer
# 較安全做法：不要整桶公開；部署後用「公開單一物件」或簽名網址。清單先建私有桶即可。
```

把實際桶名填進 GitHub Variable `GCS_BUCKET`。

（可選）若將來要改推 repo 的 `data/demo/`：另建 fine-grained PAT，存成 Secret Manager 的 `GITHUB_FEED_PAT`，並加 repo Contents 寫入權限——**非必須**，優先用 GCS。

---

## 7. Artifact Registry（映像倉庫）

```bash
gcloud artifacts repositories create trader \
  --repository-format=docker \
  --location=asia-east1 \
  --description="Crypto demo trader images"
```

映像路徑將為：  
`asia-east1-docker.pkg.dev/PROJECT_ID/trader/crypto-trader:TAG`

---

## 8. 完成後告訴我們

請回覆：「**設定好了**」，並確認（可打馬賽克後貼）：

- [ ] 專案 ID／專案編號  
- [ ] 七個 GitHub Variables 已建好（`TRADER_MODE=dry-run`）  
- [ ] 兩個 Secret 已建、**沒有**貼到聊天  
- [ ] GCS 桶名  
- [ ] WIF 已限制到 `robinjfor/crypto-strategy-dashboard`

我們會用 **workflow_dispatch** 手動跑部署：先 **probe**（ping／time／帳戶非零餘額），確認 asia-east1 出口沒被 Binance 擋（HTTP 451），再考慮把 `TRADER_MODE` 改成 `live`。

---

## 費用與風險提醒（簡短）

- 預期：Cloud Run Job 每小時跑一次、很小映像、GCS 幾 KB → **約 $0**（Free tier）。$5 警報只是保險。  
- **asia-east1 標示為臺灣**，但出口 IP 的地理庫是否被 Binance 判成臺灣，**必須用 probe 實測**；若仍 451，再討論 VPC／代理（那時再談，現在先設好）。  
- 預設 `TRADER_MODE=dry-run`：只算訊號、不下單。`TRADER_ENABLED=false` 可當總開關。


---

## 附註：SOL 核心槽日排程

- 策略：`donchian20_atr_btcRegime__SOL__1d`，名義 **1500 USDT**，ATR＝**SMA TR14**（不是 Wilder）。
- 下單／決策窗口：**UTC 00:05–00:15**（台北 08:05–08:15）。
- 請建立第二個 Scheduler：`crypto-trader-daily-sol`，cron `7 0 * * *`（UTC），同樣觸發 Cloud Run Job `crypto-trader`。
- 部署 workflow 會一併建立／更新此排程；你不必手動建，但名稱請保留一致。
- Expectation 日誌物件：`gs://$GCS_BUCKET/trader/sol_expectation_log.json`。
