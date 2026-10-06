import pandas as pd,numpy as np
rng=np.random.default_rng(7)
fee=lambda p:0.05*p*(1-p)
def boot(df,col,B=3000):
    gg=df.groupby('game')[col].agg(['sum','count']).values
    idx=rng.integers(0,len(gg),(B,len(gg))); s=gg[idx,0].sum(1)/gg[idx,1].sum(1)
    return f"{100*df[col].mean():+.2f}c [{np.percentile(100*s,2.5):+.2f},{np.percentile(100*s,97.5):+.2f}] n={len(df)} g={df.game.nunique()}"
p=pd.read_pickle('j_paths.pkl')
for T in (0,50,100,200,500,1000,5000,15000):
    p[f'h{T}']=p.won-p[f'a{T}']-fee(p[f'a{T}'])
p=p[p.a15000<0.99]
fp=p.sort_values('first_seen').drop_duplicates(['game','day','poly_team'])  # first per side
print('== survivorship: logged vs unlogged gated arbs ==')
exec(open('j_outcomes.py').read()) if False else None
print('== decay (all arbs | first-per-side) ==')
for T in (0,50,100,200,500,1000,5000,15000):
    print(T, boot(p,f'h{T}'),' | ',boot(fp,f'h{T}'))
print('== adverse selection split (settlement, entry at a0) ==')
for nm,d in (('all',p),('fps',fp)):
    for L in ('lift100','lift200'):
        print(nm,L,'lifted:',boot(d[d[L]],'h0'),' notlifted:',boot(d[~d[L]],'h0'))
print('== depth confound: within depth terciles (all arbs) ==')
p['dq']=pd.qcut(p.d0,[0,.33,.67,1],labels=['lo','mid','hi'])
print(p.groupby('dq',observed=True).apply(lambda d:pd.Series({'n':len(d),'lift200':d.lift200.mean(),'d0med':d.d0.median()})))
for q in ['lo','mid','hi']:
    d=p[p.dq==q]
    if (~d.lift200).sum()>5: print(q,'lifted',boot(d[d.lift200],'h0'),' not',boot(d[~d.lift200],'h0'))
print('== gross spread among lifted vs not ==', p.groupby('lift200').gross_spread.median().to_dict())
# logit-ish: regress h0 on lift200 + gross + log depth, cluster-agnostic
import numpy.linalg as la
X=np.column_stack([np.ones(len(p)),p.lift200.astype(float),p.gross_spread,np.log1p(p.d0)])
b=la.lstsq(X,p.h0.values,rcond=None)[0]; print('OLS h0 ~ 1+lift200+gross+log depth:',b.round(4))
# convergence vs settlement link
print('conv15 split:', 'conv',boot(p[p.conv15],'h0'),' not',boot(p[~p.conv15],'h0'))
print('lift200 rate',p.lift200.mean().round(3),' conv15 rate',p.conv15.mean().round(3))
