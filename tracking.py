from __future__ import annotations

import hashlib
import json
import re
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
OBSERVATIONS = ROOT / "verified_prices.jsonl"
STATUS = ROOT / "collection_status.json"
TAIPEI = timezone(timedelta(hours=8))
TARGETS = [
    "Jordan 1 Low", "Dunk Low", "Air Force 1",
    "Adidas Samba", "New Balance 530", "New Balance 9060",
]
PLATFORMS = ["PChome", "momo購物網"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36"}
COLUMNS = ["observed_at", "date", "query", "platform", "product_id", "title", "url",
           "price", "currency", "price_scope", "source_url", "response_sha256",
           "source_item", "schema_version"]


def now_tw():
    return datetime.now(TAIPEI)


def numeric_price(value):
    # Reject price ranges, missing values and invalid text, rather than concatenate digits.
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    number = float(text)
    return number if np.isfinite(number) and number > 0 else None


def matches(query, title):
    from app import query_matches_product
    title = re.sub(r"\bNB(?=\s*\d)", "New Balance ", title, flags=re.I)
    return query_matches_product(query, title)


def observation(query, platform, product_id, title, url, price, response, item):
    timestamp = now_tw()
    return {
        "observed_at": timestamp.isoformat(timespec="seconds"),
        "date": timestamp.date().isoformat(), "query": query, "platform": platform,
        "product_id": str(product_id), "title": title, "url": url,
        "price": price, "currency": "TWD",
        "price_scope": "平台搜尋刊登價；未限定尺寸，未含運費及個人折價券",
        "source_url": response.url,
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
        "source_item": item, "schema_version": 1,
    }


def collect_platform(query, platform):
    rows = []
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        if platform == "PChome":
            for page in (1, 2):
                response = session.get(
                    "https://ecshweb.pchome.com.tw/search/v3.3/all/results",
                    params={"q": query, "page": page, "sort": "sale/dc"}, timeout=25)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("prods"), list):
                    raise ValueError("搜尋 API 格式異常")
                for item in payload["prods"]:
                    name, pid = item.get("name"), item.get("Id")
                    price = numeric_price(item.get("price"))
                    if not name or not pid or price is None or not matches(query, name):
                        continue
                    rows.append(observation(query, platform, pid, name,
                        f"https://24h.pchome.com.tw/prod/{pid}", price, response, item))
                if not payload["prods"]:
                    break
        else:
            response = session.get("https://www.momoshop.com.tw/search/" + quote_plus(query),
                                   params={"viewport": "desktop"}, timeout=25)
            response.raise_for_status()
            scripts = re.findall(r'<script[^>]*type=["\x27]application/ld\+json["\x27][^>]*>(.*?)</script>',
                                 response.text, re.S | re.I)
            found_list = False
            for script in scripts:
                payload = json.loads(script)
                nodes = payload if isinstance(payload, list) else payload.get("@graph", [payload])
                for node in nodes:
                    if not isinstance(node, dict) or node.get("@type") != "ItemList":
                        continue
                    found_list = True
                    for entry in node.get("itemListElement", []):
                        item = entry.get("item", entry)
                        name, url = item.get("name"), item.get("url")
                        offers = item.get("offers", {})
                        if not isinstance(offers, dict):
                            continue
                        price = numeric_price(offers.get("price"))
                        if offers.get("priceCurrency", "TWD") not in {"TWD", "NTD"}:
                            continue
                        availability = offers.get("availability", "")
                        if any(x in availability for x in ("OutOfStock", "Discontinued", "SoldOut")):
                            continue
                        if not name or not url or price is None or not matches(query, name):
                            continue
                        parsed = urlparse(url)
                        if parsed.hostname not in {"www.momoshop.com.tw", "momoshop.com.tw"}:
                            continue
                        pid = parse_qs(parsed.query).get("i_code", [None])[0]
                        if not pid:
                            continue
                        url = "https://www.momoshop.com.tw/goods/GoodsDetail.jsp?" + urlencode({"i_code": pid})
                        rows.append(observation(query, platform, pid, name, url, price, response, item))
            if not found_list:
                raise ValueError("未取得商品結構化資料；可能遭驗證或頁面格式改變")
        unique = {(r["platform"], r["product_id"]): r for r in rows}
        return list(unique.values()), {"query": query, "platform": platform,
            "status": "ok" if unique else "no_matches", "count": len(unique),
            "checked_at": now_tw().isoformat(timespec="seconds"),
            "message": "" if unique else "當次未取得符合條件的商品；不沿用舊價格"}
    finally:
        session.close()


def load_observations(path=OBSERVATIONS):
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("schema_version") != 1 or not row.get("source_item") or not row.get("response_sha256"):
            continue
        if numeric_price(row.get("price")) is None or row.get("currency") != "TWD":
            continue
        captured = datetime.fromisoformat(row["observed_at"]).astimezone(TAIPEI)
        if captured.date().isoformat() != row["date"] or captured > now_tw() + timedelta(minutes=5):
            continue
        rows.append(row)
    return pd.DataFrame(rows, columns=COLUMNS)


def save_observations(new_rows, path=OBSERVATIONS):
    # Preserve all actual captures, including intraday changes. Never rewrite old dates.
    existing = load_observations(path).to_dict("records")
    keys = {(r["observed_at"], r["query"], r["platform"], r["product_id"]) for r in existing}
    for row in new_rows:
        key = (row["observed_at"], row["query"], row["platform"], row["product_id"])
        if key not in keys:
            existing.append(row)
            keys.add(key)
    text = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in existing)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def daily_series(frame, today=None):
    today = pd.Timestamp(today or now_tw().date())
    if frame.empty:
        return pd.Series(dtype=float)
    part = frame.copy()
    # One fixed platform/product only; daily last observed price, not a mixed basket.
    if part[["platform", "product_id"]].drop_duplicates().shape[0] != 1:
        raise ValueError("時間序列必須屬於同平台、同商品")
    part["day"] = pd.to_datetime(part["date"])
    part = part[(part["day"] <= today) & (part["day"] >= today - pd.Timedelta(days=89))]
    if part.empty:
        return pd.Series(dtype=float)
    part = part.sort_values("observed_at").drop_duplicates("day", keep="last")
    values = pd.Series(part["price"].astype(float).values, index=pd.DatetimeIndex(part["day"]))
    # Missing days remain NaN: neither forward fill nor interpolation is observed data.
    return values.reindex(pd.date_range(values.index.min(), today, freq="D"))


def forecast_arima(series, today=None):
    today = pd.Timestamp(today or now_tw().date())
    valid = series.dropna()
    if len(valid) < 30:
        return None, f"只有 {len(valid)} 天真實觀測；至少累積 30 天後才建立 ARIMA。"
    if (today - valid.index.max()).days > 1:
        return None, "最新觀測已超過 1 天，暫停預測，等待更新。"
    if len(valid) / len(series) < 0.8:
        return None, "近期待用資料缺漏超過 20%，暫停預測。"
    if (valid <= 0).any():
        return None, "價格資料無效，無法預測。"
    from statsmodels.tsa.arima.model import ARIMA
    logs = np.log(series.astype(float))
    train, holdout = logs.iloc[:-7], series.iloc[-7:]
    if holdout.notna().sum() < 5 or train.notna().sum() < 20:
        return None, "最近 7 天或訓練期間的實際觀測不足，無法回測。"
    best = None
    # Time-ordered validation; the last 7 days never enter candidate training.
    orders = [(0, 1, 0), (1, 0, 0), (1, 1, 0), (0, 1, 1), (1, 1, 1)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for order in orders:
            try:
                fit = ARIMA(train, order=order).fit()
                if not fit.mle_retvals.get("converged", False):
                    continue
                predicted = np.exp(np.asarray(fit.forecast(7)))
                mask = holdout.notna().to_numpy()
                mae = float(np.mean(np.abs(predicted[mask] - holdout.to_numpy()[mask])))
                if np.isfinite(mae) and (best is None or mae < best[0]):
                    best = (mae, order)
            except (ValueError, np.linalg.LinAlgError):
                continue
        if best is None:
            return None, "ARIMA 候選模型未收斂，暫不提供預測。"
        try:
            fitted = ARIMA(logs, order=best[1]).fit()
            if not fitted.mle_retvals.get("converged", False):
                return None, "ARIMA 最終模型未收斂，暫不提供預測。"
            result = fitted.get_forecast(steps=7)
            interval = np.exp(np.asarray(result.conf_int(alpha=0.05)))
            median = np.exp(np.asarray(result.predicted_mean))
            if not np.isfinite(interval).all() or not np.isfinite(median).all():
                return None, "預測數值不穩定，暫不提供預測。"
        except (ValueError, np.linalg.LinAlgError):
            return None, "ARIMA 計算失敗，暫不提供預測。"
    baseline = float(np.mean(np.abs(holdout.dropna() - np.exp(train.dropna().iloc[-1]))))
    table = pd.DataFrame({"日期": pd.date_range(today + pd.Timedelta(days=1), periods=7),
        "預測中位價格": median, "95%下界": interval[:, 0], "95%上界": interval[:, 1]})
    return table, (f"對數價格 ARIMA{best[1]}；最後 7 天留出驗證 MAE NT$ {best[0]:.1f}，"
                   f"維持前價基準 MAE NT$ {baseline:.1f}。"
                   + ("模型未優於維持前價基準；請審慎解讀。" if best[0] >= baseline else "")
                   + " 區間為模型估計，並非未來實際售價保證。")


def render_dashboard():
    import base64
    import altair as alt
    import streamlit as st
    st.set_page_config(page_title="真實球鞋價格追蹤與 ARIMA", page_icon="👟", layout="wide")
    hero = base64.b64encode((ROOT / "sneaker-hero.png").read_bytes()).decode("ascii")
    st.markdown(f'''<div style="background:#08090b;border-radius:16px;overflow:hidden;margin-bottom:1.25rem">
        <img src="data:image/png;base64,{hero}" alt="黑白球鞋搭配價格趨勢圖的主視覺"
        style="display:block;width:100%;height:clamp(220px,32vw,360px);object-fit:contain" />
        </div>''', unsafe_allow_html=True)
    info_panel = '''<section aria-label="球鞋價格追蹤資訊" style="background:rgba(45,145,220,.16);
        border:1px solid rgba(80,170,230,.24);border-radius:12px;padding:1.25rem 1.5rem;margin:1rem 0 1.5rem">
        <h1 style="font-size:clamp(1.5rem,3vw,2.35rem);line-height:1.3;margin:0 0 .65rem;padding:0">
        球鞋價格追蹤與 7 天預測</h1>
        <p style="margin:0 0 .65rem;opacity:.8">真實平台刊登價 → 30／60／90 天觀測 → ARIMA 模型預測</p>
        <p style="margin:0;line-height:1.7">每筆附採集時間、商品連結及來源摘要。只記錄成功取得的價格，
        不把舊價當今日價格；不補造歷史。未限定尺寸／顏色，刊登價不包含運費與個人折價券。</p>
        </section>'''
    mode = st.sidebar.radio("選擇查詢方式", ["下拉選單", "自由搜尋"],
                            horizontal=True, key="shoe_query_mode")
    query = None
    submitted = False
    if mode == "下拉選單":
        query = st.sidebar.selectbox("每日追蹤鞋款", TARGETS, key="tracked_shoe")
        submitted = st.sidebar.button("搜尋", key="search_tracked_shoe")
        search_text = query
        st.sidebar.caption("選好鞋款後按「搜尋」，取得最新平台價格。")
    else:
        with st.sidebar.form("shoe_search"):
            search_text = st.text_input("搜尋其他鞋款", placeholder="例如：Kobe 6、Jordan 4",
                                        key="shoe_search_text")
            submitted = st.form_submit_button("搜尋")
        st.sidebar.caption("輸入鞋款後按搜尋或 Enter；切換「下拉選單」可查看預設鞋款。")
    if submitted:
        keyword = " ".join(search_text.split())
        if not keyword:
            st.sidebar.warning("請先輸入鞋款名稱。")
        else:
            rows, results = [], []
            with st.spinner(f"正在搜尋 {keyword} 的平台價格…"):
                for platform in PLATFORMS:
                    try:
                        found, result = collect_platform(keyword, platform)
                        rows.extend(found)
                        results.append(result)
                    except (requests.RequestException, ValueError, KeyError, TypeError):
                        results.append({"query": keyword, "platform": platform,
                            "status": "error", "message": "即時搜尋暫時無法取得資料，請稍後再試。"})
            st.session_state["shoe_search_result"] = {
                "mode": mode, "query": keyword, "rows": rows, "results": results,
                "finished_at": now_tw().isoformat(timespec="seconds")}
    days = st.sidebar.radio("歷史期間（天）", [30, 60, 90], horizontal=True)
    st.sidebar.caption("代表性鞋款清單，並非即時銷量排行。每日台灣時間 09:17 收集；排程可能延遲。")
    st.sidebar.link_button("查看每日收集執行紀錄", "https://github.com/lyuyu9577-maker/sneaker-price-app/actions")
    frame = load_observations()
    status = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    search = st.session_state.get("shoe_search_result")
    if search and (search.get("mode") != mode or
                   (mode == "下拉選單" and search["query"] != query)):
        search = None
    if mode == "自由搜尋" and not search:
        st.markdown(info_panel, unsafe_allow_html=True)
        st.info("請在側邊欄輸入鞋款名稱，按「搜尋」查看價格。")
        return
    if search:
        query = search["query"]
        status = search
        historical = frame.loc[frame["title"].map(
            lambda title: matches(query, str(title))).astype(bool)].copy()
        selected = pd.concat([historical, pd.DataFrame(search["rows"], columns=COLUMNS)],
                             ignore_index=True).drop_duplicates(
                                 ["observed_at", "platform", "product_id"], keep="last")
        st.caption(f"搜尋結果：{query}")
        if mode == "自由搜尋":
            st.caption("自訂搜尋不會加入每日自動追蹤；本次結果保留於目前工作階段。")
        else:
            st.caption("本次即時查詢結果保留於目前工作階段；歷史紀錄仍來自已保存的觀測。")
    else:
        selected = frame[frame["query"].eq(query)].copy()
        st.caption(f"目前追蹤鞋款：{query}")
    st.caption("最近收集：" + status.get("finished_at", "尚未執行"))
    st.markdown(info_panel, unsafe_allow_html=True)
    for entry in status.get("results", []):
        if entry["query"] == query and entry["status"] != "ok":
            st.warning(f'{entry["platform"]}：{entry["message"]}')
    st.subheader("1 · 各平台目前價格")
    today = now_tw().date().isoformat()
    current = selected[selected["date"].eq(today)].sort_values("observed_at").drop_duplicates(
        ["platform", "product_id"], keep="last")
    if search:
        current = pd.DataFrame(search["rows"], columns=COLUMNS)
        current = current[current["date"].eq(today)].drop_duplicates(
            ["platform", "product_id"], keep="last")
    for platform, col in zip(PLATFORMS, st.columns(2)):
        subset = current[current["platform"].eq(platform)]
        with col:
            if subset.empty:
                st.metric(platform, "今日尚無有效價格")
            else:
                st.metric(platform + " · 今日觀測最低刊登價", f'NT$ {subset["price"].min():,.0f}')
                st.caption(f'{len(subset)} 個商品；不同商品／配色／尺寸可能價格不同。')
    if not current.empty:
        table = current.sort_values(["platform", "price"])[["platform", "title", "price", "observed_at", "url"]]
        st.dataframe(table.rename(columns={"platform":"平台","title":"商品","price":"刊登價",
            "observed_at":"採集時間（UTC+8）","url":"商品連結"}), hide_index=True,
            column_config={"商品連結":st.column_config.LinkColumn("商品連結")})
    st.subheader(f"2 · 歷史 {days} 天價格")
    st.caption("選擇固定商品查看走勢；每一天取最後一次成功觀測，缺漏日留空，未以其他商品價格補上。")
    if selected.empty:
        st.warning("此鞋款尚未取得有效觀測。無法顯示真實歷史或產生可靠預測。")
        return
    for platform in PLATFORMS:
        items = selected[selected["platform"].eq(platform)]
        if items.empty:
            st.info(platform + " 尚無可用歷史。")
            continue
        latest = items.sort_values("observed_at").drop_duplicates("product_id", keep="last")
        names = latest.set_index("product_id")["title"].to_dict()
        with st.expander(platform + " · 固定商品走勢與預測", expanded=True):
            pid = st.selectbox("商品", list(names), format_func=lambda p, names=names: names[p] + " [" + p + "]", key=platform)
            product = items[items["product_id"].eq(pid)]
            series = daily_series(product)
            period = series.reindex(pd.date_range(now_tw().date()-timedelta(days=days-1), periods=days))
            history = pd.DataFrame({"日期":period.index, "實際刊登價":period.values})
            st.caption(f"此期間實際記錄 {period.notna().sum()}／{days} 天。歷史視窗不代表已有完整資料。")
            chart = alt.Chart(history).mark_line(point=True).encode(
                x=alt.X("日期:T", title="日期"), y=alt.Y("實際刊登價:Q", scale=alt.Scale(zero=False)),
                tooltip=["日期:T", "實際刊登價:Q"])
            st.altair_chart(chart, use_container_width=True)
            st.dataframe(history.dropna(), hide_index=True)
            st.markdown("**3 · 未來 7 天 ARIMA 預測**")
            forecast, note = forecast_arima(series)
            if forecast is None:
                st.info(note)
            else:
                st.caption(note)
                base = alt.Chart(forecast).encode(x="日期:T")
                band = base.mark_area(opacity=0.15, color="#d97706").encode(y="95%下界:Q", y2="95%上界:Q")
                line = base.mark_line(point=True, color="#d97706", strokeDash=[6,4]).encode(
                    y=alt.Y("預測中位價格:Q", scale=alt.Scale(zero=False)),
                    tooltip=["日期:T", "預測中位價格:Q", "95%下界:Q", "95%上界:Q"])
                st.altair_chart(band + line, use_container_width=True)
                st.dataframe(forecast.round(2), hide_index=True)
            st.download_button("下載此商品真實觀測（含來源證據）",
                product.to_json(orient="records", force_ascii=False, indent=2),
                file_name=f"{platform}-{pid}-observations.json", mime="application/json", key="download"+platform)
    st.caption("舊 price_history.csv 的來源與採集時間不足以驗證，未納入此走勢及訓練。"
               "模型以同平台、同商品近 90 天資料訓練，至少 30 個觀測日及 80% 覆蓋率；"
               "缺漏保留為空值。ARIMA 不保證捕捉突然促銷、缺貨或價格改制。")
