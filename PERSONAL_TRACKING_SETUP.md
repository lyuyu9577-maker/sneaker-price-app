# 個人長期追蹤啟用設定

程式碼已支援 Google OIDC 登入、每個 Google 帳號最多五款的個人清單，以及每日收集。啟用前請完成下列設定；不要把任何密鑰提交到 GitHub 或貼在公開 Issue。

## 1. 建立 Supabase 資料表

1. 建立 Supabase 專案。
2. 在 Supabase SQL Editor 執行此儲存庫的 supabase_personal_watchlist.sql 檔案內容。
3. 從專案設定取得 Project URL 和 service_role key。service_role key 具有管理資料的權限，請只放在伺服器端 Secrets。

## 2. 設定 Google 登入

1. 在 Google Cloud Console 建立 OAuth 用戶端，應用程式類型選「網頁應用程式」。
2. 將下列 URI 加入「已授權的重新導向 URI」：
   https://sneaker-price-lyuyu9577.streamlit.app/oauth2callback
3. 若要讓一般訪客都能登入，OAuth 同意畫面需允許外部使用者；若留在測試模式，只有列入測試名單的帳號能登入。

## 3. 設定 Streamlit Secrets

在 Streamlit Cloud 的 app Settings → Secrets 加入以下內容，並填入自己的值：

[auth]
redirect_uri = "https://sneaker-price-lyuyu9577.streamlit.app/oauth2callback"
cookie_secret = "請使用安全的隨機長字串"
client_id = "Google OAuth 用戶端 ID"
client_secret = "Google OAuth 用戶端密鑰"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

[supabase]
url = "https://你的專案.supabase.co"
service_role_key = "Supabase service_role key"

## 4. 設定每日收集

在 GitHub 儲存庫 Settings → Secrets and variables → Actions 建立兩個 Repository secrets：

- SUPABASE_URL：與 Streamlit Secrets 中的 Supabase URL 相同。
- SUPABASE_SERVICE_ROLE_KEY：與 Streamlit Secrets 中的 service_role key 相同。

每日 GitHub Actions 會收集四款全站示範鞋款、其他全站追蹤商品，以及 Supabase 中所有個人追蹤商品。個人清單本身只存於 Supabase；價格觀測沿用目前全站共用的歷史資料檔，不會記錄 Google 帳號識別碼。

## 5. 完成啟用

儲存 Streamlit Secrets 後，重新啟動 app。首次用 Google 帳號登入即可新增個人追蹤；每個帳號上限五款。全站現有示範清單仍保留原有四款。
