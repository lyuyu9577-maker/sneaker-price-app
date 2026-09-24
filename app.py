from __future__ import annotations

import json
import re
import time
import unicodedata
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
    return unicodedata.normalize("NFKC", query).strip()


def search_tokens(value: str) -> list[str]:
    value = normalize_query(value).lower()
    for source, target in {
        "喬丹": " jordan ", "乔丹": " jordan ", "科比": " kobe ",
        "耐吉": " nike ", "愛迪達": " adidas ", "阿迪達斯": " adidas ",
        "紐巴倫": " new balance ", "紐百倫": " new balance ",
    }.items():
        value = value.replace(source, target)
    value = re.sub(r"\ball[\s-]*stars?\b", "allstar", value)
    value = re.sub(r"\blow[\s-]*top\b", "lowtop", value)
    value = re.sub(r"\bhigh[\s-]*top\b", "hightop", value)
    value = re.sub(r"\baj(?=\s*\d)", "jordan ", value)
    value = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", value)
    # Keep model numbers as complete tokens: 1 must not match 11 or 100.
    return re.findall(r"[a-z]+|\d+(?:\.\d+)?|[\u4e00-\u9fff]+", value)

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
    """Match complete terms, contextual generations, cuts and exact style codes."""
    if not is_sneaker_product(product_name):
        return False
    def prepare(value: str) -> str:
        value = normalize_query(value).lower()
        for pattern, replacement in [
            (r"低筒|低幫|低帮|\blow[\s-]*top\b", " low "),
            (r"中筒|中幫|中帮|\bmid[\s-]*top\b", " mid "),
            (r"高筒|高幫|高帮|\bhigh[\s-]*top\b", " high "),
        ]:
            value = re.sub(pattern, replacement, value)
        return value

    query_text, product_text = prepare(query), prepare(product_name)
    # Keep a complete style code together instead of matching its pieces elsewhere.
    sku_pattern = r"(?<![a-z0-9])(?:[a-z]{1,4}\d{3,7}|\d{5,7})-\d{3}(?![a-z0-9])"
    for code in re.findall(sku_pattern, query_text):
        if not re.search(r"(?<![a-z0-9])" + re.escape(code) + r"(?![a-z0-9])", product_text):
            return False
        query_text = query_text.replace(code, " ")
    product_text = re.sub(sku_pattern, " ", product_text)

    ignored = {"鞋", "球鞋", "運動鞋", "休閒鞋", "籃球鞋", "男鞋", "女鞋", "童鞋",
               "sneaker", "sneakers", "shoe", "shoes", "款", "代"}
    query_parts = [part for part in search_tokens(query_text) if part not in ignored]
    product_parts = search_tokens(product_text)
    roman_numbers = {expand_number_aliases([str(n)])[-1][0]: str(n) for n in range(1, 21)}
    generation_models = {"jordan", "kobe", "lebron", "kd", "kyrie", "curry",
                         "ja", "tatum", "giannis", "harden", "luka", "zion", "book",
                         "dame", "wade", "sabrina", "ae"}
    def generation(token: str) -> str:
        return roman_numbers.get(token, token)
    def same(left: str, right: str) -> bool:
        if re.fullmatch(r"[\u4e00-\u9fff]+", left):
            return left in right
        return left == right

    index = 0
    while index < len(query_parts):
        part = query_parts[index]
        if (index + 1 < len(query_parts) and re.fullmatch(r"[a-z]+", part)
                and (query_parts[index + 1].isdigit()
                     or (part in generation_models and query_parts[index + 1] in roman_numbers))):
            wanted = query_parts[index + 1]
            def number_matches(token: str) -> bool:
                return (generation(token) == generation(wanted)
                        if part in generation_models else token == wanted)
            positions = [i for i in range(len(product_parts) - 1)
                         if product_parts[i] == part and number_matches(product_parts[i + 1])]
            if not positions:
                return False
            if part in generation_models:
                # Reject both "Kobe 6 / Kobe 8" and "Kobe 6/8" multi-model listings.
                for i, token in enumerate(product_parts[:-1]):
                    if token != part:
                        continue
                    following = product_parts[i + 1]
                    if (following.isdigit() or following in roman_numbers) and not number_matches(following):
                        return False
                for i in positions:
                    j = i + 2
                    while j < len(product_parts) and (product_parts[j].isdigit() or product_parts[j] in roman_numbers):
                        if not number_matches(product_parts[j]):
                            return False
                        j += 1
            index += 2
        else:
            if not any(same(part, token) for token in product_parts):
                return False
            index += 1
    # Punctuation-only input must not match the entire catalog.
    return bool(search_tokens(query))


def detect_shoe_category(product_name: str) -> str:
    lowered = normalize_query(product_name).lower()
    english = set(re.findall(r"[a-z]+(?:'[a-z]+)?", lowered))
    has_kids = any(word in lowered for word in ["童鞋", "兒童", "小童", "中童", "大童"]) or bool(
        english & {"kids", "kid", "youth", "gs", "ps", "td"})
    has_women = any(word in lowered for word in ["女鞋", "女款", "女子", "女性"]) or bool(
        english & {"women", "womens", "women's", "wmns"})
    has_men = any(word in lowered for word in ["男鞋", "男款", "男子", "男性"]) or bool(
        english & {"men", "mens", "men's"})
    unisex = "男女" in lowered or "unisex" in english
    if has_kids:
        return "童鞋"
    if unisex or (has_women and has_men):
        return "男女通用"
    if has_women:
        return "女鞋"
    if has_men:
        return "男鞋"
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
    name = normalize_query(name).lower()
    excluded = ["襪", "背包", "包包", "球衣", "上衣", "外套", "褲", "帽", "吊飾", "鑰匙圈",
                "鞋帶", "鞋墊", "鞋盒", "鞋櫃", "鞋架", "鞋袋", "鞋撐", "鞋拔", "鞋扣", "鞋刷",
                "鞋用", "清潔", "清洗", "洗鞋", "除臭", "保養", "修補", "防水噴", "模型", "公仔",
                "拖鞋", "涼鞋", "皮鞋", "高跟鞋", "樂福鞋", "瑪莉珍", "豆豆鞋", "雨鞋", "靴",
                "多款", "任選", "隨機", "混款", "多型號"]
    if any(word in name for word in excluded):
        return False
    if re.search(r"\b(socks?|laces?|insoles?|shoelaces?|cleaner|slippers?|sandals?|boots?|jerseys?|hoodies?|backpacks?|keychains?|shoehorns?|shirts?|t-shirts?|caps?|toys?)\b", name):
        return False
    if re.search(r"\bshoe\s+(box|rack|bag|tree|care|charm)s?\b", name):
        return False
    tokens = search_tokens(name)
    families = set(tokens) & {"kobe", "dunk", "samba", "sambae", "lebron", "ja", "kyrie", "kd", "curry", "tatum"}
    if any(left == "jordan" and (right.isdigit() or right in {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii", "xiii", "xiv"})
           for left, right in zip(tokens, tokens[1:])):
        families.add("jordan")
    if any(left == "air" and right == "force" for left, right in zip(tokens, tokens[1:])):
        families.add("airforce")
    # Reject titles stuffing multiple different shoe families into one listing.
    if len(families) > 1:
        return False
    return "鞋" in name or bool(re.search(r"\b(sneakers?|shoes?|trainers?)\b", name))

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
            if not product_id or not name or search_price is None or search_price <= 0:
                continue
            if not query_matches_product(query, name):
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

    if not rows:
        return pd.DataFrame(columns=PRODUCT_CATALOG_COLUMNS)
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
        if not name or not product_url or price is None or price <= 0:
            continue
        if not query_matches_product(query, name):
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

    if not rows:
        return pd.DataFrame(columns=PRODUCT_CATALOG_COLUMNS)
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
    for enabled, platform, fetch in [
        (use_live_pchome, "PChome", fetch_pchome_products),
        (use_live_momo, "momo", fetch_momo_products),
    ]:
        if not enabled:
            continue
        try:
            found = fetch(query)
            if not found.empty:
                product_frames.append(found)
            else:
                warnings.append(f"{platform} 未回傳商品；若有已保存的符合商品，會標示資料來源。")
        except Exception:
            warnings.append(f"{platform} 即時資料暫時無法取得；已保存價格不代表最新售價。")
    saved_products = search_product_catalog(query)
    if not saved_products.empty:
        product_frames.append(saved_products)
    if not product_frames:
        return pd.DataFrame(columns=PRODUCT_CATALOG_COLUMNS), warnings
    products = pd.concat(product_frames, ignore_index=True)
    products["價格"] = pd.to_numeric(products["價格"], errors="coerce")
    products = products[
        products["價格"].gt(0)
        & ~products["資料來源"].astype(str).str.contains("模擬", na=False)
        & products["商品名稱"].map(lambda name: query_matches_product(query, str(name)))
    ].copy()
    if products.empty:
        return pd.DataFrame(columns=PRODUCT_CATALOG_COLUMNS), warnings
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
    review_score = np.log1p(reviews.clip(lower=0)) / max(float(np.log1p(reviews.max())), 1.0)

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
    from tracking import render_dashboard
    render_dashboard()


if __name__ == "__main__":
    main()
