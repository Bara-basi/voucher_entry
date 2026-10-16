"""Read-only Yonyou acquisitions using verified request recipes."""
import argparse
import copy
import json
from datetime import datetime
from pathlib import Path
from read_feishu import config

HOME = Path(__file__).resolve().parent.parent


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def request_json(context, recipe, payload):
    response = context.request.fetch(recipe['url'], method=recipe.get('method', 'POST'), data=payload, timeout=60000)
    if not response.ok:
        raise RuntimeError('Read request HTTP status ' + str(response.status))
    try:
        body = response.json()
    except Exception:
        raise RuntimeError('Expected JSON; session may have expired') from None
    if not isinstance(body, dict) or body.get('code') not in (0, 200) or 'data' not in body:
        raise RuntimeError('Platform rejected read request or returned an unexpected structure')
    return body['data']


def flatten(rows):
    result = []
    for row in rows:
        result.append({k: v for k, v in row.items() if k != 'children'})
        result.extend(flatten(row.get('children') or []))
    return result


def paged_records(context, recipe, *, tree=False, key=None, refresh_dates=False):
    payload = copy.deepcopy(recipe['payload'])
    payload.pop('queryId', None)
    payload.pop('isOnlyCount', None)
    if refresh_dates and isinstance(payload.get('data'), str):
        data = json.loads(payload['data'])
        for name in ('makeTime', 'maketime'):
            if name in data:
                data[name] = datetime.now().date().isoformat()
        payload['data'] = json.dumps(data, ensure_ascii=False)
    records, pages, total = [], [], None
    while total is None or len(records) < total:
        payload['page'] = {'pageSize': 1000, 'pageIndex': len(pages) + 1, 'totalCount': total or 0}
        data = request_json(context, recipe, payload)
        if not isinstance(data, dict) or 'recordList' not in data or 'recordCount' not in data:
            raise RuntimeError('Unexpected paged response structure')
        current_total = int(data['recordCount'])
        if total is not None and current_total != total:
            raise RuntimeError('Record count changed during acquisition; rerun')
        total = current_total
        batch = flatten(data['recordList']) if tree else data['recordList']
        if total and not batch:
            raise RuntimeError('Empty page before acquisition was complete')
        records.extend(batch)
        pages.append(len(batch))
        if len(records) > total or len(pages) > 10000:
            raise RuntimeError('Record count or pagination mismatch')
        if not total:
            break
    if len(records) != total:
        raise RuntimeError('Incomplete acquisition')
    if key and (any(key not in r for r in records) or len({r[key] for r in records}) != total):
        raise RuntimeError('Duplicate or missing dataset key: ' + key)
    return {'records': records, 'record_count': total, 'pages': pages, 'query': payload}


def fetch_mappings(context, output):
    recipes = load(HOME / 'mappings/platform/已验证接口及请求参数.json')
    counts = {}
    for filename, recipe in recipes['plans'].items():
        data = paged_records(context, recipe, tree=recipe.get('tree', False), key=recipe.get('key'), refresh_dates=True)
        counts[filename] = data['record_count']
        save(output / filename, {'exported_at': datetime.now().astimezone().isoformat(), 'scope': recipes['scope'], 'verification': {'reported_total': data['record_count'], 'exported_records': len(data['records']), 'complete_in_reported_scope': True, 'key_field': recipe.get('key')}, 'records': data['records']})
        print(filename + ': ' + str(data['record_count']), flush=True)
    configuration = []
    for recipe in recipes.get('configuration_reads', []):
        configuration.append({'url': recipe['url'], 'data': request_json(context, recipe, recipe.get('payload'))})
    save(output / 'configuration_reads.json', {'exported_at': datetime.now().astimezone().isoformat(), 'reads': configuration})
    save(output / 'refresh_complete.json', {'completed_at': datetime.now().astimezone().isoformat(), 'counts': counts, 'configuration_reads': len(configuration)})
    return counts


def fetch_vouchers(context, output, contract=None, period=None):
    recipe = copy.deepcopy(load(HOME / 'config/voucher_query.json'))
    for item in recipe['payload']['condition']['commonVOs']:
        if item['itemName'] == 'bodydescription' and contract is not None:
            item['value1'] = contract
        if item['itemName'] == 'rangeperiod' and period is not None:
            item['value1'] = period
    data = paged_records(context, recipe)
    identities = [(r.get('id'), r.get('bodyid')) for r in data['records']]
    if identities and all(k[1] is not None for k in identities) and len(set(identities)) != len(identities):
        raise RuntimeError('Duplicate voucher entry IDs')
    data['voucher_count'] = len({r['id'] for r in data['records']})
    data['fetched_at'] = datetime.now().astimezone().isoformat()
    data['scope'] = recipe.get('scope', {})
    if contract is not None:
        data['scope']['summary_contains'] = contract
    if period is not None:
        data['scope']['period'] = period
    save(output / 'actual_vouchers.json', data)
    return {'records': data['record_count'], 'vouchers': data['voucher_count']}


def login(page, credentials):
    page.goto(credentials['YONG_YOU_URL'], wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(1500)
    accept = page.get_by_role('button', name='接受', exact=True)
    if accept.count() and accept.first.is_visible():
        accept.first.click()
    frame = None
    for _ in range(60):
        frame = next((f for f in page.frames if f.locator('#username').count()), None)
        if frame is not None:
            break
        page.wait_for_timeout(500)
    if frame is None:
        raise RuntimeError('Login fields not found; inspect the current login flow')
    frame.locator('#username').fill(credentials['YONG_YOU_NAME'])
    frame.locator('#password').fill(credentials['YONG_YOU_PASSWORD'])
    frame.locator('#submit_btn_login').click()
    page.locator('input[placeholder="人员/服务等"]:visible').wait_for(timeout=60000)
    page.wait_for_timeout(1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['mappings', 'vouchers', 'all'], default='all')
    parser.add_argument('--output', type=Path, default=HOME / 'data/yonyou_refresh')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--contract', help='Replace voucher summary filter')
    parser.add_argument('--period', help='For example: 2026-01,2026-09,0')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    credentials = config()
    output = args.output.resolve() / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    output.mkdir(parents=True, exist_ok=False)
    with sync_playwright() as playwright:
        chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        options = {'headless': not args.headed}
        if chrome.exists():
            options['executable_path'] = str(chrome)
        browser = playwright.chromium.launch(**options)
        try:
            with browser.new_context(locale='zh-CN', viewport={'width': 1600, 'height': 1000}) as context:
                login(context.new_page(), credentials)
                if args.kind in ('mappings', 'all'):
                    fetch_mappings(context, output / 'mappings')
                if args.kind in ('vouchers', 'all'):
                    print(json.dumps(fetch_vouchers(context, output / 'vouchers', args.contract, args.period), ensure_ascii=False))
        finally:
            browser.close()
    print('Saved to: ' + str(output))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Avoid printing server content or typed login credentials.
        print('Acquisition failed (' + type(exc).__name__ + '). Check login, permissions and request recipes. No completion is claimed.')
        raise SystemExit(1)
