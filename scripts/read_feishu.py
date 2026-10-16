"""Read-only Feishu snapshot. Credentials remain in memory and are never logged."""
import json,sys
from datetime import datetime,timezone
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.parse import urlencode
from urllib.error import URLError

ROOT=Path(__file__).resolve().parent.parent
DATA=ROOT/'data'
DATA.mkdir(exist_ok=True)
def config():
    result={}
    for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:continue
        key,value=line.split('=',1);result[key.strip()]=value.strip().strip('"').strip("'")
    return result

def main():
    c=config()
    payload=json.dumps({'app_id':c['LARK_APP_ID'],'app_secret':c['LARK_APP_SECRET']}).encode()
    req=Request('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',data=payload,headers={'Content-Type':'application/json'},method='POST')
    with urlopen(req,timeout=30) as response:auth=json.load(response)
    if auth.get('code')!=0:raise RuntimeError('Feishu authentication failed: '+str(auth.get('code'))+' '+str(auth.get('msg')))
    headers={'Authorization':'Bearer '+auth['tenant_access_token']}
    ai='--ai' in sys.argv
    base='https://open.feishu.cn/open-apis/bitable/v1/apps/'+c['MT_FINANCE_AI_APP_TOKEN' if ai else 'MT_FINANCE_DATA_TABLE_COPY_APP_TOKEN']
    def get(suffix,params=None):
        url=base+suffix+('?' + urlencode(params) if params else '')
        with urlopen(Request(url,headers=headers),timeout=45) as response:d=json.load(response)
        if d.get('code')!=0:raise RuntimeError('Feishu read failed: '+str(d.get('code'))+' '+str(d.get('msg')))
        return d['data']
    app=get('');tables=get('/tables',{'page_size':100})
    if '--metadata' in sys.argv:
        (DATA/('ai_table_catalog.json' if ai else 'table_catalog.json')).write_text(json.dumps(tables,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(tables,ensure_ascii=False));return
    requested_table=sys.argv[sys.argv.index('--table-name')+1] if '--table-name' in sys.argv else None
    selected_by_name=next((t for t in tables.get('items',[]) if requested_table and t['name']==requested_table),None)
    if requested_table and not selected_by_name:raise RuntimeError('Requested table name not present in accessible table listing')
    target=selected_by_name['table_id'] if selected_by_name else c['MT_FINANCE_AI_TABLE_ID' if ai else 'MT_FINANCE_DATA_COPY_TABLE_ID']
    table=next((t for t in tables.get('items',[]) if t['table_id']==target),None)
    expected=requested_table.replace(' ','') if requested_table else ('2026年报关记录AI' if ai else '2026年报关数据')
    if not table or table['name'].replace(' ','')!=expected:raise RuntimeError('Configured table does not match requested '+expected+'; available names: '+str([t['name'] for t in tables.get('items',[])]))
    fields=[];cursor=None
    while True:
        params={'page_size':100}
        if cursor:params['page_token']=cursor
        data=get('/tables/'+target+'/fields',params);fields.extend(data.get('items',[]))
        if not data.get('has_more'):break
        cursor=data['page_token']
    records=[];cursor=None
    while True:
        params={'page_size':500}
        if cursor:params['page_token']=cursor
        data=get('/tables/'+target+'/records',params);records.extend(data.get('items',[]))
        if not data.get('has_more'):break
        cursor=data['page_token']
        print('Read records:',len(records),flush=True)
    snapshot={'fetched_at':datetime.now(timezone.utc).isoformat(),'app_name':app.get('app',{}).get('name'),'table_name':table['name'],'tables':tables.get('items',[]),'fields':fields,'records':records}
    filename=sys.argv[sys.argv.index('--output')+1] if '--output' in sys.argv else ('ai_source_snapshot.json' if ai else 'source_snapshot.json')
    dest=(DATA/filename).resolve()
    if dest.parent!=DATA:raise ValueError('Snapshot output must be inside next_step/data')
    dest.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'app_name':snapshot['app_name'],'table_name':table['name'],'records':len(records),'fields':[{'name':f['field_name'],'type':f['type']} for f in fields]},ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except URLError as exc:
        reason=getattr(exc,'reason',None)
        print('Network access failed ('+type(exc).__name__+', reason='+type(reason).__name__+', errno='+str(getattr(reason,'errno',None))+'); credentials and endpoint identifiers omitted.',file=sys.stderr);sys.exit(2)
