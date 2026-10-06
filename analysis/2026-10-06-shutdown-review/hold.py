import sys,glob,numpy as np
exec(open('vps_pull_20261006_final/analysis/outcomes.py').read())
R='./'
fs=[R+'vps_pull_20261006_2027/arb_durations_4.csv']+[f for f in glob.glob(R+'historical-data/arb_durations_4_*.csv') if 'apr28-29' not in f]
parts=[]
for f in fs:
    a=pd.read_csv(f); a['src']=f.split('/')[-1]; parts.append(a)
a=pd.concat(parts)
op=a[a.event=='OPEN'].copy()
cl=a[a.event=='CLOSE'][['first_seen','game','poly_team','duration_seconds']].drop_duplicates(['first_seen','game','poly_team'])
op=op.drop_duplicates(['first_seen','game','poly_team']).merge(cl,on=['first_seen','game','poly_team'],how='left',suffixes=('','_c'))
op['won']=[outcome(*r) for r in op[['sport','game_datetime','poly_team','kalshi_team']].itertuples(index=False)]
op=op[op.won.notna()].copy(); op['won']=op.won.astype(int)
op['fee']=0.05*op.poly_ask*(1-op.poly_ask)
op['hold']=op.won-op.poly_ask-op.fee
op['kfair']=1-(op.kalshi_ask+op.kalshi_bid)/2
op['fs']=pd.to_datetime(op.first_seen,format='ISO8601')
op.to_pickle('vps_pull_20261006_final/analysis/hold.pkl')
