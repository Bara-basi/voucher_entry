"""Interactive Python Playwright session; no cookies or credentials saved to disk."""
import sys,json,queue,re,traceback,runpy
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
# Install playwright through requirements.txt; no dependency on old workspace.
from playwright.sync_api import sync_playwright
from read_feishu import config
ROOT=Path(__file__).resolve().parent
HOME=ROOT.parent
DATA=HOME/'data'
DATA.mkdir(exist_ok=True)
C=config();events=[];pending=[];downloads=[]
SECRET=re.compile(r'password|passwd|secret|authorization|access.?token|refresh.?token|cookie',re.I)
def clean(value):
    if isinstance(value,dict):return {k:('[REDACTED]' if SECRET.search(k) else clean(v)) for k,v in value.items()}
    if isinstance(value,list):return [clean(x) for x in value]
    if isinstance(value,str):
        for k in ['YONG_YOU_PASSWORD','YONG_YOU_NAME','LARK_APP_SECRET']:
            if C.get(k):value=value.replace(C[k],'[REDACTED]')
    return value
def safe_url(url):
    p=urlsplit(url);return urlunsplit((p.scheme,p.netloc,p.path,urlencode([(k,'[REDACTED]' if SECRET.search(k) else v) for k,v in parse_qsl(p.query,keep_blank_values=True)]),''))
def response(r):
    if r.request.resource_type in ['xhr','fetch'] and 'json' in r.headers.get('content-type','').lower():pending.append(r)
def collect():
    while pending:
        r=pending.pop(0)
        if re.search(r'login|auth|token|captcha',urlsplit(r.url).path,re.I):continue
        try:
            body=r.json();request=r.request
            try:payload=request.post_data_json
            except Exception:payload=request.post_data
            events.append({'url':safe_url(r.url),'method':request.method,'request':clean(payload),'status':r.status,'body':clean(body)})
        except Exception:pass
def snapshot(page):
    return [{'frame':i,'url':safe_url(f.url),'text':clean(f.locator('body').inner_text(timeout=5000)[-12000:]),'inputs':f.locator('input').evaluate_all('(els)=>els.filter(e=>e.type!=="hidden"&&e.offsetParent).map(e=>({type:e.type,placeholder:e.placeholder,id:e.id,name:e.name}))'),'buttons':f.locator('button:visible').all_text_contents()} for i,f in enumerate(page.frames)]
with sync_playwright() as p:
    chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    options={'headless':'--headed' not in sys.argv}
    if chrome.exists():options['executable_path']=str(chrome)
    browser=p.chromium.launch(**options)
    context=browser.new_context(viewport={'width':1600,'height':1000},locale='zh-CN',accept_downloads=True)
    page=context.new_page();page.on('response',response)
    page.on('download',lambda d:downloads.append(d))
    page.goto(C['YONG_YOU_URL'],wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(1500)
    print(json.dumps({'ready':True,'snapshot':snapshot(page)},ensure_ascii=False),flush=True)
    for line in sys.stdin:
        try:
            cmd=json.loads(line);action=cmd['action'];frame=page.frames[cmd.get('frame',0)]
            if action=='close':browser.close();print('Closed without saving voucher.',flush=True);break
            elif action=='snapshot':result=snapshot(page)
            elif action=='login':
                frame.locator(cmd['user_selector']).fill(C['YONG_YOU_NAME']);frame.locator(cmd['password_selector']).fill(C['YONG_YOU_PASSWORD']);frame.locator(cmd['submit_selector']).click();page.wait_for_timeout(2000);result=snapshot(page)
            elif action=='click':
                frame.locator(cmd['selector']).nth(cmd.get('nth',0)).click(timeout=15000);page.wait_for_timeout(cmd.get('wait_ms',800));result=snapshot(page)
            elif action=='fill':
                frame.locator(cmd['selector']).nth(cmd.get('nth',0)).fill(cmd['value']);page.wait_for_timeout(cmd.get('wait_ms',800));result=snapshot(page)
            elif action=='press':
                frame.locator(cmd['selector']).nth(cmd.get('nth',0)).press(cmd['key']);page.wait_for_timeout(1000);result=snapshot(page)
            elif action=='eval':result=clean(frame.evaluate(cmd['expression']))
            elif action=='events':
                collect();start=cmd.get('start',0);result=[{'i':i,'url':e['url'],'method':e['method'],'status':e['status'],'request':e['request'],'body_summary':str(e['body'])[:cmd.get('chars',1000)]} for i,e in enumerate(events) if i>=start]
            elif action=='event':collect();result=events[cmd['index']]
            elif action=='save_events':
                collect();(DATA/Path(cmd.get('file','query_captured_responses.json')).name).write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8');result={'events':len(events)}
            elif action=='task':
                task_path=(ROOT/cmd['script']).resolve()
                if task_path.parent!=ROOT:raise ValueError('Task script must be inside next_step/scripts')
                result=runpy.run_path(str(task_path),init_globals={'context':context,'page':page,'events':events,'ROOT':ROOT,'clean':clean,'collect':collect,'downloads':downloads}).get('RESULT',{'finished':True})
            elif action=='request':
                r=context.request.fetch(cmd['url'],method=cmd.get('method','GET'),data=cmd.get('data'),timeout=45000);result={'status':r.status,'body':clean(r.json())}
                if cmd.get('save_as'):
                    (DATA/Path(cmd['save_as']).name).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');result={'status':result['status'],'saved_as':cmd['save_as'],'body_keys':list(result['body']) if isinstance(result['body'],dict) else None}
            elif action=='screenshot':page.screenshot(path=str(DATA/'platform_screen.png'));result='Screenshot saved.'
            else:raise ValueError('Unsupported action')
            collect();print(json.dumps({'result':clean(result),'events_count':len(events)},ensure_ascii=False),flush=True)
        except Exception as e:print(json.dumps({'error':clean(str(e))},ensure_ascii=False),flush=True)
