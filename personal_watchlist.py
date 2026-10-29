"""Private per-Google-user watchlists stored in Supabase."""
import os

import requests

MAX_PERSONAL_TRACKED = 5
TABLE = "personal_watchlist"
TIMEOUT = 15


def _settings(use_environment=False):
    if use_environment:
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    else:
        import streamlit as st
        config = st.secrets.get("supabase", {})
        url = config.get("url", "")
        key = config.get("service_role_key", "")
    if not url or not key:
        raise RuntimeError("尚未設定 Supabase 雲端資料庫。")
    return url.rstrip("/"), key


def is_configured():
    try:
        _settings()
        return True
    except (RuntimeError, KeyError, FileNotFoundError):
        return False


def google_login_is_configured():
    import streamlit as st
    try:
        auth = st.secrets.get("auth", {})
        return bool(auth.get("client_id") and auth.get("client_secret") and
                    auth.get("server_metadata_url") and auth.get("cookie_secret") and
                    auth.get("redirect_uri"))
    except (KeyError, FileNotFoundError):
        return False


def _headers(key):
    return {"apikey": key, "Authorization": "Bearer " + key,
            "Content-Type": "application/json", "Accept": "application/json"}


def list_for_user(user_id):
    url, key = _settings()
    response = requests.get(url + "/rest/v1/" + TABLE,
        params={"select": "platform,product_id,query,title,url,added_at",
                "user_id": "eq." + user_id, "order": "added_at.asc"},
        headers=_headers(key), timeout=TIMEOUT)
    response.raise_for_status()
    rows = response.json()
    if len(rows) > MAX_PERSONAL_TRACKED:
        raise RuntimeError("個人追蹤清單資料超過每人上限，請聯絡管理者。")
    return rows


def list_all_for_collection():
    """Used only by the scheduled collector with GitHub Actions secrets."""
    try:
        url, key = _settings(use_environment=True)
    except RuntimeError:
        return []
    response = requests.get(url + "/rest/v1/" + TABLE,
        params={"select": "user_id,platform,product_id,query,title,url,added_at"},
        headers=_headers(key), timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def add_for_user(user_id, product):
    url, key = _settings()
    payload = {"p_user_id": user_id, "p_platform": product["platform"],
        "p_product_id": product["product_id"], "p_query": product["query"],
        "p_title": product["title"], "p_url": product["url"]}
    response = requests.post(url + "/rest/v1/rpc/add_personal_tracking",
        json=payload, headers=_headers(key), timeout=TIMEOUT)
    if not response.ok:
        message = response.text[:300]
        if "limit" in message.lower() or "上限" in message:
            raise ValueError("每個 Google 帳號最多同時追蹤五款球鞋。")
        if response.status_code == 404:
            raise RuntimeError("個人追蹤資料表尚未建立，請先完成資料庫設定。")
        raise RuntimeError("儲存個人追蹤失敗：" + message)
    return "已加入個人長期追蹤"


def remove_for_user(user_id, platform, product_id):
    url, key = _settings()
    response = requests.delete(url + "/rest/v1/" + TABLE,
        params={"user_id": "eq." + user_id, "platform": "eq." + platform,
                "product_id": "eq." + product_id},
        headers={**_headers(key), "Prefer": "return=minimal"}, timeout=TIMEOUT)
    response.raise_for_status()
