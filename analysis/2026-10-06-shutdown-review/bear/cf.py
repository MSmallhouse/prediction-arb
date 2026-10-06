import pandas as pd, numpy as np
SP='vps_pull_20261006_final/analysis/bear/'
e=pd.read_csv('executions.csv'); e['ts']=pd.to_datetime(e.timestamp,format='ISO8601'); e=e[e.ts>='2026-09-19']
b=e[e.action.isin(['BUY','BUY_FAILED'])].drop_duplicates('arb_id').set_index('arb_id')
c=pd.read_parquet(SP+'conv_attempts.parquet')
def fee(p): return 0.0695*p*(1-p)  # approx per share taker
rows=[]
for aid,g in c.groupby('arb_id'):
    r=b.loc[aid]; p=r.buy_price; bf=r.buy_fee if pd.notna(r.buy_fee) else fee(p)
    g=g.sort_values('t_offset_ms'); w=g[(g.t_offset_ms>0)&(g.t_offset_ms<=15000)]
    pnl=None; how=None
    for _,t in w.iterrows():
        if t.poly_bid>=p+0.05: pnl=0.05-bf; how='conv'; break
        if p-t.poly_ask>=0.05: pnl=t.poly_bid-p-bf-fee(t.poly_bid); how='drop'; break
    if pnl is None:
        lb=w.poly_bid.iloc[-1] if len(w) else g.poly_bid.iloc[-1]
        pnl=lb-p-bf-fee(lb); how='timeout'
    rows.append(dict(arb_id=aid,action=r.action,sport=r.sport,how=how,pnl=pnl))
R=pd.DataFrame(rows); R.to_csv(SP+'cf_rows.csv',index=False)
print(R.groupby('action').how.value_counts(normalize=True).unstack().round(3))
print(R.groupby('action').pnl.agg(['mean','median','size']))
print(R.groupby(['sport','action']).pnl.agg(['mean','median','size']).round(4))
# bootstrap CI of mean for failures
f=R[R.action=='BUY_FAILED'].pnl.values; rng=np.random.default_rng(0)
bs=[rng.choice(f,len(f)).mean() for _ in range(5000)]; print('fail mean CI',np.percentile(bs,[2.5,97.5]))
