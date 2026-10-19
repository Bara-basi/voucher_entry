"""Read-only Yonyou acquisitions using verified request recipes."""
import argparse
import copy
import json
import time
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


def request_json(context, recipe, payload, timeout_ms=120000):
    response = context.request.fetch(recipe['url'], method=recipe.get('method', 'POST'),
                                     data=payload, timeout=timeout_ms)
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


def acquire_once(context, recipe, *, tree=False, key=None, refresh_dates=False, verbose=False,
                 page_size=500, timeout_ms=120000, page_attempts=3):
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
        # totalCount is a "records already read" hint on this endpoint: sending the running
        # total makes the server return a remainder instead of the full count, so keep it at 0.
        payload['page'] = {'pageSize': page_size, 'pageIndex': len(pages) + 1, 'totalCount': 0}
        for page_attempt in range(1, page_attempts + 1):
            try:
                data = request_json(context, recipe, payload, timeout_ms=timeout_ms)
                break
            except RuntimeError as exc:
                if page_attempt == page_attempts:
                    raise
                print('   page ' + str(len(pages) + 1) + ' attempt ' + str(page_attempt)
                      + ' failed (' + str(exc) + '); retrying', flush=True)
                time.sleep(3)
        if not isinstance(data, dict) or 'recordList' not in data or 'recordCount' not in data:
            raise RuntimeError('Unexpected paged response structure')
        current_total = int(data['recordCount'])
        if verbose:
            print('   page ' + str(len(pages) + 1) + ': recordCount=' + str(current_total)
                  + ', running_total=' + str(len(records)), flush=True)
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


def paged_records(context, recipe, *, tree=False, key=None, refresh_dates=False,
                  attempts=4, retry_seconds=5, verbose=False, page_size=500, timeout_ms=120000):
    """Retry the whole acquisition when the live dataset shifts between pages."""
    for attempt in range(1, attempts + 1):
        try:
            return acquire_once(context, recipe, tree=tree, key=key,
                                refresh_dates=refresh_dates, verbose=verbose,
                                page_size=page_size, timeout_ms=timeout_ms)
        except RuntimeError as exc:
            if 'Record count changed' not in str(exc) or attempt == attempts:
                raise
            print('acquisition attempt ' + str(attempt) + ' aborted ('
                  + str(exc) + '); retrying in ' + str(retry_seconds) + 's', flush=True)
            time.sleep(retry_seconds)
    raise RuntimeError('Unreachable')


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


def fetch_vouchers(context, output, contract=None, period=None, maker=None, retries=4, verbose=False,
                   page_size=500, timeout_ms=120000):
    recipe = copy.deepcopy(load(HOME / 'config/voucher_query.json'))
    for item in recipe['payload']['condition']['commonVOs']:
        if item['itemName'] == 'bodydescription' and contract:
            item['value1'] = contract
        if item['itemName'] == 'rangeperiod' and period is not None:
            item['value1'] = period
    if contract == 'all':
        recipe['payload']['condition']['commonVOs'] = [
            item for item in recipe['payload']['condition']['commonVOs'] if item['itemName'] != 'bodydescription']
    if maker == 'all':
        recipe['payload']['condition']['commonVOs'] = [
            item for item in recipe['payload']['condition']['commonVOs'] if item['itemName'] != 'maker']
    elif maker:
        for item in recipe['payload']['condition']['commonVOs']:
            if item['itemName'] == 'maker':
                item['value1'] = [maker]
    data = paged_records(context, recipe, attempts=retries, verbose=verbose,
                         page_size=page_size, timeout_ms=timeout_ms)
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
    if maker:
        data['scope']['maker'] = maker
    save(output / 'actual_vouchers.json', data)
    return {'records': data['record_count'], 'vouchers': data['voucher_count']}


def dismiss_cookie_banner(page):
    """The privacy banner overlays the login iframe; click it away before typing."""
    for _ in range(3):
        clicked = False
        for locator in (page.get_by_role('button', name='接受', exact=True),
                        page.get_by_text('接受', exact=True)):
            if not locator.count():
                continue
            try:
                locator.first.click(timeout=5000)
                clicked = True
            except Exception:
                try:
                    locator.first.dispatch_event('click')
                    clicked = True
                except Exception:
                    clicked = False
            if clicked:
                break
        page.wait_for_timeout(800)
        if not clicked:
            return


def login(page, credentials):
    page.goto(credentials['YONG_YOU_URL'], wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(1500)
    dismiss_cookie_banner(page)
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
    try:
        frame.locator('#submit_btn_login').click(timeout=20000)
    except Exception:
        frame.eval_on_selector('#submit_btn_login', 'el => el.click()')
    page.locator('input[placeholder="人员/服务等"]:visible').wait_for(timeout=60000)
    page.wait_for_timeout(1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['mappings', 'vouchers', 'all'], default='all')
    parser.add_argument('--output', type=Path, default=HOME / 'data/yonyou_refresh')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--contract', help="Replace voucher summary filter; 'all' removes it")
    parser.add_argument('--period', help='For example: 2026-01,2026-09,0')
    parser.add_argument('--maker', help="Replace voucher maker filter; 'all' removes it")
    parser.add_argument('--retries', type=int, default=4, help='Retry count when the live dataset shifts')
    parser.add_argument('--page-size', type=int, default=500)
    parser.add_argument('--timeout-ms', type=int, default=120000, help='Per-request timeout in milliseconds')
    parser.add_argument('--verbose', action='store_true')
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
                    print(json.dumps(fetch_vouchers(context, output / 'vouchers', args.contract,
                                                    args.period, args.maker, args.retries,
                                                    args.verbose, args.page_size,
                                                    args.timeout_ms), ensure_ascii=False))
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
