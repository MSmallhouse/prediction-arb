import pandas as pd, numpy as np
SP='vps_pull_20261006_final/analysis/'
op=pd.read_pickle(SP+'hold.pkl')
g=op[(op.opener=='kalshi')&(op.gross_spread>=0.04)&(op.poly_depth>=1)&(op.poly_ask>=0.15)&(op.minutes_to_first_pitch<=180)].copy()
g=g[g.fs>='2026-09-19']
print('gated since revive rows',len(g))
ids=set(g.first_seen)
c=pd.read_csv('./vps_pull_20261006_2027/convergence_log.csv',usecols=['arb_id','poly_team','t_offset_ms','poly_ask','poly_bid'])
c=c[c.arb_id.isin(ids)]
c.to_parquet(SP+'bear/conv_gated.parquet')
out={}
for (aid,pt),x in c.groupby(['arb_id','poly_team']):
    x=x.sort_values('t_offset_ms'); a0=x.poly_ask.iloc[0]
    w=x[(x.t_offset_ms>0)&(x.t_offset_ms<=15000)]
    conv=(w.poly_bid>=a0+0.05).any()
    lift=x[(x.t_offset_ms>0)&(x.poly_ask>a0+1e-9)].t_offset_ms.min()
    bid15=w.poly_bid.iloc[-1] if len(w) else np.nan
    out[(aid,pt)]=(conv,lift,bid15)
g['key']=list(zip(g.first_seen,g.poly_team))
g=g[g.key.isin(out)].copy()
g['conv']=[out[k][0] for k in g.key]; g['lift']=[out[k][1] for k in g.key]; g['bid15']=[out[k][2] for k in g.key]
g['lift100']=g.lift<=100
g.to_pickle(SP+'bear/gated_conv.pkl')
print('matched',len(g))
for col in ['conv','lift100']:
    print(g.groupby(col).hold.agg(['mean','median','size']))
print(g.groupby(['sport','conv']).hold.agg(['mean','size']).unstack())
