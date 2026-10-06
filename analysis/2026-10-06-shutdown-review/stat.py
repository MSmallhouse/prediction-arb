import pandas as pd, numpy as np
op=pd.read_pickle('vps_pull_20261006_final/analysis/hold.pkl')
op['gdate']=op.game_datetime.str[:10]
def rep(x,label,col='hold'):
    x=x.copy(); x['cl']=x.game+'|'+x.gdate+'|'+x.poly_team
    # game-level cluster (both sides same game correlated -> cluster by game)
    x['g']=x.game+'|'+x.gdate
    rng=np.random.default_rng(0); gs=x.g.unique()
    grp={k:v[col].values for k,v in x.groupby('g')}
    # one-trade-per-cluster version: first arb per (game,team)
    first=x.sort_values('fs').drop_duplicates('cl')
    bs=[]
    for _ in range(2000):
        s=rng.choice(gs,len(gs)); bs.append(np.concatenate([grp[k] for k in s]).mean())
    gf={k:v[col].values for k,v in first.groupby('g')}; gsf=list(gf)
    bf=[]
    for _ in range(2000):
        s=rng.choice(gsf,len(gsf)); bf.append(np.concatenate([gf[k] for k in s]).mean())
    print(f"{label:38s} rows={len(x):5d} games={len(gs):4d} mean={x[col].mean()*100:+.2f}c CI[{np.percentile(bs,2.5)*100:+.2f},{np.percentile(bs,97.5)*100:+.2f}] | first-per-side n={len(first)} mean={first[col].mean()*100:+.2f}c CI[{np.percentile(bf,2.5)*100:+.2f},{np.percentile(bf,97.5)*100:+.2f}] wr={x.won.mean():.3f} ask={x.poly_ask.mean():.3f}")
