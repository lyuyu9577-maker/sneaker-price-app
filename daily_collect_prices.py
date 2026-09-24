from __future__ import annotations

import pandas as pd

import app


KEYWORDS = [
    "Jordan",
    "Kobe",
    "Dunk",
    "Air Force 1",
    "New Balance",
    "Adidas Samba",
    "Nike Basketball",
    "Running Shoes",
]


def collect_keyword(keyword: str) -> tuple[int, int]:
    frames = []

    try:
        pchome_products = app.fetch_pchome_products(keyword, max_pages=4, detail_limit=24)
        if not pchome_products.empty:
            frames.append(pchome_products)
    except Exception as exc:
        print(f"PChome failed for {keyword}: {exc}")

    try:
        momo_products = app.fetch_momo_products(keyword, limit=60)
        if not momo_products.empty:
            frames.append(momo_products)
    except Exception as exc:
        print(f"momo failed for {keyword}: {exc}")

    if not frames:
        return 0, 0

    products = pd.concat(frames, ignore_index=True)
    products["鞋款類別"] = products["商品名稱"].map(app.detect_shoe_category)
    products = products.drop_duplicates(subset=["平台", "商品連結"], keep="first")
    products["推薦分數"] = app.calculate_scores(products)

    catalog = app.record_product_catalog(products)

    recorded_rows = 0
    for shoe_category in app.SHOE_CATEGORIES:
        category_products = products[
            products["商品名稱"].map(
                lambda name: app.category_matches_product(shoe_category, str(name))
            )
        ].copy()
        if category_products.empty:
            continue
        history = app.record_price_history(keyword, shoe_category, category_products)
        recorded_rows += len(
            app.filter_history_for_products(history, keyword, shoe_category, category_products)
        )

    return len(catalog), recorded_rows


def main() -> None:
    total_recorded = 0
    catalog_size = 0
    for keyword in KEYWORDS:
        print(f"Collecting {keyword}...")
        catalog_size, recorded_rows = collect_keyword(keyword)
        total_recorded += recorded_rows
        print(f"  recorded rows in current 90-day window: {recorded_rows}")

    print(f"Catalog size: {catalog_size}")
    print(f"Total current-window history rows touched: {total_recorded}")


if __name__ == "__main__":
    main()
