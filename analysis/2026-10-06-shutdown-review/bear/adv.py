import pandas as pd, numpy as np
SP='vps_pull_20261006_final/analysis/bear/'
e=pd.read_csv('executions.csv'); e['ts']=pd.to_datetime(e.timestamp,format='ISO8601')
e=e[e.ts>='2026-09-19']
b=e[e.action.isin(['BUY','BUY_FAILED'])].drop_duplicates('arb_id').set_index('arb_id')
c=pd.read_parquet(SP+'conv_attempts.parquet')
# fire offset: timestamp - arb_id
b['fire_off']=(b.ts-pd.to_datetime(b.index.to_series(),format='ISO8601')).dt.total_seconds()*1000
print('fire offset ms median',b.fire_off.median())
b['has_log']=b.index.isin(c.arb_id)
print(b.groupby(['action','has_log']).size().unstack())
print(b.groupby(['sport','action','has_log']).size().unstack())
res=[]
for aid,g in c.groupby('arb_id'):
    r=b.loc[aid]; p=r.buy_price
    g=g.sort_values('t_offset_ms')
    w=g[(g.t_offset_ms<=15000)]
    hit=w[w.poly_bid>=p+0.05]
    t_hit=hit.t_offset_ms.min() if len(hit) else np.nan
    # bid at 15s (last obs <=15000)
    bid15=w.poly_bid.iloc[-1]
    # min ask in window (adverse move)
    after=g[g.t_offset_ms>0]
    res.append(dict(arb_id=aid,action=r.action,sport=r.sport,p=p,conv=len(hit)>0,t_hit=t_hit,
        bid15_minus_p=bid15-p, max_bid_minus_p=w.poly_bid.max()-p, min_bid_minus_p=w.poly_bid.min()-p,
        ticks=len(w), gross=r.gross_spread, depth=r.poly_depth, lat=r.buy_latency_ms, age=r.poly_ws_age_ms,
        bid0=g.poly_bid.iloc[0], ask0=g.poly_ask.iloc[0]))
R=pd.DataFrame(res); R.to_csv(SP+'adv_rows.csv',index=False)
print(R.groupby('action')[['conv']].agg(['mean','size']))
print(R.groupby(['sport','action']).conv.agg(['mean','size']).unstack())
print(R.groupby('action')[['bid15_minus_p','max_bid_minus_p','min_bid_minus_p','t_hit','ticks','gross','depth','lat','age']].median())
print('spread at t0 (ask0-bid0) median', R.groupby('action').apply(lambda d:(d.ask0-d.bid0).median()))
