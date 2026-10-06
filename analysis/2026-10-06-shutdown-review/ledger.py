import json,csv,sys
from collections import defaultdict
d=json.load(open('poly_activities.json'))
rows=[];ext=0
for a in d['activities']:
    t=a['type']
    if t=='ACTIVITY_TYPE_TRADE':
        tr=a['trade']; o=tr['aggressor'] if tr['isAggressor'] else tr['passive']
        side=1 if o['action']=='ORDER_ACTION_BUY' else -1
        cost=float(tr['cost']['value']); px=float(tr['price']['value']); q=float(tr['qtyDecimal'])
        fee=float((tr['aggressorExecution'] if tr['isAggressor'] else tr['passiveExecution'])['commissionNotionalCollected']['value'])
        rows.append(dict(ts=tr['createTime'][:23],slug=tr['marketSlug'],kind='TRADE',intent=o['intent'].replace('ORDER_INTENT_',''),
            tif=o['tif'][-4:],maker=not tr['isAggressor'],px=px,qty=q,fee=fee,cash=-cost if side==1 else cost,
            realized=float(tr.get('effectiveRealizedPnl',{}).get('value',0) or 0)))
    elif t=='ACTIVITY_TYPE_POSITION_RESOLUTION':
        r=a['positionResolution']; b=r['beforePosition']; af=r['afterPosition']
        rows.append(dict(ts=r['updateTime'][:23],slug=r['marketSlug'],kind='RESOLUTION',intent=r.get('side',''),tif='',maker='',px='',qty=b['netPosition'],fee=0,cash='',
            realized=float(af['realized']['value'])-float(b['realized']['value'])))
    else:
        abc=a['accountBalanceChange']; amt=float(abc['amount']['value'])
        rows.append(dict(ts=abc['createTime'][:23],slug='',kind=t.replace('ACTIVITY_TYPE_',''),intent='',tif='',maker='',px='',qty='',fee=0,cash=amt,realized=''))
rows.sort(key=lambda r:r['ts'])
w=csv.DictWriter(open('poly_ledger.csv','w'),fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
# P&L by day (trading realized per Polymarket accounting)
day=defaultdict(lambda:[0.0,0,0.0]);sl=defaultdict(float)
for r in rows:
    if r['kind'] in('TRADE','RESOLUTION'):
        day[r['ts'][:10]][0]+=r['realized'] or 0; day[r['ts'][:10]][2]+=r['fee']
        if r['kind']=='TRADE' and r['intent'].startswith('BUY'): day[r['ts'][:10]][1]+=1
        sl[r['slug'].split('-')[1]+(' recent' if r['ts']>='2026-09-19' else ' may')]+=r['realized'] or 0
tot=0
for k in sorted(day): tot+=day[k][0]; print(k,'realized %+.3f buys %d fees %.3f cum %+.3f'%(day[k][0],day[k][1],day[k][2],tot))
print({k:round(v,3) for k,v in sl.items()})
print('non-trade flows:',[(r['ts'][:10],r['kind'],r['cash']) for r in rows if r['kind'] not in('TRADE','RESOLUTION')])
