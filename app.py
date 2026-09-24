from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote_plus
import altair as alt
import numpy as np
import pandas as pd
import requests
import streamlit as st


PLATFORMS = ["momo購物網", "PChome"]
SHOE_CATEGORIES = ["男鞋", "女鞋", "童鞋", "全部"]
PRICE_HISTORY_PATH = Path(__file__).with_name("price_history.csv")
PRODUCT_CATALOG_PATH = Path(__file__).with_name("sneaker_catalog.csv")
PRICE_HISTORY_COLUMNS = [
    "日期",
    "搜尋關鍵字",
    "鞋款類別",
    "平台",
    "商品名稱",
    "商品連結",
    "價格",
    "資料來源",
]
PRODUCT_CATALOG_COLUMNS = [
    "更新日期",
    "平台",
    "鞋款類別",
    "商品名稱",
    "價格",
    "評價",
    "評論數",
    "賣家",
    "賣家穩定度",
    "網路價",
    "優惠",
    "推薦分數",
    "價格說明",
    "資料來源",
    "商品連結",
]
PCHOME_SEARCH_API = "https://ecshweb.pchome.com.tw/search/v3.3/all/results"
PCHOME_PRODUCT_API = "https://ecapi.pchome.com.tw/ecshop/prodapi/v2/prod/button"
MOMO_SEARCH_URL = "https://www.momoshop.com.tw/search/{keyword}"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}


@dataclass(frozen=True)
class Recommendation:
    title: str
    platform: str
    price: int
    score: float
    reason: str


def normalize_query(query: str) -> str:
    return query.strip()


def normalize_match_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value).lower()


def tokenize_match_text(value: str) -> list[str]:
    return re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]+", value.lower())


def split_letter_number_token(token: str) -> list[str]:
    return re.findall(r"[a-zA-Z]+|\d+|[\u4e00-\u9fff]+", token)


def product_contains_parts(product_tokens: list[str], parts: list[str]) -> bool:
    search_from = 0
    for part in parts:
        try:
            search_from = product_tokens.index(part, search_from) + 1
        except ValueError:
            return False
    return True


def expand_number_aliases(parts: list[str]) -> list[list[str]]:
    digit_to_roman = {
        "1": "i",
        "2": "ii",
        "3": "iii",
        "4": "iv",
        "5": "v",
        "6": "vi",
        "7": "vii",
        "8": "viii",
        "9": "ix",
        "10": "x",
        "11": "xi",
        "12": "xii",
        "13": "xiii",
        "14": "xiv",
        "15": "xv",
        "16": "xvi",
        "17": "xvii",
        "18": "xviii",
        "19": "xix",
        "20": "xx",
    }
    roman_to_digit = {roman: digit for digit, roman in digit_to_roman.items()}

    alternatives = [parts]
    for index, part in enumerate(parts):
        replacement = digit_to_roman.get(part) or roman_to_digit.get(part)
        if replacement:
            alternate = parts.copy()
            alternate[index] = replacement
            alternatives.append(alternate)
    return alternatives


def build_query_term_groups(query: str) -> list[list[list[str]]]:
    raw_tokens = tokenize_match_text(query)
    ignored_tokens = {
        "鞋",
        "球鞋",
        "運動鞋",
        "休閒鞋",
        "籃球鞋",
        "男鞋",
        "女鞋",
        "童鞋",
        "sneaker",
        "sneakers",
        "shoe",
        "shoes",
        "款",
        "代",
    }
    alias_groups = {
        "allstar": [["allstar"], ["all", "star"]],
        "allstars": [["allstars"], ["all", "stars"], ["all", "star"]],
        "lowtop": [["lowtop"], ["low", "top"]],
        "hightop": [["hightop"], ["high", "top"]],
    }

    groups = []
    for token in raw_tokens:
        normalized_token = normalize_match_text(token)
        if not normalized_token or normalized_token in ignored_tokens:
            continue
        if normalized_token.isalpha() and len(normalized_token) <= 1:
            continue

        if normalized_token in alias_groups:
            groups.append(alias_groups[normalized_token])
            continue

        parts = split_letter_number_token(normalized_token)
        if len(parts) > 1:
            groups.append([[normalized_token], *expand_number_aliases(parts)])
        else:
            groups.append(expand_number_aliases([normalized_token]))

    return groups


def query_matches_product(query: str, product_name: str) -> bool:
    normalized_name = normalize_match_text(product_name)
    product_tokens = tokenize_match_text(product_name)
    term_groups = build_query_term_groups(query)

    if not term_groups:
        return True

    for alternatives in term_groups:
        if any(
            normalize_match_text("".join(parts)) in normalized_name
            or product_contains_parts(product_tokens, parts)
            for parts in alternatives
        ):
            continue
        return False

    return True


def detect_shoe_category(product_name: str) -> str:
    lowered = product_name.lower()

    kids_markers = [
        "童鞋",
        "兒童",
        "小童",
        "中童",
        "大童",
        "kids",
        "kid",
        "youth",
        "gs",
        "ps",
        "td",
    ]
    women_markers = ["女鞋", "女款", "女子", "女性", "women", "womens", "women's", "wmns"]
    men_markers = ["男鞋", "男款", "男子", "男性", "men", "mens", "men's"]

    has_kids = any(marker in lowered for marker in kids_markers)
    has_women = any(marker in lowered for marker in women_markers)
    has_men = any(marker in lowered for marker in men_markers)

    if has_kids:
        return "童鞋"
    if has_women and not has_men:
        return "女鞋"
    if has_men and not has_women:
        return "男鞋"
    if has_men and has_women:
        return "男女通用"
    return "未標示"


def category_matches_product(selected_category: str, product_name: str) -> bool:
    if selected_category == "全部":
        return True

    detected_category = detect_shoe_category(product_name)
    if selected_category == "男鞋":
        return detected_category in {"男鞋", "男女通用"}
    if selected_category == "女鞋":
        return detected_category in {"女鞋", "男女通用"}
    if selected_category == "童鞋":
        return detected_category == "童鞋"
    return True


def build_platform_search_url(platform: str, keyword: str) -> str:
    encoded_keyword = quote_plus(keyword.strip())
    urls = {
        "momo購物網": f"https://www.momoshop.com.tw/search/searchShop.jsp?keyword={encoded_keyword}",
        "PChome": f"https://ecshweb.pchome.com.tw/search/v3.3/?q={encoded_keyword}",
    }
    return urls.get(platform, f"https://www.google.com/search?q={encoded_keyword}")


def build_mock_products(query: str, platforms: list[str] | None = None) -> pd.DataFrame:
    selected_platforms = platforms or PLATFORMS
    seed = sum(ord(char) for char in query.lower())
    rng = np.random.default_rng(seed)
    base_price = 2600 + seed % 4200

    product_types = [
        "經典低筒",
        "復刻高筒",
        "實戰籃球鞋",
        "限定配色",
        "人氣休閒款",
        "輕量訓練款",
    ]

    rows = []
    for index, style in enumerate(product_types):
        platform = PLATFORMS[index % len(PLATFORMS)]
        if platform not in selected_platforms:
            continue
        product_name = f"{query} {style}"
        price_noise = int(rng.normal(0, 420))
        platform_adjustment = {"momo購物網": 90, "PChome": 180}[platform]
        price = max(1280, base_price + price_noise + platform_adjustment + index * 180)
        rating = round(float(rng.uniform(4.1, 4.95)), 2)
        review_count = int(rng.integers(36, 950))
        seller_stability = round(float(rng.uniform(0.72, 0.98)), 2)
        discount = int(rng.integers(0, 16))

        rows.append(
            {
                "平台": platform,
                "商品名稱": product_name,
                "價格": price,
                "評價": rating,
                "評論數": review_count,
                "賣家": f"{platform} 精選店 {index + 1}",
                "賣家穩定度": seller_stability,
                "網路價": price,
                "優惠": f"{discount}%",
                "商品連結": build_platform_search_url(platform, product_name),
                "資料來源": "模擬資料",
                "價格說明": "模擬價格",
            }
        )

    products = pd.DataFrame(rows)
    if products.empty:
        return products
    products["推薦分數"] = calculate_scores(products)
    return products.sort_values("推薦分數", ascending=False).reset_index(drop=True)


def parse_int_price(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def is_sneaker_product(name: str) -> bool:
    include_keywords = [
        "鞋",
        "球鞋",
        "運動鞋",
        "休閒鞋",
        "籃球鞋",
        "男鞋",
        "女鞋",
        "童鞋",
        "sneaker",
    ]
    exclude_keywords = ["襪", "背包", "包包", "衣", "褲", "帽", "吊飾", "鑰匙圈"]
    lower_name = name.lower()
    return any(keyword.lower() in lower_name for keyword in include_keywords) and not any(
        keyword.lower() in lower_name for keyword in exclude_keywords
    )


def parse_pchome_product_page(html: str) -> dict[str, int | float | str | None]:
    final_price = None
    list_price = None
    rating = None
    review_count = None

    discount_match = re.search(r'商品價格\s*折扣價\s*([\d,]+)元', html)
    if discount_match:
        final_price = parse_int_price(discount_match.group(1))

    list_match = re.search(r'網路價\s*([\d,]+)元', html)
    if list_match:
        list_price = parse_int_price(list_match.group(1))

    json_ld_match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    if json_ld_match:
        try:
            payload = json.loads(json_ld_match.group(1))
            products = payload if isinstance(payload, list) else [payload]
            product = next((item for item in products if item.get("@type") == "Product"), None)
            if product:
                offers = product.get("offers") or {}
                final_price = final_price or parse_int_price(offers.get("price"))
                aggregate = product.get("aggregateRating") or {}
                rating = float(aggregate.get("ratingValue")) if aggregate.get("ratingValue") else None
                review_count = parse_int_price(aggregate.get("reviewCount"))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    if not final_price:
        visible_price_match = re.search(
            r'o-prodPrice__price[^>]*>\$?([\d,]+)<',
            html,
        )
        if visible_price_match:
            final_price = parse_int_price(visible_price_match.group(1))

    return {
        "final_price": final_price,
        "list_price": list_price,
        "rating": rating,
        "review_count": review_count,
    }


@st.cache_data(ttl=600, show_spinner=False)
def fetch_pchome_product_detail(product_id: str) -> dict[str, int | float | str | None]:
    try:
        fields = "Seq,Id,Price,Qty,ButtonType,SaleStatus"
        response = requests.get(
            f"{PCHOME_PRODUCT_API}&id={product_id}&fields={fields}",
            headers=REQUEST_HEADERS,
            timeout=12,
        )
        response.raise_for_status()
        variants = response.json()
        if not isinstance(variants, list):
            return {"error": "PChome 商品價格 API 格式異常"}

        prices = []
        for variant in variants:
            if variant.get("ButtonType") != "ForSale" or int(variant.get("Qty") or 0) <= 0:
                continue
            price_info = variant.get("Price") or {}
            final_price = parse_int_price(price_info.get("Low") or price_info.get("P"))
            list_price = parse_int_price(price_info.get("P") or price_info.get("M"))
            if final_price:
                prices.append(
                    {
                        "final_price": final_price,
                        "list_price": list_price or final_price,
                    }
                )

        if not prices:
            return {"error": "PChome 商品目前無可售規格"}

        return min(prices, key=lambda item: item["final_price"])
    except (requests.RequestException, ValueError) as exc:
        return {"error": str(exc)}


@st.cache_data(ttl=600, show_spinner=False)
def fetch_pchome_products(query: str, max_pages: int = 4, detail_limit: int = 24) -> pd.DataFrame:
    rows = []

    for page in range(1, max_pages + 1):
        response = requests.get(
            PCHOME_SEARCH_API,
            params={"q": query, "page": page, "sort": "sale/dc"},
            headers=REQUEST_HEADERS,
            timeout=12,
        )
        response.raise_for_status()
        products = response.json().get("prods", [])

        for item in products:
            product_id = item.get("Id")
            name = item.get("name")
            search_price = parse_int_price(item.get("price"))
            if not product_id or not name or search_price is None:
                continue
            if not is_sneaker_product(name):
                continue

            product_url = f"https://24h.pchome.com.tw/prod/{product_id}"
            origin_price = parse_int_price(item.get("originPrice")) or search_price

            rows.append(
                {
                    "平台": "PChome",
                    "商品名稱": name,
                    "價格": int(search_price),
                    "評價": 4.5,
                    "評論數": 0,
                    "賣家": "PChome 24h購物",
                    "賣家穩定度": 0.9,
                    "網路價": int(origin_price),
                    "優惠": "0%",
                    "商品連結": product_url,
                    "資料來源": "PChome 即時資料",
                    "價格說明": "搜尋頁價格",
                    "_product_id": product_id,
                }
            )

    products_df = pd.DataFrame(rows).drop_duplicates(subset=["商品連結"])
    if products_df.empty:
        return products_df

    products_df = products_df.sort_values("價格", ascending=True).reset_index(drop=True)
    for index in products_df.head(detail_limit).index:
        detail = fetch_pchome_product_detail(str(products_df.at[index, "_product_id"]))
        search_price = int(products_df.at[index, "價格"])
        origin_price = int(products_df.at[index, "網路價"])

        final_price = int(detail.get("final_price") or search_price)
        origin_price = int(detail.get("list_price") or origin_price or final_price)
        discount = round((1 - final_price / origin_price) * 100) if origin_price > final_price else 0

        products_df.at[index, "價格"] = final_price
        products_df.at[index, "網路價"] = origin_price
        products_df.at[index, "優惠"] = f"{discount}%"
        if detail.get("error"):
            products_df.at[index, "價格說明"] = "搜尋頁價格（價格 API 失敗）"
        else:
            products_df.at[index, "價格說明"] = (
                "PChome API 最低折扣價" if final_price != search_price else "PChome API 價格"
            )
        time.sleep(0.05)

    products_df["推薦分數"] = calculate_scores(products_df)
    products_df = products_df.drop(columns=["_product_id"])
    return products_df.sort_values(["價格", "推薦分數"], ascending=[True, False]).reset_index(drop=True)


@st.cache_data(ttl=600, show_spinner=False)
def fetch_momo_products(query: str, limit: int = 60) -> pd.DataFrame:
    response = requests.get(
        MOMO_SEARCH_URL.format(keyword=quote_plus(query)),
        params={"viewport": "desktop"},
        headers=REQUEST_HEADERS,
        timeout=15,
    )
    response.raise_for_status()

    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        response.text,
        flags=re.DOTALL,
    )
    if not match:
        return pd.DataFrame()

    payload = json.loads(match.group(1))
    item_lists = [
        node
        for node in payload.get("@graph", [])
        if isinstance(node, dict) and node.get("@type") == "ItemList"
    ]
    if not item_lists:
        return pd.DataFrame()

    rows = []
    for item in item_lists[0].get("itemListElement", [])[:limit]:
        offers = item.get("offers") or {}
        aggregate = item.get("aggregateRating") or {}
        price = parse_int_price(offers.get("price"))
        name = item.get("name")
        product_url = item.get("url")
        if not name or not product_url or price is None:
            continue
        if not is_sneaker_product(name):
            continue

        rows.append(
            {
                "平台": "momo購物網",
                "商品名稱": name,
                "價格": int(price),
                "評價": float(aggregate.get("ratingValue") or 4.5),
                "評論數": int(parse_int_price(aggregate.get("reviewCount")) or 0),
                "賣家": "momo購物網",
                "賣家穩定度": 0.88,
                "網路價": int(price),
                "優惠": "0%",
                "商品連結": product_url,
                "資料來源": "momo 即時搜尋資料",
                "價格說明": "momo 搜尋頁售價",
            }
        )

    products_df = pd.DataFrame(rows).drop_duplicates(subset=["商品連結"])
    if products_df.empty:
        return products_df

    products_df["推薦分數"] = calculate_scores(products_df)
    return products_df.sort_values(["價格", "推薦分數"], ascending=[True, False]).reset_index(drop=True)


def load_product_catalog() -> pd.DataFrame:
    if not PRODUCT_CATALOG_PATH.exists() or PRODUCT_CATALOG_PATH.stat().st_size == 0:
        return pd.DataFrame(columns=PRODUCT_CATALOG_COLUMNS)
    try:
        return pd.read_csv(PRODUCT_CATALOG_PATH, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=PRODUCT_CATALOG_COLUMNS)


def save_product_catalog(catalog: pd.DataFrame) -> None:
    catalog.to_csv(PRODUCT_CATALOG_PATH, index=False, encoding="utf-8-sig")


def record_product_catalog(products: pd.DataFrame) -> pd.DataFrame:
    if products.empty:
        return load_product_catalog()

    real_products = products[products["資料來源"] != "模擬資料"].copy()
    if real_products.empty:
        return load_product_catalog()

    if "鞋款類別" not in real_products.columns:
        real_products["鞋款類別"] = real_products["商品名稱"].map(detect_shoe_category)
    if "推薦分數" not in real_products.columns:
        real_products["推薦分數"] = calculate_scores(real_products)

    real_products.insert(0, "更新日期", date.today().isoformat())
    catalog = pd.concat([load_product_catalog(), real_products], ignore_index=True)
    catalog = catalog.drop_duplicates(subset=["平台", "商品連結"], keep="last")
    catalog = catalog[PRODUCT_CATALOG_COLUMNS]
    save_product_catalog(catalog)
    return catalog


def search_product_catalog(query: str) -> pd.DataFrame:
    catalog = load_product_catalog()
    if catalog.empty:
        return catalog

    matches = catalog[catalog["商品名稱"].map(lambda name: query_matches_product(query, str(name)))].copy()
    if matches.empty:
        return matches

    matches["資料來源"] = matches["資料來源"].astype(str) + "（已保存）"
    matches["價格"] = pd.to_numeric(matches["價格"], errors="coerce")
    matches["網路價"] = pd.to_numeric(matches["網路價"], errors="coerce").fillna(matches["價格"])
    matches["評價"] = pd.to_numeric(matches["評價"], errors="coerce").fillna(4.5)
    matches["評論數"] = pd.to_numeric(matches["評論數"], errors="coerce").fillna(0)
    matches["賣家穩定度"] = pd.to_numeric(matches["賣家穩定度"], errors="coerce").fillna(0.85)
    matches["推薦分數"] = calculate_scores(matches)
    return matches.drop(columns=["更新日期"], errors="ignore")


def build_products(
    query: str,
    use_live_pchome: bool,
    use_live_momo: bool,
) -> tuple[pd.DataFrame, list[str]]:
    warnings = []
    product_frames = []

    if use_live_pchome:
        try:
            pchome_products = fetch_pchome_products(query)
            if not pchome_products.empty:
                product_frames.append(pchome_products)
            else:
                warnings.append("PChome 目前沒有回傳符合的商品，已改用模擬資料。")
        except Exception as exc:
            warnings.append(f"PChome 即時資料讀取失敗，已改用模擬資料：{exc}")

    if use_live_momo:
        try:
            momo_products = fetch_momo_products(query)
            if not momo_products.empty:
                product_frames.append(momo_products)
            else:
                warnings.append("momo 目前沒有回傳符合的商品，已改用模擬資料。")
        except Exception as exc:
            warnings.append(f"momo 即時資料讀取失敗，已改用模擬資料：{exc}")

    saved_products = search_product_catalog(query)
    if not saved_products.empty:
        product_frames.append(saved_products)

    mock_platforms = []
    if not use_live_momo or not any(
        frame["平台"].eq("momo購物網").any() for frame in product_frames
    ):
        mock_platforms.append("momo購物網")
    if not use_live_pchome or not product_frames:
        mock_platforms.append("PChome")

    mock_products = build_mock_products(query, mock_platforms)
    if not mock_products.empty:
        product_frames.append(mock_products)

    products = pd.concat(product_frames, ignore_index=True)
    products["鞋款類別"] = products["商品名稱"].map(detect_shoe_category)
    products = products.drop_duplicates(subset=["平台", "商品連結"], keep="first")
    products["推薦分數"] = calculate_scores(products)
    record_product_catalog(products)
    return products.sort_values("推薦分數", ascending=False).reset_index(drop=True), warnings


def calculate_scores(products: pd.DataFrame) -> pd.Series:
    price = products["價格"].astype(float)
    rating = products["評價"].astype(float)
    reviews = products["評論數"].astype(float)
    stability = products["賣家穩定度"].astype(float)

    price_score = 1 - (price - price.min()) / max(price.max() - price.min(), 1)
    rating_score = (rating - 4.0) / 1.0
    review_score = np.log1p(reviews) / np.log1p(reviews.max())

    score = (
        price_score.clip(0, 1) * 0.42
        + rating_score.clip(0, 1) * 0.28
        + stability.clip(0, 1) * 0.2
        + review_score.clip(0, 1) * 0.1
    )
    return (score * 100).round(1)


def load_price_history() -> pd.DataFrame:
    if not PRICE_HISTORY_PATH.exists() or PRICE_HISTORY_PATH.stat().st_size == 0:
        return pd.DataFrame(columns=PRICE_HISTORY_COLUMNS)
    try:
        history = pd.read_csv(PRICE_HISTORY_PATH, encoding="utf-8-sig")
        for column in PRICE_HISTORY_COLUMNS:
            if column not in history.columns:
                history[column] = "已保存價格紀錄" if column == "資料來源" else ""
        return history[PRICE_HISTORY_COLUMNS]
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=PRICE_HISTORY_COLUMNS)


def save_price_history(history: pd.DataFrame) -> None:
    history.to_csv(PRICE_HISTORY_PATH, index=False, encoding="utf-8-sig")


def record_price_history(query: str, shoe_category: str, products: pd.DataFrame) -> pd.DataFrame:
    if products.empty:
        return load_price_history()

    snapshot = products[
        ["平台", "商品名稱", "商品連結", "價格", "資料來源"]
    ].copy()
    snapshot.insert(0, "鞋款類別", shoe_category)
    snapshot.insert(0, "搜尋關鍵字", query)
    snapshot.insert(0, "日期", date.today().isoformat())

    history = pd.concat([load_price_history(), snapshot], ignore_index=True)
    history = history.drop_duplicates(
        subset=["日期", "搜尋關鍵字", "鞋款類別", "平台", "商品連結"],
        keep="last",
    )
    history = history[PRICE_HISTORY_COLUMNS]
    save_price_history(history)
    return history


def filter_history_for_products(
    history: pd.DataFrame,
    query: str,
    shoe_category: str,
    products: pd.DataFrame,
) -> pd.DataFrame:
    if history.empty or products.empty:
        return pd.DataFrame(columns=PRICE_HISTORY_COLUMNS)

    links = set(products["商品連結"].astype(str))
    matched_history = history[
        (history["搜尋關鍵字"].astype(str) == query)
        & (history["鞋款類別"].astype(str) == shoe_category)
        & history["商品連結"].astype(str).isin(links)
    ].copy()
    if matched_history.empty:
        return matched_history

    matched_history["日期"] = pd.to_datetime(matched_history["日期"])
    cutoff_date = pd.Timestamp(date.today() - timedelta(days=90))
    matched_history = matched_history[matched_history["日期"] >= cutoff_date]
    matched_history["價格"] = pd.to_numeric(matched_history["價格"], errors="coerce")
    matched_history = matched_history.dropna(subset=["價格"])
    matched_history["商品標籤"] = (
        matched_history["平台"].astype(str)
        + "｜"
        + matched_history["商品名稱"].astype(str).str.slice(0, 28)
    )
    return matched_history.sort_values(["日期", "平台", "商品名稱"]).reset_index(drop=True)


def get_recommendation(products: pd.DataFrame, history: pd.DataFrame) -> Recommendation:
    best = products.iloc[0]
    product_history = history[
        history["商品連結"].astype(str) == str(best["商品連結"])
    ]["價格"]
    current_price = int(best["價格"])

    if len(product_history) > 1:
        avg_price = float(product_history.mean())
        gap = avg_price - current_price
        if gap > 250:
            reason = "目前價格低於此商品既有歷史平均，且評價與賣家穩定度表現佳，適合優先考慮。"
        elif gap > 0:
            reason = "目前略低於此商品既有歷史平均，可列入觀察清單，若有折扣券可考慮入手。"
        else:
            reason = "推薦分數最高，但目前價格未明顯低於此商品既有歷史平均，建議等待促銷或補貨。"
    else:
        reason = "推薦分數最高；此商品目前只有今日價格紀錄，尚未累積足夠歷史資料判斷是否低於均價。"

    return Recommendation(
        title=str(best["商品名稱"]),
        platform=str(best["平台"]),
        price=current_price,
        score=float(best["推薦分數"]),
        reason=reason,
    )


def format_price(value: int | float) -> str:
    return f"NT$ {int(value):,}"


def render_metric_cards(products: pd.DataFrame, history: pd.DataFrame) -> None:
    lowest = products.loc[products["價格"].idxmin()]
    highest_rating = products.loc[products["評價"].idxmax()]
    current_avg = products["價格"].mean()
    history_days = history["日期"].nunique() if not history.empty else 0

    col1, col2, col3 = st.columns(3)
    col1.metric("目前最低價", format_price(lowest["價格"]), lowest["平台"])
    col2.metric("最高評價商品", f"{highest_rating['評價']:.2f} / 5", highest_rating["平台"])
    col3.metric("目前搜尋均價", format_price(current_avg), f"歷史 {history_days} 天")


def render_recommendation_card(recommendation: Recommendation) -> None:
    st.markdown(
        f"""
        <div class="recommendation-card">
            <div class="eyebrow">最佳購買建議</div>
            <h3>{recommendation.title}</h3>
            <div class="recommendation-grid">
                <span>平台</span><strong>{recommendation.platform}</strong>
                <span>價格</span><strong>{format_price(recommendation.price)}</strong>
                <span>推薦分數</span><strong>{recommendation.score:.1f} / 100</strong>
            </div>
            <p>{recommendation.reason}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def apply_styles() -> None:
    st.markdown(
        """
        <style>
            .block-container {
                padding-top: 2rem;
                padding-bottom: 3rem;
                max-width: 1120px;
            }
            .hero {
                border-bottom: 1px solid #e7e9ee;
                margin-bottom: 1.25rem;
                padding-bottom: 1.25rem;
            }
            .hero h1 {
                font-size: 2.35rem;
                line-height: 1.15;
                margin-bottom: 0.5rem;
            }
            .hero p {
                color: #5b6472;
                font-size: 1.02rem;
                max-width: 820px;
            }
            .recommendation-card {
                border: 1px solid #dfe4ec;
                border-radius: 8px;
                padding: 1.1rem 1.2rem;
                background: #ffffff;
                box-shadow: 0 8px 24px rgba(35, 45, 65, 0.06);
            }
            .recommendation-card h3 {
                font-size: 1.2rem;
                margin: 0.25rem 0 0.9rem;
            }
            .recommendation-card p {
                color: #526071;
                margin: 0.95rem 0 0;
            }
            .recommendation-grid {
                display: grid;
                grid-template-columns: 88px 1fr;
                gap: 0.45rem 0.8rem;
            }
            .recommendation-grid span,
            .eyebrow {
                color: #697586;
                font-size: 0.86rem;
            }
            .eyebrow {
                font-weight: 700;
                letter-spacing: 0;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(
        page_title="智慧價格預測系統",
        page_icon="👟",
        layout="wide",
    )
    apply_styles()

    st.markdown(
        """
        <section class="hero">
            <h1>智慧價格預測系統</h1>
            <p>
                以球鞋為範例，整合跨平台比價、歷史價格分析與推薦分數。
                目前版本使用假資料建立完整介面流程，後續可替換為 Selenium、
                BeautifulSoup、SQLite 與 ARIMA 模型。
            </p>
        </section>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.header("搜尋條件")
        query = normalize_query(
            st.text_input("球鞋名稱", value="", placeholder="例如 Kobe、Jordan、Dunk")
        )
        shoe_category = st.radio("鞋款類別", SHOE_CATEGORIES, horizontal=True)
        platforms = st.multiselect("平台", PLATFORMS, default=PLATFORMS)
        max_price = st.slider("最高價格", min_value=500, max_value=50000, value=50000, step=500)
        search = st.button("搜尋商品", type="primary", use_container_width=True)

    use_live_pchome = st.sidebar.checkbox("使用 PChome 即時商品資料", value=True)
    use_live_momo = st.sidebar.checkbox("使用 momo 即時搜尋資料", value=True)

    catalog_count = len(load_product_catalog())
    if not query:
        st.subheader("請輸入球鞋名稱")
        if catalog_count:
            st.caption(f"已累積 {catalog_count} 筆實際球鞋商品資料，可供搜尋使用。")
        st.info("請在左側輸入關鍵字，例如 Kobe、Jordan、Dunk，然後按下搜尋商品。")
        return

    products, data_warnings = build_products(query, use_live_pchome, use_live_momo)

    products = products[
        products["平台"].isin(platforms)
        & (products["價格"] <= max_price)
        & products["商品名稱"].map(lambda name: query_matches_product(query, str(name)))
        & products["商品名稱"].map(lambda name: category_matches_product(shoe_category, str(name)))
    ].reset_index(drop=True)

    st.subheader(f"{query} 搜尋結果")
    for data_warning in data_warnings:
        st.warning(data_warning)
    active_sources = []
    if use_live_pchome:
        active_sources.append("PChome 商品 API")
    if use_live_momo:
        active_sources.append("momo 搜尋頁 JSON")
    if active_sources:
        st.info(f"目前即時資料來源：{'、'.join(active_sources)}。")
    if catalog_count:
        st.caption(f"已累積 {catalog_count} 筆實際球鞋商品資料，可供後續搜尋使用。")
    if products.empty:
        st.warning("目前篩選條件沒有符合的商品，請調整平台或最高價格。")
        return

    full_history = record_price_history(query, shoe_category, products)
    history = filter_history_for_products(full_history, query, shoe_category, products)

    if search:
        st.toast(f"已更新 {query} 的比價結果")

    render_metric_cards(products, history)

    st.divider()

    table_col, card_col = st.columns([2.2, 1])
    with table_col:
        st.subheader("跨平台比價")
        display_products = products.sort_values("價格", ascending=True).reset_index(drop=True).copy()
        display_products.insert(0, "價格排名", range(1, len(display_products) + 1))
        display_products["價格"] = display_products["價格"].map(format_price)
        display_products["網路價"] = display_products["網路價"].map(format_price)
        display_products["推薦分數"] = display_products["推薦分數"].map(lambda value: f"{value:.1f}")
        st.dataframe(
            display_products[
                [
                    "價格排名",
                    "平台",
                    "鞋款類別",
                    "商品名稱",
                    "價格",
                    "網路價",
                    "評價",
                    "評論數",
                    "賣家",
                    "賣家穩定度",
                    "優惠",
                    "推薦分數",
                    "價格說明",
                    "資料來源",
                    "商品連結",
                ]
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "商品連結": st.column_config.LinkColumn(
                    "商品連結",
                    display_text="開啟商品/搜尋",
                ),
                "賣家穩定度": st.column_config.ProgressColumn(
                    "賣家穩定度",
                    min_value=0,
                    max_value=1,
                    format="%.2f",
                ),
            },
        )

    with card_col:
        render_recommendation_card(get_recommendation(products, history))

    st.divider()

    st.subheader("近 3 個月價格歷史走勢")
    if history.empty:
        st.info("近 3 個月內還沒有這次搜尋商品的價格紀錄。按下搜尋後，系統會保存當天找到的商品價格。")
    else:
        history_days = history["日期"].nunique()
        history_products = history["商品連結"].nunique()
        st.caption(
            f"這裡只使用近 3 個月內你實際搜尋到的商品價格。現在共有 {history_days} 天、"
            f"{history_products} 個商品的紀錄。"
        )
        chart_start = pd.Timestamp(date.today() - timedelta(days=90))
        chart_end = pd.Timestamp(date.today())
        chart_history = (
            history.sort_values("價格")
            .groupby(["日期", "平台"], as_index=False)
            .first()
            .rename(columns={"價格": "每日最低價"})
        )
        base_chart = alt.Chart(chart_history).encode(
            x=alt.X(
                "日期:T",
                title="日期",
                scale=alt.Scale(domain=[chart_start, chart_end]),
                axis=alt.Axis(format="%m/%d", labelAngle=0, grid=False),
            ),
            y=alt.Y(
                "每日最低價:Q",
                title="每日最低價",
                scale=alt.Scale(zero=False),
                axis=alt.Axis(grid=True, gridDash=[2, 2]),
            ),
            color=alt.Color(
                "平台:N",
                title="平台",
                scale=alt.Scale(
                    domain=["PChome", "momo購物網"],
                    range=["#0b8bdc", "#15b8a6"],
                ),
            ),
        )
        line_chart = base_chart.mark_line(strokeWidth=2).encode(
            tooltip=[
                alt.Tooltip("日期:T", title="日期", format="%Y-%m-%d"),
                alt.Tooltip("平台:N", title="平台"),
                alt.Tooltip("商品名稱:N", title="商品名稱"),
                alt.Tooltip("每日最低價:Q", title="每日最低價", format=",.0f"),
            ],
        )
        point_chart = base_chart.mark_circle(size=110, opacity=1).encode(
            tooltip=[
                alt.Tooltip("日期:T", title="日期", format="%Y-%m-%d"),
                alt.Tooltip("平台:N", title="平台"),
                alt.Tooltip("商品名稱:N", title="商品名稱"),
                alt.Tooltip("每日最低價:Q", title="每日最低價", format=",.0f"),
            ],
        )
        label_chart = base_chart.mark_text(
            align="center",
            baseline="bottom",
            dy=-10,
            fontSize=12,
            fontWeight="bold",
        ).encode(
            text=alt.Text("每日最低價:Q", format=",.0f"),
        )
        styled_chart = (
            (line_chart + point_chart + label_chart)
            .properties(height=360)
            .configure_view(stroke=None)
        )
        st.altair_chart(styled_chart, use_container_width=True)
        if history_days < 2:
            st.caption("目前只有 1 天價格紀錄，所以圖上會先看到單日價格點；累積到第 2 天後會連成折線。")

        st.subheader("近 3 個月歷史價格明細")
        history_detail = history.sort_values(["日期", "平台", "價格"]).copy()
        history_detail["日期"] = history_detail["日期"].dt.strftime("%Y-%m-%d")
        history_detail["價格"] = history_detail["價格"].map(format_price)
        st.dataframe(
            history_detail[
                [
                    "日期",
                    "平台",
                    "鞋款類別",
                    "商品名稱",
                    "價格",
                    "資料來源",
                    "商品連結",
                ]
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "商品連結": st.column_config.LinkColumn(
                    "商品連結",
                    display_text="開啟商品",
                ),
            },
        )

    trend_col, timing_col = st.columns(2)
    with trend_col:
        st.subheader("近 3 個月摘要")
        if history.empty:
            st.write("尚無價格紀錄。")
        else:
            min_row = history.loc[history["價格"].idxmin()]
            max_row = history.loc[history["價格"].idxmax()]
            st.write(f"最低紀錄：{min_row['平台']}，{format_price(min_row['價格'])}，{min_row['日期'].date()}")
            st.write(f"最高紀錄：{max_row['平台']}，{format_price(max_row['價格'])}，{max_row['日期'].date()}")

    with timing_col:
        st.subheader("下一階段模組")
        st.write("爬蟲：Selenium + BeautifulSoup 蒐集平台商品資料")
        st.write("資料庫：SQLite 儲存每日歷史價格")
        st.write("預測：statsmodels ARIMA 產生未來價格區間")


if __name__ == "__main__":
    main()
