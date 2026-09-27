"""Durable, bounded product tracking backed by repository data."""
import json
import re
from pathlib import Path
from urllib.parse import urlencode

MAX_TRACKED = 20
WATCHLIST = Path(__file__).with_name('tracked_products.json')
REPO = 'https://github.com/lyuyu9577-maker/sneaker-price-app'


def validate_request(value):
    platform, pid, query = (value.get(k) for k in ('platform', 'product_id', 'query'))
    if platform not in ('PChome', 'momo購物網'):
        raise ValueError('不支援的平台')
    pattern = r'[A-Z0-9]{6}-[A-Z0-9]+' if platform == 'PChome' else r'[0-9]{1,20}'
    if not isinstance(pid, str) or not re.fullmatch(pattern, pid):
        raise ValueError('商品編號格式不正確')
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
        raise ValueError('搜尋名稱需為 1–100 字')
    return {'platform': platform, 'product_id': pid, 'query': query.strip()}


def load_watchlist(path=WATCHLIST):
    rows = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    if not isinstance(rows, list) or len(rows) > MAX_TRACKED:
        raise ValueError('追蹤清單格式或數量異常')
    for row in rows:
        validate_request(row)
    return rows


def request_url(platform, pid, query):
    payload = validate_request(dict(platform=platform, product_id=pid, query=query))
    return REPO + '/issues/new?' + urlencode({
        'title': '[追蹤申請] ' + platform + ' ' + pid,
        'body': json.dumps(payload, ensure_ascii=False, indent=2)})


def collect_fixed(target):
    from tracking import collect_platform
    # Search results may change order. Only this exact platform/product may be saved.
    rows, status = collect_platform(target['query'], target['platform'])
    rows = [r for r in rows if r['product_id'] == target['product_id']]
    if not rows:
        rows, status = collect_platform(target['product_id'], target['platform'],
                                        exact_product_id=target['product_id'])
        rows = [r for r in rows if r['product_id'] == target['product_id']]
        for row in rows:
            row['query'] = target['query']
    status['query'] = target['query']
    status.update(product_id=target['product_id'], count=len(rows),
                  status='ok' if rows else 'no_matches',
                  message='' if rows else '本次未找到指定商品；不沿用舊價格或替換其他商品')
    return rows, status


def register(value, path=WATCHLIST):
    from tracking import now_tw, save_observations
    target = validate_request(value)
    rows = load_watchlist(path)
    if any((r['platform'], r['product_id']) == (target['platform'], target['product_id']) for r in rows):
        return '已在追蹤清單中'
    if len(rows) >= MAX_TRACKED:
        raise ValueError(f'追蹤清單已滿（{MAX_TRACKED} 件）')
    observations, _ = collect_fixed(target)
    if not observations:
        raise ValueError('未取得指定商品的有效價格，未加入追蹤；請重新搜尋後再申請')
    target.update(title=observations[-1]['title'], url=observations[-1]['url'],
                  added_at=now_tw().isoformat(timespec='seconds'))
    rows.append(target)
    save_observations(observations)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return '已加入每日追蹤'


if __name__ == '__main__':
    import os
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    issue = event.get('issue', {})
    if issue and not issue.get('title', '').startswith('[追蹤申請] '):
        raise SystemExit(0)
    value = json.loads(issue['body']) if issue else event['inputs']
    print(register(value))
