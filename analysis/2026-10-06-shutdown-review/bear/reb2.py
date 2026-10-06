import pandas as pd, numpy as np
exec(open('vps_pull_20261006_final/analysis/stat.py').read())
SP='vps_pull_20261006_final/analysis/'
g=pd.read_pickle(SP+'bear/gated_conv.pkl'); g['gdate']=g.game_datetime.str[:10]
c=pd.read_parquet(SP+'bear/conv_gated.parquet')
C={k:x.sort_values('t_offset_ms') for k,x in c.groupby(['arb_id','poly_team'])}
fee=lambda p:0.05*p*(1-p)
for T in [200,1000,5000,15000]:
    v=[]
    for k,won in zip(g.key,g.won):
        x=C[k]; x=x[x.t_offset_ms<=T]; a=x.poly_ask.iloc[-1]
        v.append(won-a-fee(a) if 0<a<1 else np.nan)
    g[f'late{T}']=v
    rep(g[g[f'late{T}'].notna()],f'taker entry at ask(t<= {T}ms)',col=f'late{T}')
# maker proxy: rest bid at a0-0.01; fills if ask later <= that within 15s
mf=[];
for k,a0 in zip(g.key,g.poly_ask):
    x=C[k]; w=x[(x.t_offset_ms>0)&(x.t_offset_ms<=15000)]
    mf.append(bool((w.poly_ask<=a0-0.01+1e-9).any()))
g['mfill']=mf; g['mk']=g.won-(g.poly_ask-0.01)
print('maker proxy fill share',g.mfill.mean())
rep(g[g.mfill],'maker bid a0-1c, filled (proxy)',col='mk')
rep(g[~g.mfill],'maker bid a0-1c, NOT filled',col='mk')
