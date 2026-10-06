import pandas as pd, numpy as np
SP='vps_pull_20261006_final/analysis/'
cl=pd.read_pickle(SP+'cl_g.pkl').sort_values(['arb_id','game','poly_team','t_offset_ms'])
rows=[]
for (a,gm,pt),x in cl.groupby(['arb_id','game','poly_team'],sort=False):
    t=x.t_offset_ms.values; ask=x.poly_ask.values; ka=x.kalshi_ask_now.values
    r=dict(arb_id=a,game=gm,poly_team=pt,won=x.won.iloc[0],ask0=x.ask0.iloc[0],k0=x.k0.iloc[0],maxt=t.max())
    early=(t<=200)&(t>0)
    r['lifted200']=bool((ask[early]>r['ask0']+1e-9).any())
    for T in [200,1000,5000,15000,60000]:
        i=np.searchsorted(t,T,side='right')-1
        r[f'ask_{T}']=ask[i] if t.max()>=T*0.5 or T<=1000 else np.nan
        r[f'k_{T}']=ka[i]
    rows.append(r)
d=pd.DataFrame(rows); d.to_pickle(SP+'paths.pkl'); print(len(d), d.lifted200.mean(), d.maxt.median())
